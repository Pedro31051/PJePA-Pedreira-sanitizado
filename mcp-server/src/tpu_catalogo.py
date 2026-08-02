"""Resolução local e auditável de siglas de classes PJe para a TPU do CNJ."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CAMINHO_CATALOGO = (
    Path(__file__).resolve().parents[1]
    / "resources"
    / "tpu"
    / "tpu_classes_estadual_1g.json"
)


@lru_cache(maxsize=1)
def carregar_catalogo() -> dict[str, Any]:
    try:
        return json.loads(CAMINHO_CATALOGO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "schema_version": "cnj.tpu-classes/v1",
            "fonte": {},
            "estatisticas": {},
            "classes": {},
        }


def metadados_catalogo() -> dict[str, Any]:
    catalogo = carregar_catalogo()
    return {
        "schema_version": catalogo.get("schema_version"),
        "fonte": catalogo.get("fonte", {}),
        "estatisticas": catalogo.get("estatisticas", {}),
        "disponivel": bool(catalogo.get("classes")),
    }


def resolver_classe(
    sigla_pje: Any,
    descricao_origem: Any = None,
    codigo_origem: Any = None,
) -> dict[str, Any]:
    """Resolve sem inventar código quando uma sigla possui mais de um TPU."""
    sigla = None if sigla_pje in (None, "") else str(sigla_pje).strip()
    descricao_pje = (
        None
        if descricao_origem in (None, "")
        else str(descricao_origem).strip()
    )
    codigo_pje = (
        None if codigo_origem in (None, "") else str(codigo_origem).strip()
    )
    entrada: dict[str, Any] | None = (
        carregar_catalogo().get("classes", {}).get(sigla)
        if sigla
        else None
    )

    candidatos = list((entrada or {}).get("candidatos", []))
    codigos_candidatos = list(dict.fromkeys(
        str(item["codigo_tpu"])
        for item in candidatos
        if item.get("codigo_tpu") not in (None, "")
    ))
    descricoes_candidatas = list(dict.fromkeys(
        str(item["descricao"])
        for item in candidatos
        if item.get("descricao") not in (None, "")
    ))
    descricao_catalogo = (
        (entrada or {}).get("descricao_preferencial")
        or (descricoes_candidatas[0] if descricoes_candidatas else None)
    )
    descricao = descricao_pje or descricao_catalogo
    codigo_catalogo = (
        (entrada or {}).get("codigo_univoco")
        if entrada
        else None
    )
    codigo = codigo_pje or codigo_catalogo
    mapeada = entrada is not None
    codigo_confirmado = codigo_pje is not None or codigo_catalogo is not None
    codigo_ambiguo = (
        codigo_pje is None and len(codigos_candidatos) > 1
    )

    if descricao_pje or codigo_pje:
        resolucao = "metadados_fornecidos_pelo_pje"
        fonte = "pje"
    elif mapeada:
        resolucao = (
            "catalogo_tpu_cnj_com_codigo_univoco"
            if codigo_confirmado
            else "catalogo_tpu_cnj_com_codigo_ambiguo"
        )
        fonte = "catalogo_tpu_cnj"
    else:
        resolucao = "sigla_nao_encontrada_no_catalogo"
        fonte = "nao_resolvida"

    exibicao = (
        f"{descricao} ({sigla})"
        if descricao and sigla
        else descricao or sigla
    )
    return {
        "sigla_pje": sigla,
        "codigo_tpu": codigo,
        "descricao_completa": descricao,
        "exibicao": exibicao,
        "fonte_resolucao": fonte,
        "resolucao": resolucao,
        "codigo_tpu_confirmado": codigo_confirmado,
        "codigo_tpu_ambiguo": codigo_ambiguo,
        "codigos_tpu_candidatos": codigos_candidatos,
        "descricoes_candidatas": descricoes_candidatas,
        "mapeada_no_catalogo": mapeada,
    }
