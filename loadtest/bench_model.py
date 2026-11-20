"""Isolated benchmark of the model stage: one-row-per-request vs micro-batched.

This deliberately does NOT touch Redis, Postgres or HTTP. It measures the part
the batcher changes, so the result is attributable. The end-to-end number comes
from loadtest/locustfile.py against the docker-compose stack; use both -- this
one explains *why* that one moved.

    python loadtest/bench_model.py                 # default: 12 threads, 400 req each
    python loadtest/bench_model.py --threads 24 --requests 200

Reports req/s and latency percentiles for each path, against the real committed
bundle. Scores are asserted identical between the two paths as it runs: a
throughput win that changed the numbers would not be a win.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time

import numpy as np

# Resolved from this file, not the cwd, so `make bench-model` works from
# deploy/fraud as well as from the repo root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from ml_platform.fraud.common.features import FEATURE_NAMES  # noqa: E402
from ml_platform.fraud.scorer.batcher import MicroBatcher  # noqa: E402

MODEL_PATH = os.path.join(ROOT, "deploy", "fraud", "models", "model.joblib")


def load_parts():
    # joblib.load on this repo's own committed training artifact, the same file
    # common/registry.py loads in production -- not untrusted input.
    import joblib
    import shap

    bundle = joblib.load(MODEL_PATH)
    return (
        bundle["xgb"],
        bundle["iso"],
        bundle["iso_scaler"],
        shap.TreeExplainer(bundle["xgb"]),
        bundle.get("xgb_weight", 0.7),
    )


def run(label: str, call, threads: int, requests: int, rows: np.ndarray):
    """Drive `call` from `threads` threads, `requests` times each."""
    latencies: list[list[float]] = [[] for _ in range(threads)]
    results: list[list[float]] = [[] for _ in range(threads)]
    barrier = threading.Barrier(threads)

    def worker(tid: int):
        barrier.wait()  # start together, so the measured window is saturated
        for i in range(requests):
            row = rows[(tid * requests + i) % len(rows)]
            t0 = time.perf_counter()
            score = call(row)
            latencies[tid].append((time.perf_counter() - t0) * 1000)
            results[tid].append(score)

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.perf_counter()
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    elapsed = time.perf_counter() - t0

    flat = sorted(ms for per in latencies for ms in per)
    total = threads * requests
    print(
        f"{label:<22} {total / elapsed:8.1f} req/s   "
        f"median {statistics.median(flat):7.1f} ms   "
        f"p95 {flat[int(len(flat) * 0.95)]:7.1f} ms   "
        f"p99 {flat[int(len(flat) * 0.99)]:7.1f} ms"
    )
    return {"rps": total / elapsed, "scores": [s for per in results for s in per]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 8)
    ap.add_argument("--requests", type=int, default=400)
    ap.add_argument("--batch-wait-ms", type=float, default=5.0)
    ap.add_argument("--max-batch", type=int, default=64)
    args = ap.parse_args()

    if not os.path.exists(MODEL_PATH):
        print(f"no bundle at {MODEL_PATH} -- run `make train` in deploy/fraud first")
        return 1

    xgb, iso, scaler, explainer, weight = load_parts()
    rng = np.random.default_rng(7)
    rows = rng.uniform(0, 1000, size=(512, len(FEATURE_NAMES)))

    def predict_batch(batch: list):
        m = np.asarray(batch, dtype=float)
        proba = xgb.predict_proba(m)[:, 1].astype(np.float64)
        iso_scores = scaler.transform((-iso.score_samples(m)).reshape(-1, 1))[:, 0]
        scores = weight * proba + (1 - weight) * iso_scores
        return list(zip(scores, explainer.shap_values(m), strict=True))

    def single(row):
        """The pre-batching path: one call per request, per model."""
        return predict_batch([row])[0][0]

    batcher = MicroBatcher(
        predict_batch, max_batch=args.max_batch, max_wait_s=args.batch_wait_ms / 1000.0
    )
    batcher.start()

    print(
        f"bundle={MODEL_PATH}  threads={args.threads}  requests/thread={args.requests}  "
        f"batch<={args.max_batch} wait<={args.batch_wait_ms}ms"
    )
    # Thread-pool settings matter enough to print: XGBoost and scikit-learn
    # use OpenMP, which defaults to one thread per core. On a single (1, 11)
    # row that is mostly fork/join overhead, and with several server workers
    # it oversubscribes the machine badly (measured: 482% CPU from one
    # uvicorn worker). A number quoted without these is not reproducible.
    _t = {k: os.environ.get(k, "unset") for k in
          ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}
    print(
        f"  xgb n_jobs={xgb.get_params().get('n_jobs')}  cores={os.cpu_count()}  "
        + "  ".join(f"{k}={v}" for k, v in _t.items())
    )
    print()
    try:
        before = run("one row per call", single, args.threads, args.requests, rows)
        after = run("micro-batched", lambda r: batcher.submit(r)[0], args.threads, args.requests, rows)
    finally:
        batcher.stop()

    # A faster path that returns different numbers is not a faster path.
    np.testing.assert_array_equal(np.array(before["scores"]), np.array(after["scores"]))
    print(f"\nscores identical across both paths: yes ({len(before['scores'])} compared)")
    print(f"model-stage speedup: {after['rps'] / before['rps']:.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
