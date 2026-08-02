#!/usr/bin/env bash
# Provisiona a infraestrutura do servidor MCP no Google Cloud. Idempotente:
# recursos existentes são mantidos. Cria recursos cobrados por hora (IP
# estático + Cloud NAT, ~US$15–20/mês) e por isso pede confirmação.
#
# Uso: ./setup_infra.sh [--yes]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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

echo "Projeto: ${PROJECT_ID} | Região: ${REGION} | Config: ${config}"
if [[ "${1:-}" != "--yes" ]]; then
    if [[ ! -t 0 ]]; then
        echo "Sem terminal interativo: rode com --yes para confirmar o provisionamento." >&2
        exit 1
    fi
    read -rp "Provisionar (inclui IP estático e Cloud NAT, cobrados por hora)? [s/N] " resp
    [[ "$resp" =~ ^[sS]$ ]] || { echo "Abortado."; exit 1; }
fi

echo "== APIs =="
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    compute.googleapis.com \
    storage.googleapis.com \
    aiplatform.googleapis.com \
    --project "$PROJECT_ID"

echo "== Artifact Registry =="
gcloud artifacts repositories describe "$AR_REPO" \
    --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud artifacts repositories create "$AR_REPO" \
        --repository-format docker --location "$REGION" --project "$PROJECT_ID"

echo "== Rede de egress com IP fixo =="
gcloud compute networks describe "$NETWORK" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud compute networks create "$NETWORK" \
        --subnet-mode custom --project "$PROJECT_ID"

gcloud compute networks subnets describe "$SUBNET" \
    --region "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud compute networks subnets create "$SUBNET" \
        --network "$NETWORK" --range "$SUBNET_RANGE" \
        --region "$REGION" --project "$PROJECT_ID"

gcloud compute addresses describe "$NAT_IP_NAME" \
    --region "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud compute addresses create "$NAT_IP_NAME" \
        --region "$REGION" --project "$PROJECT_ID"

gcloud compute routers describe "$ROUTER" \
    --region "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud compute routers create "$ROUTER" \
        --network "$NETWORK" --region "$REGION" --project "$PROJECT_ID"

# NAT restrito à sub-rede do serviço: todo egress do Cloud Run sai pelo IP
# estático reservado acima, o único a cadastrar em eventual whitelist do TJPA.
gcloud compute routers nats describe "$NAT_GATEWAY" \
    --router "$ROUTER" --region "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud compute routers nats create "$NAT_GATEWAY" \
        --router "$ROUTER" --region "$REGION" --project "$PROJECT_ID" \
        --nat-external-ip-pool "$NAT_IP_NAME" \
        --nat-custom-subnet-ip-ranges "$SUBNET"

echo "== Bucket de estado =="
gcloud storage buckets describe "gs://${STATE_BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud storage buckets create "gs://${STATE_BUCKET}" \
        --location "$REGION" --project "$PROJECT_ID" \
        --uniform-bucket-level-access --public-access-prevention

echo "== Contas de serviço =="
gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$RUNTIME_SA_ID" \
        --display-name "PJePA MCP runtime" --project "$PROJECT_ID"

gcloud iam service-accounts describe "$INVOKER_SA" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$INVOKER_SA_ID" \
        --display-name "PJePA MCP invoker" --project "$PROJECT_ID"

gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
    --member "serviceAccount:${RUNTIME_SA}" \
    --role roles/storage.objectAdmin --project "$PROJECT_ID" >/dev/null

# O especialista processual (google-genai com vertexai=True) usa a identidade
# da carga de trabalho; nenhuma GEMINI_API_KEY entra no ambiente.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${RUNTIME_SA}" \
    --role roles/aiplatform.user --condition None >/dev/null

echo "== Segredos =="
segredo_existe() {
    gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1
}

# Sem terminal interativo, os segredos digitáveis são criados vazios e o valor
# é preenchido depois no Console (Secret Manager → nova versão), para que
# nenhuma credencial transite por histórico de shell ou logs de sessão.
SEGREDOS_PENDENTES=()

criar_segredo() {
    local nome="$1" rotulo="$2" valor
    if segredo_existe "$nome"; then
        if ! gcloud secrets versions list "$nome" --project "$PROJECT_ID" \
            --limit 1 --format 'value(name)' | grep -q .; then
            SEGREDOS_PENDENTES+=("${nome} — ${rotulo}")
        fi
        echo "segredo ${nome} já existe; mantido"
        return
    fi
    if [[ "$nome" == "${SECRET_PREFIX}-audit-master-key" ]]; then
        valor="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
        echo "audit_master_key gerada automaticamente (32 bytes, base64 urlsafe)"
    elif [[ -t 0 ]]; then
        read -rsp "Valor para ${rotulo} (não ecoa): " valor
        echo
        [[ -n "$valor" ]] || { echo "valor vazio para ${nome}" >&2; exit 1; }
    else
        gcloud secrets create "$nome" \
            --project "$PROJECT_ID" \
            --replication-policy user-managed --locations "$REGION"
        SEGREDOS_PENDENTES+=("${nome} — ${rotulo}")
        echo "segredo ${nome} criado SEM valor; preencher no Console"
        return
    fi
    printf '%s' "$valor" | gcloud secrets create "$nome" \
        --project "$PROJECT_ID" \
        --replication-policy user-managed --locations "$REGION" \
        --data-file=-
}

criar_segredo "${SECRET_PREFIX}-cpf" "CPF de acesso ao PJe"
criar_segredo "${SECRET_PREFIX}-senha" "senha do PJe/PDPJ"
criar_segredo "${SECRET_PREFIX}-totp-seed" "seed TOTP do PJe"
criar_segredo "${SECRET_PREFIX}-audit-master-key" "chave da auditoria"

for sufixo in cpf senha totp-seed audit-master-key; do
    gcloud secrets add-iam-policy-binding "${SECRET_PREFIX}-${sufixo}" \
        --project "$PROJECT_ID" \
        --member "serviceAccount:${RUNTIME_SA}" \
        --role roles/secretmanager.secretAccessor >/dev/null
done

ip_saida="$(gcloud compute addresses describe "$NAT_IP_NAME" \
    --region "$REGION" --project "$PROJECT_ID" --format 'value(address)')"

echo
echo "Infraestrutura pronta."
echo "IP estático de saída (para eventual whitelist junto ao TJPA): ${ip_saida}"
if ((${#SEGREDOS_PENDENTES[@]})); then
    echo
    echo "ATENÇÃO — segredos sem valor (o deploy falha até preenchê-los):"
    printf '  - %s\n' "${SEGREDOS_PENDENTES[@]}"
    echo "Preencha no navegador: console.cloud.google.com → projeto ${PROJECT_ID}"
    echo "→ busque 'Secret Manager' → clique no segredo → 'Nova versão' → cole o valor."
fi
echo "Próximo passo: ./deploy.sh"
