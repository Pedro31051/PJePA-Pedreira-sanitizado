"""Deterministic input controls for confidential process dossiers."""

from __future__ import annotations

import copy
import json
from typing import Any

MAX_DOCUMENTS = 250
MAX_PAGES = 5_000
MAX_TEXT_CHARS = 2_000_000
FORBIDDEN_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "refresh_token",
}


class DossierRejected(ValueError):
    """Safe public error raised before a dossier reaches a model."""


def validate_and_normalize_dossier(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate text-only input, reject secrets, and return a defensive copy."""
    dossier = copy.deepcopy(payload)
    documents = dossier.get("documents")
    if not isinstance(documents, list) or not documents:
        raise DossierRejected("o dossiê precisa conter documentos textuais")
    if len(documents) > MAX_DOCUMENTS:
        raise DossierRejected("limite de documentos excedido")

    total_pages = 0
    total_chars = 0
    for document in documents:
        if not isinstance(document, dict):
            raise DossierRejected("documento inválido")
        lower_keys = {str(key).casefold() for key in document}
        if lower_keys & FORBIDDEN_KEYS:
            raise DossierRejected("credencial ou segredo detectado no dossiê")
        digest = str(document.get("sha256") or "")
        if len(digest) != 64 or any(
            ch not in "0123456789abcdefABCDEF" for ch in digest
        ):
            raise DossierRejected("sha256 de documento inválido")
        pages = document.get("pages")
        if not isinstance(pages, list) or not pages:
            raise DossierRejected("documento sem páginas textuais")
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("text"), str):
                raise DossierRejected("página não textual detectada")
            if int(page.get("page") or 0) < 1:
                raise DossierRejected("número de página inválido")
            total_chars += len(page["text"])
        total_pages += len(pages)

    if total_pages > MAX_PAGES:
        raise DossierRejected("limite de páginas excedido")
    if total_chars > MAX_TEXT_CHARS:
        raise DossierRejected("limite de texto excedido")
    dossier["input_controls"] = {
        "text_only": True,
        "documents": len(documents),
        "pages": total_pages,
        "characters": total_chars,
    }
    return dossier


def dossier_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        validate_and_normalize_dossier(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    )
