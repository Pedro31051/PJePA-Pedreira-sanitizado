#!/usr/bin/env bash
# Rollback atômico para uma release já preparada. Exige alvo explícito.
set -euo pipefail

TARGET="${1:-}"
RELEASE_ROOT="${PJE_RELEASE_ROOT:-/opt/pjepa-mcp}"
if [ -z "$TARGET" ] || [ ! -d "$RELEASE_ROOT/releases/$TARGET" ]; then
  echo "uso: $0 <commit-de-release-existente>" >&2
  exit 1
fi

CURRENT="$(readlink -f "$RELEASE_ROOT/current" 2>/dev/null || true)"
ln -sfn "$RELEASE_ROOT/releases/$TARGET" "$RELEASE_ROOT/current.next"
mv -Tf "$RELEASE_ROOT/current.next" "$RELEASE_ROOT/current"

if ! sudo systemctl restart pjepa-mcp.service || ! systemctl is-active --quiet pjepa-mcp.service; then
  if [ -n "$CURRENT" ]; then
    ln -sfn "$CURRENT" "$RELEASE_ROOT/current.next"
    mv -Tf "$RELEASE_ROOT/current.next" "$RELEASE_ROOT/current"
    sudo systemctl restart pjepa-mcp.service
  fi
  echo "rollback falhou; release anterior restaurada" >&2
  exit 1
fi

echo "rollback verificado: $TARGET"
