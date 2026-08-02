"""Entrypoint determinístico para materialização diária do acervo PJe."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

import caixas_tarefas
import cliente_singleton


def _gravar_json_atomico(destino: Path, dados: dict) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descritor, temporario = tempfile.mkstemp(
        prefix=f".{destino.name}.",
        dir=destino.parent,
    )
    try:
        with os.fdopen(descritor, "w", encoding="utf-8") as arquivo:
            json.dump(dados, arquivo, ensure_ascii=False, indent=2)
            arquivo.write("\n")
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.chmod(temporario, 0o600)
        os.replace(temporario, destino)
    finally:
        if os.path.exists(temporario):
            os.unlink(temporario)


async def executar(args: argparse.Namespace) -> dict:
    cliente = await cliente_singleton.get_cliente(args.persona, args.grau)
    try:
        sincronizacao = await cliente.sincronizar_caixas_tarefas(
            concorrencia=args.concorrencia,
            max_retentativas=args.max_retentativas,
            lotacao=args.lotacao,
            modo=args.modo,
        )
        if sincronizacao.get("status") != "completo":
            raise RuntimeError(
                "sincronização não produziu snapshot completo: "
                f"{sincronizacao.get('status')}"
            )
        snapshot_id = sincronizacao["snapshot_id"]
        manifesto = await asyncio.to_thread(
            caixas_tarefas.exportar_acervo,
            "ndjson",
            "compacto",
            snapshot_id,
            grau=args.grau,
            persona=args.persona,
        )
        estatisticas = await asyncio.to_thread(
            caixas_tarefas.estatisticas_acervo,
            "tarefa",
            (
                "quantidade,mediana_dias,p90_dias,max_dias,prioritarios,"
                "sigilosos,maiores_180,maiores_365"
            ),
            snapshot_id,
            grau=args.grau,
            persona=args.persona,
        )
        resultado = {
            "status": "ok",
            "snapshot_id": snapshot_id,
            "sincronizacao": {
                "status": sincronizacao["status"],
                "cobertura_percentual": sincronizacao[
                    "cobertura_percentual"
                ],
                "incremental": sincronizacao.get("incremental", {}),
            },
            "export": manifesto,
            "estatisticas": estatisticas,
        }
        _gravar_json_atomico(Path(args.saida), resultado)
        return resultado
    finally:
        await cliente_singleton.fechar_cliente()


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser()
    cli.add_argument("--persona", default="advogado")
    cli.add_argument("--grau", default="1")
    cli.add_argument("--modo", choices=("incremental", "integral"),
                     default="incremental")
    cli.add_argument("--concorrencia", type=int, default=4)
    cli.add_argument("--max-retentativas", type=int, default=3)
    cli.add_argument("--lotacao", default="")
    cli.add_argument(
        "--saida",
        default="/var/lib/pjepa-mcp/exports/manifesto-diario.json",
    )
    return cli


def main() -> int:
    args = parser().parse_args()
    resultado = asyncio.run(executar(args))
    print(json.dumps({
        "status": resultado["status"],
        "snapshot_id": resultado["snapshot_id"],
        "manifesto": args.saida,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
