"""Teste ao vivo do ultimas_movimentacoes integrado (parse DOM).

Faz 1 login e consulta os processos passados como argumento (ou os 2
processos de referencia). Nao passa pelo servidor MCP - valida direto o
PJeClient, antes de reiniciar o server no Claude Desktop.

Uso:
    venv/bin/python teste_movs_ao_vivo.py [cnj1 cnj2 ...]
"""
import asyncio
import os
import sys
from pathlib import Path

if os.environ.get("PJE_ENABLE_LIVE_TESTS") != "1":
    raise SystemExit("recusado: defina PJE_ENABLE_LIVE_TESTS=1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import keyring

from pje_client import PJeClient

KEYRING_SERVICE = "mcp-pje-tjpi"
CNJS_DEFAULT = [
    # coloque aqui numeros CNJ REAIS seus no TJMA pra testar
    "0000000-00.0000.8.10.0000",
]


async def main(cnjs):
    cpf = keyring.get_password(KEYRING_SERVICE, "cpf")
    senha = keyring.get_password(KEYRING_SERVICE, "senha")
    seed = keyring.get_password(KEYRING_SERVICE, "totp_seed")

    async with PJeClient(cpf, senha, seed, persona="advogado", headless=True) as pje:
        for cnj in cnjs:
            r = await pje.ultimas_movimentacoes(cnj, limite=8)
            print(f"\n=== {cnj}: {r['total_encontrado']} movimentacoes ===")
            if r.get("aviso"):
                print(f"  AVISO: {r['aviso']}")
            for m in r["movimentacoes"]:
                print(f"  {m.get('data') or '?':>12}  {m.get('hora', '?')}  "
                      f"{m['tipo'][:60]}"
                      + (f"  [doc {m['id']}]" if m.get("id") else ""))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or CNJS_DEFAULT))
