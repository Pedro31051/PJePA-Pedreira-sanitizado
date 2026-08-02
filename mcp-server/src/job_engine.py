"""Durable Job Engine over SQLite WAL (`job_engine.py`).

Provides transactional job persistence, atomic claims, worker heartbeats,
stale worker reaping, step checkpointing, and request coalescing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from contracts.error_taxonomy import ErrorCategory, ErrorCode, ErrorDetail

logger = logging.getLogger("job_engine")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DurableJobEngine:
    _instance: Optional[DurableJobEngine] = None

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            try:
                import auditoria_processual
                self.db_path = auditoria_processual.database_path()
            except Exception:
                self.db_path = Path("./jobs_storage/durable_jobs.db")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db_schema()

    @classmethod
    def get_instance(cls, db_path: Optional[Union[str, Path]] = None) -> DurableJobEngine:
        if cls._instance is None or db_path is not None:
            cls._instance = cls(db_path=db_path)
        return cls._instance

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS durable_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    process_cnj TEXT,
                    grau TEXT,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    worker_id TEXT,
                    heartbeat_at TEXT,
                    checkpoint_json TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_durable_jobs_lookup
                    ON durable_jobs(process_cnj, grau, job_type, status);
                CREATE INDEX IF NOT EXISTS idx_durable_jobs_status_heartbeat
                    ON durable_jobs(status, heartbeat_at);
                """
            )

    def submit_job(
        self,
        job_type: str,
        process_cnj: Optional[str] = None,
        grau: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        allow_coalesce: bool = True,
    ) -> str:
        """Submits a job to the engine. If allow_coalesce is True and an active
        (PENDING or RUNNING) job exists with the same (job_type, process_cnj, grau),
        returns the existing job_id (Request Coalescing).
        """
        payload_data = payload or {}
        now = _now_iso()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                if allow_coalesce and process_cnj:
                    grau_val = grau or "1g"
                    cur = conn.execute(
                        """
                        SELECT job_id FROM durable_jobs
                        WHERE process_cnj = ? AND grau = ? AND job_type = ?
                          AND status IN ('PENDING', 'RUNNING')
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (process_cnj, grau_val, job_type),
                    )
                    row = cur.fetchone()
                    if row:
                        logger.info(f"Coalesced request for {job_type}/{process_cnj} to existing job {row['job_id']}")
                        conn.execute("COMMIT;")
                        return row["job_id"]

                job_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO durable_jobs (
                        job_id, job_type, status, process_cnj, grau,
                        payload_json, retry_count, max_retries, created_at, updated_at
                    ) VALUES (?, ?, 'PENDING', ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        job_id,
                        job_type,
                        process_cnj,
                        grau or "1g",
                        json.dumps(payload_data, default=str, ensure_ascii=False),
                        max_retries,
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT;")
                return job_id
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def claim_job(
        self, worker_id: str, job_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Atomically claims a pending or stale job for worker execution."""
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            if job_id:
                cur = conn.execute(
                    "SELECT * FROM durable_jobs WHERE job_id = ? AND status = 'PENDING'",
                    (job_id,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT * FROM durable_jobs
                    WHERE status = 'PENDING'
                    ORDER BY created_at ASC LIMIT 1
                    """
                )
            row = cur.fetchone()
            if not row:
                conn.execute("COMMIT;")
                return None

            target_job_id = row["job_id"]
            conn.execute(
                """
                UPDATE durable_jobs
                SET status = 'RUNNING', worker_id = ?, heartbeat_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (worker_id, now, now, target_job_id),
            )
            conn.execute("COMMIT;")

            cur_updated = conn.execute(
                "SELECT * FROM durable_jobs WHERE job_id = ?", (target_job_id,)
            )
            updated_row = cur_updated.fetchone()
            return dict(updated_row) if updated_row else None

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Updates heartbeat_at for an active running job."""
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE durable_jobs
                SET heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND worker_id = ? AND status = 'RUNNING'
                """,
                (now, now, job_id, worker_id),
            )
            return cur.rowcount > 0

    def save_checkpoint(self, job_id: str, checkpoint_data: Dict[str, Any]) -> bool:
        """Saves intermediate execution state/checkpoint for process restart recovery."""
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE durable_jobs
                SET checkpoint_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (json.dumps(checkpoint_data, default=str, ensure_ascii=False), now, job_id),
            )
            return cur.rowcount > 0

    def get_checkpoint(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves the last saved checkpoint data for a job."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT checkpoint_json FROM durable_jobs WHERE job_id = ?", (job_id,)
            )
            row = cur.fetchone()
            if row and row["checkpoint_json"]:
                try:
                    return json.loads(row["checkpoint_json"])
                except Exception:
                    return None
            return None

    def complete_job(self, job_id: str, result: Dict[str, Any]) -> bool:
        """Marks job as COMPLETED with output payload."""
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE durable_jobs
                SET status = 'COMPLETED', result_json = ?, completed_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (json.dumps(result, default=str, ensure_ascii=False), now, now, job_id),
            )
            return cur.rowcount > 0

    def fail_job(self, job_id: str, error: Union[str, Dict[str, Any], ErrorDetail]) -> bool:
        """Marks job as FAILED with standardized error detail."""
        now = _now_iso()
        if isinstance(error, ErrorDetail):
            err_data = error.to_dict()
        elif isinstance(error, dict):
            err_data = error
        else:
            err_data = ErrorDetail(
                code=ErrorCode.JOB_FAILED,
                category=ErrorCategory.JOB_ENGINE,
                message=str(error),
                is_recoverable=False,
                origin="INTERNAL_MCP",
            ).to_dict()

        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE durable_jobs
                SET status = 'FAILED', error_json = ?, completed_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (json.dumps(err_data, default=str, ensure_ascii=False), now, now, job_id),
            )
            return cur.rowcount > 0

    def cancel_job(self, job_id: str) -> bool:
        """Cancels a PENDING or RUNNING job."""
        now = _now_iso()
        err_data = ErrorDetail(
            code=ErrorCode.JOB_CANCELLED,
            category=ErrorCategory.JOB_ENGINE,
            message="Job cancelled by user request",
            is_recoverable=False,
            origin="INTERNAL_MCP",
        ).to_dict()

        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE durable_jobs
                SET status = 'CANCELLED', error_json = ?, completed_at = ?, updated_at = ?
                WHERE job_id = ? AND status IN ('PENDING', 'RUNNING')
                """,
                (json.dumps(err_data, ensure_ascii=False), now, now, job_id),
            )
            return cur.rowcount > 0

    def reap_stale_workers(self, stale_threshold_seconds: float = 30.0) -> int:
        """Reaps jobs whose worker heartbeats are older than stale_threshold_seconds.
        If retry_count < max_retries, resets to PENDING for another worker to claim.
        Otherwise marks as FAILED.
        """
        now_dt = datetime.now(timezone.utc)
        reaped_count = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                "SELECT * FROM durable_jobs WHERE status = 'RUNNING'"
            )
            running_jobs = cur.fetchall()

            for row in running_jobs:
                hb_str = row["heartbeat_at"] or row["updated_at"]
                is_stale = False
                if hb_str:
                    try:
                        hb_dt = datetime.fromisoformat(hb_str)
                        if hb_dt.tzinfo is None:
                            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                        delta = (now_dt - hb_dt).total_seconds()
                        if delta > stale_threshold_seconds:
                            is_stale = True
                    except Exception:
                        is_stale = True
                else:
                    is_stale = True

                if is_stale:
                    reaped_count += 1
                    retry_cnt = row["retry_count"]
                    max_ret = row["max_retries"]
                    job_id = row["job_id"]
                    now_str = _now_iso()

                    if retry_cnt < max_ret:
                        conn.execute(
                            """
                            UPDATE durable_jobs
                            SET status = 'PENDING', worker_id = NULL,
                                retry_count = retry_count + 1, updated_at = ?
                            WHERE job_id = ?
                            """,
                            (now_str, job_id),
                        )
                        logger.warning(
                            f"Reaped stale worker job {job_id} (retry {retry_cnt + 1}/{max_ret})"
                        )
                    else:
                        err_data = ErrorDetail(
                            code=ErrorCode.JOB_FAILED,
                            category=ErrorCategory.JOB_ENGINE,
                            message=f"Worker heartbeat timed out after {stale_threshold_seconds}s (max retries reached)",
                            is_recoverable=False,
                            origin="INTERNAL_MCP",
                        ).to_dict()
                        conn.execute(
                            """
                            UPDATE durable_jobs
                            SET status = 'FAILED', error_json = ?, completed_at = ?, updated_at = ?
                            WHERE job_id = ?
                            """,
                            (json.dumps(err_data, ensure_ascii=False), now_str, now_str, job_id),
                        )
                        logger.error(f"Job {job_id} failed: max retries reached during stale reap")

            conn.execute("COMMIT;")
        return reaped_count

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM durable_jobs WHERE job_id = ?", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("payload_json"):
                try:
                    d["payload"] = json.loads(d["payload_json"])
                except Exception:
                    d["payload"] = {}
            if d.get("result_json"):
                try:
                    d["result"] = json.loads(d["result_json"])
                except Exception:
                    d["result"] = None
            if d.get("error_json"):
                try:
                    d["error"] = json.loads(d["error_json"])
                except Exception:
                    d["error"] = d["error_json"]
            if d.get("checkpoint_json"):
                try:
                    d["checkpoint"] = json.loads(d["checkpoint_json"])
                except Exception:
                    d["checkpoint"] = None
            return d

    def list_jobs(
        self,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
        process_cnj: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM durable_jobs WHERE 1=1"
        params: list[Any] = []
        if job_type:
            query += " AND job_type = ?"
            params.append(job_type)
        if status:
            query += " AND status = ?"
            params.append(status)
        if process_cnj:
            query += " AND process_cnj = ?"
            params.append(process_cnj)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            cur = conn.execute(query, params)
            rows = cur.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                if d.get("payload_json"):
                    try:
                        d["payload"] = json.loads(d["payload_json"])
                    except Exception:
                        d["payload"] = {}
                if d.get("result_json"):
                    try:
                        d["result"] = json.loads(d["result_json"])
                    except Exception:
                        d["result"] = None
                results.append(d)
            return results

    async def start_heartbeat_loop(
        self, job_id: str, worker_id: str, interval_seconds: float = 5.0
    ) -> asyncio.Task:
        async def _loop():
            while True:
                await asyncio.sleep(interval_seconds)
                success = self.heartbeat(job_id, worker_id)
                if not success:
                    break
        return asyncio.create_task(_loop())

    async def start_reaper_loop(
        self, interval_seconds: float = 15.0, stale_threshold_seconds: float = 30.0
    ) -> asyncio.Task:
        async def _reap_loop():
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    self.reap_stale_workers(stale_threshold_seconds=stale_threshold_seconds)
                except Exception as e:
                    logger.error(f"Error in stale worker reaper loop: {e}")
        return asyncio.create_task(_reap_loop())
