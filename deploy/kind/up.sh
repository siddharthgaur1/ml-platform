#!/usr/bin/env bash
# Stand up the whole local Kubernetes story: kind cluster, dependencies,
# metrics-server, Prometheus/Grafana, and the scorer chart.
#
#     bash deploy/kind/up.sh            # full stack
#     SKIP_MONITORING=1 bash deploy/kind/up.sh   # skip kube-prometheus-stack
#
# Idempotent: re-running upgrades in place rather than failing on existing
# releases. Tear down with: kind delete cluster --name ml-platform
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT="$PWD"

CLUSTER="${CLUSTER:-ml-platform}"
IMAGE="${IMAGE:-ml-platform/fraud-scorer:0.1.0}"
RELEASE="${RELEASE:-fraud}"
SKIP_MONITORING="${SKIP_MONITORING:-}"

step() { echo; echo "=== $* ==="; }

step "1/7 cluster"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "cluster '$CLUSTER' exists, reusing"
else
  kind create cluster --config deploy/kind/kind-config.yaml
fi
kubectl cluster-info --context "kind-$CLUSTER" >/dev/null
kubectl config use-context "kind-$CLUSTER"

step "2/7 image (runtime-with-model: a pod has no host dir to mount)"
docker build -f deploy/fraud/Dockerfile --target runtime-with-model -t "$IMAGE" .
# kind nodes have their own image store; without this the pods get
# ErrImageNeverPull, since values-local.yaml sets pullPolicy: Never.
kind load docker-image "$IMAGE" --name "$CLUSTER"

step "3/7 helm repos"
helm repo add bitnami https://charts.bitnami.com/bitnami >/dev/null 2>&1 || true
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null 2>&1 || true
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

step "4/7 redis + postgres"
helm upgrade --install redis bitnami/redis \
  --set auth.enabled=false --set architecture=standalone \
  --set master.resources.requests.cpu=100m --wait --timeout 5m
helm upgrade --install postgres bitnami/postgresql \
  --set auth.username=fraud --set auth.password=fraud --set auth.database=fraud \
  --set primary.resources.requests.cpu=100m --wait --timeout 5m

step "5/7 metrics-server (without it the HPA reports <unknown> CPU forever)"
# --kubelet-insecure-tls: kind's kubelet serving certs are self-signed, so the
# default verification fails and no metrics are ever collected.
helm upgrade --install metrics-server metrics-server/metrics-server \
  --namespace kube-system \
  --set 'args={--kubelet-insecure-tls}' --wait --timeout 5m

if [ -z "$SKIP_MONITORING" ]; then
  step "6/7 prometheus + grafana (installs the CRDs the ServiceMonitor needs)"
  # Trimmed: the default stack also brings alertmanager and a pile of exporters,
  # which is a lot of RAM on a laptop for a throughput demo.
  helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
    --set alertmanager.enabled=false \
    --set prometheus.prometheusSpec.retention=2h \
    --set prometheus.prometheusSpec.resources.requests.memory=512Mi \
    --set grafana.adminPassword=admin \
    --wait --timeout 12m
else
  step "6/7 monitoring SKIPPED (SKIP_MONITORING set)"
fi

step "7/7 the scorer"
EXTRA=()
[ -n "$SKIP_MONITORING" ] && EXTRA+=(--set metrics.serviceMonitor.enabled=false)
helm upgrade --install "$RELEASE" deploy/helm/fraud-scorer \
  -f deploy/kind/values-local.yaml "${EXTRA[@]}" --wait --timeout 5m

step "schema"
# docker-compose applies init.sql through the Postgres entrypoint; the Bitnami
# chart does not, so scoring would work while every write failed.
kubectl exec -i postgres-postgresql-0 -- \
  env PGPASSWORD=fraud psql -U fraud -d fraud < src/ml_platform/fraud/db/init.sql

step "status"
kubectl get pods -o wide
kubectl get hpa,svc

cat <<'EOF'

Ready. Two things to open:

  kubectl port-forward svc/fraud-fraud-scorer 8000:8000
  kubectl port-forward svc/monitoring-grafana 3000:80     # admin / admin

Then drive it and watch the HPA:

  kubectl get hpa fraud-fraud-scorer -w
  locust -f loadtest/locustfile.py --host http://localhost:8000 \
    --headless -u 200 -r 20 -t 300s

Import deploy/helm/grafana-dashboard.json into Grafana. If "Mean model batch
size" sits at 1 under load, batching is not engaging and throughput will look
like the old numbers.
EOF
