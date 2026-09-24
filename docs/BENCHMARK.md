# Benchmarking the fraud scorer

Read this before quoting a throughput number from this repo. The service has two
settings that cap a load test while reporting perfect health, and one measurement
artifact that makes latency look like work when it is queueing.

## The three traps

**1. `RATE_LIMIT_PER_MIN` is per key, and defaults to 600 — i.e. 10 req/s.**
Every locust user shares one API key, so they share one bucket. A 60-second run at
50 users will spend most of itself collecting `429`s. The locustfile marks those
as failures, so it is visible if you look, but the throughput line still reads
like a measurement. Set `RATE_LIMIT_PER_MIN=24000` (400 req/s) for a benchmark.

Note that `ALLOW_UNAUTHENTICATED=true` makes `require_api_key` return before the
limiter runs, so the limit silently does not apply in that mode. Whether the cap
is active therefore depends on the auth config — check which one you are in.

**2. One uvicorn worker caps the service at its serial rate.** Scoring is
CPU-bound and CPython holds one GIL, so a single worker serves ~30 req/s no matter
how much load arrives or how many threads the pool has. Either `--workers N`
(compose) or N pods (Kubernetes).

**3. Latency at saturation is mostly queue wait, not work.** FastAPI runs sync
handlers in a 40-thread pool. Past capacity, requests sit in that pool, and the
reported latency is wait + work while the handler's own `latency_ms` field reports
only work. The original `29.7 req/s, median 1400 ms` is this: 29.7 × 1.4 s = 41.6
requests in flight ≈ the 40-thread pool, saturated. The service was never taking
1.4 s to score anything.

This is why `--limit-concurrency` matters. Shedding with `503` past the limit
bounds the tail; without it, latency grows as long as load exceeds capacity.

## Measuring the model stage (no stack required)

Attributable, and it runs anywhere the bundle does:

```bash
python loadtest/bench_model.py                      # or: make bench-model
python loadtest/bench_model.py --threads 24 --requests 200
```

It drives one-row-per-call and micro-batched paths from the same threads against
the real committed bundle, and asserts the scores are identical between them
before reporting a speedup — a faster path that returns different numbers is not
a faster path.

Measured, 12 threads × 400 requests, `deploy/fraud/models/model.joblib`,
12-core laptop:

| path | throughput | median | p95 | p99 |
|---|---|---|---|---|
| one row per call | 24.5 req/s | 391 ms | 696 ms | 880 ms |
| micro-batched | **222.6 req/s** | **51 ms** | **71 ms** | 103 ms |

**9.1x, scores bitwise identical across 4,800 requests.**

That 24.5 req/s is worth dwelling on: it is the model stage *alone*, with no
Redis, no Postgres and no HTTP, and it independently reproduces the 29.7 req/s
measured end-to-end. The model call was essentially the entire bottleneck.

Per-call cost that explains it:

| stage | 1 row | 256 rows | per row, batched |
|---|---|---|---|
| `iso.score_samples` | **10.94 ms** | 26.2 ms | 0.10 ms |
| `shap.shap_values` | 1.23 ms | 9.7 ms | 0.04 ms |
| `xgb.predict_proba` | 0.70 ms | 2.1 ms | 0.01 ms |
| scaler + features | 0.17 ms | — | — |
| **total** | **13.04 ms** | | **~0.15 ms** |

84% of it is `IsolationForest.score_samples` overhead on a `(1, 11)` array — 256
rows cost 2.4x one row, so it is call overhead, not arithmetic.

## Measuring end to end (docker-compose)

```bash
cd deploy/fraud
make train                      # once, produces models/model.joblib
RATE_LIMIT_PER_MIN=24000 UVICORN_WORKERS=4 make up
make load-test                  # 50 users, 60s, headless locust
```

Record these next to any number, because without them it is not reproducible:

- `RATE_LIMIT_PER_MIN`, `UVICORN_WORKERS`, `UVICORN_LIMIT_CONCURRENCY`
- `MODEL_BATCH_MAX`, `MODEL_BATCH_WAIT_MS`
- host CPU count, and whether the stack ran under Docker Desktop / WSL2
- locust users, spawn rate, duration
- the failure count, not just the throughput line

On Docker Desktop for Windows, loopback to a published port crosses the WSL2
boundary; the per-stage histogram (`fraud_score_stage_latency_seconds`) shows how
much of a request is Redis and Postgres I/O versus model CPU, which is what tells
you whether a result is CPU-bound or environmental. Quote the stage breakdown
alongside the total rather than attributing the whole delta to code.

## Reading the per-stage metrics

```
curl -s localhost:8000/metrics | grep -E 'stage_latency|batch_size|queue'
```

- `fraud_score_stage_latency_seconds{stage="model"}` — should be ~0.15 ms/row
  amortised under load. If it sits near 11 ms, batching is not engaging.
- `fraud_model_batch_size` — sitting at 1 under load means `MODEL_BATCH_WAIT_MS`
  is too short to collect anything and the single-row cost is back.
- `fraud_db_write_queue_depth` / `_dropped_total` — climbing depth means Postgres
  cannot keep up; drops are lost persistence (`/stats` and drift go blind) while
  scoring still serves correctly.

With `--workers > 1`, `/metrics` reflects only the worker that answered the
scrape, so treat those values as a sample and locust as the throughput source of
truth. The Kubernetes path runs one worker per pod specifically to avoid this.

## Publishing a before/after

Report two rows, not one. Per-replica gain and horizontal scale-out are different
claims, and collapsing 12 replicas against one container into a single "29.7 →
400" invites the obvious objection:

| | throughput | note |
|---|---|---|
| before | 29.7 req/s | 1 worker, single-row model call |
| after, same box | *measure* | batched + `--workers`, code change only |
| after, K8s scale-out | *measure* | per-replica × N, with the HPA graph |
