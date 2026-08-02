"""Durable & Resumable Background Job Manager (`durable_jobs.py`).

Provides async background job management, status tracking, state persistence,
cancellation, resumption, and PDF consolidation jobs backed by SQLite WAL `DurableJobEngine`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import pypdf

from job_engine import DurableJobEngine

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class Job:
    job_id: str
    job_type: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    payload: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "status": str(self.status.value if isinstance(self.status, JobStatus) else self.status),
            "progress": self.progress,
            "payload": self.payload,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Job:
        raw_status = data.get("status", "PENDING")
        try:
            status = JobStatus(raw_status)
        except ValueError:
            status = JobStatus.PENDING
        return cls(
            job_id=data["job_id"],
            job_type=data.get("job_type", "generic"),
            status=status,
            progress=float(data.get("progress", 0.0)),
            payload=data.get("payload", {}),
            result=data.get("result"),
            error=data.get("error"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )


class JobManager:
    def __init__(self, storage_dir: Optional[Union[str, Path]] = None):
        self.storage_dir = Path(storage_dir) if storage_dir else Path("./jobs_storage")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        db_file = self.storage_dir / "durable_jobs.db"
        self.engine = DurableJobEngine(db_path=db_file)
        self._jobs: Dict[str, Job] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._job_funcs: Dict[str, Callable] = {}
        self.load_jobs_from_disk()

    def register_job_type(self, job_type: str, func: Callable) -> None:
        """Register a handler function for job resumption."""
        self._job_funcs[job_type] = func

    def load_jobs_from_disk(self) -> None:
        """Syncs jobs from SQLite WAL database and JSON files into memory cache."""
        if self.storage_dir.exists():
            for file_path in self.storage_dir.glob("*.json"):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        job = Job.from_dict(data)
                        self._jobs[job.job_id] = job
                except Exception as e:
                    logger.warning(f"Failed to load job file {file_path}: {e}")

        # Also load from SQLite engine
        db_jobs = self.engine.list_jobs(limit=500)
        for dj in db_jobs:
            j_id = dj["job_id"]
            chk = dj.get("checkpoint") or {}
            prog = float(chk.get("progress", 1.0 if dj["status"] == "COMPLETED" else 0.0))
            err_msg = None
            if dj.get("error"):
                err_msg = dj["error"].get("message") if isinstance(dj["error"], dict) else str(dj["error"])
            
            job = Job(
                job_id=j_id,
                job_type=dj["job_type"],
                status=JobStatus(dj["status"]) if dj["status"] in JobStatus.__members__ else JobStatus.PENDING,
                progress=prog,
                payload=dj.get("payload") or {},
                result=dj.get("result"),
                error=err_msg,
                created_at=dj["created_at"],
                updated_at=dj["updated_at"],
            )
            self._jobs[j_id] = job

    def save_job_to_disk(self, job: Job) -> None:
        """Persist job state to JSON file and SQLite WAL engine."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        file_path = self.storage_dir / f"{job.job_id}.json"
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(job.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save job {job.job_id}: {e}")

        self.engine.save_checkpoint(job.job_id, {"progress": job.progress})

    def create_job(self, job_type: str, payload: Optional[Dict[str, Any]] = None) -> Job:
        job_payload = payload or {}
        job_id = self.engine.submit_job(job_type=job_type, payload=job_payload, allow_coalesce=False)
        job = Job(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.PENDING,
            payload=job_payload,
        )
        self._jobs[job_id] = job
        self.save_job_to_disk(job)
        return job

    def submit_job(
        self,
        job_type: str,
        func: Callable,
        *args,
        payload: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> str:
        """Submit an async background job backed by DurableJobEngine."""
        self.register_job_type(job_type, func)
        job_payload = payload or kwargs or {}
        job = self.create_job(job_type, payload=job_payload)

        task = asyncio.create_task(self._run_job_wrapper(job.job_id, func, *args, **kwargs))
        self._tasks[job.job_id] = task
        return job.job_id

    async def _run_job_wrapper(self, job_id: str, func: Callable, *args, **kwargs) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return

        if job.status == JobStatus.CANCELLED:
            return

        worker_id = f"worker_{uuid.uuid4().hex[:8]}"
        claimed = self.engine.claim_job(worker_id=worker_id, job_id=job_id)
        if not claimed:

            pass

        hb_task = await self.engine.start_heartbeat_loop(job_id, worker_id, interval_seconds=2.0)

        job.status = JobStatus.RUNNING
        job.progress = 0.1
        self.save_job_to_disk(job)

        try:
            if self._func_expects_manager(func):
                res = func(self, job_id, *args, **kwargs)
            else:
                res = func(*args, **kwargs)

            if inspect.isawaitable(res):
                res = await res

            current_job = self._jobs.get(job_id)
            if current_job and current_job.status == JobStatus.CANCELLED:
                self.engine.cancel_job(job_id)
                return

            job.status = JobStatus.COMPLETED
            job.progress = 1.0
            job.result = res
            self.engine.complete_job(job_id, result=res)
            self.save_job_to_disk(job)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.error = "Job cancelled by user"
            self.engine.cancel_job(job_id)
            self.save_job_to_disk(job)
            raise
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            self.engine.fail_job(job_id, error=str(e))
            self.save_job_to_disk(job)
        finally:
            hb_task.cancel()

    def _func_expects_manager(self, func: Callable) -> bool:
        try:
            sig = inspect.signature(func)
            params = list(sig.parameters.keys())
            if len(params) < 2:
                return False
            p0 = params[0].lower()
            p1 = params[1].lower()
            return ("manager" in p0 or "self" in p0 or "job" in p0 or "mgr" in p0) and ("id" in p1 or "job" in p1)
        except Exception:
            return False

    def update_job_progress(self, job_id: str, progress: float, status_msg: Optional[str] = None) -> None:
        job = self._jobs.get(job_id)
        if job and job.status == JobStatus.RUNNING:
            job.progress = max(0.0, min(1.0, progress))
            if status_msg:
                if job.payload is None:
                    job.payload = {}
                job.payload["status_msg"] = status_msg
            self.save_job_to_disk(job)
            self.engine.save_checkpoint(job_id, {"progress": job.progress, "status_msg": status_msg})

    def get_job(self, job_id: str) -> Optional[Job]:
        # Try in-memory cache first
        j = self._jobs.get(job_id)
        if j:
            # Sync status from engine
            db_j = self.engine.get_job(job_id)
            if db_j:
                if db_j["status"] in JobStatus.__members__:
                    j.status = JobStatus(db_j["status"])
                j.result = db_j.get("result")
                if db_j.get("error"):
                    j.error = db_j["error"].get("message") if isinstance(db_j["error"], dict) else str(db_j["error"])
            return j

        db_j = self.engine.get_job(job_id)
        if db_j:
            err_msg = None
            if db_j.get("error"):
                err_msg = db_j["error"].get("message") if isinstance(db_j["error"], dict) else str(db_j["error"])
            chk = db_j.get("checkpoint") or {}
            prog = float(chk.get("progress", 1.0 if db_j["status"] == "COMPLETED" else 0.0))
            job = Job(
                job_id=job_id,
                job_type=db_j["job_type"],
                status=JobStatus(db_j["status"]) if db_j["status"] in JobStatus.__members__ else JobStatus.PENDING,
                progress=prog,
                payload=db_j.get("payload") or {},
                result=db_j.get("result"),
                error=err_msg,
                created_at=db_j["created_at"],
                updated_at=db_j["updated_at"],
            )
            self._jobs[job_id] = job
            return job
        return None

    def get_job_status(self, job_id: str) -> Optional[str]:
        job = self.get_job(job_id)
        if not job:
            return None
        return str(job.status.value if isinstance(job.status, JobStatus) else job.status)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running or pending job."""
        job = self.get_job(job_id)
        if not job:
            return False

        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            return False

        job.status = JobStatus.CANCELLED
        job.error = "Cancelled by user"
        self.engine.cancel_job(job_id)
        self.save_job_to_disk(job)

        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        return True

    def resume_job(self, job_id: str, func: Optional[Callable] = None) -> bool:
        """Resume a failed, pending or cancelled job."""
        job = self.get_job(job_id)
        if not job:
            return False

        handler = func or self._job_funcs.get(job.job_type)
        if not handler:
            logger.error(f"Cannot resume job {job_id}: no handler registered for job type {job.job_type}")
            return False

        job.status = JobStatus.PENDING
        job.error = None
        job.progress = 0.0
        self.save_job_to_disk(job)

        task = asyncio.create_task(self._run_job_wrapper(job.job_id, handler, **job.payload))
        self._tasks[job.job_id] = task
        return True

    def resume_pending_jobs(self) -> List[str]:
        resumed = []
        for job_id, job in list(self._jobs.items()):
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                if self.resume_job(job_id):
                    resumed.append(job_id)
        return resumed

    def list_jobs(self, status: Optional[Union[str, JobStatus]] = None) -> List[Dict[str, Any]]:
        target_status = str(status.value if isinstance(status, JobStatus) else status) if status else None
        db_jobs = self.engine.list_jobs(status=target_status, limit=500)
        results = []
        for dj in db_jobs:
            j = self.get_job(dj["job_id"])
            if j:
                results.append(j.to_dict())
        return results


async def consolidate_process_pdf_job(
    job_manager: JobManager,
    job_id: str,
    process_number: str,
    document_files: List[Union[str, Path]],
    output_pdf_path: Union[str, Path],
) -> Dict[str, Any]:
    """Background job for process analysis and PDF consolidation."""
    output_path = Path(output_pdf_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    writer = pypdf.PdfWriter()
    total_files = len(document_files)
    consolidated_docs = 0
    total_pages = 0

    for idx, doc_file in enumerate(document_files, 1):
        doc_path = Path(doc_file)
        if not doc_path.exists():
            continue

        try:
            reader = pypdf.PdfReader(doc_path)
            for page in reader.pages:
                writer.add_page(page)
                total_pages += 1
            consolidated_docs += 1
        except Exception as e:
            logger.warning(f"Could not parse PDF {doc_path}: {e}")

        progress = 0.1 + (0.8 * (idx / max(total_files, 1)))
        job_manager.update_job_progress(job_id, progress, f"Merged {idx}/{total_files} documents")

    if total_pages == 0:
        writer.add_blank_page(width=612, height=792)
        total_pages = 1

    with open(output_path, "wb") as f_out:
        writer.write(f_out)

    result = {
        "process_number": process_number,
        "output_pdf_path": str(output_path),
        "document_count": consolidated_docs,
        "total_pages": total_pages,
        "consolidated_at": datetime.now(timezone.utc).isoformat(),
    }
    return result
