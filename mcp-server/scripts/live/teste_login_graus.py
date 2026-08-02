"""Smoke test ao vivo: login + painel de expedientes no 1g e no 2g do TJMA.

Nao consulta processo nenhum (nada de registro CNJ 121) - so a area logada
do proprio usuario. Uso:
    ./venv/bin/python teste_login_graus.py [1g|2g|ambos]
"""
import asyncio
import os
import sys
from pathlib import Path

if os.environ.get("PJE_ENABLE_LIVE_TESTS") != "1":
    raise SystemExit("recusado: defina PJE_ENABLE_LIVE_TESTS=1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cliente_singleton import _get_creds
from pje_client import PJeClient


async def testar(grau: str) -> bool:
    print(f"\n{'=' * 60}\n  TESTE {grau.upper()}\n{'=' * 60}", flush=True)
    cpf, senha, seed = _get_creds()
    cliente = PJeClient(cpf, senha, seed, persona="advogado", grau=grau)
    try:
        await cliente._iniciar()
        await cliente._login()
        await cliente._trocar_perfil()
        print(f"[{grau}] LOGIN OK - url pos-login: {cliente._page.url}", flush=True)

        r = await cliente.expedientes_pendentes()
        print(f"[{grau}] PAINEL OK - {r.get('total', 0)} expediente(s) pendente(s)",
              flush=True)
        for e in (r.get("expedientes") or [])[:3]:
            print(f"   - {str(e)[:140]}", flush=True)
        return True
    except Exception as e:
        print(f"[{grau}] FALHOU: {type(e).__name__}: {e}", flush=True)
        print(f"[{grau}] url atual: {cliente._page.url if cliente._page else '-'}",
              flush=True)
        return False
    finally:
        await cliente._fechar()


async def main():
    alvo = sys.argv[1] if len(sys.argv) > 1 else "ambos"
    graus = ["1g", "2g"] if alvo == "ambos" else [alvo]
    resultados = {}
    for g in graus:
        resultados[g] = await testar(g)
    print(f"\nRESULTADO: {resultados}", flush=True)
    sys.exit(0 if all(resultados.values()) else 1)


if __name__ == "__main__":
    asyncio.run(main())
