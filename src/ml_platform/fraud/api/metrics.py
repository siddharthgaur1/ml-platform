"""Prometheus metrics for the scoring service.

Uses prometheus-client rather than hand-rolling the exposition format: the
part worth getting right is the latency histogram, and correct bucket
accounting plus the text format's escaping rules are more code — and more
subtly wrong at 3am — than the dependency is worth.
"""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SCORE_REQUESTS = Counter(
    "fraud_score_requests_total",
    "Scoring requests handled, by source and outcome.",
    ["source", "outcome"],  # source: http|stream, outcome: ok|error|overloaded
)

SCORE_LATENCY = Histogram(
    "fraud_score_latency_seconds",
    "Model scoring latency.",
    ["source"],
    # Tuned to the observed range: scoring is single-digit ms, and the default
    # buckets put almost everything in the first one, which makes p99 useless.
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

FRAUD_FLAGGED = Counter(
    "fraud_flagged_total",
    "Transactions scored at or above the fraud threshold.",
    ["source"],
)

DB_INSERT_FAILURES = Counter(
    "fraud_db_insert_failures_total",
    "Score rows that could not be persisted. Sustained non-zero means the "
    "stats and drift views are running blind even while scoring looks healthy.",
)

STREAM_LAG_SECONDS = Gauge(
    "fraud_stream_last_message_age_seconds",
    "Seconds since the Kafka consumer last processed a message. Rises without "
    "bound if the consumer thread has died while the process stays up.",
)

MODEL_INFO = Gauge(
    "fraud_model_info",
    "Always 1; the labels carry the loaded model's identity.",
    ["version", "source"],
)

DRIFT_SHARE = Gauge(
    "fraud_drift_share",
    "Fraction of monitored columns flagged as drifted in the latest report.",
)


STAGE_LATENCY = Histogram(
    "fraud_score_stage_latency_seconds",
    "Latency of one stage inside a scoring request. Attributing total latency to "
    "redis_history / model / redis_update is what tells you whether a regression "
    "is model CPU or backend I/O -- SCORE_LATENCY alone cannot.",
    ["stage"],
    buckets=(0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1.0),
)

MODEL_BATCH_SIZE = Histogram(
    "fraud_model_batch_size",
    "Rows per batched model call. Sitting at 1 under load means the batch window "
    "is too short to be collecting anything and the 10.9ms single-row cost is back.",
    buckets=(1, 2, 4, 8, 16, 32, 64, 128),
)

DB_QUEUE_DEPTH = Gauge(
    "fraud_db_write_queue_depth",
    "Score rows waiting to be written. Climbing steadily means the writer cannot "
    "keep up and rows are about to be dropped.",
)

DB_QUEUE_DROPPED = Counter(
    "fraud_db_write_queue_dropped_total",
    "Score rows discarded because the write queue was full. The response was "
    "still served -- this is persistence loss, not scoring loss.",
)


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
