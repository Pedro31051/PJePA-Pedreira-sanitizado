#!/usr/bin/env python3
"""Canário manual somente leitura para análise integral no PJe.

Este script nunca possui valores padrão de processo, perfil, autorização ou
credencial. A execução exige opt-in explícito e não participa da suíte offline.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--numero-cnj", required=True)
    parser.add_argument("--perfil", required=True)
    parser.add_argument("--grau", choices=("1", "2", "1g", "2g"), required=True)
    parser.add_argument("--persona", choices=("servidor", "magistrado"), required=True)
    parser.add_argument("--autorizacao-ref", required=True)
    parser.add_argument(
        "--confirmar-acesso-real",
        action="store_true",
        help="confirma que o operador autorizou esta consulta real somente leitura",
    )
    return parser.parse_args()


async def _executar(args: argparse.Namespace) -> int:
    if not args.confirmar_acesso_real:
        raise SystemExit("recusado: informe --confirmar-acesso-real")
    if os.environ.get("PJE_ENABLE_LIVE_TESTS") != "1":
        raise SystemExit("recusado: defina PJE_ENABLE_LIVE_TESTS=1")

    import server

    resultado = await server.analisar_processo_completo_pje(
        acao="iniciar",
        numero_cnj=args.numero_cnj,
        autorizacao_leitura=True,
        autorizacao_ref=args.autorizacao_ref,
        persona=args.persona,
        grau=args.grau,
        perfil=args.perfil,
    )
    job_id = resultado.get("job_id")
    print(f"job iniciado: {job_id or 'não criado'}")
    return 0 if job_id else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_executar(_argumentos())))
