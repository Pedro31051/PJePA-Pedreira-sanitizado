#!/usr/bin/env bash
# Entrypoint do Cloud Run: traduz o contrato da plataforma (PORT, segredos em
# variáveis de ambiente) para o contrato do servidor (MCP_PORT e
# $CREDENTIALS_DIRECTORY com os mesmos arquivos do systemd LoadCredential).
# Nenhuma mudança de código no servidor é necessária.
set -euo pipefail

export MCP_HOST="${MCP_HOST:-0.0.0.0}"
export MCP_PORT="${PORT:-${MCP_PORT:-8080}}"

export PJE_STORAGE_DIR="${PJE_STORAGE_DIR:-/var/lib/pjepa-mcp/storage}"
# O perfil do navegador é efêmero de propósito: o Chromium não opera de forma
# confiável sobre GCS FUSE (locks, mmap). A sessão autenticada persiste como
# storage_state dentro de PJE_STORAGE_DIR e sobrevive à reciclagem da instância.
export PJE_PROFILE_DIR="${PJE_PROFILE_DIR:-/tmp/pjepa-profile}"
export PJE_AUDIT_LOG="${PJE_AUDIT_LOG:-/var/lib/pjepa-mcp/logs/audit.log}"

mkdir -p "$PJE_STORAGE_DIR" "$PJE_PROFILE_DIR" "$(dirname "$PJE_AUDIT_LOG")"

# Materializa $CREDENTIALS_DIRECTORY a partir dos segredos injetados pelo
# Secret Manager como variáveis de ambiente. Os nomes de arquivo são os mesmos
# que a unit pjepa-mcp.service entrega via LoadCredentialEncrypted.
if [[ -z "${CREDENTIALS_DIRECTORY:-}" ]]; then
    cred_dir="$(mktemp -d /tmp/pjepa-creds.XXXXXX)"
    chmod 0700 "$cred_dir"
    materializou=0
    for nome in cpf senha totp_seed audit_master_key; do
        var="PJE_CRED_$(printf '%s' "$nome" | tr '[:lower:]' '[:upper:]')"
        valor="${!var:-}"
        if [[ -n "$valor" ]]; then
            printf '%s' "$valor" > "$cred_dir/$nome"
            chmod 0400 "$cred_dir/$nome"
            materializou=1
        fi
    done
    if [[ "$materializou" == "1" ]]; then
        export CREDENTIALS_DIRECTORY="$cred_dir"
    else
        rmdir "$cred_dir"
    fi
fi
# As variáveis brutas não devem sobreviver no ambiente do servidor.
unset PJE_CRED_CPF PJE_CRED_SENHA PJE_CRED_TOTP_SEED PJE_CRED_AUDIT_MASTER_KEY

cd /app/mcp-server/src
exec python server.py
