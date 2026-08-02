#!/usr/bin/env python3
"""Gera o catálogo compacto de classes a partir da planilha oficial do CNJ.

A planilha publicada como ``.xls`` é, na prática, uma tabela HTML. Este script
é de manutenção: o servidor MCP usa apenas o JSON gerado e não depende da rede
nem do CNJ durante uma consulta.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lxml import html

URL_PADRAO = (
    "https://www.cnj.jus.br/sgt/versoes_tabelas/planilhas/"
    "79_Tabela_Classes_Justica_Estadual_1_Grau.xls"
)


def _texto(celula) -> str:
    return " ".join(celula.text_content().split())


def _baixar(url: str) -> bytes:
    requisicao = urllib.request.Request(
        url,
        headers={"User-Agent": "pjepa-mcp-catalogo-tpu/1.0"},
    )
    with urllib.request.urlopen(requisicao, timeout=60) as resposta:
        return resposta.read()


def _extrair_fonte(bruto: bytes, url: str) -> dict:
    documento = html.fromstring(bruto)
    entradas = defaultdict(list)
    versao = None

    texto_documento = " ".join(documento.text_content().split())
    achado_versao = re.search(
        r"versão do dia\s+(\d{2}/\d{2}/\d{4})",
        texto_documento,
        flags=re.IGNORECASE,
    )
    if achado_versao:
        versao = achado_versao.group(1)

    for linha in documento.xpath("//tr"):
        celulas = [_texto(celula) for celula in linha.xpath("./td")]
        if len(celulas) < 10:
            continue
        codigo = celulas[5].strip()
        sigla = celulas[9].strip()
        if not codigo.isdigit() or not sigla:
            continue
        descricao = next(
            (valor for valor in reversed(celulas[:5]) if valor.strip()),
            "",
        )
        if not descricao:
            continue
        candidato = {
            "codigo_tpu": codigo,
            "descricao": descricao,
            "publicado_em": celulas[12] or None
            if len(celulas) > 12
            else None,
            "alterado_em": celulas[13] or None
            if len(celulas) > 13
            else None,
            "inativado_em": celulas[14] or None
            if len(celulas) > 14
            else None,
            "reativado_em": celulas[15] or None
            if len(celulas) > 15
            else None,
        }
        if candidato not in entradas[sigla]:
            entradas[sigla].append(candidato)

    classes = {}
    for sigla, candidatos in sorted(entradas.items()):
        candidatos.sort(
            key=lambda item: (
                item["publicado_em"] or "",
                int(item["codigo_tpu"]),
            ),
            reverse=True,
        )
        descricoes = list(dict.fromkeys(
            item["descricao"] for item in candidatos
        ))
        classes[sigla] = {
            "sigla_pje": sigla,
            "descricao_preferencial": descricoes[0],
            "descricoes_encontradas": descricoes,
            "candidatos": candidatos,
            "codigo_univoco": (
                candidatos[0]["codigo_tpu"]
                if len({item["codigo_tpu"] for item in candidatos}) == 1
                else None
            ),
            "ambigua": (
                len({item["codigo_tpu"] for item in candidatos}) > 1
                or len(descricoes) > 1
            ),
        }

    return {
        "schema_version": "cnj.tpu-classes/v1",
        "fonte": {
            "orgao": "Conselho Nacional de Justiça",
            "nome": "Tabela de Classes da Justiça Estadual - 1º Grau",
            "url": url,
            "versao_publicada": versao,
            "sha256_arquivo_origem": hashlib.sha256(bruto).hexdigest(),
            "gerado_em": datetime.now(timezone.utc).isoformat(),
        },
        "estatisticas": {
            "siglas": len(classes),
            "registros_fonte": sum(
                len(item["candidatos"]) for item in classes.values()
            ),
            "siglas_ambiguas": sum(
                bool(item["ambigua"]) for item in classes.values()
            ),
        },
        "classes": classes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=URL_PADRAO)
    parser.add_argument("--arquivo")
    parser.add_argument(
        "--saida",
        default="resources/tpu/tpu_classes_estadual_1g.json",
    )
    args = parser.parse_args()

    bruto = (
        Path(args.arquivo).read_bytes()
        if args.arquivo
        else _baixar(args.url)
    )
    catalogo = _extrair_fonte(bruto, args.url)
    destino = Path(args.saida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        json.dumps(
            catalogo,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"{destino}: {catalogo['estatisticas']['siglas']} siglas, "
        f"{catalogo['estatisticas']['registros_fonte']} registros"
    )


if __name__ == "__main__":
    main()
