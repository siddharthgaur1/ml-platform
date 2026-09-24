#!/usr/bin/env bash
# End-to-end A/B of the scoring service through HTTP, Redis and Postgres.
#
# Run from the repo root, with the docker-compose stack already built:
#     bash loadtest/ab_endtoend.sh
#
# Three arms, from one image, differing only in two env vars:
#
#   before:     UVICORN_WORKERS=1  MODEL_BATCH_ENABLED=false
#   batched-1w: UVICORN_WORKERS=1  MODEL_BATCH_ENABLED=true
#   after:      UVICORN_WORKERS=N  MODEL_BATCH_ENABLED=true
#
# Three and not two, because "we made it faster" is two separate claims: the code
# change (batching) and the process change (workers). The middle arm attributes
# them separately, and the batched-1w -> after gain should be roughly linear in
# worker count. If it is not, something other than CPU is the ceiling and the
# per-stage histogram printed after each arm will say what.
#
# MODEL_BATCH_ENABLED=false scores inline on the calling thread: the pre-batching
# code path exactly, which is what makes the "before" arm honest without checking
# out an old commit.
#
# Do NOT use MODEL_BATCH_WAIT_MS=0 as a stand-in for "before". It still routes
# every request through the single batcher thread, serialising the model call with
# none of the batching benefit. Measured, that is ~6.7 req/s against ~30 for the
# real before arm -- it is a pessimisation, not a baseline, and using it would
# have inflated the reported speedup by about 4x.
#
# RATE_LIMIT_PER_MIN is held HIGH and CONSTANT across all arms. The 600/min
# default is 10 req/s per key, shared by every locust user through one key, and
# would cap every arm at the same wrong number. See docs/BENCHMARK.md.
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
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
      echo "  healthy"
      return 0
    fi
    sleep 3
  done
  echo "  scorer never became healthy; recent logs:" >&2
  (cd "$COMPOSE_DIR" && docker compose logs --tail 40 scorer) >&2
  return 1
}

run_arm() {
  local name="$1" workers="$2" batching="$3" omp="${4:-1}"
  echo
  echo "=== arm: $name (workers=$workers batching=$batching omp=$omp rate_limit=$RATE_LIMIT_PER_MIN) ==="
  (
    cd "$COMPOSE_DIR"
    UVICORN_WORKERS="$workers" MODEL_BATCH_ENABLED="$batching" MODEL_BATCH_WAIT_MS=5 \
      OMP_NUM_THREADS="$omp" OPENBLAS_NUM_THREADS="$omp" MKL_NUM_THREADS="$omp" \
      docker compose up -d --no-build scorer redis postgres redpanda
  )

  # Verify the container actually took the new config. Compose silently reusing a
  # container would make this a comparison of one arm against itself, and the
  # output would look completely normal.
  local got_batching got_cmd
  got_batching=$(docker inspect fraud-scorer-1 \
    --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep '^MODEL_BATCH_ENABLED=' | cut -d= -f2 || true)
  got_cmd=$(docker inspect fraud-scorer-1 --format '{{json .Config.Cmd}}')
  if [ "$got_batching" != "$batching" ]; then
    echo "  FATAL: container has MODEL_BATCH_ENABLED='$got_batching', expected '$batching'" >&2
    return 1
  fi
  case "$got_cmd" in
    *"\"--workers\",\"$workers\""*) ;;
    *) echo "  FATAL: container cmd lacks --workers $workers: $got_cmd" >&2; return 1 ;;
  esac
  echo "  config verified: batching=$got_batching workers=$workers"

  wait_healthy

  # Warm the model, the SHAP explainer and the connection pools, so the first arm
  # does not pay one-time costs the later arms avoid.
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

  echo "--- per-stage mean latency, arm=$name ---"
  curl -s http://localhost:8000/metrics > "$OUT/$name-metrics.txt" || true
  python - "$OUT/$name-metrics.txt" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()

def val(pattern):
    m = re.search(pattern, text, re.M)
    return float(m.group(1)) if m else None

for stage in ("redis_history", "model", "redis_update", "db_insert"):
    c = val(rf'^fraud_score_stage_latency_seconds_count\{{stage="{stage}"\}} ([0-9.e+]+)')
    s = val(rf'^fraud_score_stage_latency_seconds_sum\{{stage="{stage}"\}} ([0-9.e+]+)')
    if c and s and c > 0:
        print(f"  {stage:15s} {s / c * 1000:8.2f} ms/call   (n={int(c)})")

bc = val(r'^fraud_model_batch_size_count ([0-9.e+]+)')
bs = val(r'^fraud_model_batch_size_sum ([0-9.e+]+)')
if bc and bc > 0:
    print(f"  {'batch size':15s} {bs / bc:8.2f} rows/batch ({int(bc)} batches)")
    if bs / bc < 1.5:
        print("  NOTE: batch size ~1 -- batching is not engaging; the 10.9ms "
              "single-row cost is in effect")
d = val(r'^fraud_db_write_queue_dropped_total ([0-9.e+]+)')
if d:
    print(f"  {'rows dropped':15s} {int(d):8d}   (persistence loss)")
PY
  echo "  NOTE: with >1 worker these are one worker's numbers only; locust is the"
  echo "        source of truth for throughput."
}

echo "A/B end-to-end: users=$USERS spawn=$SPAWN duration=$DURATION after_workers=$AFTER_WORKERS"

run_arm before 1 false 1
run_arm batched-1w 1 true 1
run_arm after "$AFTER_WORKERS" true 1
# Same as `after`, but letting OpenMP spawn one thread per core in every
# worker: AFTER_WORKERS x cores threads competing for cores cores. Passed as an
# explicit core count rather than 0, because OMP_NUM_THREADS=0 is
# implementation-defined and libgomp may just ignore it.
run_arm after-ompN "$AFTER_WORKERS" true "$(nproc 2>/dev/null || echo 12)"

echo
echo "=== summary (from locust CSVs) ==="
python - <<'PY'
import csv, os

out = os.path.join("loadtest", "results")
rows = {}
for arm in ("before", "batched-1w", "after", "after-ompN"):
    path = os.path.join(out, f"{arm}_stats.csv")
    if not os.path.exists(path):
        print(f"{arm}: no CSV")
        continue
    with open(path, newline="", encoding="utf-8") as f:
        agg = [r for r in csv.DictReader(f) if r.get("Name") == "Aggregated"]
    if not agg:
        print(f"{arm}: no Aggregated row")
        continue
    r = agg[0]
    rows[arm] = float(r["Requests/s"])
    print(f"{arm:11s} {float(r['Requests/s']):8.1f} req/s   "
          f"median {r['Median Response Time']:>6} ms   "
          f"p95 {r.get('95%', '?'):>6} ms   "
          f"p99 {r.get('99%', '?'):>7} ms   "
          f"failures {r['Failure Count']}/{r['Request Count']}")

if {"before", "batched-1w"} <= rows.keys():
    print(f"\ncode change alone (batching, 1 worker): {rows['batched-1w'] / rows['before']:.1f}x")
if {"batched-1w", "after"} <= rows.keys():
    print(f"process change alone (workers):          {rows['after'] / rows['batched-1w']:.1f}x")
if {"before", "after"} <= rows.keys():
    print(f"combined:                                {rows['after'] / rows['before']:.1f}x")
PY
echo
echo "Any non-zero failure count invalidates that arm. Check for 429 (rate limit)"
echo "or 503 (limit-concurrency shedding) before quoting a number."
