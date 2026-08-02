"""Métricas locais e não sensíveis das consultas do acervo de tarefas."""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any

_MAX_AMOSTRAS = 2048
_AMOSTRAS: deque[dict[str, float]] = deque(maxlen=_MAX_AMOSTRAS)
_LOCK = threading.Lock()


def registrar(amostra: dict[str, Any]) -> None:
    """Registra apenas números; rejeita silenciosamente conteúdo não numérico."""
    limpa: dict[str, float] = {}
    for chave, valor in amostra.items():
        if isinstance(valor, bool):
            limpa[str(chave)] = float(int(valor))
        elif isinstance(valor, (int, float)) and math.isfinite(float(valor)):
            limpa[str(chave)] = float(valor)
    if not limpa:
        return
    with _LOCK:
        _AMOSTRAS.append(limpa)


def _percentil(valores: list[float], percentual: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    indice = max(0, math.ceil(percentual * len(ordenados)) - 1)
    return round(ordenados[indice], 3)


def resumo() -> dict[str, Any]:
    """Retorna percentis agregados sem filtros nem identificadores processuais."""
    with _LOCK:
        amostras = list(_AMOSTRAS)
    chaves = sorted({chave for amostra in amostras for chave in amostra})
    metricas: dict[str, Any] = {}
    for chave in chaves:
        valores = [amostra[chave] for amostra in amostras if chave in amostra]
        metricas[chave] = {
            "amostras": len(valores),
            "min": round(min(valores), 3),
            "p50": _percentil(valores, 0.50),
            "p95": _percentil(valores, 0.95),
            "p99": _percentil(valores, 0.99),
            "max": round(max(valores), 3),
        }
    return {
        "schema_version": "pje.acervo-performance/v1",
        "armazenamento": "memoria_do_processo",
        "max_amostras": _MAX_AMOSTRAS,
        "amostras_consultas": len(amostras),
        "dados_processuais_incluidos": False,
        "metricas": metricas,
    }


def limpar_para_testes() -> None:
    with _LOCK:
        _AMOSTRAS.clear()
