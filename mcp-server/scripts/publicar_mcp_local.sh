#!/usr/bin/env bash
# Prepara uma release imutável e, somente com --activate, troca o symlink atual.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD)"
RELEASE_ROOT="${PJE_RELEASE_ROOT:-/opt/pjepa-mcp}"
RELEASE_DIR="$RELEASE_ROOT/releases/$COMMIT"
ACTIVATE="${1:-}"

if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
  echo "recusado: a árvore precisa estar limpa" >&2
  exit 1
fi

cd "$REPO_DIR/mcp-server"
python3 -m pytest tests/ -q
python3 -m compileall -q src
python3 -m ruff check .

if [ ! -d "$RELEASE_DIR" ]; then
  TMP_RELEASE="$(mktemp -d "$RELEASE_ROOT/releases/.tmp-$COMMIT-XXXXXX")"
  trap 'test -z "${TMP_RELEASE:-}" || rm -rf -- "$TMP_RELEASE"' EXIT
  git -C "$REPO_DIR" archive "$COMMIT" | tar -x -C "$TMP_RELEASE"
  mv -- "$TMP_RELEASE" "$RELEASE_DIR"
  TMP_RELEASE=""
fi

echo "release preparada: $RELEASE_DIR"
if [ "$ACTIVATE" != "--activate" ]; then
  echo "nenhuma alteração em produção; use --activate após revisão operacional"
  exit 0
fi

PREVIOUS="$(readlink -f "$RELEASE_ROOT/current" 2>/dev/null || true)"
ln -sfn "$RELEASE_DIR" "$RELEASE_ROOT/current.next"
mv -Tf "$RELEASE_ROOT/current.next" "$RELEASE_ROOT/current"

if ! sudo systemctl restart pjepa-mcp.service || ! systemctl is-active --quiet pjepa-mcp.service; then
  if [ -n "$PREVIOUS" ]; then
    ln -sfn "$PREVIOUS" "$RELEASE_ROOT/current.next"
    mv -Tf "$RELEASE_ROOT/current.next" "$RELEASE_ROOT/current"
    sudo systemctl restart pjepa-mcp.service
  fi
  echo "ativação falhou; rollback automático aplicado" >&2
  exit 1
fi

echo "release ativa: $COMMIT"
