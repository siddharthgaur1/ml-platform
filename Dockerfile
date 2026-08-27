FROM python:3.11-slim

WORKDIR /app

# The 2026-08 merge moved this from a flat ml_monitor/ package into
# src/ml_platform/monitoring/ alongside the features and fraud subsystems;
# this Dockerfile (and deploy/features, deploy/fraud) still copied the old
# pre-merge layout and had not built since.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir ".[api,dashboard,sklearn]"

ENV ML_MONITOR_DB=/data/ml_platform.db
VOLUME ["/data"]

EXPOSE 8080 8501

# Runs the REST API by default; run the dashboard with:
#   docker compose exec ml-monitor streamlit run -m ml_platform.monitoring.dashboard -- --db /data/ml_platform.db
CMD ["python", "-m", "ml_platform.monitoring.cli", "status", "--db", "/data/ml_platform.db"]
