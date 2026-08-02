"""Unit and integration tests for SQLite WAL DurableJobEngine (`test_sqlite_wal_job_engine.py`).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_engine import DurableJobEngine


@pytest.fixture
def engine(tmp_path: Path):
    db_file = tmp_path / "test_wal_jobs.db"
    return DurableJobEngine(db_path=db_file)


def test_submit_and_claim_job_atomic(engine: DurableJobEngine):
    job_id = engine.submit_job(
        job_type="test_pdf_merge",
        process_cnj="0800001-61.2024.8.14.0028",
        grau="1g",
        payload={"docs": [1, 2, 3]},
    )
    assert job_id is not None

    claimed = engine.claim_job(worker_id="worker_alpha", job_id=job_id)
    assert claimed is not None
    assert claimed["status"] == "RUNNING"
    assert claimed["worker_id"] == "worker_alpha"

    # Second worker attempt to claim same job returns None
    claimed_again = engine.claim_job(worker_id="worker_beta", job_id=job_id)
    assert claimed_again is None


def test_request_coalescing(engine: DurableJobEngine):
    cnj = "0800002-77.2026.8.14.0001"
    job_id1 = engine.submit_job("pdf_consolidation", process_cnj=cnj, grau="1g", payload={"req": 1})
    job_id2 = engine.submit_job("pdf_consolidation", process_cnj=cnj, grau="1g", payload={"req": 2})

    # Coalescing should return exact same active job ID
    assert job_id1 == job_id2


def test_heartbeat_and_reaper(engine: DurableJobEngine):
    job_id = engine.submit_job("stale_task", process_cnj="0001-2026", max_retries=2)
    claimed = engine.claim_job(worker_id="worker_dead", job_id=job_id)
    assert claimed is not None

    # Heartbeat works for active worker
    hb_ok = engine.heartbeat(job_id, "worker_dead")
    assert hb_ok is True

    # Simulate worker death by setting heartbeat in past
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with engine._connect() as conn:
        conn.execute("UPDATE durable_jobs SET heartbeat_at = ? WHERE job_id = ?", (old_time, job_id))

    # Reap stale workers
    reaped = engine.reap_stale_workers(stale_threshold_seconds=10.0)
    assert reaped == 1

    # Job status reset to PENDING with retry_count incremented
    job_state = engine.get_job(job_id)
    assert job_state["status"] == "PENDING"
    assert job_state["retry_count"] == 1
    assert job_state["worker_id"] is None


def test_stale_reaper_max_retries_exceeded(engine: DurableJobEngine):
    job_id = engine.submit_job("failing_task", max_retries=1)
    engine.claim_job("worker_dead", job_id)

    # Set retry_count = 1 and heartbeat in past
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with engine._connect() as conn:
        conn.execute(
            "UPDATE durable_jobs SET heartbeat_at = ?, retry_count = 1 WHERE job_id = ?",
            (old_time, job_id),
        )

    reaped = engine.reap_stale_workers(stale_threshold_seconds=10.0)
    assert reaped == 1

    job_state = engine.get_job(job_id)
    assert job_state["status"] == "FAILED"
    assert "Worker heartbeat timed out" in str(job_state["error"])


def test_checkpoint_saving_and_recovery(engine: DurableJobEngine):
    job_id = engine.submit_job("analysis_job", process_cnj="0809999-11.2026.8.14.0001")
    engine.claim_job("worker_1", job_id)

    chk_data = {"processed_index": 14, "last_doc_id": "doc_14"}
    saved = engine.save_checkpoint(job_id, chk_data)
    assert saved is True

    recovered_chk = engine.get_checkpoint(job_id)
    assert recovered_chk == chk_data


@pytest.mark.asyncio
async def test_async_heartbeat_loop(engine: DurableJobEngine):
    job_id = engine.submit_job("async_task")
    engine.claim_job("worker_async", job_id)

    task = await engine.start_heartbeat_loop(job_id, "worker_async", interval_seconds=0.05)
    await asyncio.sleep(0.15)
    task.cancel()

    job_state = engine.get_job(job_id)
    assert job_state["heartbeat_at"] is not None
