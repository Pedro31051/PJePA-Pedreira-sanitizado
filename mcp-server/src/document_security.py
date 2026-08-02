"""Defesas determinísticas para conteúdo processual não confiável.

O módulo não executa modelos e não altera o PJe. Ele separa trechos com sinais
de injeção indireta antes que o texto seja usado por extratores ou agentes.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

SECURITY_SCHEMA = "pje.document-security/v1"
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_PAGE_MARKER = re.compile(r"^--- Página (\d+) ---\s*$")
_INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(?:todas?\s+)?(?:as\s+)?instru[cç][oõ]es\b", re.IGNORECASE),
    re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions\b", re.IGNORECASE),
    re.compile(r"\b(?:system|developer)\s+prompt\b", re.IGNORECASE),
    re.compile(r"\brevele?\s+(?:o\s+)?prompt\b", re.IGNORECASE),
    re.compile(
        r"\b(?:envie|exfiltre|transmita)\b.{0,120}\b(?:dados|autos|segredo|token)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:use|chame|execute)\b.{0,80}\b(?:ferramenta|tool|source_add)\b", re.IGNORECASE
    ),
)


def _line_reasons(line: str) -> list[str]:
    reasons: list[str] = []
    if _ZERO_WIDTH.search(line):
        reasons.append("caractere_invisivel")
    if any(pattern.search(line) for pattern in _INJECTION_PATTERNS):
        reasons.append("instrucao_adversarial")
    return reasons


def segregate_untrusted_text(
    text: str,
    *,
    document_id: str,
) -> dict[str, Any]:
    """Remove linhas suspeitas do texto canônico e preserva prova sem conteúdo."""
    current_page = 1
    canonical: list[str] = []
    anomalies: list[dict[str, Any]] = []
    quarantined_hashes: list[str] = []

    for line_number, line in enumerate(str(text or "").splitlines(), start=1):
        page_match = _PAGE_MARKER.match(line.strip())
        if page_match:
            current_page = int(page_match.group(1))
            canonical.append(line)
            continue
        reasons = _line_reasons(line)
        if not reasons:
            canonical.append(line)
            continue
        digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
        quarantined_hashes.append(digest)
        anomalies.append(
            {
                "document_id": str(document_id),
                "page": current_page,
                "line": line_number,
                "reasons": reasons,
                "sha256": digest,
                "characters": len(line),
                "requires_human_review": True,
            }
        )

    canonical_text = "\n".join(canonical).strip()
    return {
        "schema_version": SECURITY_SCHEMA,
        "canonical_text": canonical_text,
        "anomalies": anomalies,
        "quarantined_sha256": quarantined_hashes,
        "safe_for_automated_analysis": not anomalies,
    }


def inspect_visual_spans(
    spans: list[dict[str, Any]],
    *,
    document_id: str,
    page: int,
    page_width: float,
    page_height: float,
) -> list[dict[str, Any]]:
    """Classifica metadados visuais produzidos por um extrator como PyMuPDF."""
    anomalies: list[dict[str, Any]] = []
    for span in spans:
        text = str(span.get("text") or "")
        bbox = tuple(span.get("bbox") or (0, 0, 0, 0))
        reasons: list[str] = []
        if float(span.get("size") or 0) < 2.0 and text.strip():
            reasons.append("fonte_menor_2pt")
        color = int(span.get("color") or 0)
        if color >= 0xF5F5F5 and text.strip():
            reasons.append("baixo_contraste_fundo_claro")
        if len(bbox) == 4 and (
            bbox[2] <= 0
            or bbox[3] <= 0
            or bbox[0] >= page_width
            or bbox[1] >= page_height
        ):
            reasons.append("fora_da_area_visivel")
        reasons.extend(
            reason for reason in _line_reasons(text) if reason not in reasons
        )
        if reasons:
            anomalies.append(
                {
                    "document_id": str(document_id),
                    "page": int(page),
                    "reasons": reasons,
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "characters": len(text),
                    "requires_human_review": True,
                }
            )
    return anomalies
