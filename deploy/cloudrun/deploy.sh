#!/usr/bin/env bash
# Constrói a imagem no Cloud Build e publica o serviço no Cloud Run.
# Pré-requisito: ./setup_infra.sh executado uma vez no projeto.
#
# Uso: ./deploy.sh [tag]   — sem argumento, usa o commit curto do Git.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
config="${PJE_CLOUDRUN_CONFIG:-$here/config.env}"
[[ -f "$config" ]] || config="$here/config.env.example"
# shellcheck source=config.env.example
source "$config"

[[ -n "${PROJECT_ID:-}" ]] || {
    echo "PROJECT_ID vazio: defina em config.env ou via gcloud config set project" >&2
    exit 1
}

RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER_SA="${INVOKER_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}"
tag="${1:-$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo manual)}"

echo "== Build (${IMAGE_URI}:${tag}) =="
gcloud builds submit "$repo_root" \
    --project "$PROJECT_ID" \
    --tag "${IMAGE_URI}:${tag}"

echo "== Deploy =="
gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "${IMAGE_URI}:${tag}" \
    --no-allow-unauthenticated \
    --service-account "$RUNTIME_SA" \
    --execution-environment gen2 \
    --cpu "$CPU" \
    --memory "$MEMORY" \
    --concurrency "$CONCURRENCY" \
    --min-instances "$MIN_INSTANCES" \
    --max-instances "$MAX_INSTANCES" \
    --timeout "$TIMEOUT" \
    --cpu-boost \
    --session-affinity \
    --network "$NETWORK" \
    --subnet "$SUBNET" \
    --vpc-egress all-traffic \
    --add-volume "name=estado,type=cloud-storage,bucket=${STATE_BUCKET}" \
    --add-volume-mount "volume=estado,mount-path=/var/lib/pjepa-mcp" \
    --set-env-vars "PJE_ENV=production" \
    --set-secrets "PJE_CRED_CPF=${SECRET_PREFIX}-cpf:latest,PJE_CRED_SENHA=${SECRET_PREFIX}-senha:latest,PJE_CRED_TOTP_SEED=${SECRET_PREFIX}-totp-seed:latest,PJE_CRED_AUDIT_MASTER_KEY=${SECRET_PREFIX}-audit-master-key:latest"

gcloud run services add-iam-policy-binding "$SERVICE" \
    --project "$PROJECT_ID" --region "$REGION" \
    --member "serviceAccount:${INVOKER_SA}" \
    --role roles/run.invoker >/dev/null

url="$(gcloud run services describe "$SERVICE" \
    --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')"

cat <<EOF

Serviço publicado: ${url}
Endpoint MCP (Streamable HTTP): ${url}/mcp

Smoke test (initialize via token de identidade do invoker):

  TOKEN=\$(gcloud auth print-identity-token \\
      --impersonate-service-account "${INVOKER_SA}" \\
      --audiences "${url}")
  curl -sS -X POST "${url}/mcp" \\
      -H "Authorization: Bearer \$TOKEN" \\
      -H "Content-Type: application/json" \\
      -H "Accept: application/json, text/event-stream" \\
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
EOF
