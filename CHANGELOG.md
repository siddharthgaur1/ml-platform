# Changelog

## [Unreleased]

### Performance
- `9d71cec` — the fraud scorer's model call is micro-batched. Profiling the
  committed bundle found **84% of per-request model CPU in
  `IsolationForest.score_samples` on a single `(1, 11)` row** — 10.9 ms of
  13.0 ms, and per-call overhead rather than arithmetic, since 256 rows cost
  only 2.4x one row. Batching that call takes the model stage from
  **24.5 to 222.6 req/s (8.8x–9.2x across runs)** with scores bitwise
  identical. `lrange`+`smembers` are now one Redis pipeline instead of two
  round trips, and the Postgres write moved off the request path into a bounded
  background queue.

  The README previously attributed the 29.7 req/s figure to "`uvicorn` running
  without `--workers`". That was half the story, and the cheaper half: the
  isolated model stage alone reproduces 24.5 req/s, so `--workers` would have
  bought replicas to hide the real defect. The 1400 ms median was not work
  either — 29.7 x 1.4 s = 41.6 requests in flight, which is FastAPI's
  40-thread pool saturated.

  Found while verifying: `predict_proba` returns float32, so the batched
  ensemble computed in float32 where the one-row path had widened via
  `float()`, diverging by ~4e-8. Caught only because the parity test asserts
  bitwise equality rather than `allclose`.

### Added
- `2f2f7b2` — Helm chart (`deploy/helm/fraud-scorer`) with HPA, PDB and
  ServiceMonitor, a 3-node `kind` config, a Grafana dashboard, and
  `docs/BENCHMARK.md` documenting the three ways a throughput number from this
  repo can be wrong — including the `RATE_LIMIT_PER_MIN` default of 10 req/s
  per key, which silently caps any load test.
- Per-stage Prometheus histograms (`fraud_score_stage_latency_seconds`),
  model batch size, and write-queue depth/drop counters.

### Fixed
- `872e8f6` — all three deployment configs (`Dockerfile`, `docker-compose.yml`,
  `deploy/fraud/`, `deploy/features/`) were broken by the repo merge; paths
  still pointed at the pre-merge layout of the three absorbed repos.

### Added
- `ffb33ce` — CI status badge, license badge, and a portfolio nav link in the
  README.

## [0.1.0] — repo merge

`featurestore`, `ml-monitor`, and `stream-fraud-detector` merged into a single
package (`src/ml_platform/`) so the three subsystems could depend on each
other directly instead of through duck-typed adapters and optional imports.
Closed three bugs that existed only because the repos were separate:

- `featurestore`'s drift detection was a documented stub (the real
  implementation lived in a "not-yet-built sibling project").
- `ml-monitor`'s feature-store adapter matched `offline_store.read()` and
  `registry.get_feature_group()` structurally instead of importing the real
  types.
- `stream-fraud-detector` wrapped `import ml_monitor` in a bare
  `try/except ImportError` that silently returned `None`, so a missing
  dependency disabled monitoring without any error.

Added `src/ml_platform/pipeline.py`, the first code path that crosses all
three subsystem boundaries, and `tests/test_pipeline_end_to_end.py`, which
could not have existed before the merge. See the README's "What integrates,
concretely" and "Limitations" sections for what the merge did and did not fix
(notably: it removed ~0 duplicate lines — the repos shared a naming skeleton,
not implementations).

Full pre-merge history lives in `docs/absorbed/` (original READMEs, RUNBOOK,
SECURITY of each of the three repos).
