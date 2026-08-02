"""Manifest V2 and Document Reconciliation Engine for PJe-TJPA.

Provides:
- Manifest V2 construction with sha256 piece hashes and metadata fingerprints.
- Document piece integrity verification.
- Final post-acquisition reconciliation between initial metadata manifest and acquired dossier/pieces.
- Auto re-queuing of missing or corrupted pieces.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import auditoria_processual

MANIFEST_SCHEMA_VERSION = "pje.document-manifest/v2"


def build_manifest_entry(
    document: dict[str, Any],
    *,
    canonical_order: int,
    source_origin: str = "dom_document_tree",
) -> dict[str, Any]:
    """Normaliza metadados para entrada de Manifesto V2 com fingerprint e sha256."""
    mapped_keys = {
        "id",
        "documento_id",
        "parent_document_id",
        "documento_pai_id",
        "tipo",
        "tipo_codigo",
        "codigo_tipo",
        "titulo",
        "data",
        "autor",
        "mime_type",
        "content_type",
        "tamanho_bytes",
        "visibilidade",
        "sigilo",
        "ativo",
        "assinado",
        "origem",
        "source_bytes_sha256",
        "sha256",
        "content_sha256",
        "status",
    }
    document_id = str(document.get("id") or document.get("documento_id") or "")
    parent_id = document.get("parent_document_id") or document.get(
        "documento_pai_id"
    )

    sha256_val = (
        document.get("source_bytes_sha256")
        or document.get("sha256")
        or document.get("content_sha256")
        or None
    )

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "document_id": document_id,
        "parent_document_id": str(parent_id) if parent_id not in (None, "") else None,
        "canonical_order": int(canonical_order),
        "type": document.get("tipo") or "",
        "type_code": document.get("tipo_codigo")
        or document.get("codigo_tipo")
        or None,
        "title": document.get("titulo") or "",
        "date": document.get("data") or None,
        "author": document.get("autor") or None,
        "mime_type": document.get("mime_type")
        or document.get("content_type")
        or None,
        "size_bytes": document.get("tamanho_bytes"),
        "visibility": document.get("visibilidade")
        or document.get("sigilo")
        or None,
        "active": document.get("ativo"),
        "signed": document.get("assinado"),
        "source_origin": document.get("origem") or source_origin,
        "source_fingerprint": document.get("source_fingerprint")
        or auditoria_processual.build_document_fingerprint(document),
        "source_bytes_sha256": str(sha256_val) if sha256_val else None,
        "status": document.get("status") or "pending",
        "source_fields": {
            key: value for key, value in document.items() if key not in mapped_keys
        },
    }


def build_manifest_from_tree(
    documents: list[dict[str, Any]],
    source_origin: str = "dom_document_tree",
) -> list[dict[str, Any]]:
    """Gera lista de entradas de Manifesto V2 preservando a ordem canônica."""
    manifest = []
    for idx, doc in enumerate(documents, start=1):
        manifest.append(
            build_manifest_entry(
                doc,
                canonical_order=idx,
                source_origin=source_origin,
            )
        )
    return manifest


def compute_manifest_sha256(manifest: list[dict[str, Any]]) -> str:
    """Calcula digest SHA-256 determinístico de um manifesto V2."""
    raw = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_piece_integrity(
    manifest_entry: dict[str, Any],
    acquired_payload: dict[str, Any],
) -> tuple[bool, str | None]:
    """Verifica a integridade de uma peça individual baixada contra o manifesto."""
    status = acquired_payload.get("status")
    if status in ("failed", "ERROR", "corrupted"):
        return False, acquired_payload.get("safe_error") or "status de aquisição indicou falha"

    acquired_sha = (
        acquired_payload.get("source_bytes_sha256")
        or acquired_payload.get("content_sha256")
        or acquired_payload.get("sha256")
    )
    manifest_sha = manifest_entry.get("source_bytes_sha256")
    if manifest_sha and (not acquired_sha or manifest_sha != acquired_sha):
        return False, f"divergência de sha256: esperado {manifest_sha}, obtido {acquired_sha}"

    size_bytes = (
        acquired_payload.get("tamanho_bytes")
        if acquired_payload.get("tamanho_bytes") is not None
        else acquired_payload.get("size_bytes")
    )
    manifest_size = (
        manifest_entry.get("size_bytes")
        if manifest_entry.get("size_bytes") is not None
        else manifest_entry.get("tamanho_bytes")
    )
    if size_bytes is not None:
        size_int = int(size_bytes)
        if size_int == 0:
            if manifest_size is None or int(manifest_size) > 0:
                return False, "tamanho nulo (0 bytes)"
        elif manifest_size is not None and int(manifest_size) > 0 and size_int != int(manifest_size):
            return False, f"divergência de tamanho: esperado {manifest_size}, obtido {size_int}"

    return True, None


def reconcile_manifest_and_dossier(
    manifest: list[dict[str, Any]],
    acquired_documents: list[dict[str, Any]],
    job_id: str | None = None,
) -> dict[str, Any]:
    """Compara o manifesto de metadados inicial com os documentos adquiridos.

    Identifica peças ausentes, corrompidas ou alteradas e reenfileira
    automaticamente os itens com falha se job_id for fornecido.
    """
    manifest_by_id: dict[str, dict[str, Any]] = {
        str(item.get("document_id") or item.get("documento_id") or item.get("id") or ""): item
        for item in manifest
        if (item.get("document_id") or item.get("documento_id") or item.get("id"))
    }
    acquired_by_id: dict[str, dict[str, Any]] = {
        str(item.get("document_id") or item.get("documento_id") or item.get("id") or ""): item
        for item in acquired_documents
        if (item.get("document_id") or item.get("documento_id") or item.get("id"))
    }

    missing_pieces: list[str] = []
    corrupted_pieces: list[dict[str, Any]] = []
    modified_pieces: list[str] = []

    for doc_id, entry in manifest_by_id.items():
        if doc_id not in acquired_by_id:
            missing_pieces.append(doc_id)
        else:
            acquired = acquired_by_id[doc_id]
            is_valid, reason = verify_piece_integrity(entry, acquired)
            if not is_valid:
                corrupted_pieces.append({
                    "document_id": doc_id,
                    "reason": reason,
                    "type": entry.get("type"),
                })

            manifest_fp = entry.get("source_fingerprint")
            acquired_fp = acquired.get("source_fingerprint") or auditoria_processual.build_document_fingerprint(acquired)
            if manifest_fp and acquired_fp and manifest_fp != acquired_fp:
                modified_pieces.append(doc_id)

    added_pieces = [
        doc_id for doc_id in acquired_by_id if doc_id not in manifest_by_id
    ]

    requeued_pieces: list[str] = []
    needs_requeue = set(missing_pieces) | {item["document_id"] for item in corrupted_pieces}

    if needs_requeue and job_id:
        import analise_processual_completa
        for doc_id in sorted(needs_requeue):
            entry = manifest_by_id.get(doc_id, {})
            fp = entry.get("source_fingerprint") or auditoria_processual.build_document_fingerprint(entry)
            try:
                analise_processual_completa.save_document_state(
                    job_id,
                    document_id=doc_id,
                    source_fingerprint=fp,
                    status="queued",
                    safe_error="requeued_for_reacquisition",
                )
                requeued_pieces.append(doc_id)
            except Exception:
                pass
    elif needs_requeue:
        requeued_pieces = sorted(list(needs_requeue))

    reconciled = len(missing_pieces) == 0 and len(corrupted_pieces) == 0

    manifest_sha = compute_manifest_sha256(manifest)
    acquired_hashes = [
        str(acquired_by_id[doc_id].get("content_sha256") or acquired_by_id[doc_id].get("sha256") or "")
        for doc_id in sorted(acquired_by_id.keys())
    ]
    acquired_sha = hashlib.sha256(json.dumps(acquired_hashes).encode("utf-8")).hexdigest()

    return {
        "reconciled": reconciled,
        "status": "reconciled" if reconciled else "reconciliation_failed",
        "manifest_sha256": manifest_sha,
        "acquired_sha256": acquired_sha,
        "total_manifest": len(manifest),
        "total_acquired": len(acquired_documents),
        "missing_pieces": missing_pieces,
        "corrupted_pieces": [item["document_id"] for item in corrupted_pieces],
        "corrupted_details": corrupted_pieces,
        "requeued_pieces": requeued_pieces,
        "manifest_delta": {
            "added": added_pieces,
            "removed": missing_pieces,
            "modified": modified_pieces,
        },
    }
