"""Loads the trained XGBoost + IsolationForest bundle and scores a single
transaction: ensemble fraud probability + top SHAP feature contributions.

The model call itself is micro-batched (scorer/batcher.py): scoring one row at a
time spends 10.9ms of its 13.0ms in `IsolationForest.score_samples` per-call
overhead. `score()` still takes and returns exactly one transaction -- the
batching is internal, and the arithmetic is row-independent, so the scores are
identical either way (tests_fraud/test_batcher.py asserts this).
"""
from __future__ import annotations

import time

import numpy as np
import shap
import structlog

from ml_platform.fraud.api import metrics
from ml_platform.fraud.common import registry
from ml_platform.fraud.common.config import (
    FRAUD_SCORE_THRESHOLD,
    MODEL_BATCH_ENABLED,
    MODEL_BATCH_MAX,
    MODEL_BATCH_TIMEOUT_SEC,
    MODEL_BATCH_WAIT_MS,
    XGB_WEIGHT,
)
from ml_platform.fraud.common.features import FEATURE_NAMES, compute_features, features_to_vector
from ml_platform.fraud.scorer.batcher import MicroBatcher
from ml_platform.fraud.scorer.redis_store import RedisFeatureStore

log = structlog.get_logger()

TOP_K_SHAP = 5


class FraudScorer:
    def __init__(self):
        # Source is the registry stage when MLflow is configured, else the
        # local joblib file. See common/registry.py.
        bundle, version, source = registry.load_bundle()
        self.version = version
        self.source = source
        self.xgb = bundle["xgb"]
        self.iso = bundle["iso"]
        self.iso_scaler = bundle["iso_scaler"]
        self.feature_names = bundle["feature_names"]
        self.xgb_weight = bundle.get("xgb_weight", XGB_WEIGHT)
        self.threshold = bundle.get("threshold", FRAUD_SCORE_THRESHOLD)
        self._shap_explainer = shap.TreeExplainer(self.xgb)
        self.store = RedisFeatureStore()
        self._batcher = None
        if MODEL_BATCH_ENABLED:
            self._batcher = MicroBatcher(
                self.predict_batch,
                max_batch=MODEL_BATCH_MAX,
                max_wait_s=MODEL_BATCH_WAIT_MS / 1000.0,
                timeout_s=MODEL_BATCH_TIMEOUT_SEC,
                on_batch=metrics.MODEL_BATCH_SIZE.observe,
            )
            self._batcher.start()
        log.info("model_loaded", version=self.version, source=source,
                 xgb_weight=self.xgb_weight, threshold=self.threshold,
                 batching=MODEL_BATCH_ENABLED, batch_max=MODEL_BATCH_MAX,
                 batch_wait_ms=MODEL_BATCH_WAIT_MS)

    def close(self) -> None:
        if self._batcher is not None:
            self._batcher.stop()

    def predict_batch(self, rows: list) -> list[tuple[float, np.ndarray]]:
        """Ensemble score + raw SHAP values for a stack of feature vectors.

        Every operation here is row-independent, which is what makes batching
        safe: row i of the output depends only on row i of the input.
        """
        matrix = np.asarray(rows, dtype=float)
        # astype(float64) is load-bearing, not cosmetic: predict_proba returns
        # float32, and leaving it there does the ensemble arithmetic in float32
        # -- which diverges from the one-row path (which widened via float())
        # by ~4e-8. Small, but it makes "batching cannot change a score" false.
        xgb_proba = self.xgb.predict_proba(matrix)[:, 1].astype(np.float64)
        iso_raw = -self.iso.score_samples(matrix)
        iso_scores = self.iso_scaler.transform(iso_raw.reshape(-1, 1))[:, 0]
        # Weight and threshold come from the bundle, not the environment. They
        # were baked in at training time and the drift reference distribution
        # was computed with them; letting a deploy-time env var override them
        # silently invalidates every drift comparison against that reference.
        fraud_scores = self.xgb_weight * xgb_proba + (1 - self.xgb_weight) * iso_scores
        shap_values = self._shap_explainer.shap_values(matrix)
        return list(zip(fraud_scores, shap_values, strict=True))

    def score(self, tx: dict) -> dict:
        started = time.perf_counter()
        prior_amounts, prior_devices = self.store.get_history(tx["user_id"])
        metrics.STAGE_LATENCY.labels(stage="redis_history").observe(time.perf_counter() - started)

        features = compute_features(
            amount=tx["amount"],
            hour=tx["hour"],
            user_age_days=tx["user_age_days"],
            merchant_category=tx["merchant_category"],
            device_id=tx["device_id"],
            prior_amounts=prior_amounts,
            prior_devices=prior_devices,
        )

        vector = features_to_vector(features)
        started = time.perf_counter()
        if self._batcher is not None:
            fraud_score, shap_values = self._batcher.submit(vector)
        else:
            # Inline on this thread: the pre-batching path.
            fraud_score, shap_values = self.predict_batch([vector])[0]
            metrics.MODEL_BATCH_SIZE.observe(1)
        metrics.STAGE_LATENCY.labels(stage="model").observe(time.perf_counter() - started)

        fraud_score = float(fraud_score)
        ranked = sorted(zip(FEATURE_NAMES, shap_values, strict=True), key=lambda kv: abs(kv[1]), reverse=True)
        shap_explanation = {name: round(float(val), 4) for name, val in ranked[:TOP_K_SHAP]}

        # record AFTER computing features, so this transaction only affects
        # rolling stats for transactions that come after it, never its own score
        started = time.perf_counter()
        self.store.update(tx["user_id"], tx["amount"], tx["device_id"])
        metrics.STAGE_LATENCY.labels(stage="redis_update").observe(time.perf_counter() - started)

        return {
            "fraud_score": round(fraud_score, 4),
            "is_fraud": fraud_score >= self.threshold,
            "shap_explanation": shap_explanation,
        }
