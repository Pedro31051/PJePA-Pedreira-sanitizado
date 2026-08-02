#!/usr/bin/env python3
"""Benchmark offline do acervo com massa exclusivamente sintética."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import caixas_tarefas


def _percentil(valores: list[float], percentual: float) -> float:
    ordenados = sorted(valores)
    indice = max(0, min(len(ordenados) - 1, int(percentual * len(ordenados) + 0.999) - 1))
    return round(ordenados[indice], 3)


def _massa_sintetica(quantidade: int):
    classes = ("CumSen", "DivLit", "ProceComCiv", "Alim", "ExeAli")
    tarefas = ("Triar", "Minutar", "Analisar", "Providências a adotar")
    for indice in range(quantidade):
        yield {
            "idTaskInstance": indice + 1,
            "idProcesso": indice + 1000,
            "numeroProcesso": f"SINTETICO-{indice:09d}",
            "classeJudicial": classes[indice % len(classes)],
            "assuntoPrincipal": f"ASSUNTO SINTETICO {indice % 25}",
            "poloAtivo": f"PARTE SINTETICA A {indice % 100}",
            "poloPassivo": f"PARTE SINTETICA B {indice % 100}",
            "nomeTarefa": tarefas[indice % len(tarefas)],
            "dataChegada": 1704067200000 + (indice % 365) * 86400000,
            "ultimoMovimento": 1704153600000 + (indice % 365) * 86400000,
            "prioridade": indice % 17 == 0,
            "sigiloso": indice % 31 == 0,
            "conferido": indice % 7 == 0,
        }


def executar(quantidade: int, repeticoes: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="pjepa-benchmark-") as pasta:
        anterior = os.environ.get("PJE_STORAGE_DIR")
        os.environ["PJE_STORAGE_DIR"] = pasta
        try:
            caixa = {
                "grupo": "tarefas",
                "nome": "CAIXA SINTETICA",
                "quantidade": quantidade,
            }
            inicio_ingestao = time.perf_counter()
            sid = caixas_tarefas.novo_snapshot("1g", "benchmark", [caixa])
            caixas_tarefas.salvar_caixa(
                sid,
                caixa,
                quantidade,
                _massa_sintetica(quantidade),
            )
            caixas_tarefas.finalizar_snapshot(sid)
            ingestao_ms = (time.perf_counter() - inicio_ingestao) * 1000

            cenarios = {}
            for pagina in (1, 100, 500, min(5000, quantidade)):
                tempos = []
                bytes_json = []
                for _ in range(repeticoes):
                    inicio = time.perf_counter()
                    resposta = caixas_tarefas.consultar_acervo_estruturado(
                        snapshot_id=sid,
                        formato="compacto",
                        campos="tarefa,classe,dias,flags",
                        itens_por_pagina=pagina,
                        incluir_facetas=False,
                        envelope="minimo",
                    )
                    tempos.append((time.perf_counter() - inicio) * 1000)
                    bytes_json.append(
                        len(
                            json.dumps(
                                resposta,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        )
                    )
                cenarios[str(pagina)] = {
                    "amostras": repeticoes,
                    "p50_ms": _percentil(tempos, 0.50),
                    "p95_ms": _percentil(tempos, 0.95),
                    "p99_ms": _percentil(tempos, 0.99),
                    "media_ms": round(statistics.fmean(tempos), 3),
                    "bytes_media": round(statistics.fmean(bytes_json)),
                }
            return {
                "schema_version": "pje.acervo-benchmark/v1",
                "dados_sinteticos": True,
                "tocou_pje": False,
                "ocorrencias": quantidade,
                "ingestao_ms": round(ingestao_ms, 3),
                "cenarios": cenarios,
            }
        finally:
            if anterior is None:
                os.environ.pop("PJE_STORAGE_DIR", None)
            else:
                os.environ["PJE_STORAGE_DIR"] = anterior


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocorrencias", type=int, default=10_000)
    parser.add_argument("--repeticoes", type=int, default=10)
    parser.add_argument("--saida", type=Path)
    args = parser.parse_args()
    if args.ocorrencias < 1 or args.ocorrencias > 1_000_000:
        parser.error("--ocorrencias deve estar entre 1 e 1000000")
    if args.repeticoes < 1 or args.repeticoes > 1000:
        parser.error("--repeticoes deve estar entre 1 e 1000")

    resultado = executar(args.ocorrencias, args.repeticoes)
    texto = json.dumps(resultado, ensure_ascii=False, indent=2)
    if args.saida:
        args.saida.write_text(texto + "\n", encoding="utf-8")
    print(texto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
