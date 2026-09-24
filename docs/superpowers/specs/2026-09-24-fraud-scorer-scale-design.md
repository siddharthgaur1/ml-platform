# Scaling the fraud scorer: 29.7 req/s → target ~400 req/s, on Kubernetes

Status: approved (design), not yet implemented
Date: 2026-09-24

## Why

`README.md` publishes **29.7 req/s, median 1400 ms, p95 2300 ms, p99 2700 ms** for
`POST /score`, and attributes the cause to "`uvicorn` running without `--workers`".
That diagnosis is half right. Measurement before optimisation says the rest.

## Measured baseline

Per-request model CPU, against the committed bundle
(`deploy/fraud/models/model.joblib`, 200-tree XGB + 200-tree IsolationForest):

| stage | 1 row | 256 rows | per-row batched |
|---|---|---|---|
| `iso.score_samples` | **10.94 ms** | 26.2 ms | 0.10 ms |
| `shap.shap_values` | 1.23 ms | 9.7 ms | 0.04 ms |
| `xgb.predict_proba` | 0.70 ms | 2.1 ms | 0.01 ms |
| `iso_scaler.transform` | 0.10 ms | — | — |
| `compute_features` | 0.07 ms | — | — |
| **total** | **13.04 ms** | | **~0.15 ms** |

Two independent defects, both invisible in the current README framing:

1. **Single-row sklearn overhead dominates.** 84% of model CPU is
   `IsolationForest.score_samples` on a `(1, 11)` array. It is call overhead,
   not arithmetic — 256 rows cost 2.4x one row. This is a ~100x on the
   dominant term, available without touching the model.
2. **The process is fully GIL-serialised.** 29.7 req/s = 1/33.6 ms. Handlers are
   sync `def`, so FastAPI runs them in the 40-thread `anyio` pool; the threads
   contend on one GIL, so throughput equals the serial rate while *latency*
   inflates by the queue depth. Little's law confirms it: 29.7 x 1.4 s = 41.6
   in flight ≈ the 40-thread pool, saturated. **The 1.4 s median is queue wait,
   not work** — the handler's own `latency_ms` field reports ~32 ms.

### I/O on the request path

Per request, serialised and blocking:

| # | call | note |
|---|---|---|
| 1 | rate limiter `incr`+`expire` | pipelined, 1 RTT |
| 2 | `lrange user:<id>:amounts` | 1 RTT |
| 3 | `smembers user:<id>:devices` | 1 RTT — **not** pipelined with #2 |
| 4 | history `update` pipeline | 1 RTT |
| 5 | Postgres `INSERT` + `commit` | 1 RTT + fsync |

Four Redis round-trips and a committing Postgres write. The gap between 13 ms
measured CPU and ~32 ms reported handler time is this I/O; on Docker
Desktop / WSL2 loopback it is the larger half.

### Benchmark trap

`RATE_LIMIT_PER_MIN` defaults to **600/min = 10 req/s per key**, shared by every
locust user hitting one dev key. At the target rate the limiter returns 429 for
~94% of traffic. The "after" run must raise it explicitly or it measures
nothing. (Note: when `ALLOW_UNAUTHENTICATED=true`, `require_api_key` returns
early and skips the limiter entirely — so whether the limiter is live depends on
auth config, which is exactly the kind of thing that invalidates a benchmark
quietly.)

## Design

Fix the application, then scale it. In that order, because scaling an
unfixed app buys replicas to hide a 10.9 ms defect, and an interviewer will ask
why each replica still serves 30 req/s.

### Phase 1 — instrument

Add a per-stage Prometheus histogram (`redis_history`, `model`, `redis_update`,
`db_insert`) alongside the existing `SCORE_LATENCY`. Nothing else in this phase.
This is the evidence for every later claim and it is the Grafana panel source
anyway. Without it, "before and after" is an assertion.

### Phase 2 — application fixes

Ordered by measured payoff:

- **2a. Pipeline `get_history`.** `lrange` + `smembers` in one Redis pipeline.
  Removes one RTT. Byte-identical result.
- **2b. Postgres insert off the request path.** Bounded `queue.Queue` + one
  background writer thread doing `executemany` per batch (size or 200 ms,
  whichever first). Removes an RTT and an fsync from every response. The
  existing handler already treats this insert as best-effort (catches, counts
  `DB_INSERT_FAILURES`, still returns 200), so the failure semantics are
  unchanged — but the queue must be **bounded**, must increment a drop counter
  when full rather than growing without limit, and must flush on shutdown.
- **2c. Micro-batch the model call.** A batcher thread collects pending requests
  for up to ~5 ms (cap ~64), runs `xgb` + `iso` + `shap` on the stacked array,
  and fans results back via per-request `threading.Event`. Sync handlers submit
  and block, which is correct inside the existing threadpool — **no async
  migration, no Redis client rewrite.** Scores are byte-identical: the same
  arithmetic on the same rows.
- **2d. Workers and bounded concurrency.** `--workers` = cores, and
  `--limit-concurrency` set so excess load is shed fast instead of queued 40
  deep. This is what converts the p95 from 2300 ms to tens of ms; it trades a
  503 under overload for a bounded tail, which is the right trade for a scorer.
- **2e. Raise `RATE_LIMIT_PER_MIN`** in the loadtest profile, documented as a
  benchmark precondition.

Feature-engineering parity is the hard constraint: `common/features.py` is shared
by trainer and scorer, and the bundle's `threshold`/`xgb_weight` were fixed at
training time with a drift reference computed against them. **No change in this
plan may alter a score.** Regression test: replay a fixed transaction set through
old and new paths, assert identical `fraud_score` and `shap_explanation`.

### Phase 3 — Kubernetes

- Helm chart in `deploy/helm/fraud-scorer/`: Deployment, Service, HPA,
  ConfigMap, Secret (API keys), ServiceMonitor. Redis and Postgres as chart
  dependencies for local, external DSNs in cloud.
- `kind` cluster first — free, fast, and reproducible by anyone reading the
  repo — then one GKE/EKS run on free credits to prove it lands and to capture
  Grafana.
- `kube-prometheus-stack` for Prometheus + Grafana; dashboard JSON committed.
- **HPA on CPU first.** Scaling on a custom `req/s` metric needs
  `prometheus-adapter` and is a large detour for a marginal gain; CPU tracks the
  real bottleneck here because the service is CPU-bound by construction. Note
  the custom-metric upgrade path in the chart README rather than building it.

### Phase 4 — publish

Report **two rows, not one**, because comparing 12 replicas against one
container is apples-to-oranges and a sharp reader will say so:

1. **Per-replica, same laptop:** 29.7 req/s → measured value. Attributable
   entirely to code, with the stage histogram to prove where it went.
2. **Horizontal scale-out on K8s:** per-replica x N, with the HPA graph.

Both numbers reproducible from the committed locustfile. Record the benchmark
preconditions (rate limit, worker count, host) next to them — the current
README's single number omits all three.

## Non-goals

- Replacing IsolationForest or retraining. Changes scores, invalidates the drift
  reference, and is unnecessary — batching gets the same win.
- Async migration of the Redis/Postgres clients. The batcher makes it
  unnecessary; it would be a large diff for no measured gain.
- `prometheus-adapter` custom-metric HPA. Documented, not built.
- Sliding-window rate limiter. The existing `ponytail:` note already scopes this.

## Risks

- **Micro-batching adds a latency floor** (~5 ms wait) to a single unloaded
  request. Acceptable: it trades 5 ms idle-case latency for ~10 ms under load,
  and the batch window must be configurable so it can be tuned to zero.
- **Windows/WSL2 loopback may be a material share of the baseline.** If so, the
  "before" number is partly environmental. This must be stated honestly rather
  than claimed as pure code gain — the stage histogram will show it.
- **Batcher is a new failure mode**: a wedged batcher thread stalls every
  request. Needs a per-request timeout and a health signal.
