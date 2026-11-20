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

(One representative run of four; see the range below.)

**9.1x, scores bitwise identical across 4,800 requests.** Across four clean runs
the speedup held at **8.8x-9.2x** (before: 23.3-27.5 req/s, after: 212.6-254.3).

**This benchmark is sensitive to machine load, and visibly so.** One run taken
while several other Python processes were competing for cores reported the
single-row path at 0.4 req/s and a 523x speedup. That figure is garbage, and it
is self-evidently garbage: 12 threads at a 293 ms median cannot produce 0.4 req/s,
so wall-clock time was inflated by something other than the work being measured.
Run it on an otherwise idle machine, run it more than once, and discard any result
whose throughput and latency percentiles disagree with each other. A speedup
number that moves by 50x between runs is measuring the machine, not the code.

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
| after, same box | 120.9 req/s | batched + 4 `--workers`, code change only |
| after, K8s scale-out | 116 req/s on kind | same laptop; *not* a scale-out number, see below |

Same-box run, `bash loadtest/ab_endtoend.sh` (50 users, 60 s per arm, 12-core
laptop, 0 failures in every arm):

| arm | req/s | median | p95 | p99 |
|---|---|---|---|---|
| before (1 worker, inline) | 30.0 | 1100 ms | 3600 ms | 5200 ms |
| batched, 1 worker | 113.8 | 190 ms | 500 ms | 830 ms |
| batched, 4 workers, OMP=1 | 120.9 | 190 ms | 450 ms | 550 ms |
| batched, 4 workers, OMP=12 | 117.2 | 210 ms | 430 ms | 600 ms |

Batching is 3.8x; workers add only 1.1x on this box, because each worker batches
over a quarter of the traffic (mean batch 5.95 rows at 1 worker, 2.94 at 4). The
OMP arms are within noise of each other. An earlier run of this script reported
the same conclusion from invalid data: compose did not forward `OMP_NUM_THREADS`,
so both OMP arms ran one config. The script now checks the container's value and
aborts on a mismatch.

## Kubernetes (kind, same laptop)

`bash deploy/kind/up.sh`, then the in-cluster load generator in
`loadtest/k8s-locust-job.yaml` (200 users, 5 min). Three runs, each fixing what
the previous one exposed:

| run | req/s | median | p95 | failures | what it showed |
|---|---|---|---|---|---|
| 1 | 94.8 | 1700 ms | 3700 ms | 0 | probes timing out at the 1 s default pulled busy pods out of the Service |
| 2 | 100.2 | 1800 ms | 2700 ms | 0 | all pods stayed ready, but 4 of 6 served **0 requests** |
| 3 | 116.0 | 880 ms | 3400 ms | 0.34% | load spread over all 8 pods; HPA at max, pods at their CPU limit |

Run 2's cause: kube-proxy balances per TCP connection. Locust keeps each user's
connection alive, so all 200 connections stayed on the 2 pods that existed at
start, and the HPA's new pods idled. Run 3 sets `CONNECTION_CLOSE=1`, which
models many independent clients. In production the same trap applies to any
client with a keep-alive pool; the fix there is an L7 load balancer (Ingress,
service mesh), not the load test.

Run 3's failures were 70 dropped connections from one pod the liveness probe
restarted while it was saturated, and 48 batcher deadline timeouts. Those
returned 500. They now return a retryable 503 (`scoring overloaded, retry`),
which is unit-tested but was not re-measured.

**Why 116 and not more:** eight 1-CPU pods on one 12-core laptop, sharing it with
locust, Prometheus, Redis and Postgres. The HPA pinned at its `maxReplicas: 8`
with pods at their CPU limit. This proves the autoscaling path works (2 -> 8
replicas in ~90 s under load) and nothing about horizontal throughput. A
scale-out number needs nodes with their own CPUs, i.e. the cloud run.
