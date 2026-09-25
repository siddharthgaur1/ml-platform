# Deploying the fraud scorer on Kubernetes

Helm chart for the fraud scoring API. Runs on `kind` locally and unmodified on
GKE/EKS — the only differences are in a values file, not in the templates.

## Prerequisites

| tool | why |
|---|---|
| `docker` | builds the image; `kind` runs its nodes as containers |
| `kind` | local cluster |
| `helm` | this chart |
| `kubectl` | everything else |

## Local run on kind

```bash
# 1. Cluster (3 nodes: pod spread and the PDB are meaningless on one)
kind create cluster --config deploy/kind/kind-config.yaml

# 2. Image, with the model baked in -- a pod has no host dir to mount, unlike
#    docker-compose which mounts ./models
docker build -f deploy/fraud/Dockerfile --target runtime-with-model \
  -t ml-platform/fraud-scorer:0.1.0 .
kind load docker-image ml-platform/fraud-scorer:0.1.0 --name ml-platform

# 3. Dependencies. Not subcharts on purpose -- see Chart.yaml.
helm repo add bitnami https://charts.bitnami.com/bitnami
helm install redis bitnami/redis --set auth.enabled=false --set architecture=standalone
helm install postgres bitnami/postgresql \
  --set auth.username=fraud --set auth.password=fraud --set auth.database=fraud

# 4. metrics-server, or the HPA has no CPU signal and reports <unknown>.
#    --kubelet-insecure-tls because kind's kubelet serving certs are self-signed.
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/
helm install metrics-server metrics-server/metrics-server \
  --namespace kube-system --set args="{--kubelet-insecure-tls}"

# 5. Prometheus + Grafana. Installs the CRDs the chart's ServiceMonitor needs,
#    so this goes BEFORE the scorer if serviceMonitor.enabled=true.
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install monitoring prometheus-community/kube-prometheus-stack

# 6. The scorer
helm install fraud deploy/helm/fraud-scorer -f deploy/kind/values-local.yaml
kubectl rollout status deploy/fraud-fraud-scorer
```

The `scored_transactions` table has to exist before rows can be written
(`src/ml_platform/fraud/db/init.sql`; docker-compose applies it via the Postgres
entrypoint, which the Bitnami chart does not):

```bash
kubectl exec -i postgres-postgresql-0 -- \
  env PGPASSWORD=fraud psql -U fraud -d fraud < src/ml_platform/fraud/db/init.sql
```

Scoring still returns correct results without it — the write is best-effort — but
`/stats` stays empty and `fraud_db_insert_failures_total` climbs, which is exactly
the "blind while healthy" case that counter exists to make visible.

### Verify

```bash
kubectl port-forward svc/fraud-fraud-scorer 8000:8000 &
curl -s localhost:8000/health | python -m json.tool
curl -s -X POST localhost:8000/score \
  -H 'Content-Type: application/json' -H 'X-API-Key: local-dev-key' \
  -d '{"transaction_id":"k8s-1","user_id":"user_00001","amount":48000,
       "merchant_category":"electronics","hour":3,"device_id":"brand-new-device",
       "location_hash":"560001","user_age_days":12}'
```

### Watch it scale

```bash
kubectl get hpa fraud-fraud-scorer -w     # one pane
locust -f loadtest/locustfile.py --host http://localhost:8000 \
  --headless -u 200 -r 20 -t 300s        # another
```

Give it ~5 minutes: `scaleUp` has a 30s stabilisation window, and `scaleDown` a
300s one, so the fleet grows quickly and shrinks slowly on purpose. Grafana:

```bash
kubectl port-forward svc/monitoring-grafana 3000:80
# admin / admin  (set by deploy/kind/up.sh)
```

Import `deploy/helm/grafana-dashboard.json` (Dashboards -> New -> Import) and pick
the Prometheus datasource. The two panels worth watching during a load test:

- **Mean model batch size.** If it sits at 1 while under load, `MODEL_BATCH_WAIT_MS`
  is too short to collect anything and the 10.9 ms single-row cost is back --
  throughput will look like the old numbers and nothing else will explain why.
- **Mean latency per stage.** Attributes a regression to model CPU vs Redis vs
  Postgres. If the Redis stages dominate, adding replicas will not help.

## Cloud (GKE / EKS)

Same chart. Push the image to a registry, then:

```bash
helm install fraud deploy/helm/fraud-scorer \
  --set image.repository=<registry>/fraud-scorer \
  --set image.pullPolicy=IfNotPresent \
  --set auth.existingSecret=fraud-api-keys \
  --set backends.redisUrl=redis://<managed-redis>:6379/0 \
  --set backends.postgresDsn=postgresql://<user>:<pass>@<managed-pg>:5432/fraud \
  --set metrics.serviceMonitor.enabled=true
```

Two things change and both matter:

- **`auth.existingSecret`.** Keys passed via `--set auth.apiKeys` are stored in
  the release manifest, readable by anyone with `get secret` in the namespace. Use
  a managed secret in a real cluster.
- **Managed Redis and Postgres.** The Bitnami charts above are for local use;
  neither is configured here for durability or failover.

Cost note: the HPA maxes at 12 replicas × 500m CPU. On free credits, lower
`autoscaling.maxReplicas` before pointing a load test at it.

## Design decisions worth knowing

**One uvicorn worker per pod, not `--workers N`.** Scoring is CPU-bound and
CPython serialises on one GIL, so the throughput unit is a process. N pods beats N
workers-in-a-pod here because (a) `prometheus_client` state is per-process, so a
`/metrics` scrape behind `--workers` hits whichever worker got the socket and
returns partial counters, and (b) the HPA can only see pods. Workers inside a pod
are invisible to it.

**Liveness probes `/metrics`, readiness probes `/health`.** `/health` returns 503
when Redis or Postgres is unreachable, which is right for readiness — that pod
cannot do its job. Using it for liveness would mean a single Redis outage fails
liveness on every pod simultaneously and the kubelet restarts the whole
deployment in a loop, turning a degraded dependency into an outage.

**A startup probe.** Loading the bundle and building the SHAP explainer takes
seconds. Without a startup probe, the liveness probe can kill the pod mid-load and
it never finishes booting.

**`terminationGracePeriodSeconds: 45`.** The model batcher and the DB write queue
both flush on shutdown. The 30s default is usually enough; 45 leaves margin under
load so a rollout does not drop queued rows.

**HPA on CPU, not on req/s.** Scaling on `fraud_score_requests_total` needs
`prometheus-adapter` and a `custom.metrics.k8s.io` API. Because this service is
CPU-bound by construction, CPU utilisation is nearly collinear with request rate,
so the adapter buys accuracy that does not change a scaling decision. If you want
it anyway: install `prometheus-community/prometheus-adapter`, expose a rule for
`rate(fraud_score_requests_total[1m])`, and replace the `Resource` metric in
`templates/hpa.yaml` with a `Pods` metric. Deliberately not built here.

**Model baked into the image.** A pod has no host directory to mount. This couples
the model and code lifecycles, which is wrong for production — set
`MLFLOW_TRACKING_URI` and the registry becomes the source of truth
(`common/registry.py`), making the baked file unused. Fine for a self-contained
demo, flagged rather than hidden.

## Chart values

See `values.yaml`; every non-obvious default is commented there. The ones most
likely to need changing:

| value | default | note |
|---|---|---|
| `scoring.rateLimitPerMin` | `600` | **10 req/s per key.** Caps load tests silently — see `docs/BENCHMARK.md` |
| `autoscaling.maxReplicas` | `12` | 6 CPU of requests at full scale |
| `workers` | `1` | see above; raising it breaks `/metrics` aggregation |
| `limitConcurrency` | `256` | sheds with 503 past this instead of queueing unboundedly |
| `auth.existingSecret` | `""` | use this, not `auth.apiKeys`, outside a local cluster |

## Troubleshooting

| symptom | cause |
|---|---|
| HPA shows `<unknown>` CPU | `metrics-server` missing, or missing `--kubelet-insecure-tls` on kind |
| `ServiceMonitor` fails to apply | Prometheus Operator CRDs absent — install `kube-prometheus-stack` first |
| Pod `CrashLoopBackOff`, logs say `API_KEYS is unset` | set `auth.apiKeys`, `auth.existingSecret`, or `auth.allowUnauthenticated` |
| `ErrImageNeverPull` on kind | `kind load docker-image` not run, or image tag mismatch |
| `/health` 503, `db_connected: false` | Postgres DSN wrong, or `init.sql` not applied |
| Throughput stuck near 30 req/s | rate limit, or `fraud_model_batch_size` sitting at 1 |
