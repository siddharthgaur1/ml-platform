"""Load test for POST /score against the full docker-compose stack.

Run: docker compose up -d && docker compose run --rm trainer  (see README Setup)
Then:
    locust -f loadtest/locustfile.py --host http://localhost:8000

Headless, for a fixed run (e.g. CI or a quick number for the README):
    locust -f loadtest/locustfile.py --host http://localhost:8000 \
        --headless -u 50 -r 10 -t 60s --csv loadtest/results
"""
import os
import random
import uuid

from locust import HttpUser, between, task

from ml_platform.fraud.common.features import MERCHANT_CATEGORIES

# Mirrors producer/stream.py's synthetic distribution -- see common/features.py
# and training/generate_data.py for the ranges these are drawn from.
_DEVICE_IDS = [f"device-{i:04d}" for i in range(200)] + ["brand-new-device"]
_LOCATION_HASHES = [f"5600{i:02d}" for i in range(20)]

# First key from API_KEYS, matching what docker-compose passes the scorer.
#
# This file previously sent no key at all, which meant it could only produce a
# number against ALLOW_UNAUTHENTICATED=true -- and that mode returns from
# require_api_key before the rate limiter runs. So the "authenticated" path and
# the rate limit were both untested by the load test that produced this repo's
# published throughput figure.
_API_KEY = os.environ.get("API_KEYS", "").split(",")[0].strip()


class ScorerUser(HttpUser):
    wait_time = between(0.05, 0.3)

    def on_start(self):
        if _API_KEY:
            self.client.headers.update({"X-API-Key": _API_KEY})

    @task
    def score_transaction(self):
        payload = {
            "transaction_id": f"loadtest-{uuid.uuid4().hex[:12]}",
            "user_id": f"user_{random.randint(0, 999):05d}",
            "amount": round(random.uniform(10, 50000), 2),
            "merchant_category": random.choice(MERCHANT_CATEGORIES),
            "hour": random.randint(0, 23),
            "device_id": random.choice(_DEVICE_IDS),
            "location_hash": random.choice(_LOCATION_HASHES),
            "user_age_days": random.randint(1, 2000),
        }
        with self.client.post("/score", json=payload, catch_response=True) as resp:
            if resp.status_code == 429:
                # Called out separately because it is the single most common way
                # a run here produces a confident, meaningless number:
                # RATE_LIMIT_PER_MIN defaults to 600/min = 10 req/s, shared by
                # every user via one key. See docs/BENCHMARK.md.
                resp.failure("429 rate limited -- raise RATE_LIMIT_PER_MIN")
            elif resp.status_code == 401:
                resp.failure("401 -- set API_KEYS for this load test, or ALLOW_UNAUTHENTICATED")
            elif resp.status_code == 503:
                resp.failure("503 -- limit-concurrency shedding, or a backend is down")
            elif resp.status_code != 200:
                resp.failure(f"status {resp.status_code}: {resp.text[:200]}")
            elif "fraud_score" not in resp.json():
                resp.failure("response missing fraud_score")
