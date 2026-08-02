"""Durable Batch Analysis Engine for PJe (`batch_engine.py`).

Provides batch creation, process queueing, cancellation, resumption, status tracking,
and result aggregation backed by the SQLite WAL database.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

import retention_policy
from analise_processual_completa import (
    _database,
    _now_iso,
    create_job,
    get_job,
    get_result,
    request_cancel,
)

logger = logging.getLogger(__name__)
MAX_PROCESS_CONCURRENCY = 3


def _normalise_mode(mode: str) -> str:
    value = str(mode or "rapida").strip().casefold()
    if value not in {"inventario", "rapida", "integral"}:
        raise ValueError("modo do lote deve ser inventario, rapida ou integral")
    return value


def _normalise_concurrency(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("concorrência do lote deve ser um número inteiro") from exc
    return max(1, min(parsed, MAX_PROCESS_CONCURRENCY))


def _global_concurrency_limit() -> int:
    try:
        configured = int(os.environ.get("PJE_MAX_BROWSER_WORKERS", "2"))
    except (TypeError, ValueError):
        configured = 2
    return max(1, min(configured, MAX_PROCESS_CONCURRENCY))


def criar_lote(
    batch_id: Optional[str] = None,
    *,
    mode: str = "rapida",
    max_concurrency: int = 2,
) -> str:
    """Cria um novo lote de análise no banco de dados e retorna seu batch_id."""
    retention_policy.require_legacy_persistence("o lote persistente de análises")
    b_id = batch_id or f"batch_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    selected_mode = _normalise_mode(mode)
    selected_concurrency = _normalise_concurrency(max_concurrency)

    with _database() as conn:
        conn.execute(
            """
            INSERT INTO analysis_batches (
                batch_id, status, total_processes, authorisation_ref,
                force_reread, mode, max_concurrency, created_at, updated_at
            ) VALUES (?, 'queued', 0, NULL, 0, ?, ?, ?, ?)
            """,
            (b_id, selected_mode, selected_concurrency, now, now),
        )
    logger.info(f"Lote {b_id} criado com sucesso.")
    return b_id


def adicionar_ao_lote(
    batch_id: str,
    cnj_list: List[str],
    grau: str = "1g",
    persona: str = "servidor",
) -> Dict[str, Any]:
    """Adiciona uma lista de processos (CNJs) ao lote especificado."""
    now = _now_iso()
    added_count = 0
    ignored_count = 0

    with _database() as conn:
        # Verifica se o lote existe
        cur = conn.execute("SELECT status FROM analysis_batches WHERE batch_id = ?", (batch_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Lote {batch_id} não encontrado")

        for cnj in cnj_list:
            cnj_clean = cnj.strip()
            if not cnj_clean:
                continue

            try:
                conn.execute(
                    """
                    INSERT INTO analysis_batch_items (
                        batch_id, process_cnj, grau, persona, status, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', ?)
                    """,
                    (batch_id, cnj_clean, grau, persona, now),
                )
                added_count += 1
            except sqlite3.IntegrityError:
                # O CNJ+grau já existe neste lote, ignoramos ou atualizamos
                ignored_count += 1

        # Atualiza a contagem total de processos no lote
        conn.execute(
            """
            UPDATE analysis_batches
            SET total_processes = (
                SELECT COUNT(*) FROM analysis_batch_items WHERE batch_id = ?
            ), updated_at = ?
            WHERE batch_id = ?
            """,
            (batch_id, now, batch_id),
        )

    logger.info(f"Adicionados {added_count} processos ao lote {batch_id} (ignorados {ignored_count})")
    return {
        "batch_id": batch_id,
        "adicionados": added_count,
        "ignorados": ignored_count,
        "total": added_count + ignored_count,
    }


def iniciar_lote(
    batch_id: str,
    authorisation_ref: str = "",
    force_reread: bool = False,
    mode: str | None = None,
    max_concurrency: int | None = None,
) -> Dict[str, Any]:
    """Marca o lote e seus itens pendentes como aptos para execução (running)."""
    now = _now_iso()
    with _database() as conn:
        cur = conn.execute(
            "SELECT status, mode, max_concurrency FROM analysis_batches WHERE batch_id = ?",
            (batch_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Lote {batch_id} não encontrado")
        selected_mode = _normalise_mode(mode or row["mode"])
        selected_concurrency = _normalise_concurrency(
            max_concurrency
            if max_concurrency is not None
            else row["max_concurrency"]
        )

        # Atualiza o status do lote para 'running', salvando a referência de autorização e force_reread
        conn.execute(
            """
            UPDATE analysis_batches 
            SET status = 'running', authorisation_ref = ?, force_reread = ?,
                mode = ?, max_concurrency = ?, updated_at = ?
            WHERE batch_id = ?
            """,
            (
                authorisation_ref,
                1 if force_reread else 0,
                selected_mode,
                selected_concurrency,
                now,
                batch_id,
            ),
        )

    logger.info(f"Lote {batch_id} marcado para execução com authorisation_ref='{authorisation_ref}'.")
    return {
        "batch_id": batch_id,
        "status": "running",
        "mode": selected_mode,
        "max_concurrency": selected_concurrency,
    }


def status_lote(batch_id: str) -> Dict[str, Any]:
    """Retorna o progresso atual do lote e a listagem de status por item."""
    with _database() as conn:
        cur = conn.execute("SELECT * FROM analysis_batches WHERE batch_id = ?", (batch_id,))
        batch_row = cur.fetchone()
        if not batch_row:
            raise ValueError(f"Lote {batch_id} não encontrado")

        cur_items = conn.execute(
            "SELECT * FROM analysis_batch_items WHERE batch_id = ?", (batch_id,)
        )
        items_rows = cur_items.fetchall()

    total = batch_row["total_processes"]
    queued = 0
    running = 0
    completed = 0
    failed = 0
    cancelled = 0

    items_list = []
    for row in items_rows:
        status = row["status"]
        if status == "queued":
            queued += 1
        elif status == "running":
            running += 1
        elif status == "completed":
            completed += 1
        elif status == "failed":
            failed += 1
        elif status == "cancelled":
            cancelled += 1

        items_list.append({
            "process_cnj": row["process_cnj"],
            "grau": row["grau"],
            "persona": row["persona"],
            "status": status,
            "job_id": row["job_id"],
            "error": row["error"],
            "updated_at": row["updated_at"],
        })

    # Calcula cobertura documental agregada baseada nos jobs individuais
    # e calcula o progresso
    progresso = 0.0
    if total > 0:
        progresso = (completed + failed + cancelled) / total

    # Reconcilia o status real do lote se todos os itens terminaram
    status_real = batch_row["status"]
    if status_real == "running" and (completed + failed + cancelled) == total:
        status_real = "completed" if failed == 0 and cancelled == 0 else "partial"
        now = _now_iso()
        with _database() as conn:
            conn.execute(
                "UPDATE analysis_batches SET status = ?, completed_at = ?, updated_at = ? WHERE batch_id = ?",
                (status_real, now, now, batch_id),
            )

    effective_limit = min(
        int(batch_row["max_concurrency"]),
        _global_concurrency_limit(),
    )
    return {
        "batch_id": batch_id,
        "status": status_real,
        "mode": batch_row["mode"],
        "max_concurrency": int(batch_row["max_concurrency"]),
        "effective_concurrency": effective_limit,
        "available_slots": max(
            0,
            effective_limit - running,
        ),
        "total_processes": total,
        "queued": queued,
        "running": running,
        "completed": completed,
        "failed": failed,
        "cancelled": cancelled,
        "progress": round(progresso, 2),
        "created_at": batch_row["created_at"],
        "updated_at": batch_row["updated_at"],
        "completed_at": batch_row["completed_at"],
        "items": items_list,
    }


def cancelar_lote(batch_id: str) -> Dict[str, Any]:
    """Cancela todos os processos pendentes ou em execução no lote."""
    now = _now_iso()
    jobs_to_cancel = []

    with _database() as conn:
        cur = conn.execute("SELECT status FROM analysis_batches WHERE batch_id = ?", (batch_id,))
        if not cur.fetchone():
            raise ValueError(f"Lote {batch_id} não encontrado")

        # Busca todos os itens em execução
        cur_running = conn.execute(
            "SELECT job_id FROM analysis_batch_items WHERE batch_id = ? AND status = 'running'",
            (batch_id,),
        )
        for r in cur_running.fetchall():
            if r["job_id"]:
                jobs_to_cancel.append(r["job_id"])

        # Cancela no banco local
        conn.execute(
            """
            UPDATE analysis_batch_items
            SET status = 'cancelled', error = 'Cancelado pelo usuário através do lote', updated_at = ?
            WHERE batch_id = ? AND status IN ('queued', 'running')
            """,
            (now, batch_id),
        )

        conn.execute(
            "UPDATE analysis_batches SET status = 'cancelled', completed_at = ?, updated_at = ? WHERE batch_id = ?",
            (now, now, batch_id),
        )

    # Solicita cancelamento para cada job individual
    cancelled_jobs_count = 0
    for job_id in jobs_to_cancel:
        try:
            request_cancel(job_id)
            cancelled_jobs_count += 1
        except Exception as e:
            logger.warning(f"Erro ao cancelar job {job_id} do lote: {e}")

    return {
        "batch_id": batch_id,
        "status": "cancelled",
        "jobs_cancelados": cancelled_jobs_count,
    }


def retomar_lote(batch_id: str) -> Dict[str, Any]:
    """Retoma a fila do lote para os processos falhos ou cancelados."""
    now = _now_iso()
    with _database() as conn:
        cur = conn.execute("SELECT status FROM analysis_batches WHERE batch_id = ?", (batch_id,))
        if not cur.fetchone():
            raise ValueError(f"Lote {batch_id} não encontrado")

        # Atualiza itens falhos ou cancelados de volta para queued
        cur_up = conn.execute(
            """
            UPDATE analysis_batch_items
            SET status = 'queued', error = NULL, job_id = NULL, updated_at = ?
            WHERE batch_id = ? AND status IN ('failed', 'cancelled')
            """,
            (now, batch_id),
        )
        retomados = cur_up.rowcount

        conn.execute(
            "UPDATE analysis_batches SET status = 'running', completed_at = NULL, updated_at = ? WHERE batch_id = ?",
            (now, now, batch_id),
        )

    logger.info(f"Retomados {retomados} processos no lote {batch_id}")
    return {"batch_id": batch_id, "status": "running", "retomados": retomados}


def resultado_lote(batch_id: str) -> Dict[str, Any]:
    """Reúne e entrega os resultados/dossiês individuais concluídos do lote."""
    items_results = []
    
    with _database() as conn:
        cur = conn.execute(
            "SELECT process_cnj, job_id, status FROM analysis_batch_items WHERE batch_id = ?",
            (batch_id,),
        )
        rows = cur.fetchall()

    for row in rows:
        cnj = row["process_cnj"]
        job_id = row["job_id"]
        status = row["status"]
        
        result_payload = None
        if job_id and status in ("completed", "failed"):
            try:
                res = get_result(job_id)
                if res and not res.get("erro"):
                    result_payload = res
            except Exception:
                pass

        items_results.append({
            "process_cnj": cnj,
            "job_id": job_id,
            "status": status,
            "resultado": result_payload,
        })

    return {
        "batch_id": batch_id,
        "resultados": items_results,
    }


def obter_proximos_itens_fila(limite: int = 1) -> List[Dict[str, Any]]:
    """Busca os próximos itens na fila ('queued') de lotes ativos ('running')."""
    with _database() as conn:
        cur = conn.execute(
            """
            SELECT i.*, b.mode, b.max_concurrency,
                   b.authorisation_ref, b.force_reread
              FROM analysis_batch_items i
            JOIN analysis_batches b ON i.batch_id = b.batch_id
            WHERE b.status = 'running' AND i.status = 'queued'
            ORDER BY b.created_at ASC, i.updated_at ASC
            LIMIT ?
            """,
            (limite,),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def job_belongs_to_parallel_batch(job_id: str) -> bool:
    """Indica se o job pode usar aba isolada sem o lock global da página."""
    with _database() as conn:
        row = conn.execute(
            """
            SELECT b.max_concurrency
              FROM analysis_batch_items i
              JOIN analysis_batches b ON b.batch_id=i.batch_id
             WHERE i.job_id=? AND i.status='running'
             LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    return bool(row and int(row["max_concurrency"] or 1) > 1)


def sincronizar_status_itens_lote() -> None:
    """Verifica e sincroniza o status dos itens de lote com seus respectivos jobs individuais."""
    now = _now_iso()
    with _database() as conn:
        cur = conn.execute(
            "SELECT batch_id, process_cnj, grau, job_id, status FROM analysis_batch_items WHERE status = 'running'"
        )
        running_items = cur.fetchall()

    for item in running_items:
        job_id = item["job_id"]
        if not job_id:
            continue

        try:
            job = get_job(job_id)
            if not job:
                continue

            job_status = job.get("status")
            if job_status in ("completed", "partial_with_gaps"):
                # Job terminou com sucesso
                with _database() as conn:
                    conn.execute(
                        """
                        UPDATE analysis_batch_items
                        SET status = 'completed', updated_at = ?
                        WHERE batch_id = ? AND process_cnj = ? AND grau = ?
                        """,
                        (now, item["batch_id"], item["process_cnj"], item["grau"]),
                    )
            elif job_status == "failed":
                # Job falhou
                err_msg = job.get("error") or "Job falhou sem mensagem de erro específica"
                with _database() as conn:
                    conn.execute(
                        """
                        UPDATE analysis_batch_items
                        SET status = 'failed', error = ?, updated_at = ?
                        WHERE batch_id = ? AND process_cnj = ? AND grau = ?
                        """,
                        (err_msg, now, item["batch_id"], item["process_cnj"], item["grau"]),
                    )
            elif job_status == "cancelled":
                # Job cancelado
                with _database() as conn:
                    conn.execute(
                        """
                        UPDATE analysis_batch_items
                        SET status = 'cancelled', error = 'Job cancelado', updated_at = ?
                        WHERE batch_id = ? AND process_cnj = ? AND grau = ?
                        """,
                        (now, item["batch_id"], item["process_cnj"], item["grau"]),
                    )
        except Exception as e:
            logger.warning(f"Erro ao sincronizar status do item do lote para o job {job_id}: {e}")


async def processar_ciclo_fila_lotes(
    agendar_analise_func,
    complete_analysis_tasks_dict,
) -> Dict[str, int]:
    """Preenche imediatamente todas as vagas globais e de cada lote."""
    sincronizar_status_itens_lote()
    global_limit = _global_concurrency_limit()
    active_jobs = {
        str(job_id)
        for job_id, task in list(complete_analysis_tasks_dict.items())
        if not task.done()
    }
    free_global_slots = max(0, global_limit - len(active_jobs))
    if not free_global_slots:
        return {"started": 0, "active": len(active_jobs), "available": 0}

    with _database() as conn:
        rows = conn.execute(
            """
            SELECT i.*, b.mode, b.max_concurrency,
                   b.authorisation_ref, b.force_reread,
                   (SELECT COUNT(*) FROM analysis_batch_items active
                     WHERE active.batch_id=i.batch_id
                       AND active.status='running') AS batch_running
              FROM analysis_batch_items i
              JOIN analysis_batches b ON b.batch_id=i.batch_id
             WHERE b.status='running' AND i.status='queued'
             ORDER BY b.created_at ASC, i.updated_at ASC
            """
        ).fetchall()

    started = 0
    running_by_batch: Dict[str, int] = {}
    for row in rows:
        if started >= free_global_slots:
            break
        item = dict(row)
        batch_id = str(item["batch_id"])
        running = running_by_batch.setdefault(
            batch_id,
            int(item.get("batch_running") or 0),
        )
        batch_limit = _normalise_concurrency(item.get("max_concurrency") or 1)
        if running >= batch_limit:
            continue

        job = create_job(
            process_number=str(item["process_cnj"]),
            grau=str(item["grau"]),
            persona=str(item["persona"]),
            mode=_normalise_mode(str(item.get("mode") or "rapida")),
            semantic_enabled=False,
            force_reread=bool(item.get("force_reread")),
            authorisation_ref=str(
                item.get("authorisation_ref") or "lote_process_analysis"
            ),
        )
        job_id = str(job["job_id"])
        with _database() as conn:
            cursor = conn.execute(
                """
                UPDATE analysis_batch_items
                   SET job_id=?, status='running', updated_at=?
                 WHERE batch_id=? AND process_cnj=? AND grau=?
                   AND status='queued'
                """,
                (
                    job_id,
                    _now_iso(),
                    batch_id,
                    item["process_cnj"],
                    item["grau"],
                ),
            )
        if cursor.rowcount != 1:
            continue
        agendar_analise_func(job_id)
        running_by_batch[batch_id] += 1
        started += 1
        logger.info(
            "[BATCH] job %s iniciado no lote %s; ocupação %s/%s",
            job_id,
            batch_id,
            running_by_batch[batch_id],
            batch_limit,
        )
    return {
        "started": started,
        "active": len(active_jobs) + started,
        "available": max(0, free_global_slots - started),
    }


async def _processar_fila_de_lotes_loop(
    agendar_analise_func,
    complete_analysis_tasks_dict,
) -> None:
    """Mantém N vagas ocupadas e repõe cada saída sem esperar o lote acabar."""
    wake_up = asyncio.Event()
    while True:
        try:
            cycle = await processar_ciclo_fila_lotes(
                agendar_analise_func,
                complete_analysis_tasks_dict,
            )
            for task in list(complete_analysis_tasks_dict.values()):
                if not getattr(task, "_batch_wakeup_registered", False):
                    task._batch_wakeup_registered = True
                    task.add_done_callback(lambda _task: wake_up.set())
            if cycle["available"] > 0:
                # Não há item apto agora; polling curto cobre lotes recém-iniciados.
                timeout = 1.0
            else:
                timeout = 5.0
            wake_up.clear()
            try:
                await asyncio.wait_for(wake_up.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[BATCH] erro seguro no scheduler: %s", exc)
            await asyncio.sleep(1)
