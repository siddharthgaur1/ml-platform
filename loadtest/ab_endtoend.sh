#!/usr/bin/env bash
# End-to-end A/B of the scoring service through HTTP, Redis and Postgres.
#
# Run from the repo root, with the docker-compose stack buildable:
#     bash loadtest/ab_endtoend.sh
#
# The two arms differ in exactly two settings, and the same image serves both:
#
#   before: UVICORN_WORKERS=1  MODEL_BATCH_WAIT_MS=0
#   after:  UVICORN_WORKERS=N  MODEL_BATCH_WAIT_MS=5
#
# MODEL_BATCH_WAIT_MS=0 makes the batcher's collection deadline expire
# immediately, so every batch holds exactly one row -- which reproduces the
# original one-call-per-request cost without checking out the old code. That
# keeps the comparison to one variable pair rather than one variable pair plus a
# rebuild.
#
# RATE_LIMIT_PER_MIN is held HIGH and CONSTANT across both arms on purpose. The
# 600/min default is 10 req/s per shared key and would cap both arms at roughly
# the same wrong number, making the change look like it did nothing. See
# docs/BENCHMARK.md.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
COMPOSE_DIR="$ROOT/deploy/fraud"
OUT="$ROOT/loadtest/results"

USERS="${USERS:-50}"
SPAWN="${SPAWN:-10}"
DURATION="${DURATION:-60s}"
AFTER_WORKERS="${AFTER_WORKERS:-4}"
export RATE_LIMIT_PER_MIN="${RATE_LIMIT_PER_MIN:-24000}"
export API_KEYS="${API_KEYS:-bench-key}"

mkdir -p "$OUT"

wait_healthy() {
  echo "  waiting for /health..."
  for _ in $(seq 1 60); do
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then echo "  healthy"; return 0; fi
    sleep 3
  done
  echo "  scorer never became healthy; recent logs:" >&2
  (cd "$COMPOSE_DIR" && docker compose logs --tail 40 scorer) >&2
  return 1
}

run_arm() {
  local name="$1" workers="$2" wait_ms="$3"
  echo
  echo "=== arm: $name (workers=$workers batch_wait_ms=$wait_ms rate_limit=$RATE_LIMIT_PER_MIN) ==="
  (
    cd "$COMPOSE_DIR"
    UVICORN_WORKERS="$workers" MODEL_BATCH_WAIT_MS="$wait_ms" \
      docker compose up -d --no-build scorer redis postgres redpanda
  )
  wait_healthy

  # Warm the model, the SHAP explainer and the connection pools. Without this the
  # first arm pays one-time costs the second does not, which flatters whichever
  # arm runs second.
  for _ in $(seq 1 20); do
    curl -sf -X POST http://localhost:8000/score \
      -H 'Content-Type: application/json' -H "X-API-Key: $API_KEYS" \
      -d '{"transaction_id":"warm","user_id":"user_00001","amount":100,
           "merchant_category":"dining","hour":12,"device_id":"d1",
           "location_hash":"560001","user_age_days":100}' >/dev/null 2>&1 || true
  done

  locust -f loadtest/locustfile.py --host http://localhost:8000 \
    --headless -u "$USERS" -r "$SPAWN" -t "$DURATION" \
    --csv "$OUT/$name" --only-summary

  echo "--- per-stage latency, arm=$name ---"
  curl -s http://localhost:8000/metrics \
    | grep -E '^fraud_(score_stage_latency_seconds_(sum|count)|model_batch_size_(sum|count)|db_write_queue)' \
    || echo "  (no stage metrics scraped)"
}

echo "A/B end-to-end: users=$USERS spawn=$SPAWN duration=$DURATION after_workers=$AFTER_WORKERS"
echo "NOTE: with >1 worker, /metrics reflects only the worker that served the"
echo "      scrape. locust is the throughput source of truth."

run_arm before 1 0
run_arm after "$AFTER_WORKERS" 5

echo
echo "=== summary (requests/s and latency from locust CSVs) ==="
python - <<'PY'
import csv, os
out = os.path.join("loadtest", "results")
for arm in ("before", "after"):
    path = os.path.join(out, f"{arm}_stats.csv")
    if not os.path.exists(path):
        print(f"{arm}: no CSV"); continue
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Name") == "Aggregated"]
    if not rows:
        print(f"{arm}: no Aggregated row"); continue
    r = rows[0]
    print(f"{arm:7s} {float(r['Requests/s']):8.1f} req/s   "
          f"median {r['Median Response Time']:>7} ms   "
          f"p95 {r.get('95%', '?'):>7} ms   "
          f"p99 {r.get('99%', '?'):>7} ms   "
          f"failures {r['Failure Count']}/{r['Request Count']}")
PY
echo
echo "Non-zero failures invalidate the run -- check for 429 (rate limit) or 503"
echo "(limit-concurrency shedding) before quoting any number above."
