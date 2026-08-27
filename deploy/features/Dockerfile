FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]"

# Baked-in default config (parquet + sqlite, no external services) so the
# image runs standalone; docker-compose.yml can bind-mount a real
# ml_platform.yaml over this to override it.
COPY ml_platform.yaml.example ./ml_platform.yaml

EXPOSE 8000
CMD ["uvicorn", "ml_platform.features.api:app", "--host", "0.0.0.0", "--port", "8000"]
