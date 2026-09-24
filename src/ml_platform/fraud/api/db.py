"""Thin sync Postgres access via a connection pool. Simple queries only, no ORM.

Scored rows are written through `ScoreWriter`, a background batch writer, rather
than inline: a committing INSERT per request put a Postgres round trip and an
fsync on the scoring path, and the caller never needed either -- the score is
already computed, and the handler always treated persistence as best-effort.
"""
from __future__ import annotations

import json
import queue
import threading
import time

import psycopg2
import psycopg2.extras
import psycopg2.pool
import structlog

from ml_platform.fraud.api import metrics
from ml_platform.fraud.common.config import (
    DB_WRITE_BATCH,
    DB_WRITE_FLUSH_SEC,
    DB_WRITE_QUEUE_MAX,
    POSTGRES_DSN,
)

log = structlog.get_logger()

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def init_pool(minconn: int = 1, maxconn: int = 10) -> None:
    global _pool
    _pool = psycopg2.pool.SimpleConnectionPool(minconn, maxconn, dsn=POSTGRES_DSN)


def close_pool() -> None:
    if _pool:
        _pool.closeall()


def ping() -> bool:
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        finally:
            _pool.putconn(conn)
    except psycopg2.Error:
        return False


_INSERT_SQL = """
    INSERT INTO scored_transactions
        (transaction_id, user_id, amount, merchant_category,
         fraud_score, is_fraud, shap_explanation)
    VALUES %s
    ON CONFLICT (transaction_id) DO NOTHING
"""


def _row_tuple(row: dict) -> tuple:
    return (
        row["transaction_id"], row["user_id"], row["amount"], row["merchant_category"],
        row["fraud_score"], row["is_fraud"], json.dumps(row["shap_explanation"]),
    )


def insert_scores(rows: list[dict]) -> None:
    """Batch insert. One statement and one commit for the whole batch -- the
    per-row commit was the expensive half, not the INSERT."""
    if not rows:
        return
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, _INSERT_SQL, [_row_tuple(r) for r in rows])
        conn.commit()
    finally:
        _pool.putconn(conn)


class ScoreWriter:
    """Drains a bounded queue of scored rows into Postgres in batches.

    Bounded on purpose: an unbounded queue converts a slow or wedged Postgres
    into unbounded memory growth, which takes scoring down too. Full queue means
    rows are dropped and counted -- persistence loss, visible in
    fraud_db_write_queue_dropped_total, while scoring keeps serving.
    """

    def __init__(
        self,
        *,
        maxsize: int = DB_WRITE_QUEUE_MAX,
        batch_size: int = DB_WRITE_BATCH,
        flush_interval_s: float = DB_WRITE_FLUSH_SEC,
        sink=None,
    ):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self._sink = sink or insert_scores
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="score-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signals the writer and waits for it to flush what is already queued."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def submit(self, row: dict) -> bool:
        """Never blocks and never raises: the response does not depend on this."""
        try:
            self._q.put_nowait(row)
        except queue.Full:
            metrics.DB_QUEUE_DROPPED.inc()
            return False
        metrics.DB_QUEUE_DEPTH.set(self._q.qsize())
        return True

    def _drain(self) -> list[dict]:
        """Collect up to batch_size rows, waiting at most flush_interval_s."""
        batch: list[dict] = []
        deadline = time.monotonic() + self.flush_interval_s
        while len(batch) < self.batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self._q.get(timeout=min(remaining, 0.05)))
            except queue.Empty:
                if self._stop.is_set():
                    break
        return batch

    def _flush(self, batch: list[dict]) -> None:
        if not batch:
            return
        started = time.perf_counter()
        try:
            self._sink(batch)
        except Exception:
            # Counted per row so the metric stays comparable to the old
            # per-request counter, and so a large failed batch is visible as one.
            metrics.DB_INSERT_FAILURES.inc(len(batch))
            log.exception("db_batch_insert_failed", rows=len(batch))
        else:
            metrics.STAGE_LATENCY.labels(stage="db_insert").observe(
                (time.perf_counter() - started) / len(batch)
            )
        metrics.DB_QUEUE_DEPTH.set(self._q.qsize())

    def _run(self) -> None:
        while not self._stop.is_set():
            self._flush(self._drain())
        # Shutdown: drain whatever is left rather than dropping it silently.
        while True:
            batch = self._drain()
            if not batch:
                break
            self._flush(batch)


_writer: ScoreWriter | None = None


def start_writer() -> ScoreWriter:
    global _writer
    _writer = ScoreWriter()
    _writer.start()
    return _writer


def stop_writer() -> None:
    global _writer
    if _writer is not None:
        _writer.stop()
        _writer = None


def enqueue_score(row: dict) -> bool:
    """Best-effort persistence used by both the HTTP handler and the stream
    consumer. Falls back to a synchronous insert if no writer is running, so
    tests and one-off scripts keep working unchanged."""
    if _writer is None:
        insert_score(row)
        return True
    return _writer.submit(row)


def insert_score(row: dict) -> None:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scored_transactions
                    (transaction_id, user_id, amount, merchant_category,
                     fraud_score, is_fraud, shap_explanation)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (transaction_id) DO NOTHING
                """,
                (
                    row["transaction_id"], row["user_id"], row["amount"], row["merchant_category"],
                    row["fraud_score"], row["is_fraud"], json.dumps(row["shap_explanation"]),
                ),
            )
        conn.commit()
    finally:
        _pool.putconn(conn)


def stats_last(minutes: int = 60) -> dict:
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*), coalesce(sum(is_fraud::int), 0), coalesce(avg(fraud_score), 0)
                FROM scored_transactions
                WHERE scored_at >= now() - (%s || ' minutes')::interval
                """,
                (minutes,),
            )
            count, fraud_count, avg_score = cur.fetchone()
        return {
            "transaction_count": count,
            "fraud_count": fraud_count,
            "fraud_rate": (fraud_count / count) if count else 0.0,
            "avg_score": float(avg_score),
        }
    finally:
        _pool.putconn(conn)


def fetch_recent(limit: int = 5000) -> list[dict]:
    """Recent scored transactions for the monitor service's drift comparison."""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT amount, fraud_score, is_fraud, scored_at
                FROM scored_transactions
                ORDER BY scored_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            cols = ["amount", "fraud_score", "is_fraud", "scored_at"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        _pool.putconn(conn)
