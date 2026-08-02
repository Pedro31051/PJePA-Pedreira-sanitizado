#!/usr/bin/env bash
# Diagnóstico SOMENTE LEITURA de deriva entre:
#   (a) o que está no disco (git working tree),
#   (b) o que o systemd acha que está rodando (ActiveEnterTimestamp),
#   (c) o que o processo em memória expõe via status_e_auditoria_pje.
#
# Não existe "MCP externo" separado: pjepa-mcp.service executa
# mcp-server/src/server.py direto desta working tree, nesta VM. Deriva aqui
# é disco-vs-processo-em-memória (precisa restart), não máquina-vs-máquina.
#
# Não altera nada. Não reinicia serviço. Não faz commit/push.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
SERVICE="pjepa-mcp.service"
LOCAL_URL="http://127.0.0.1:8001/mcp"

echo "== Estado no disco (git) =========================================="
cd "$REPO_DIR"
COMMIT_DISCO=$(git rev-parse HEAD)
BRANCH_DISCO=$(git rev-parse --abbrev-ref HEAD)
echo "commit:  $COMMIT_DISCO"
echo "branch:  $BRANCH_DISCO"

DIRTY=$(git status --porcelain)
if [ -n "$DIRTY" ]; then
  N=$(echo "$DIRTY" | wc -l | tr -d ' ')
  echo "árvore:  SUJA ($N arquivo(s) não commitado(s))"
else
  echo "árvore:  limpa"
fi

if UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
  AB=$(git rev-list --left-right --count "HEAD...$UPSTREAM")
  AHEAD=$(echo "$AB" | awk '{print $1}')
  BEHIND=$(echo "$AB" | awk '{print $2}')
  echo "upstream: $UPSTREAM (ahead=$AHEAD behind=$BEHIND)"
  if [ "$AHEAD" != "0" ]; then
    echo "  ATENÇÃO: $AHEAD commit(s) local(is) não enviados ao remoto — perdidos se a VM cair."
  fi
else
  echo "upstream: NENHUM configurado para $BRANCH_DISCO"
fi

echo
echo "== Estado do serviço systemd ======================================"
if systemctl is-active --quiet "$SERVICE"; then
  ACTIVE_SINCE=$(systemctl show "$SERVICE" -p ActiveEnterTimestamp --value)
  PID=$(systemctl show "$SERVICE" -p MainPID --value)
  echo "serviço: ativo (PID $PID, desde $ACTIVE_SINCE)"

  # Arquivo rastreado mais recente no disco, comparado ao horário do restart.
  NEWEST_FILE=$(git ls-files -z -- 'mcp-server/src' | xargs -0 -I{} stat -c '%Y %n' "$REPO_DIR/{}" 2>/dev/null | sort -rn | head -1)
  NEWEST_TS=$(echo "$NEWEST_FILE" | awk '{print $1}')
  ACTIVE_TS=$(date -d "$ACTIVE_SINCE" +%s 2>/dev/null || echo 0)
  if [ -n "$NEWEST_TS" ] && [ "$NEWEST_TS" -gt "$ACTIVE_TS" ]; then
    echo "  ATENÇÃO: existe arquivo em src/ modificado APÓS o último restart — processo em memória pode estar desatualizado em relação ao disco. Reinicie para aplicar."
  else
    echo "  disco não mudou (em src/) desde o último restart."
  fi
else
  echo "serviço: NÃO está ativo"
fi

echo
echo "== Identidade exposta pelo processo em memória ===================="
INIT_PAYLOAD='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verificar_sincronismo","version":"1.0"}}}'
CALL_PAYLOAD='{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"status_e_auditoria_pje","arguments":{"acao":"status"}}}'

RESP=$(curl -s -m 8 -X POST "$LOCAL_URL" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -c /tmp/pjepa_sync_cookies.$$ \
  -d "$INIT_PAYLOAD" \
  && curl -s -m 8 -X POST "$LOCAL_URL" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -b /tmp/pjepa_sync_cookies.$$ \
  -d "$CALL_PAYLOAD")
rm -f /tmp/pjepa_sync_cookies.$$

RUNTIME_COMMIT=$(echo "$RESP" | grep -o '"git_commit":[[:space:]]*"[a-f0-9]*"' | tail -1 | grep -o '[a-f0-9]\{7,40\}' || true)

if [ -z "$RUNTIME_COMMIT" ]; then
  echo "não foi possível ler a identidade do processo em memória (resposta bruta abaixo)"
  echo "$RESP" | tail -c 500
else
  echo "commit exposto pelo processo: $RUNTIME_COMMIT"
  if [ "$RUNTIME_COMMIT" = "$COMMIT_DISCO" ]; then
    echo "  OK: igual ao commit no disco."
  else
    echo "  DERIVA: diferente do commit no disco ($COMMIT_DISCO). Reinicie o serviço."
  fi
fi

echo
echo "== Veredito ========================================================"
if [ -n "$DIRTY" ]; then
  echo "DRIFT_DETECTED: árvore suja no disco (não commitado)."
elif [ -n "${AHEAD:-}" ] && [ "$AHEAD" != "0" ]; then
  echo "DRIFT_DETECTED: commits locais não enviados ao remoto."
elif [ -n "$RUNTIME_COMMIT" ] && [ "$RUNTIME_COMMIT" != "$COMMIT_DISCO" ]; then
  echo "DRIFT_DETECTED: processo em memória não reflete o disco (falta restart)."
else
  echo "NO_DRIFT: disco, git remoto e processo em memória alinhados."
fi
