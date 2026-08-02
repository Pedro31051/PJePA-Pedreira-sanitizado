"""Política central de retenção de dados processuais.

Com ``PJE_ZERO_RETENTION=1`` os caminhos legados que persistem PDFs, dossiês
ou resultados por dias deixam de gravar: cada escrita falha em modo fechado
apontando o fluxo efêmero (``preparar_pdf_integral``), no qual os dados vivem
apenas numa cápsula local descartada após a entrega. Leituras de dados já
persistidos continuam permitidas até a expiração natural.
"""

from __future__ import annotations

import os

_TRUE_VALUES = {"1", "true", "yes", "on"}


class ZeroRetentionError(ValueError):
    """Escrita legada bloqueada pela política de retenção zero."""


def zero_retention_enabled() -> bool:
    return (
        str(os.environ.get("PJE_ZERO_RETENTION", "")).strip().casefold()
        in _TRUE_VALUES
    )


def require_legacy_persistence(feature: str) -> None:
    """Bloqueia escrita legada quando a retenção zero está ativa."""
    if zero_retention_enabled():
        raise ZeroRetentionError(
            f"retenção zero ativa: {feature} não pode persistir dados "
            "processuais; use o fluxo efêmero preparar_pdf_integral"
        )
