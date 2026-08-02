"""Processamento local e determinístico do PDF integral do PJe.

O original nunca é alterado. Páginas sem texto recebem OCR local e uma cópia
"analysis-only" com camada textual invisível. O provedor remoto recebe somente
o manifesto textual produzido depois do portão de completude.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import fitz

from analysis_capsule import Capsule

SCHEMA_VERSION = "pje.consolidated-process/v1"
DOCUMENT_ID_RE = re.compile(
    r"(?im)(?:id\s+(?:do\s+)?documento|documento\s+id|id\.)\s*[:#-]?\s*(\d{5,})"
)
SIGNATURE_CONTEXT_RE = re.compile(
    r"(?i)(?:assinatura|assinado|signat[aá]rio|certificado|emissor).{0,80}$"
)


class ConsolidatedProcessError(ValueError):
    """Erro seguro: nunca contém texto extraído."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_text(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value.replace("\x00", "")).strip()


def probable_document_ids(text: str) -> list[str]:
    """Extrai IDs documentais, ignorando IDs que aparecem em bloco de assinatura."""
    found: list[str] = []
    for match in DOCUMENT_ID_RE.finditer(str(text or "")):
        prefix = text[max(0, match.start() - 100) : match.start()]
        if SIGNATURE_CONTEXT_RE.search(prefix):
            continue
        value = match.group(1)
        if value not in found:
            found.append(value)
    return found


def _validate_pdf(path: Path) -> fitz.Document:
    if not path.is_file() or path.stat().st_size < 100:
        raise ConsolidatedProcessError("PDF integral ausente ou vazio")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ConsolidatedProcessError("arquivo integral não é PDF")
        stream.seek(max(0, path.stat().st_size - 4096))
        if b"%%EOF" not in stream.read():
            raise ConsolidatedProcessError("PDF integral está truncado")
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise ConsolidatedProcessError("PDF integral não pôde ser aberto") from exc
    if document.page_count < 1:
        document.close()
        raise ConsolidatedProcessError("PDF integral não contém páginas")
    return document


def _ocr_page(page: fitz.Page, *, language: str, dpi: int) -> tuple[str, list]:
    try:
        text_page = page.get_textpage_ocr(
            language=language,
            dpi=dpi,
            full=True,
        )
        text = page.get_text("text", textpage=text_page, sort=True)
        words = page.get_text("words", textpage=text_page, sort=True)
        return _normalise_text(text), list(words)
    except Exception as exc:
        raise ConsolidatedProcessError("OCR local falhou em uma ou mais páginas") from exc


def _insert_invisible_words(page: fitz.Page, words: Iterable[tuple]) -> None:
    for word in words:
        if len(word) < 5 or not str(word[4]).strip():
            continue
        rect = fitz.Rect(float(word[0]), float(word[1]), float(word[2]), float(word[3]))
        if rect.is_empty or rect.height <= 0:
            continue
        page.insert_text(
            fitz.Point(rect.x0, max(rect.y0 + 3.0, rect.y1 - 1.0)),
            str(word[4]),
            fontsize=max(3.0, min(18.0, rect.height * 0.75)),
            fontname="helv",
            render_mode=3,
            overlay=True,
        )


@dataclass(frozen=True)
class PageExtraction:
    page: int
    text: str
    method: str
    sha256: str
    probable_document_ids: list[str]


def _normalise_mapping(
    mapping: list[dict[str, Any]], page_count: int
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    if not mapping:
        raise ConsolidatedProcessError("manifesto de IDs documentais não informado")
    page_to_document: dict[int, str] = {}
    normalised = []
    seen_ids = set()
    for raw in mapping:
        document_id = str(raw.get("document_id") or "").strip()
        if not document_id or document_id in seen_ids:
            raise ConsolidatedProcessError("ID documental ausente ou duplicado")
        seen_ids.add(document_id)
        try:
            start = int(raw.get("start_page"))
            end = int(raw.get("end_page", start))
        except (TypeError, ValueError) as exc:
            raise ConsolidatedProcessError("intervalo documental inválido") from exc
        if start < 1 or end < start or end > page_count:
            raise ConsolidatedProcessError("intervalo documental fora do PDF")
        for page in range(start, end + 1):
            if page in page_to_document:
                raise ConsolidatedProcessError("página atribuída a mais de um documento")
            page_to_document[page] = document_id
        normalised.append(
            {
                "document_id": document_id,
                "title": str(raw.get("title") or ""),
                "type": str(raw.get("type") or ""),
                "date": str(raw.get("date") or ""),
                "start_page": start,
                "end_page": end,
            }
        )
    missing = sorted(set(range(1, page_count + 1)) - set(page_to_document))
    if missing:
        raise ConsolidatedProcessError("manifesto não cobre todas as páginas")
    normalised.sort(key=lambda item: item["start_page"])
    return normalised, page_to_document


def infer_document_mapping(
    pages: list[PageExtraction], known_documents: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Usa IDs visíveis nas páginas e recusa associação incerta.

    Páginas de capa/sumário que citam vários documentos não viram âncoras. O
    primeiro marcador não ambíguo de cada peça define sua fronteira.
    """
    known = {
        str(item.get("id") or item.get("document_id") or "").strip(): item
        for item in known_documents
        if str(item.get("id") or item.get("document_id") or "").strip()
    }
    if not known:
        raise ConsolidatedProcessError("árvore documental vazia")
    anchors: dict[str, int] = {}
    for page in pages:
        candidates = [value for value in page.probable_document_ids if value in known]
        if len(candidates) == 1:
            anchors.setdefault(candidates[0], page.page)
    missing = sorted(set(known) - set(anchors))
    if missing:
        raise ConsolidatedProcessError(
            "não foi possível associar todos os IDs da árvore às páginas"
        )
    ordered = sorted(anchors.items(), key=lambda item: item[1])
    if len({page for _, page in ordered}) != len(ordered):
        raise ConsolidatedProcessError("mais de um documento possui a mesma página âncora")
    mapping: list[dict[str, Any]] = []
    first_page = ordered[0][1]
    if first_page > 1:
        mapping.append(
            {
                "document_id": "__pje_front_matter__",
                "title": "Capa e índice gerados pelo PJe",
                "type": "metadado_sistema",
                "start_page": 1,
                "end_page": first_page - 1,
            }
        )
    for index, (document_id, start_page) in enumerate(ordered):
        next_start = ordered[index + 1][1] if index + 1 < len(ordered) else len(pages) + 1
        source = known[document_id]
        mapping.append(
            {
                "document_id": document_id,
                "title": str(source.get("titulo") or source.get("title") or ""),
                "type": str(source.get("tipo") or source.get("type") or ""),
                "date": str(source.get("data") or source.get("date") or ""),
                "start_page": start_page,
                "end_page": next_start - 1,
            }
        )
    return mapping


def process_consolidated_pdf(
    source_pdf: str | os.PathLike[str],
    capsule: Capsule,
    *,
    document_mapping: list[dict[str, Any]] | None = None,
    known_documents: list[dict[str, Any]] | None = None,
    language: str | None = None,
    dpi: int | None = None,
    minimum_native_characters: int = 20,
) -> dict[str, Any]:
    """Copia, extrai, faz OCR e só retorna ``complete=True`` com cobertura total."""
    source = Path(source_pdf).resolve()
    original = capsule.file("source/process-original.pdf")
    if source != original:
        shutil.copyfile(source, original)
        os.chmod(original, 0o600)
    original_document = _validate_pdf(original)
    derived = capsule.file("derived/process-searchable-analysis-only.pdf")
    searchable = fitz.open()
    searchable.insert_pdf(original_document)
    selected_language = str(language or os.environ.get("PJE_OCR_LANGUAGE", "por"))
    selected_dpi = max(96, min(300, int(dpi or os.environ.get("PJE_OCR_DPI", "150"))))
    extractions: list[PageExtraction] = []
    try:
        for index in range(original_document.page_count):
            page_number = index + 1
            source_page = original_document[index]
            native = _normalise_text(source_page.get_text("text", sort=True))
            if len(native) >= minimum_native_characters:
                text, method, words = native, "native_text", []
            else:
                text, words = _ocr_page(
                    source_page,
                    language=selected_language,
                    dpi=selected_dpi,
                )
                method = "ocr"
            if not text:
                raise ConsolidatedProcessError("página sem texto após OCR local")
            if words:
                _insert_invisible_words(searchable[index], words)
            text_path = capsule.file(f"text/page-{page_number:06d}.txt")
            text_path.write_text(text, encoding="utf-8")
            os.chmod(text_path, 0o600)
            extractions.append(
                PageExtraction(
                    page=page_number,
                    text=text,
                    method=method,
                    sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    probable_document_ids=probable_document_ids(text[:2500]),
                )
            )
        proposed_mapping = document_mapping or infer_document_mapping(
            extractions, list(known_documents or [])
        )
        mapping, page_to_document = _normalise_mapping(
            proposed_mapping, original_document.page_count
        )
        searchable.set_metadata(
            {
                **searchable.metadata,
                "subject": "Copia pesquisavel para analise local; assinaturas do original nao se transferem",
            }
        )
        searchable.save(derived, garbage=3, deflate=True)
    finally:
        searchable.close()
        original_document.close()

    # Segunda leitura prova que a camada textual realmente foi persistida.
    verification = _validate_pdf(derived)
    try:
        searchable_pages = [
            bool(_normalise_text(verification[index].get_text("text", sort=True)))
            for index in range(verification.page_count)
        ]
    finally:
        verification.close()
    if not all(searchable_pages):
        raise ConsolidatedProcessError("PDF derivado não ficou integralmente pesquisável")

    documents = []
    for document in mapping:
        pages = [
            {
                "page": item.page,
                "text": item.text,
                "text_sha256": item.sha256,
                "extraction_method": item.method,
                "probable_document_ids": item.probable_document_ids,
            }
            for item in extractions
            if page_to_document[item.page] == document["document_id"]
        ]
        document_text_hash = hashlib.sha256(
            "\n\f\n".join(page["text"] for page in pages).encode("utf-8")
        ).hexdigest()
        documents.append({**document, "pages": pages, "content_sha256": document_text_hash})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "capsule_id": capsule.capsule_id,
        "complete": True,
        "fail_closed": True,
        "page_count": len(extractions),
        "pages_with_native_text": sum(item.method == "native_text" for item in extractions),
        "pages_with_local_ocr": sum(item.method == "ocr" for item in extractions),
        "original_sha256": _sha256(original),
        "searchable_derivative_sha256": _sha256(derived),
        "searchable_derivative_is_analysis_only": True,
        "documents": documents,
        "coverage": {
            "pages_expected": len(extractions),
            "pages_extracted": len(extractions),
            "pages_mapped": len(page_to_document),
            "empty_pages": [],
            "truncated": False,
        },
    }
    manifest_path = capsule.file("manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    return manifest


def build_text_shards(
    manifest: dict[str, Any], *, maximum_characters: int = 120_000
) -> list[dict[str, Any]]:
    """Divide localmente sem perder nem duplicar páginas."""
    if manifest.get("complete") is not True:
        raise ConsolidatedProcessError("manifesto incompleto não pode ser dividido")
    maximum_characters = max(10_000, int(maximum_characters))
    shards: list[dict[str, Any]] = []
    current_pages: list[dict[str, Any]] = []
    used = 0
    for document in manifest.get("documents") or []:
        for page in document.get("pages") or []:
            text = str(page.get("text") or "")
            if len(text) > maximum_characters:
                raise ConsolidatedProcessError("uma página excede o limite sem divisão segura")
            if current_pages and used + len(text) > maximum_characters:
                shards.append({"pages": current_pages, "characters": used})
                current_pages, used = [], 0
            current_pages.append(
                {
                    "document_id": document["document_id"],
                    "page": page["page"],
                    "text": text,
                    "text_sha256": page["text_sha256"],
                }
            )
            used += len(text)
    if current_pages:
        shards.append({"pages": current_pages, "characters": used})
    expected = int(manifest.get("page_count") or 0)
    sent = [(p["document_id"], p["page"]) for s in shards for p in s["pages"]]
    if len(sent) != expected or len(set(sent)) != expected:
        raise ConsolidatedProcessError("divisão perdeu ou duplicou páginas")
    for index, shard in enumerate(shards, start=1):
        shard["shard_id"] = f"shard-{index:04d}"
    return shards
