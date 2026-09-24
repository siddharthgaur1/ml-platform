"""Micro-batching for the model call.

`IsolationForest.score_samples` on a single (1, 11) row costs 10.9 ms against the
committed bundle, and almost all of it is per-call overhead rather than
arithmetic: 256 rows cost 26 ms, i.e. 0.10 ms/row. XGB and SHAP have the same
shape of overhead, smaller. Under concurrent load the fix is to stop making one
call per request.

A worker thread collects rows arriving within `max_wait_s` (capped at
`max_batch`), runs the predict function once on the stacked array, and hands each
caller back its own row. Callers are ordinary sync FastAPI handlers running in
the anyio threadpool: they block on an Event, which is what that pool is for.

Deliberately threads, not asyncio: the Redis and Postgres clients on this path
are sync, so an async migration would be a large diff for no measured gain.
Batching is row-independent arithmetic, so scores are identical to the one-row
path -- tests_fraud/test_batcher.py asserts that against the real bundle.
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence

import structlog

log = structlog.get_logger()

_STOP = object()


class _Pending:
    """One in-flight row and the caller waiting on it."""

    __slots__ = ("row", "event", "result", "error")

    def __init__(self, row: Sequence[float]):
        self.row = row
        self.event = threading.Event()
        self.result = None
        self.error: BaseException | None = None


class MicroBatcher:
    def __init__(
        self,
        predict_batch: Callable[[list], Sequence],
        *,
        max_batch: int = 64,
        max_wait_s: float = 0.005,
        timeout_s: float = 5.0,
        on_batch: Callable[[int], None] | None = None,
    ):
        self._predict_batch = predict_batch
        self.max_batch = max_batch
        self.max_wait_s = max_wait_s
        self.timeout_s = timeout_s
        self._on_batch = on_batch
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="model-batcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._q.put(_STOP)
        self._thread.join(timeout=timeout)
        self._thread = None

    def submit(self, row: Sequence[float]):
        """Block until this row has been scored. Raises on batch failure."""
        if self._thread is None or not self._thread.is_alive():
            # Better a clear 500 than a request that blocks for timeout_s on a
            # batcher that will never drain it.
            raise RuntimeError("model batcher is not running")
        pending = _Pending(row)
        self._q.put(pending)
        if not pending.event.wait(self.timeout_s):
            raise TimeoutError(f"model batch did not complete within {self.timeout_s}s")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def _run(self) -> None:
        while True:
            first = self._q.get()
            if first is _STOP:
                return
            batch = [first]
            deadline = time.monotonic() + self.max_wait_s
            while len(batch) < self.max_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._q.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is _STOP:
                    # Still score what we already took, then exit -- those
                    # callers are blocked on us.
                    self._stopping = True
                    break
                batch.append(item)
            self._dispatch(batch)
            if self._stopping:
                return

    def _dispatch(self, batch: list[_Pending]) -> None:
        try:
            results = self._predict_batch([p.row for p in batch])
            if len(results) != len(batch):
                raise RuntimeError(
                    f"predict_batch returned {len(results)} results for {len(batch)} rows"
                )
        except BaseException as exc:  # noqa: BLE001 - re-raised in every caller
            log.exception("model_batch_failed", batch_size=len(batch))
            for pending in batch:
                pending.error = exc
        else:
            for pending, result in zip(batch, results, strict=True):
                pending.result = result
        finally:
            # Must happen even if the callback or the assignment above throws:
            # an unset event is a caller hung for timeout_s.
            if self._on_batch is not None:
                try:
                    self._on_batch(len(batch))
                except Exception:  # noqa: BLE001 - metrics must never break scoring
                    log.exception("on_batch_callback_failed")
            for pending in batch:
                pending.event.set()
