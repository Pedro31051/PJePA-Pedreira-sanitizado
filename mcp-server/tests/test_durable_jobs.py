"""Unit tests for Durable Background Jobs (`test_durable_jobs.py`).

Tests job creation, async execution, status persistence, state recovery,
cancellation, resumption, and PDF consolidation job execution.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pypdf
import pytest

from durable_jobs import JobManager, JobStatus, consolidate_process_pdf_job


@pytest.mark.asyncio
async def test_job_submission_and_execution(tmp_path: Path):
    manager = JobManager(storage_dir=tmp_path / "jobs")

    async def sample_task(val: int):
        await asyncio.sleep(0.05)
        return {"double": val * 2}

    job_id = manager.submit_job("sample_task", sample_task, val=21)
    assert manager.get_job_status(job_id) in (JobStatus.PENDING, JobStatus.RUNNING)

    # Wait for completion
    for _ in range(20):
        await asyncio.sleep(0.05)
        if manager.get_job_status(job_id) == JobStatus.COMPLETED:
            break

    job = manager.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.result == {"double": 42}


@pytest.mark.asyncio
async def test_job_status_persistence_and_reload(tmp_path: Path):
    storage_dir = tmp_path / "jobs"
    manager1 = JobManager(storage_dir=storage_dir)

    async def dummy_work():
        return "done"

    job_id = manager1.submit_job("dummy", dummy_work)
    await asyncio.sleep(0.1)

    assert manager1.get_job_status(job_id) == JobStatus.COMPLETED

    # Reload from disk with new JobManager instance
    manager2 = JobManager(storage_dir=storage_dir)
    job_reloaded = manager2.get_job(job_id)
    assert job_reloaded is not None
    assert job_reloaded.status == JobStatus.COMPLETED
    assert job_reloaded.result == "done"


@pytest.mark.asyncio
async def test_job_cancellation(tmp_path: Path):
    manager = JobManager(storage_dir=tmp_path / "jobs")

    async def long_running_task():
        await asyncio.sleep(2.0)
        return "should_not_reach"

    job_id = manager.submit_job("long_task", long_running_task)
    await asyncio.sleep(0.02)

    cancelled = manager.cancel_job(job_id)
    assert cancelled is True
    assert manager.get_job_status(job_id) == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_job_resumption(tmp_path: Path):
    manager = JobManager(storage_dir=tmp_path / "jobs")

    attempts = 0

    async def flaky_task(manager_ref, j_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Temporary failure")
        return "success_on_retry"

    manager.register_job_type("flaky", flaky_task)
    job_id = manager.submit_job("flaky", flaky_task)

    # Wait for first attempt to fail
    for _ in range(10):
        await asyncio.sleep(0.05)
        if manager.get_job_status(job_id) == JobStatus.FAILED:
            break

    assert manager.get_job_status(job_id) == JobStatus.FAILED

    # Resume job
    resumed = manager.resume_job(job_id)
    assert resumed is True

    # Wait for second attempt to complete
    for _ in range(10):
        await asyncio.sleep(0.05)
        if manager.get_job_status(job_id) == JobStatus.COMPLETED:
            break

    job = manager.get_job(job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.result == "success_on_retry"


@pytest.mark.asyncio
async def test_consolidate_process_pdf_job_execution(tmp_path: Path):
    storage_dir = tmp_path / "jobs"
    manager = JobManager(storage_dir=storage_dir)

    # Create 2 sample PDF files
    pdf1_path = tmp_path / "doc1.pdf"
    pdf2_path = tmp_path / "doc2.pdf"
    output_pdf_path = tmp_path / "consolidated_process.pdf"

    w1 = pypdf.PdfWriter()
    w1.add_blank_page(width=612, height=792)
    with open(pdf1_path, "wb") as f:
        w1.write(f)

    w2 = pypdf.PdfWriter()
    w2.add_blank_page(width=612, height=792)
    w2.add_blank_page(width=612, height=792)
    with open(pdf2_path, "wb") as f:
        w2.write(f)

    job_id = manager.submit_job(
        "pdf_consolidation",
        consolidate_process_pdf_job,
        process_number="0801234-56.2026.8.14.0001",
        document_files=[pdf1_path, pdf2_path],
        output_pdf_path=output_pdf_path,
    )

    # Wait for completion
    for _ in range(20):
        await asyncio.sleep(0.05)
        if manager.get_job_status(job_id) == JobStatus.COMPLETED:
            break

    job = manager.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert output_pdf_path.exists()

    # Verify consolidated PDF has 3 pages
    reader = pypdf.PdfReader(output_pdf_path)
    assert len(reader.pages) == 3
    assert job.result["document_count"] == 2
    assert job.result["total_pages"] == 3
