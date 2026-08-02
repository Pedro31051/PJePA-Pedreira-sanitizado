"""Extração local e OCR seletivo de PDFs baixados pelo bridge do PJe."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any


class PdfScanError(RuntimeError):
    """Falha saneada ao validar ou analisar um PDF local."""


def _allowed_artifact_root() -> Path:
    configured = os.environ.get("PJE_BROWSER_ARTIFACT_DIR", "").strip()
    storage = os.environ.get("PJE_STORAGE_DIR", "").strip()
    raw = configured or (str(Path(storage) / "browser-bridge") if storage else "")
    if not raw or not Path(raw).is_absolute():
        raise PdfScanError("Storage de artefatos do navegador não configurado.")
    return Path(raw).resolve()


def _validated_pdf_path(value: str | Path) -> Path:
    root = _allowed_artifact_root()
    try:
        candidate = Path(value).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise PdfScanError("PDF baixado não foi encontrado no storage local.") from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PdfScanError("PDF fora do diretório controlado da automação.") from exc
    if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
        raise PdfScanError("Artefato informado não é um arquivo PDF.")
    with candidate.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PdfScanError("Artefato não contém a assinatura de um PDF.")
    return candidate


def inspect_pdf(
    value: str | Path,
    *,
    apply_ocr: bool = True,
    max_ocr_pages: int = 10,
    max_chars: int = 20_000,
) -> dict[str, Any]:
    """Extrai texto nativo e aplica OCR apenas nas páginas que precisam dele."""
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - depende do pacote de produção
        raise PdfScanError("PyMuPDF não está disponível para analisar o PDF.") from exc

    path = _validated_pdf_path(value)
    max_ocr_pages = max(0, min(int(max_ocr_pages), 100))
    max_chars = max(1, min(int(max_chars), 100_000))
    ocr_available = bool(apply_ocr and shutil.which("tesseract"))
    language = os.environ.get("PJE_OCR_LANGUAGE", "por").strip() or "por"
    dpi = max(96, min(int(os.environ.get("PJE_OCR_DPI", "150")), 300))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    pages: list[dict[str, Any]] = []
    text_parts: list[str] = []
    native_characters = 0
    ocr_characters = 0
    ocr_attempted = 0
    ocr_completed = 0
    unreadable_pages: list[int] = []

    try:
        document = fitz.open(path)
    except Exception as exc:
        raise PdfScanError("PDF inválido, corrompido ou protegido contra leitura.") from exc
    try:
        for index, page in enumerate(document, start=1):
            try:
                text = page.get_text("text") or ""
            except Exception:
                text = ""
            native_length = len(text.strip())
            native_characters += native_length
            method = "native_text"
            status = "extracted" if native_length >= 30 else "low_text"
            if native_length < 30 and ocr_available and ocr_attempted < max_ocr_pages:
                ocr_attempted += 1
                try:
                    text_page = page.get_textpage_ocr(
                        language=language, dpi=dpi, full=True
                    )
                    ocr_text = page.get_text(textpage=text_page) or ""
                except Exception:
                    ocr_text = ""
                if len(ocr_text.strip()) > native_length:
                    text = ocr_text
                    method = "ocr"
                    status = "extracted"
                    ocr_completed += 1
                    ocr_characters += len(ocr_text.strip())
                else:
                    status = "ocr_insufficient"
            elif native_length < 30:
                status = "ocr_required" if not ocr_available else "ocr_limit_reached"
            if not text.strip():
                unreadable_pages.append(index)
            pages.append(
                {
                    "page": index,
                    "method": method,
                    "status": status,
                    "characters": len(text.strip()),
                }
            )
            text_parts.append(f"--- Página {index} ---\n{text.strip()}")
    finally:
        document.close()

    full_text = "\n\n".join(text_parts)
    return {
        "status": "analyzed",
        "sha256": digest.hexdigest(),
        "bytes": path.stat().st_size,
        "pages_total": len(pages),
        "native_characters": native_characters,
        "ocr_available": ocr_available,
        "ocr_attempted_pages": ocr_attempted,
        "ocr_completed_pages": ocr_completed,
        "ocr_characters": ocr_characters,
        "unreadable_pages": unreadable_pages,
        "pages": pages,
        "text": full_text[:max_chars],
        "text_truncated": len(full_text) > max_chars,
    }
