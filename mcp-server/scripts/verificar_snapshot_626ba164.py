#!/usr/bin/env python3
"""Regressão do snapshot real de 27/07/2026 sem alterar o banco original."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

SNAPSHOT_ID = "626ba164099a4e898c5a93ce4cdd61e1"
BANCO_DEFAULT = Path(
    "/var/lib/pjepa-mcp/storage/inventario/caixas_tarefas.sqlite3"
)


def main() -> int:
    origem = Path(os.environ.get("PJE_FIXTURE_DB", BANCO_DEFAULT))
    if not origem.is_file():
        print(f"fixture ausente: {origem}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="pjepa-regressao-") as temp:
        destino = Path(temp) / "inventario" / "caixas_tarefas.sqlite3"
        destino.parent.mkdir()
        shutil.copy2(origem, destino)
        os.environ["PJE_STORAGE_DIR"] = temp

        import caixas_tarefas

        estatisticas = caixas_tarefas.estatisticas_acervo(
            snapshot_id=SNAPSHOT_ID,
            dimensao="tarefa",
        )
        global_ = estatisticas["global"]
        assert global_["quantidade"] == 8496
        assert global_["mediana_dias"] == 66
        assert global_["p90_dias"] == 354
        assert global_["maiores_365"] == 739

        minutar = next(
            linha for linha in estatisticas["linhas"]
            if linha["valor"] == "Minutar ato de decisão"
        )
        assert minutar["quantidade"] == 1217
        assert minutar["mediana_dias"] == 333
        assert minutar["max_dias"] == 1186
        assert estatisticas["motor_leitura"] == "cursor_sqlite"

    print("snapshot 626ba164: regressão aprovada")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
