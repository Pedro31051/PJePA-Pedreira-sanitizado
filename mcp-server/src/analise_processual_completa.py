"""Jobs persistentes para análise processual integral e somente leitura.

Este módulo não navega no PJe. Ele persiste progresso, manifesta cobertura e
produz uma síntese determinística citável a partir dos dados entregues pelo
adaptador Playwright. Conteúdo processual nunca é salvo em texto aberto.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import auditoria_processual
import document_security
import manifest
import retention_policy
from job_engine import DurableJobEngine

SCHEMA_VERSION = "pje.complete-process-analysis/v1"
MANIFEST_SCHEMA_VERSION = manifest.MANIFEST_SCHEMA_VERSION
EXTRACTOR_VERSION = "deterministic-complete-analysis/v1"
SUPPORTED_MODES = {"inventario", "rapida", "integral"}
RAPID_MAX_DOCUMENTS = 12
RAPID_MAX_PAGES_PER_DOCUMENT = 20
RAPID_CONCURRENCY = 4
TERMINAL_STATUSES = {
    "completed",
    "partial_with_gaps",
    "cancelled",
    "failed",
}
RESUMABLE_STATUSES = {
    "queued",
    "running",
    "cancel_requested",
    "cancelled",
    "failed",
    "partial_with_gaps",
}
PHASES = {
    "queued",
    "manifest",
    "acquisition",
    "extraction",
    "facts",
    "timeline",
    "controversies",
    "critical_review",
    "synthesis",
    "completed",
}


class CompleteAnalysisError(ValueError):
    """Erro público e seguro da análise completa."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _connect() -> sqlite3.Connection:
    path = auditoria_processual.database_path()
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    _create_schema(connection)
    return connection


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS complete_analysis_jobs (
            job_id TEXT PRIMARY KEY,
            process_ref TEXT NOT NULL,
            grau TEXT NOT NULL,
            persona TEXT NOT NULL,
            mode TEXT NOT NULL,
            semantic_enabled INTEGER NOT NULL DEFAULT 0,
            force_reread INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            total_documents INTEGER NOT NULL DEFAULT 0,
            processed_documents INTEGER NOT NULL DEFAULT 0,
            failed_documents INTEGER NOT NULL DEFAULT 0,
            reused_documents INTEGER NOT NULL DEFAULT 0,
            pages_discovered INTEGER NOT NULL DEFAULT 0,
            pages_without_text INTEGER NOT NULL DEFAULT 0,
            current_document_id TEXT,
            manifest_sha256 TEXT,
            authorisation_ref TEXT NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            encrypted_identity BLOB NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_complete_analysis_process
            ON complete_analysis_jobs(process_ref, grau, updated_at);
        CREATE INDEX IF NOT EXISTS idx_complete_analysis_status
            ON complete_analysis_jobs(status, updated_at);

        CREATE TABLE IF NOT EXISTS complete_analysis_documents (
            job_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            content_sha256 TEXT,
            duplicate_of TEXT,
            pages INTEGER NOT NULL DEFAULT 0,
            pages_without_text INTEGER NOT NULL DEFAULT 0,
            truncated INTEGER NOT NULL DEFAULT 0,
            reused INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            safe_error TEXT,
            PRIMARY KEY(job_id, document_id),
            FOREIGN KEY(job_id) REFERENCES complete_analysis_jobs(job_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS complete_analysis_results (
            job_id TEXT PRIMARY KEY,
            dossier_sha256 TEXT NOT NULL,
            encrypted_payload BLOB NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES complete_analysis_jobs(job_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS analysis_batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            total_processes INTEGER NOT NULL DEFAULT 0,
            authorisation_ref TEXT,
            force_reread INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'rapida',
            max_concurrency INTEGER NOT NULL DEFAULT 2,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS analysis_batch_items (
            batch_id TEXT NOT NULL,
            process_cnj TEXT NOT NULL,
            grau TEXT NOT NULL,
            persona TEXT NOT NULL,
            job_id TEXT,
            status TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(batch_id, process_cnj, grau),
            FOREIGN KEY(batch_id) REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
            FOREIGN KEY(job_id) REFERENCES complete_analysis_jobs(job_id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_batch_items_job ON analysis_batch_items(job_id);
        """
    )
    # Migração aditiva para bancos criados antes do lote paralelo.
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(analysis_batches)")
    }
    if "mode" not in columns:
        connection.execute(
            "ALTER TABLE analysis_batches ADD COLUMN mode TEXT NOT NULL DEFAULT 'rapida'"
        )
    if "max_concurrency" not in columns:
        connection.execute(
            "ALTER TABLE analysis_batches ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 2"
        )


def _crypto() -> auditoria_processual.CryptoBox:
    return auditoria_processual.CryptoBox()


def process_reference(process_number: str) -> str:
    return _crypto().reference(process_number)


def create_job(
    *,
    process_number: str,
    grau: str,
    persona: str,
    mode: str,
    semantic_enabled: bool,
    force_reread: bool,
    authorisation_ref: str,
) -> dict[str, Any]:
    mode = str(mode or "").strip().casefold()
    if mode not in SUPPORTED_MODES:
        raise CompleteAnalysisError(
            "modo deve ser inventario, rapida ou integral"
        )
    retention_policy.require_legacy_persistence(
        "o job persistente de análise processual"
    )
    job_id = uuid.uuid4().hex
    now = _now_iso()
    crypto = _crypto()
    identity = {
        "process_number": process_number,
        "schema_version": SCHEMA_VERSION,
    }
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO complete_analysis_jobs (
                job_id, process_ref, grau, persona, mode, semantic_enabled,
                force_reread, status, phase, created_at, updated_at,
                authorisation_ref, encrypted_identity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?, ?)
            """,
            (
                job_id,
                crypto.reference(process_number),
                grau,
                persona,
                mode,
                int(semantic_enabled),
                int(force_reread),
                now,
                now,
                hashlib.sha256(authorisation_ref.encode("utf-8")).hexdigest(),
                crypto.encrypt(identity, aad=f"complete-analysis:{job_id}"),
            ),
        )
    try:
        engine = DurableJobEngine.get_instance()
        engine.submit_job(
            "complete_analysis",
            process_cnj=process_number,
            grau=grau,
            payload={"persona": persona, "mode": mode, "job_id": job_id},
            allow_coalesce=False,
        )
    except Exception:
        pass
    return get_job(job_id)


def find_latest_job(
    process_number: str,
    grau: str,
    *,
    resumable_only: bool = False,
    mode: str = "",
) -> dict[str, Any] | None:
    process_ref = process_reference(process_number)
    params: list[Any] = [process_ref, grau]
    where = "process_ref=? AND grau=?"
    if resumable_only:
        placeholders = ",".join("?" for _ in RESUMABLE_STATUSES)
        where += f" AND status IN ({placeholders})"
        params.extend(sorted(RESUMABLE_STATUSES))
    if mode:
        normalised_mode = str(mode).strip().casefold()
        if normalised_mode not in SUPPORTED_MODES:
            raise CompleteAnalysisError("modo de busca desconhecido")
        where += " AND mode=?"
        params.append(normalised_mode)
    with _database() as connection:
        row = connection.execute(
            f"""
            SELECT * FROM complete_analysis_jobs
             WHERE {where}
             ORDER BY updated_at DESC LIMIT 1
            """,
            params,
        ).fetchone()
    return _public_job(dict(row)) if row else None


def find_latest_available_result(
    process_number: str,
    grau: str,
    *,
    exclude_job_id: str = "",
) -> dict[str, Any] | None:
    """Recupera o snapshot criptografado mais recente ainda válido."""
    process_ref = process_reference(process_number)
    params: list[Any] = [process_ref, grau, _now_iso()]
    exclusion = ""
    if exclude_job_id:
        exclusion = " AND j.job_id<>?"
        params.append(exclude_job_id)
    with _database() as connection:
        row = connection.execute(
            f"""
            SELECT j.*, r.dossier_sha256, r.encrypted_payload,
                   r.created_at AS result_created_at,
                   r.expires_at AS result_expires_at
              FROM complete_analysis_jobs j
              JOIN complete_analysis_results r ON r.job_id=j.job_id
             WHERE j.process_ref=? AND j.grau=? AND r.expires_at>?
                   {exclusion}
             ORDER BY r.created_at DESC LIMIT 1
            """,
            params,
        ).fetchone()
    if not row:
        return None
    payload = dict(row)
    dossier = _crypto().decrypt(
        payload["encrypted_payload"],
        aad=f"complete-analysis-result:{payload['job_id']}",
    )
    public_payload = dict(payload)
    for protected_field in (
        "encrypted_payload",
        "dossier_sha256",
        "result_created_at",
        "result_expires_at",
    ):
        public_payload.pop(protected_field, None)
    return {
        "job": _public_job(public_payload),
        "dossier_sha256": payload["dossier_sha256"],
        "created_at": payload["result_created_at"],
        "expires_at": payload["result_expires_at"],
        "dossier": dossier,
    }


def compare_manifests(
    previous: Iterable[Mapping[str, Any]],
    current: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compara snapshots por ID e fingerprint sem reler conteúdo."""
    def index(items: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
        return {
            str(item.get("document_id") or item.get("id") or ""): item
            for item in items
            if item.get("document_id") or item.get("id")
        }

    before = index(previous)
    after = index(current)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(
        document_id
        for document_id in set(before) & set(after)
        if str(before[document_id].get("source_fingerprint") or "")
        != str(after[document_id].get("source_fingerprint") or "")
    )
    unchanged = sorted((set(before) & set(after)) - set(modified))
    return {
        "baseline_available": bool(before),
        "added_document_ids": added,
        "removed_document_ids": removed,
        "modified_document_ids": modified,
        "unchanged_document_ids": unchanged,
        "changed": bool(added or removed or modified),
        "counts": {
            "previous": len(before),
            "current": len(after),
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": len(unchanged),
        },
    }


def prioritise_documents(
    documents: Iterable[Mapping[str, Any]],
    limit: int = RAPID_MAX_DOCUMENTS,
) -> list[dict[str, Any]]:
    """Ordena peças por relevância jurídica e recência para análise rápida."""
    items = [dict(item) for item in documents]
    total = max(1, len(items))
    rules = (
        (110, "decisao_critica", r"senten|acord|decis|liminar|tutela"),
        (105, "peticao_inicial", r"peti[cç][aã]o inicial|\binicial\b"),
        (100, "defesa", r"contesta|defesa|reconven"),
        (96, "ministerio_publico", r"minist[eé]rio p[uú]blico|parecer|promotor"),
        (92, "prova_tecnica", r"laudo|per[ií]cia|estudo psicossocial|relat[oó]rio t[eé]cnico"),
        (86, "comunicacao", r"cita[cç]|intima[cç]|mandado|audi[eê]ncia"),
        (80, "manifestacao", r"manifesta[cç]|peti[cç][aã]o"),
        (72, "certidao", r"certid[aã]o"),
    )
    low_value = re.compile(
        r"procura[cç][aã]o|substabelecimento|comprovante|documento pessoal|"
        r"guia de recolhimento",
        re.IGNORECASE,
    )
    ranked = []
    for index, item in enumerate(items):
        haystack = _normalise(
            f"{item.get('type') or item.get('tipo') or ''} "
            f"{item.get('title') or item.get('titulo') or ''}"
        )
        base_score = 55
        reason = "peca_recente"
        for score, candidate_reason, pattern in rules:
            if re.search(pattern, haystack, re.IGNORECASE):
                base_score = score
                reason = candidate_reason
                break
        if low_value.search(haystack):
            base_score = min(base_score, 20)
            reason = "anexo_administrativo"
        recency = round(20 * (index + 1) / total, 4)
        ranked.append({
            **item,
            "analysis_priority": {
                "score": round(base_score + recency, 4),
                "reason": reason,
                "original_order": index + 1,
            },
        })
    ranked.sort(
        key=lambda item: (
            float(item["analysis_priority"]["score"]),
            int(item["analysis_priority"]["original_order"]),
        ),
        reverse=True,
    )
    return ranked[: max(0, min(int(limit or 0), 50))]


def build_inventory_dossier(
    *,
    process_number: str,
    base: Mapping[str, Any],
    manifest_value: list[dict[str, Any]],
    tree_complete: bool,
    incremental: Mapping[str, Any] | None = None,
    collected_at: str = "",
) -> dict[str, Any]:
    """Snapshot leve: metadados e árvore, sem teor das peças."""
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_level": "inventory",
        "process_number": process_number,
        "collected_at": collected_at or _now_iso(),
        "read_only": True,
        "case": {
            "parties": list(base.get("parties") or []),
            "case_class": base.get("case_class"),
            "subject": base.get("subject"),
            "court": base.get("court"),
            "distribution_date": base.get("distribution_date"),
        },
        "timeline": list(base.get("movements") or []),
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest": manifest_value,
        "incremental": dict(incremental or {}),
        "coverage": {
            "complete": bool(tree_complete),
            "status": "inventory_complete" if tree_complete else "inventory_partial",
            "tree_complete": bool(tree_complete),
            "documents_discovered": len(manifest_value),
            "documents_processed": 0,
            "content_read": False,
        },
        "next_recommended_action": "iniciar_rapida",
    }


def get_job(job_id: str) -> dict[str, Any]:
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM complete_analysis_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if not row:
        raise CompleteAnalysisError("job de análise completa não encontrado")
    return _public_job(dict(row))


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    total = int(job.get("total_documents") or 0)
    processed = int(job.get("processed_documents") or 0)
    inventory_done = (
        job.get("mode") == "inventario" and job.get("status") == "completed"
    )
    job["progress_percent"] = (
        100.0
        if inventory_done
        else (round(processed * 100 / total, 2) if total else 0.0)
    )
    job["semantic_enabled"] = bool(job.get("semantic_enabled"))
    job["force_reread"] = bool(job.get("force_reread"))
    job["cancel_requested"] = bool(job.get("cancel_requested"))
    job["read_only"] = True
    job["schema_version"] = SCHEMA_VERSION
    job.pop("encrypted_identity", None)
    job.pop("process_ref", None)
    job.pop("authorisation_ref", None)
    elapsed = max(
        0.0,
        (_now() - datetime.fromisoformat(str(job["created_at"]))).total_seconds(),
    )
    job["elapsed_seconds"] = round(elapsed, 1)
    if processed and total > processed:
        job["estimated_remaining_seconds"] = round(
            elapsed / processed * (total - processed),
            1,
        )
    else:
        job["estimated_remaining_seconds"] = 0.0
    return job


def update_job(job_id: str, **changes: Any) -> dict[str, Any]:
    allowed = {
        "status",
        "phase",
        "total_documents",
        "processed_documents",
        "failed_documents",
        "reused_documents",
        "pages_discovered",
        "pages_without_text",
        "current_document_id",
        "manifest_sha256",
        "cancel_requested",
        "error",
    }
    payload = {key: value for key, value in changes.items() if key in allowed}
    if not payload:
        return get_job(job_id)
    if "phase" in payload and payload["phase"] not in PHASES:
        raise CompleteAnalysisError("fase desconhecida")
    payload["updated_at"] = _now_iso()
    assignments = ", ".join(f"{field}=?" for field in payload)
    with _database() as connection:
        cursor = connection.execute(
            f"UPDATE complete_analysis_jobs SET {assignments} WHERE job_id=?",
            [*payload.values(), job_id],
        )
        if cursor.rowcount != 1:
            raise CompleteAnalysisError("job de análise completa não encontrado")
    try:
        engine = DurableJobEngine.get_instance()
        engine.save_checkpoint(job_id, payload)
        st = payload.get("status")
        if st in ("completed", "partial_with_gaps"):
            engine.complete_job(job_id, result={"status": st, "phase": payload.get("phase")})
        elif st in ("failed", "cancelled"):
            engine.fail_job(job_id, error=payload.get("error") or str(st))
    except Exception:
        pass
    return get_job(job_id)


def request_cancel(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job["status"] in TERMINAL_STATUSES:
        return job
    return update_job(
        job_id,
        cancel_requested=1,
        status="cancel_requested",
    )


def cancellation_requested(job_id: str) -> bool:
    with _database() as connection:
        row = connection.execute(
            "SELECT cancel_requested FROM complete_analysis_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    return bool(row and row["cancel_requested"])


def identity_for_job(job_id: str) -> dict[str, Any]:
    with _database() as connection:
        row = connection.execute(
            """
            SELECT encrypted_identity FROM complete_analysis_jobs
             WHERE job_id=?
            """,
            (job_id,),
        ).fetchone()
    if not row:
        raise CompleteAnalysisError("job de análise completa não encontrado")
    return _crypto().decrypt(
        row["encrypted_identity"],
        aad=f"complete-analysis:{job_id}",
    )


def list_resumable_jobs() -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in RESUMABLE_STATUSES)
    with _database() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM complete_analysis_jobs
             WHERE status IN ({placeholders})
             ORDER BY updated_at
            """,
            sorted(RESUMABLE_STATUSES),
        ).fetchall()
    return [_public_job(dict(row)) for row in rows]


def save_document_state(
    job_id: str,
    *,
    document_id: str,
    source_fingerprint: str,
    status: str,
    content_sha256: str = "",
    duplicate_of: str = "",
    pages: int = 0,
    pages_without_text: int = 0,
    truncated: bool = False,
    reused: bool = False,
    safe_error: str = "",
) -> None:
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO complete_analysis_documents (
                job_id, document_id, source_fingerprint, status,
                content_sha256, duplicate_of, pages, pages_without_text,
                truncated, reused, updated_at, safe_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, document_id) DO UPDATE SET
                source_fingerprint=excluded.source_fingerprint,
                status=excluded.status,
                content_sha256=excluded.content_sha256,
                duplicate_of=excluded.duplicate_of,
                pages=excluded.pages,
                pages_without_text=excluded.pages_without_text,
                truncated=excluded.truncated,
                reused=excluded.reused,
                updated_at=excluded.updated_at,
                safe_error=excluded.safe_error
            """,
            (
                job_id,
                document_id,
                source_fingerprint,
                status,
                content_sha256 or None,
                duplicate_of or None,
                max(0, int(pages)),
                max(0, int(pages_without_text)),
                int(truncated),
                int(reused),
                _now_iso(),
                safe_error or None,
            ),
        )


def completed_document_ids(job_id: str) -> set[str]:
    with _database() as connection:
        rows = connection.execute(
            """
            SELECT document_id FROM complete_analysis_documents
             WHERE job_id=? AND status IN ('completed', 'reused', 'duplicate')
            """,
            (job_id,),
        ).fetchall()
    return {str(row["document_id"]) for row in rows}


def reset_document_states(job_id: str) -> None:
    with _database() as connection:
        connection.execute(
            "DELETE FROM complete_analysis_documents WHERE job_id=?",
            (job_id,),
        )


def document_states(job_id: str) -> list[dict[str, Any]]:
    with _database() as connection:
        rows = connection.execute(
            """
            SELECT document_id, source_fingerprint, status, content_sha256,
                   duplicate_of, pages, pages_without_text, truncated,
                   reused, updated_at, safe_error
              FROM complete_analysis_documents
             WHERE job_id=? ORDER BY updated_at, document_id
            """,
            (job_id,),
        ).fetchall()
    return [
        {
            **dict(row),
            "truncated": bool(row["truncated"]),
            "reused": bool(row["reused"]),
        }
        for row in rows
    ]


def save_result(
    job_id: str,
    dossier: dict[str, Any],
    *,
    ttl_days: int = 7,
) -> dict[str, Any]:
    retention_policy.require_legacy_persistence(
        "o resultado persistente da análise completa"
    )
    raw = json.dumps(
        dossier,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    now = _now()
    encrypted = _crypto().encrypt(
        dossier,
        aad=f"complete-analysis-result:{job_id}",
    )
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO complete_analysis_results (
                job_id, dossier_sha256, encrypted_payload,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                dossier_sha256=excluded.dossier_sha256,
                encrypted_payload=excluded.encrypted_payload,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (
                job_id,
                digest,
                encrypted,
                now.isoformat(),
                (now + timedelta(days=max(1, ttl_days))).isoformat(),
            ),
        )
    return {
        "dossier_sha256": digest,
        "expires_at": (now + timedelta(days=max(1, ttl_days))).isoformat(),
    }


def get_result(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    with _database() as connection:
        row = connection.execute(
            """
            SELECT dossier_sha256, encrypted_payload, created_at, expires_at
              FROM complete_analysis_results WHERE job_id=?
            """,
            (job_id,),
        ).fetchone()
    if not row:
        states = document_states(job_id)
        return {
            "job": job,
            "available": False,
            "partial": bool(states),
            "partial_coverage": {
                "documents_persisted": len(states),
                "documents_failed": sum(
                    1 for item in states if item["status"] == "failed"
                ),
                "pages_discovered": sum(int(item.get("pages") or 0) for item in states),
                "pages_without_text": sum(
                    int(item.get("pages_without_text") or 0) for item in states
                ),
            },
            "message": ("resultado final ainda não disponível; progresso persistido"),
            "read_only": True,
        }
    if datetime.fromisoformat(str(row["expires_at"])) <= _now():
        raise CompleteAnalysisError("resultado expirado; inicie nova análise")
    dossier = _crypto().decrypt(
        row["encrypted_payload"],
        aad=f"complete-analysis-result:{job_id}",
    )
    return {
        "job": job,
        "available": True,
        "dossier_sha256": row["dossier_sha256"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "dossier": dossier,
        "read_only": True,
    }


def explain(
    job_id: str,
    topic: str = "",
    finding_id: str = "",
) -> dict[str, Any]:
    result = get_result(job_id)
    if not result.get("available"):
        return result
    dossier = result["dossier"]
    collections = (
        "next_steps",
        "contradictions",
        "decisions",
        "claims",
        "defences",
        "orders",
    )
    wanted = str(finding_id or "").strip()
    if wanted:
        for collection in collections:
            for item in dossier.get(collection) or []:
                if str(item.get("id") or "") == wanted:
                    return {
                        "job_id": job_id,
                        "finding": item,
                        "collection": collection,
                        "read_only": True,
                    }
        raise CompleteAnalysisError("conclusão não encontrada no dossiê")
    normalised = _normalise(topic)
    matches = []
    for collection in collections:
        for item in dossier.get(collection) or []:
            haystack = _normalise(json.dumps(item, ensure_ascii=False))
            if not normalised or normalised in haystack:
                matches.append({"collection": collection, "finding": item})
    return {
        "job_id": job_id,
        "topic": topic,
        "matches": matches[:50],
        "total_matches": len(matches),
        "read_only": True,
    }


def _normalise(value: Any) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().casefold()


def split_pages(text: str) -> list[dict[str, Any]]:
    chunks = re.split(r"(?m)^--- Página (\d+) ---\s*$", str(text or ""))
    if len(chunks) <= 1:
        cleaned = str(text or "").strip()
        return [{"page": 1, "text": cleaned}] if cleaned else []
    pages = []
    for index in range(1, len(chunks), 2):
        page = int(chunks[index])
        content = chunks[index + 1].strip() if index + 1 < len(chunks) else ""
        pages.append({"page": page, "text": content})
    return pages


def build_manifest_entry(
    document: dict[str, Any],
    *,
    canonical_order: int,
    source_origin: str = "dom_document_tree",
) -> dict[str, Any]:
    """Normaliza metadados sem descartar campos desconhecidos da fonte (via manifest.py)."""
    return manifest.build_manifest_entry(
        document,
        canonical_order=canonical_order,
        source_origin=source_origin,
    )


def reconcile_manifest_and_dossier(
    manifest_list: list[dict[str, Any]],
    acquired_documents: list[dict[str, Any]],
    job_id: str | None = None,
) -> dict[str, Any]:
    """Reconcilia manifesto de metadados inicial com peças adquiridas e reenfileira falhas."""
    return manifest.reconcile_manifest_and_dossier(
        manifest=manifest_list,
        acquired_documents=acquired_documents,
        job_id=job_id,
    )


def _parse_date(date_val: Any) -> datetime | None:
    if not date_val:
        return None
    s = str(date_val).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    match = re.search(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if match:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        hour = int(match.group(4)) if match.group(4) else 0
        minute = int(match.group(5)) if match.group(5) else 0
        second = int(match.group(6)) if match.group(6) else 0
        try:
            return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _evaluate_temporal_consistency(
    base: dict[str, Any],
    movements: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    case_metadata: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Avalia a consistência temporal entre movimentações, distribuição e documentos."""
    movements_chronological = True
    distribution_valid = True
    document_dates_consistent = True

    now_dt = _now()

    dist_date = base.get("distribution_date") or case_metadata.get("distribution_date")
    parsed_dist = _parse_date(dist_date) if dist_date else None
    if parsed_dist and parsed_dist > now_dt + timedelta(days=1):
        distribution_valid = False

    for mov in movements:
        data_str = mov.get("data") or mov.get("date") or mov.get("data_hora")
        if data_str:
            dt = _parse_date(data_str)
            if dt:
                if dt > now_dt + timedelta(days=1):
                    movements_chronological = False
                if parsed_dist and dt < parsed_dist - timedelta(days=1):
                    movements_chronological = False

    for doc in documents:
        doc_date = doc.get("date") or doc.get("data")
        if doc_date:
            dt = _parse_date(doc_date)
            if dt and dt > now_dt + timedelta(days=1):
                document_dates_consistent = False

    is_consistent = movements_chronological and distribution_valid and document_dates_consistent
    return is_consistent, {
        "status": "complete" if is_consistent else "partial",
        "movements_chronological": movements_chronological,
        "distribution_valid": distribution_valid,
        "document_dates_consistent": document_dates_consistent,
    }


def normalise_document_payload(
    document: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    raw_text = str(
        response.get("texto") or response.get("conteudo") or response.get("teor") or ""
    )
    security = document_security.segregate_untrusted_text(
        raw_text,
        document_id=str(document.get("id") or ""),
    )
    text = str(security["canonical_text"])
    pages = split_pages(text)
    if not pages and text.strip():
        pages = [{"page": 1, "text": text.strip()}]
    structured_by_page = {
        int(item.get("page") or 0): item
        for item in response.get("paginas_estruturadas") or []
        if int(item.get("page") or 0) > 0
    }
    blank_page_numbers = {
        int(value) for value in response.get("paginas_sem_texto") or []
    }
    represented_pages = {int(page.get("page") or 0) for page in pages}
    for page_number in sorted(set(structured_by_page) | blank_page_numbers):
        if page_number not in represented_pages:
            pages.append({"page": page_number, "text": ""})
    pages.sort(key=lambda item: int(item.get("page") or 0))
    for page in pages:
        page_number = int(page.get("page") or 0)
        structured = structured_by_page.get(page_number, {})
        has_text = bool(str(page.get("text") or "").strip())
        page["tables"] = list(structured.get("tables") or [])
        page["extraction_method"] = (
            structured.get("extraction_method")
            or ("native_text" if has_text else "none")
        )
        page["status"] = (
            structured.get("status")
            or (
                "extracted"
                if has_text
                else (
                    "ocr_required_unavailable"
                    if page_number in blank_page_numbers
                    else "text_unavailable"
                )
            )
        )
        page["confidence"] = float(
            structured.get("confidence")
            if structured.get("confidence") is not None
            else (1.0 if has_text else 0.0)
        )
        page["line_count"] = structured.get(
            "line_count",
            len(str(page.get("text") or "").splitlines()) if page.get("text") else 0,
        )
        page["word_count"] = structured.get(
            "word_count",
            len(str(page.get("text") or "").split()) if page.get("text") else 0,
        )
        page["layout_preserved"] = structured.get("layout_preserved", True)
    text_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    source_digest = str(response.get("source_bytes_sha256") or "")
    pages_without_text = len(response.get("paginas_sem_texto") or [])
    if not text.strip() and not pages_without_text:
        pages_without_text = 1
    return {
        "document_id": str(document.get("id") or ""),
        "title": document.get("titulo") or document.get("tipo"),
        "type": document.get("tipo") or "",
        "date": document.get("data") or None,
        "pages": pages,
        "page_coverage": {
            "total": int(response.get("num_paginas") or len(pages)),
            "processed": len(pages),
            "native_text": sum(
                1
                for page in pages
                if page.get("extraction_method") == "native_text"
            ),
            "ocr": sum(
                1 for page in pages if page.get("extraction_method") == "ocr"
            ),
            "failed_or_unavailable": sum(
                1 for page in pages if page.get("status") != "extracted"
            ),
        },
        "ocr_status": response.get("ocr_status")
        or ("required_unavailable" if blank_page_numbers else "not_required"),
        # ``content_sha256`` permanece como alias de deduplicação para
        # consumidores antigos; quando disponível, agora representa os bytes
        # originais, não apenas o texto extraído.
        "content_sha256": source_digest or text_digest,
        "source_bytes_sha256": source_digest or None,
        "extracted_text_sha256": text_digest,
        "pages_without_text": pages_without_text,
        "truncated": bool(response.get("truncado")),
        "source_fingerprint": (
            auditoria_processual.build_document_fingerprint(document)
        ),
        "security": {
            "schema_version": security["schema_version"],
            "anomalies": security["anomalies"],
            "quarantined_sha256": security["quarantined_sha256"],
            "safe_for_automated_analysis": security["safe_for_automated_analysis"],
        },
    }


def _citation(
    document: dict[str, Any],
    page: dict[str, Any],
    excerpt: str,
) -> dict[str, Any]:
    return {
        "document_id": document["document_id"],
        "page": int(page["page"]),
        "excerpt": re.sub(r"\s+", " ", excerpt).strip()[:700],
        "sha256": document["content_sha256"],
        "source_bytes_sha256": document.get("source_bytes_sha256"),
    }


def _case_metadata_from_documents(
    base: dict[str, Any],
    documents: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    metadata = {
        "case_class": base.get("case_class"),
        "subject": base.get("subject"),
        "court": base.get("court"),
        "distribution_date": base.get("distribution_date"),
    }
    evidence: dict[str, list[dict[str, Any]]] = {}
    patterns = {
        "case_class": re.compile(
            r"(?im)^\s*(?:a[cç][aã]o|classe judicial|classe)\s*:"
            r"\s*([^\r\n]{3,160})"
        ),
        "subject": re.compile(
            r"(?im)^\s*assunto(?:\s+principal)?\s*:"
            r"\s*([^\r\n]{3,220})"
        ),
        "court": re.compile(
            r"(?ims)^\s*(?:ao douto ju[ií]zo da|ao ju[ií]zo da)\s+"
            r"(.{5,300}?)(?=\s+(?:n[º°]\s*judici[aá]rio|processo|"
            r"a[cç][aã]o)\s*:)"
        ),
    }
    for document in documents:
        for page in document.get("pages") or []:
            text = str(page.get("text") or "")
            for field, pattern in patterns.items():
                if metadata.get(field):
                    continue
                match = pattern.search(text)
                if not match:
                    continue
                value = re.sub(r"\s+", " ", match.group(1)).strip(" .:-")
                if not value:
                    continue
                metadata[field] = value
                evidence[field] = [_citation(document, page, match.group(0))]
    return metadata, evidence


def _sentences(text: str) -> Iterable[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    for sentence in re.split(r"(?<=[.!?;])\s+", cleaned):
        if 20 <= len(sentence) <= 1600:
            yield sentence.strip()


def _items_with_citations(
    documents: list[dict[str, Any]],
    pattern: re.Pattern[str],
    *,
    prefix: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    items = []
    seen = set()
    for document in documents:
        for page in document.get("pages") or []:
            for sentence in _sentences(str(page.get("text") or "")):
                if not pattern.search(_normalise(sentence)):
                    continue
                key = _normalise(sentence)
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "id": f"{prefix}-{len(items) + 1}",
                        "text": sentence,
                        "citations": [_citation(document, page, sentence)],
                    }
                )
                if len(items) >= limit:
                    return items
    return items


def _detect_contradictions(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    positions: dict[str, dict[bool, list[dict[str, Any]]]] = {}
    assertion = re.compile(
        r"(?:quest[aã]o\s*(\d+).{0,220})?"
        r"assertiva\s*([ivx]+).{0,180}?"
        r"\b(verdadeira|correta|falsa|incorreta)\b",
        re.IGNORECASE,
    )
    for document in documents:
        for page in document.get("pages") or []:
            text = re.sub(r"\s+", " ", str(page.get("text") or ""))
            for match in assertion.finditer(text):
                question = match.group(1) or "não identificada"
                roman = match.group(2).upper()
                polarity = _normalise(match.group(3)) in {
                    "verdadeira",
                    "correta",
                }
                key = f"questão {question}, assertiva {roman}"
                excerpt = text[max(0, match.start() - 180) : match.end() + 260]
                positions.setdefault(key, {True: [], False: []})[polarity].append(
                    _citation(document, page, excerpt)
                )
    contradictions = []
    for key, polarities in positions.items():
        if not polarities[True] or not polarities[False]:
            continue
        contradictions.append(
            {
                "id": f"contradiction-{len(contradictions) + 1}",
                "subject": key,
                "status": "documented_conflict",
                "positive_evidence": polarities[True][:5],
                "negative_evidence": polarities[False][:5],
                "requires_human_review": True,
            }
        )
    return contradictions


def _extract_inventario_data(documents: list[dict[str, Any]], base: dict[str, Any] = None) -> dict[str, Any]:
    from domain_analyzers.inventory_v1 import extract_inventory_v1
    res = extract_inventory_v1(documents, base)
    res["falecido"] = res.get("parties", {}).get("deceased", {}).get("name")
    res["meeiro"] = res.get("heirs", {}).get("meeiro", {}).get("name") if res.get("heirs", {}).get("meeiro") else None
    res["herdeiros"] = [h.get("name") for h in res.get("heirs", {}).get("heirs", []) if isinstance(h, dict)]
    res["testamento"] = res.get("testament_exists", False)
    res["bens"] = [p.get("description") for p in res.get("properties", {}).get("properties", []) if isinstance(p, dict)]
    res["itcd_pago"] = res.get("itcd", {}).get("paid", False)
    return res


def _extract_usucapiao_data(documents: list[dict[str, Any]], base: dict[str, Any] = None) -> dict[str, Any]:
    from domain_analyzers.adverse_possession_v1 import extract_adverse_possession_v1
    res = extract_adverse_possession_v1(documents, base)
    res["confrontantes"] = [c.get("name") for c in res.get("confrontantes", {}).get("confrontantes", []) if isinstance(c, dict)]
    res["local_incerto"] = res.get("unknown_location_edital", False)
    res["planta_memorial"] = res.get("land_area", {}).get("memorial_descritivo_present", False) or res.get("land_area", {}).get("topographic_map_present", False)
    pm = res.get("public_notices", {})
    res["manifestacoes"] = {
        "uniao": pm.get("uniao", {}).get("manifestation", "não_detectada") if isinstance(pm.get("uniao"), dict) else "não_detectada",
        "estado": pm.get("estado", {}).get("manifestation", "não_detectada") if isinstance(pm.get("estado"), dict) else "não_detectada",
        "municipio": pm.get("municipio", {}).get("manifestation", "não_detectada") if isinstance(pm.get("municipio"), dict) else "não_detectada",
    }
    return res


def build_dossier(
    *,
    process_number: str,
    base: dict[str, Any],
    manifest: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    document_states_value: list[dict[str, Any]],
    tree_complete: bool = True,
    expedients: list[dict[str, Any]],
    collected_at: str,
    expedients_complete: bool = True,
    attachments_complete: bool = True,
    expedients_source: str = "provided",
    expedients_safe_error: str | None = None,
    manifest_reconciled: bool = True,
    manifest_delta: dict[str, Any] | None = None,
    metadata_complete: bool = True,
    domain_profile: str = "generic_read_only/v1",
    domain_analysis_complete: bool | None = None,
) -> dict[str, Any]:
    # Auto-detecção de perfil de domínio se for genérico ou auto
    if domain_profile in ("generic_read_only/v1", "auto", ""):
        classes_raw = base.get("case_class") or base.get("classe") or base.get("classes") or ""
        if isinstance(classes_raw, list):
            classe = " ".join(str(c) for c in classes_raw).lower()
        else:
            classe = str(classes_raw).lower()

        assunto_raw = base.get("subject") or base.get("assunto") or base.get("assuntos") or ""
        if isinstance(assunto_raw, list):
            assunto = " ".join(str(a) for a in assunto_raw).lower()
        else:
            assunto = str(assunto_raw).lower()

        if any(w in classe or w in assunto for w in ["inventario", "inventário", "arrolamento", "partilha"]):
            domain_profile = "inventory/v1"
        elif any(w in classe or w in assunto for w in ["usucapiao", "usucapião"]):
            domain_profile = "adverse-possession/v1"

    from domain_analyzers import extract_domain_data
    domain_data = extract_domain_data(domain_profile, documents, base)

    decisions = _items_with_citations(
        documents,
        re.compile(r"\b(defiro|indefiro|acolho|rejeito|julgo|decido|determino)\b"),
        prefix="decision",
    )
    claims = _items_with_citations(
        documents,
        re.compile(r"\b(requer|pleiteia|pretende|postula|impetra)\b"),
        prefix="claim",
    )
    defences = _items_with_citations(
        documents,
        re.compile(
            r"\b(denegacao da seguranca|improcedencia|ausencia de direito "
            r"liquido|perda superveniente|requer a denegacao)\b"
        ),
        prefix="defence",
    )
    orders = _items_with_citations(
        documents,
        re.compile(
            r"\b(notifique-se|intime-se|intimem-se|publique-se|cumpra-se|"
            r"remetam-se|voltem os autos conclusos|de-se ciencia)\b"
        ),
        prefix="order",
    )
    contradictions = _detect_contradictions(documents)
    security_alerts = [
        anomaly
        for document in documents
        for anomaly in (document.get("security") or {}).get("anomalies") or []
    ]
    movements = list(base.get("movements") or [])
    case_metadata, case_evidence = _case_metadata_from_documents(
        base,
        documents,
    )

    active_deadlines = [
        item
        for item in expedients
        if not item.get("fechado") and item.get("data_limite")
    ]
    uncertain_deadlines = [
        item
        for item in expedients
        if not item.get("fechado") and not item.get("data_limite")
    ]
    latest_document = documents[0] if documents else None
    latest_text = _normalise(
        " ".join(
            str(page.get("text") or "")
            for page in (latest_document or {}).get("pages") or []
        )
    )
    sentence_order = None
    for order in reversed(orders):
        if re.search(r"conclus[oa].{0,80}senten", _normalise(order["text"])):
            sentence_order = order
            break

    next_steps = []
    if security_alerts:
        next_steps.append(
            {
                "id": "next-step-security-review",
                "act": "Revisão humana de conteúdo documental anômalo",
                "status": "security_review_required",
                "confidence": 0.0,
                "reason": (
                    "Conteúdo potencialmente adversarial foi segregado antes "
                    "da análise automática."
                ),
                "citations": [],
            }
        )
    elif sentence_order and re.search(
        r"ministerio publico|promotor de justica", latest_text
    ):
        next_steps.append(
            {
                "id": "next-step-1",
                "act": "Conclusão para sentença",
                "status": "probable",
                "confidence": 0.92,
                "reason": (
                    "A ordem judicial determinou retorno concluso após a "
                    "manifestação ministerial, e a peça mais recente é do MP."
                ),
                "citations": [
                    *sentence_order["citations"],
                    _citation(
                        latest_document,
                        latest_document["pages"][0],
                        str(latest_document["pages"][0]["text"])[:700],
                    ),
                ],
            }
        )
    elif active_deadlines:
        next_steps.append(
            {
                "id": "next-step-1",
                "act": "Aguardar ou controlar prazo processual ativo",
                "status": "probable",
                "confidence": 0.85,
                "reason": "Há expediente não fechado com data-limite.",
                "citations": [],
            }
        )
    else:
        next_steps.append(
            {
                "id": "next-step-1",
                "act": "Revisão humana do próximo ato",
                "status": "insufficient_evidence",
                "confidence": 0.0,
                "reason": (
                    "Não foi encontrada uma sequência determinística única "
                    "para o próximo ato."
                ),
                "citations": [],
            }
        )

    failed = [
        state for state in document_states_value if state.get("status") == "failed"
    ]
    missing_text = sum(
        int(state.get("pages_without_text") or 0) for state in document_states_value
    )
    ocr_pages = sum(
        1
        for document in documents
        for page in document.get("pages") or []
        if page.get("extraction_method") == "ocr"
    )
    truncated = [state for state in document_states_value if state.get("truncated")]
    gaps = []
    gap_details = []

    def add_gap(
        code: str,
        dimension: str,
        message: str,
        *,
        severity: str = "high",
    ) -> None:
        gaps.append(message)
        gap_details.append(
            {
                "code": code,
                "dimension": dimension,
                "severity": severity,
                "message": message,
            }
        )

    attachment_docs = [
        item for item in manifest
        if item.get("parent_document_id")
        or "anexo" in str(item.get("type", "")).lower()
        or item.get("type_code") in (2, 3, "attachment", "anexo")
    ]
    attachment_ids = {
        str(d.get("document_id") or d.get("documento_id") or d.get("id") or "")
        for d in attachment_docs
        if (d.get("document_id") or d.get("documento_id") or d.get("id"))
    }
    valid_states_by_id = {
        str(state.get("document_id") or state.get("documento_id") or state.get("id") or ""): state
        for state in document_states_value
        if state.get("status") not in ("failed", "ERROR", "corrupted")
        and (state.get("document_id") or state.get("documento_id") or state.get("id"))
    }
    missing_or_failed_attachments = [
        att_id for att_id in attachment_ids
        if att_id not in valid_states_by_id
    ]
    attachments_complete = len(missing_or_failed_attachments) == 0

    temporal_consistent, temporal_details = _evaluate_temporal_consistency(
        base, movements, documents, case_metadata
    )

    if not tree_complete:
        add_gap(
            "SOURCE_DOCUMENT_TREE_INCOMPLETE",
            "source",
            "árvore de documentos incompleta",
            severity="critical",
        )
    if not expedients_complete:
        add_gap(
            "SOURCE_EXPEDIENTS_UNAVAILABLE",
            "source",
            "fonte de expedientes não pôde confirmar presença ou ausência",
        )
    if not manifest_reconciled:
        add_gap(
            "SOURCE_MANIFEST_NOT_RECONCILED",
            "source",
            "manifesto não foi reconciliado ao final da coleta",
        )
    if failed:
        add_gap(
            "DOCUMENT_READ_FAILED",
            "documents",
            "uma ou mais peças não puderam ser lidas",
            severity="critical",
        )
    if len(document_states_value) != len(manifest):
        add_gap(
            "DOCUMENT_COVERAGE_MISMATCH",
            "documents",
            "quantidade de estados documentais diverge do manifesto",
            severity="critical",
        )
    if not attachments_complete:
        add_gap(
            "ATTACHMENTS_INCOMPLETE",
            "attachments",
            "um ou mais anexos documentais falharam ou estão ausentes",
            severity="critical",
        )
    if missing_text:
        add_gap(
            "PAGE_TEXT_UNAVAILABLE",
            "pages",
            "há páginas sem texto; OCR não estava disponível ou falhou",
            severity="critical",
        )
    if truncated:
        add_gap(
            "PAGE_EXTRACTION_TRUNCATED",
            "pages",
            "há documentos truncados",
            severity="critical",
        )
    if not metadata_complete:
        add_gap(
            "METADATA_NOT_PROVEN",
            "metadata",
            "metadados e vínculos documentais ainda não foram provados",
        )
    if not temporal_consistent:
        add_gap(
            "TEMPORAL_INCONSISTENCY_DETECTED",
            "temporal_consistency",
            "inconsistência temporal detectada entre movimentações e datas do processo",
            severity="high",
        )
    if security_alerts:
        add_gap(
            "DOCUMENT_SECURITY_REVIEW_REQUIRED",
            "documents",
            "conteúdo documental anômalo foi segregado para revisão humana",
        )

    domain_required = domain_profile != "generic_read_only/v1"
    domain_complete = (
        not domain_required
        if domain_analysis_complete is None
        else bool(domain_analysis_complete)
    )
    if domain_required and not domain_complete:
        add_gap(
            "DOMAIN_ANALYSIS_INCOMPLETE",
            "domain_analysis",
            f"análise especializada {domain_profile} está incompleta",
            severity="critical",
        )

    dimensions = {
        "source": {
            "required": True,
            "status": (
                "complete"
                if tree_complete and expedients_complete and manifest_reconciled
                else "partial"
            ),
            "tree_complete": tree_complete,
            "expedients_complete": expedients_complete,
            "manifest_reconciled": manifest_reconciled,
            "manifest_delta": manifest_delta,
        },
        "documents": {
            "required": True,
            "status": (
                "complete"
                if len(document_states_value) == len(manifest)
                and not failed
                and not security_alerts
                else "partial"
            ),
            "discovered": len(manifest),
            "processed": len(document_states_value),
            "failed": len(failed),
        },
        "attachments": {
            "required": True,
            "status": "complete" if attachments_complete else "partial",
            "discovered_attachments": len(attachment_docs),
            "failed_attachments": len(missing_or_failed_attachments),
        },
        "pages": {
            "required": True,
            "status": (
                "complete" if not missing_text and not truncated else "partial"
            ),
            "without_text": missing_text,
            "truncated_documents": len(truncated),
        },
        "metadata": {
            "required": True,
            "status": "complete" if metadata_complete else "partial",
        },
        "temporal_consistency": {
            "required": True,
            "status": "complete" if temporal_consistent else "partial",
            "movements_chronological": temporal_details["movements_chronological"],
            "distribution_valid": temporal_details["distribution_valid"],
            "document_dates_consistent": temporal_details["document_dates_consistent"],
        },
        "domain_analysis": {
            "required": domain_required,
            "profile": domain_profile,
            "schema_version": (
                domain_data.get("schema_version") or domain_profile
                if isinstance(domain_data, dict)
                else domain_profile
            ),
            "status": (
                "not_applicable"
                if not domain_required
                else ("complete" if domain_complete or bool(domain_data) else "partial")
            ),
            "data": domain_data,
        },
    }
    complete = all(
        dimension["status"] == "complete"
        for dimension in dimensions.values()
        if dimension["required"]
    )
    deduplicated = sum(
        1 for state in document_states_value if state.get("duplicate_of")
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "process_number": process_number,
        "collected_at": collected_at,
        "read_only": True,
        "semantic_analysis": "domain-extractors/v1" if domain_data else "disabled",
        "domain_analysis": dimensions["domain_analysis"],
        "ocr": (
            "partial_or_unavailable"
            if missing_text
            else ("completed" if ocr_pages else "not_required")
        ),
        "document_security": {
            "schema_version": document_security.SECURITY_SCHEMA,
            "status": "review_required" if security_alerts else "clear",
            "anomalies": security_alerts,
        },
        "case": {
            "parties": base.get("parties") or [],
            **case_metadata,
            "evidence": case_evidence,
        },
        "manifest_schema_version": (
            str(manifest[0].get("schema_version"))
            if manifest and manifest[0].get("schema_version")
            else "pje.document-manifest/v1"
        ),
        "manifest": manifest,
        "timeline": movements,
        "expedients": {
            "complete": expedients_complete,
            "source": expedients_source,
            "safe_error": expedients_safe_error,
            "total": len(expedients),
            "items": expedients,
            "active_deadlines": active_deadlines,
            "uncertain_open_items": uncertain_deadlines,
            "closed_items": sum(1 for item in expedients if item.get("fechado")),
        },
        "claims": claims,
        "defences": defences,
        "decisions": decisions,
        "orders": orders,
        "contradictions": contradictions,
        "next_steps": next_steps,
        "current_task": {
            "value": None,
            "status": "not_asserted_without_current_snapshot",
        },
        "coverage": {
            "complete": complete,
            "status": "complete" if complete else "partial_with_gaps",
            "tree_complete": tree_complete,
            "documents_discovered": len(manifest),
            "documents_processed": len(document_states_value),
            "documents_failed": len(failed),
            "documents_deduplicated": deduplicated,
            "pages_without_text": missing_text,
            "truncated_documents": len(truncated),
        },
        "completeness": dimensions,
        "gaps": gaps,
        "gap_details": gap_details,
        "extractor_versions": {
            "analysis": EXTRACTOR_VERSION,
            "documents": "pje-client-single-open/v1",
            "semantic": "domain-extractors/v1" if domain_data else "disabled",
            "ocr": "selective-local/v1",
            "ocr_pages": ocr_pages,
        },
    }


def reanalyse_from_cache(
    job_id: str,
    source_job_id: str,
) -> dict[str, Any]:
    """Reaplica extratores ao cache criptografado, sem abrir o navegador."""
    source_result = get_result(source_job_id)
    if not source_result.get("available"):
        raise CompleteAnalysisError("dossiê anterior indisponível para reanálise")
    source = source_result["dossier"]
    identity = identity_for_job(job_id)
    process_number = str(identity["process_number"])
    manifest = list(source.get("manifest") or [])
    manifest_sha = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    reset_document_states(job_id)
    update_job(
        job_id,
        status="running",
        phase="extraction",
        cancel_requested=0,
        total_documents=len(manifest),
        processed_documents=0,
        failed_documents=0,
        reused_documents=0,
        pages_discovered=0,
        pages_without_text=0,
        manifest_sha256=manifest_sha,
        error=None,
    )

    documents: list[dict[str, Any]] = []
    hash_owner: dict[str, str] = {}
    processed = failed = reused = pages = pages_without_text = 0
    for processed, item in enumerate(manifest, start=1):
        document_id = str(item.get("document_id") or "")
        fingerprint = str(item.get("source_fingerprint") or "")
        payload = auditoria_processual.get_cached_document(
            process_number,
            document_id,
            fingerprint,
        )
        if payload is None:
            failed += 1
            save_document_state(
                job_id,
                document_id=document_id,
                source_fingerprint=fingerprint,
                status="failed",
                safe_error="cache criptografado indisponível",
            )
        else:
            reused += 1
            content_hash = str(payload.get("content_sha256") or "")
            payload_pages = list(payload.get("pages") or [])
            blank_pages = int(payload.get("pages_without_text") or 0)
            pages += len(payload_pages)
            pages_without_text += blank_pages
            duplicate_of = hash_owner.get(content_hash, "")
            if not duplicate_of:
                hash_owner[content_hash] = document_id
                documents.append(dict(payload))
            save_document_state(
                job_id,
                document_id=document_id,
                source_fingerprint=fingerprint,
                status="duplicate" if duplicate_of else "reused",
                content_sha256=content_hash,
                duplicate_of=duplicate_of,
                pages=len(payload_pages),
                pages_without_text=blank_pages,
                truncated=bool(payload.get("truncated")),
                reused=True,
            )
        update_job(
            job_id,
            processed_documents=processed,
            failed_documents=failed,
            reused_documents=reused,
            pages_discovered=pages,
            pages_without_text=pages_without_text,
            current_document_id=document_id,
        )

    states = document_states(job_id)
    states_by_id = {str(item["document_id"]): item for item in states}
    manifest_with_coverage = [
        {
            **item,
            "status": states_by_id.get(str(item.get("document_id") or ""), {}).get(
                "status", "not_attempted"
            ),
            "content_sha256": states_by_id.get(
                str(item.get("document_id") or ""), {}
            ).get("content_sha256"),
            "pages": int(
                states_by_id.get(str(item.get("document_id") or ""), {}).get("pages")
                or 0
            ),
            "pages_without_text": int(
                states_by_id.get(str(item.get("document_id") or ""), {}).get(
                    "pages_without_text"
                )
                or 0
            ),
            "duplicate_of": states_by_id.get(
                str(item.get("document_id") or ""), {}
            ).get("duplicate_of"),
        }
        for item in manifest
    ]
    source_case = dict(source.get("case") or {})
    source_case_evidence = dict(source_case.get("evidence") or {})
    base = {
        "parties": source_case.get("parties") or [],
        "movements": source.get("timeline") or [],
    }
    for field in ("case_class", "subject", "court", "distribution_date"):
        if field not in source_case_evidence:
            base[field] = source_case.get(field)
    source_expedients = dict(source.get("expedients") or {})
    source_completeness = dict(source.get("completeness") or {})
    source_dimension = dict(source_completeness.get("source") or {})
    metadata_dimension = dict(source_completeness.get("metadata") or {})
    domain_dimension = dict(source_completeness.get("domain_analysis") or {})
    dossier = build_dossier(
        process_number=process_number,
        base=base,
        manifest=manifest_with_coverage,
        documents=documents,
        document_states_value=states,
        tree_complete=bool((source.get("coverage") or {}).get("tree_complete")),
        expedients=list(source_expedients.get("items") or []),
        collected_at=_now_iso(),
        expedients_complete=bool(source_expedients.get("complete", True)),
        expedients_source=str(source_expedients.get("source") or "cache"),
        expedients_safe_error=source_expedients.get("safe_error"),
        manifest_reconciled=bool(
            source_dimension.get("manifest_reconciled", True)
        ),
        manifest_delta=source_dimension.get("manifest_delta"),
        metadata_complete=metadata_dimension.get("status", "complete")
        == "complete",
        domain_profile=str(
            domain_dimension.get("profile") or "generic_read_only/v1"
        ),
        domain_analysis_complete=domain_dimension.get("status") == "complete",
    )
    if not source_expedients.get("items") and source_expedients.get("total"):
        dossier["expedients"] = source_expedients
    update_job(job_id, phase="synthesis", current_document_id=None)
    save_result(job_id, dossier)
    final_status = (
        "completed" if dossier["coverage"]["complete"] else "partial_with_gaps"
    )
    return update_job(
        job_id,
        status=final_status,
        phase="completed",
        current_document_id=None,
    )
