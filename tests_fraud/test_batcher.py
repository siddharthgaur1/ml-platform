"""Tests for the model micro-batcher and the asynchronous score writer.

The property that matters most is parity: batching is only a legitimate
optimisation if it cannot change a score. `test_real_bundle_batched_matches_single_row`
asserts that against the committed model, because a silent score change would
invalidate the drift reference the bundle was trained with.
"""
from __future__ import annotations

import os
import threading
import time

import numpy as np
import pytest
import shap

from ml_platform.fraud.scorer.batcher import MicroBatcher
from ml_platform.fraud.scorer.model import FraudScorer

MODEL_PATH = os.path.join("deploy", "fraud", "models", "model.joblib")


@pytest.fixture
def batcher_factory():
    created: list[MicroBatcher] = []

    def make(fn, **kw):
        b = MicroBatcher(fn, **kw)
        b.start()
        created.append(b)
        return b

    yield make
    for b in created:
        b.stop()


def test_each_caller_gets_its_own_row(batcher_factory):
    """The failure this guards against is an off-by-one in the fan-out, which
    would hand callers each other's fraud scores."""
    b = batcher_factory(lambda rows: [sum(r) for r in rows], max_wait_s=0.02)
    results: dict[int, float] = {}

    def worker(i):
        results[i] = b.submit([float(i), float(i)])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert results == {i: float(i * 2) for i in range(20)}


def test_concurrent_calls_are_actually_batched(batcher_factory):
    """Without this the optimisation silently does nothing: correctness tests
    pass at batch size 1."""
    sizes: list[int] = []

    def fn(rows):
        sizes.append(len(rows))
        return [0.0] * len(rows)

    b = batcher_factory(fn, max_wait_s=0.05, max_batch=64)
    threads = [threading.Thread(target=lambda: b.submit([1.0])) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max(sizes) > 1, f"nothing batched: {sizes}"
    assert sum(sizes) == 16


def test_predict_failure_reaches_every_caller_in_the_batch(batcher_factory):
    """A batch failure must surface as an error per caller, not as a hang."""
    def fn(rows):
        raise ValueError("model exploded")

    b = batcher_factory(fn, max_wait_s=0.01)
    errors = []

    def worker():
        try:
            b.submit([1.0])
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == ["ValueError"] * 5


def test_wrong_result_count_is_an_error_not_a_mismatch(batcher_factory):
    """Returning the wrong number of rows must fail loudly; zip() would
    otherwise drop the tail of the batch and leave those callers hanging."""
    b = batcher_factory(lambda rows: [1.0] * (len(rows) - 1), max_wait_s=0.01)
    with pytest.raises(RuntimeError, match="results for"):
        b.submit([1.0])


def test_submit_on_a_stopped_batcher_fails_fast():
    b = MicroBatcher(lambda rows: [0.0] * len(rows))
    b.start()
    b.stop()
    with pytest.raises(RuntimeError, match="not running"):
        b.submit([1.0])


def test_submit_times_out_rather_than_hanging_forever(batcher_factory):
    """A wedged model call must become a 500, not an indefinitely held thread."""
    b = batcher_factory(lambda rows: time.sleep(10) or [0.0], max_wait_s=0.001, timeout_s=0.2)
    with pytest.raises(TimeoutError):
        b.submit([1.0])


@pytest.mark.skipif(not os.path.exists(MODEL_PATH), reason="trained bundle not present")
def test_real_bundle_batched_matches_single_row():
    """Batching must not change a score. Row-independent arithmetic says it
    cannot; this asserts it against the actual trained models."""
    # joblib.load on the repo's own committed training artifact -- the same
    # path common/registry.py loads in production, not untrusted input.
    import joblib

    from ml_platform.fraud.common.features import FEATURE_NAMES

    bundle = joblib.load(MODEL_PATH)
    xgb, iso, scaler = bundle["xgb"], bundle["iso"], bundle["iso_scaler"]
    weight = bundle.get("xgb_weight", 0.7)

    rng = np.random.default_rng(0)
    rows = rng.uniform(0, 1000, size=(17, len(FEATURE_NAMES)))

    def score_one(row):
        """The pre-batching one-row path, verbatim."""
        v = np.array([row], dtype=float)
        p = float(xgb.predict_proba(v)[0, 1])
        i = float(scaler.transform((-iso.score_samples(v)).reshape(-1, 1))[0, 0])
        return weight * p + (1 - weight) * i

    singles = np.array([score_one(r) for r in rows])

    # Exercise the real batched implementation, not a re-derivation of it.
    scorer = FraudScorer.__new__(FraudScorer)
    scorer.xgb, scorer.iso, scorer.iso_scaler = xgb, iso, scaler
    scorer.xgb_weight = weight
    scorer._shap_explainer = shap.TreeExplainer(xgb)
    batched = np.array([s for s, _ in scorer.predict_batch(rows)])

    # Bitwise, not approximate. predict_proba returns float32, so the ensemble
    # must be widened to float64 exactly as the one-row path's float() did;
    # allclose here would have let that regression through at ~4e-8.
    np.testing.assert_array_equal(singles, batched)


# --- score writer ----------------------------------------------------------

def test_writer_batches_and_flushes_on_stop():
    from ml_platform.fraud.api.db import ScoreWriter

    written: list[list[dict]] = []
    w = ScoreWriter(batch_size=100, flush_interval_s=0.05, sink=written.append)
    w.start()
    for i in range(50):
        assert w.submit({"transaction_id": str(i)}) is True
    w.stop()

    rows = [r for batch in written for r in batch]
    assert len(rows) == 50, f"rows lost: {len(rows)}"
    assert max(len(b) for b in written) > 1, "writer never batched"


def test_full_queue_drops_and_counts_rather_than_blocking():
    """A slow Postgres must not become unbounded memory growth, and must never
    block the response path."""
    from ml_platform.fraud.api import db, metrics

    before = metrics.DB_QUEUE_DROPPED._value.get()
    w = db.ScoreWriter(maxsize=2, sink=lambda rows: None)
    # Deliberately not started: nothing drains, so the queue fills.
    assert w.submit({"transaction_id": "a"}) is True
    assert w.submit({"transaction_id": "b"}) is True
    assert w.submit({"transaction_id": "c"}) is False
    assert metrics.DB_QUEUE_DROPPED._value.get() == before + 1


def test_sink_failure_is_counted_and_does_not_kill_the_writer():
    from ml_platform.fraud.api import db, metrics

    before = metrics.DB_INSERT_FAILURES._value.get()
    calls: list[int] = []

    def flaky(rows):
        calls.append(len(rows))
        if len(calls) == 1:
            raise RuntimeError("postgres down")

    w = db.ScoreWriter(batch_size=1, flush_interval_s=0.05, sink=flaky)
    w.start()
    w.submit({"transaction_id": "a"})
    time.sleep(0.3)
    w.submit({"transaction_id": "b"})
    w.stop()

    assert len(calls) >= 2, "writer died on the first failure"
    assert metrics.DB_INSERT_FAILURES._value.get() > before
