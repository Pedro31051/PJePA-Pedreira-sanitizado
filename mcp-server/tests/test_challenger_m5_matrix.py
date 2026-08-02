"""Empirical Challenger Tests for Milestone M5 (`test_challenger_m5_matrix.py`).

Tests cache hit ratio calculation, dual invalidation on extractor version change,
durable job resumption across simulated process crashes, and HTML dashboard element integrity.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from durable_jobs import JobManager, JobStatus
from generate_pje_dashboard import generate_html_dashboard
from structured_cache import StructuredCache, compute_hash

# ============================================================================
# 1. Cache Hit Ratio Calculation Tests
# ============================================================================

def test_cache_hit_ratio_precision_and_edge_cases(tmp_path: Path):
    cache = StructuredCache(cache_dir=tmp_path / "cache_store")
    
    # 0 requests initially
    stats0 = cache.get_cache_stats()
    assert stats0["hits"] == 0
    assert stats0["misses"] == 0
    assert stats0["negative_hits"] == 0
    assert stats0["total_requests"] == 0
    assert stats0["hit_ratio"] == 0.0

    byte_h = compute_hash("bytes_v1")
    ext_h = compute_hash("extractor_v1")

    # 1 Miss
    res_miss, status_miss = cache.get_with_status("key1", byte_h, ext_h)
    assert res_miss is None
    assert status_miss == "MISS"
    
    stats1 = cache.get_cache_stats()
    assert stats1["hits"] == 0
    assert stats1["misses"] == 1
    assert stats1["hit_ratio"] == 0.0

    # Put key1 and get -> 1 Hit
    cache.put("key1", byte_h, ext_h, {"data": "value1"})
    res_hit, status_hit = cache.get_with_status("key1", byte_h, ext_h)
    assert res_hit == {"data": "value1"}
    assert status_hit == "HIT"

    stats2 = cache.get_cache_stats()
    assert stats2["hits"] == 1
    assert stats2["misses"] == 1
    assert stats2["total_requests"] == 2
    assert stats2["hit_ratio"] == 0.5

    # Put negative key2 and get -> 1 Negative Hit
    cache.put_negative("key2", byte_h, ext_h, reason="Unreadable OCR", ttl=600)
    res_neg, status_neg = cache.get_with_status("key2", byte_h, ext_h)
    assert res_neg is None
    assert status_neg == "NEGATIVE_HIT"

    stats3 = cache.get_cache_stats()
    assert stats3["hits"] == 1
    assert stats3["misses"] == 1
    assert stats3["negative_hits"] == 1
    assert stats3["total_requests"] == 3
    assert stats3["hit_ratio"] == round(2 / 3, 4)

    # 7 more Hits on key1
    for _ in range(7):
        cache.get("key1", byte_h, ext_h)

    stats4 = cache.get_cache_stats()
    assert stats4["hits"] == 8
    assert stats4["misses"] == 1
    assert stats4["negative_hits"] == 1
    assert stats4["total_requests"] == 10
    assert stats4["hit_ratio"] == round(9 / 10, 4)  # 0.9

    # Reset stats
    cache.reset_stats()
    stats_reset = cache.get_cache_stats()
    assert stats_reset["hits"] == 0
    assert stats_reset["misses"] == 0
    assert stats_reset["negative_hits"] == 0
    assert stats_reset["hit_ratio"] == 0.0


# ============================================================================
# 2. Dual Invalidation when Extractor Version Changes Tests
# ============================================================================

def test_dual_invalidation_extractor_version_change(tmp_path: Path):
    cache_dir = tmp_path / "cache_store"
    cache = StructuredCache(cache_dir=cache_dir)

    key = "dossier_analysis_001"
    byte_hash = compute_hash("pdf_dossier_content_bytes")
    extractor_hash_v1 = compute_hash("text_cascade_v1.0.0")
    extractor_hash_v2 = compute_hash("text_cascade_v1.1.0_updated")

    payload = {"extracted_entities": ["Autor", "Réu"], "cascade_tier": "tier1"}

    # Store entry with extractor v1
    cache.put(key, byte_hash, extractor_hash_v1, payload)

    # Verify hit with extractor v1
    val1 = cache.get(key, byte_hash, extractor_hash_v1)
    assert val1 == payload

    # Verify disk persistence file exists
    safe_filename = compute_hash(key) + ".json"
    persisted_file = cache_dir / safe_filename
    assert persisted_file.exists()

    # Query with extractor v2 (same byte hash) -> dual invalidation trigger
    val2, status2 = cache.get_with_status(key, byte_hash, extractor_hash_v2)
    assert val2 is None
    assert status2 == "MISS"

    # Crucial assertion: invalidation must remove entry from memory AND disk
    assert key not in cache._memory_cache
    assert not persisted_file.exists()

    # Subsequent query with extractor v1 should now also be a MISS
    val1_retry = cache.get(key, byte_hash, extractor_hash_v1)
    assert val1_retry is None

    # Test dual invalidation on Negative Cache entry when extractor version changes
    cache.put_negative(key, byte_hash, extractor_hash_v1, error_reason="Scan corrupted", ttl=600)
    assert cache.is_negative(key, byte_hash, extractor_hash_v1) is True

    # Query negative entry with extractor v2 -> invalidates negative entry
    val_neg2, status_neg2 = cache.get_with_status(key, byte_hash, extractor_hash_v2)
    assert val_neg2 is None
    assert status_neg2 == "MISS"
    assert cache.is_negative(key, byte_hash, extractor_hash_v1) is False


# ============================================================================
# 3. Job Resumption after Simulated Process Crash Tests
# ============================================================================

@pytest.mark.asyncio
async def test_job_resumption_after_simulated_process_crash(tmp_path: Path):
    storage_dir = tmp_path / "durable_jobs_storage"

    # Step 1: Process 1 initializes JobManager and submits job
    manager1 = JobManager(storage_dir=storage_dir)

    execution_counts = {}

    async def resumable_handler(mgr: JobManager, j_id: str, **payload):
        task_name = payload.get("task_name", "default")
        execution_counts[task_name] = execution_counts.get(task_name, 0) + 1
        mgr.update_job_progress(j_id, 0.5, "Halfway done")
        await asyncio.sleep(0.05)
        return {"completed_by": task_name, "attempt": execution_counts[task_name]}

    manager1.register_job_type("analysis_pipeline", resumable_handler)

    # Submit job
    job1_id = manager1.submit_job(
        "analysis_pipeline",
        resumable_handler,
        payload={"task_name": "process_8001"}
    )
    
    # Wait until job enters RUNNING or COMPLETED
    await asyncio.sleep(0.1)

    # Step 2: Simulate process crash by directly constructing an interrupted job state file on disk
    crashed_job_id = "crashed-job-uuid-1234"
    crashed_job_file = storage_dir / f"{crashed_job_id}.json"
    crashed_job_data = {
        "job_id": crashed_job_id,
        "job_type": "analysis_pipeline",
        "status": "RUNNING",
        "progress": 0.3,
        "payload": {"task_name": "crashed_process_999"},
        "result": None,
        "error": None,
        "created_at": "2026-07-31T20:00:00Z",
        "updated_at": "2026-07-31T20:01:00Z",
    }
    with open(crashed_job_file, "w", encoding="utf-8") as f:
        json.dump(crashed_job_data, f)

    # Step 3: Simulate application crash / restart: instantiate new JobManager (Manager 2)
    manager2 = JobManager(storage_dir=storage_dir)
    
    # Register handler on new process instance
    manager2.register_job_type("analysis_pipeline", resumable_handler)

    # Confirm Manager 2 loaded crashed job as RUNNING from disk
    loaded_crashed_job = manager2.get_job(crashed_job_id)
    assert loaded_crashed_job is not None
    assert loaded_crashed_job.status == JobStatus.RUNNING
    assert loaded_crashed_job.progress == 0.3

    # Step 4: Resume pending / interrupted jobs
    resumed_ids = manager2.resume_pending_jobs()
    assert crashed_job_id in resumed_ids

    # Wait for resumed job to finish execution
    for _ in range(20):
        await asyncio.sleep(0.05)
        if manager2.get_job_status(crashed_job_id) == JobStatus.COMPLETED:
            break

    job_after_resumption = manager2.get_job(crashed_job_id)
    assert job_after_resumption is not None
    assert job_after_resumption.status == JobStatus.COMPLETED
    assert job_after_resumption.progress == 1.0
    assert job_after_resumption.result == {"completed_by": "crashed_process_999", "attempt": 1}

    # Verify disk persistence reflects COMPLETED status
    with open(crashed_job_file, "r", encoding="utf-8") as f:
        persisted_json = json.load(f)
        assert persisted_json["status"] == "COMPLETED"
        assert persisted_json["progress"] == 1.0


# ============================================================================
# 4. HTML Dashboard Element Integrity Tests
# ============================================================================

def test_html_dashboard_element_integrity_and_security():
    # Test complex analysis report with XSS attempts, all 6D completeness metrics, and conflicts
    adversarial_report = {
        "process_number": "<script>alert('xss_proc')</script>0801234-56.2026.8.14.0001",
        "status": "completed",
        "completeness": {
            "overall_score": 0.92,
            "dimensions": {
                "origin": 1.0,
                "documents": 0.95,
                "attachments": 0.90,
                "pages": 0.85,
                "metadata": 0.90,
                "temporal": 1.0,
            },
        },
        "domain_profile": {
            "profile_name": "<b style='color:red'>inventory/v1</b>",
            "entities": {
                "deceased": "João Silva & Cia",
                "heirs": ["Maria Silva", "Pedro Silva"],
            },
        },
        "missing_documents": [
            "<img src=x onerror=alert(1)>Certidão de Casamento",
            "Certidão de Óbito",
        ],
        "timeline": [
            {
                "date": "01/01/2026",
                "title": "Abertura de Inventário",
                "description": "<script>console.log('xss')</script>Petição inicial protocolada",
            }
        ],
        "conflicts": [
            {
                "category": "date_discrepancy",
                "topic": "<iframe src='evil.com'>Data do Óbito</iframe>",
                "status": "abstained",
                "abstention_reason": "Insufficiency of evidence",
            }
        ],
    }

    html_output = generate_html_dashboard(adversarial_report)

    # 1. Structural checks
    assert html_output.startswith("<!DOCTYPE html>")
    assert "</html>" in html_output
    assert "<head>" in html_output
    assert "<body>" in html_output

    # 2. Key Dashboard Sections & 6D Dimensions presence
    assert "Painel de Análise Processual" in html_output
    assert "92%" in html_output
    assert "Origem dos Dados" in html_output
    assert "Cobertura Documental" in html_output
    assert "Integridade dos Anexos" in html_output
    assert "Legibilidade de Páginas" in html_output
    assert "Metadados &amp; Indexação" in html_output or "Metadados & Indexação" in html_output
    assert "Coerência Temporal" in html_output

    # 3. Security XSS Escaping Verification
    assert "<script>alert('xss_proc')</script>" not in html_output
    assert "&lt;script&gt;alert(&#x27;xss_proc&#x27;)&lt;/script&gt;" in html_output or "&lt;script&gt;alert('xss_proc')&lt;/script&gt;" in html_output

    assert "<b style='color:red'>" not in html_output
    assert "&lt;b style=&#x27;color:red&#x27;&gt;" in html_output or "&lt;b style='color:red'&gt;" in html_output

    assert "<img src=x onerror=alert(1)>" not in html_output
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_output

    assert "<script>console.log('xss')</script>" not in html_output
    assert "&lt;script&gt;console.log(&#x27;xss&#x27;)&lt;/script&gt;" in html_output or "&lt;script&gt;console.log('xss')&lt;/script&gt;" in html_output

    assert "<iframe src='evil.com'>" not in html_output
    assert "&lt;iframe src=&#x27;evil.com&#x27;&gt;" in html_output or "&lt;iframe src='evil.com'&gt;" in html_output

    # 4. Explicit Abstention Badges & Status
    assert "ABSTENÇÃO EXPLÍCITA" in html_output
    assert "Insufficiency of evidence" in html_output

    # 5. Empty Edge Case Test
    empty_report = {}
    empty_html = generate_html_dashboard(empty_report)
    assert "<!DOCTYPE html>" in empty_html
    assert "0%" in empty_html
    assert "Nenhum evento registrado" in empty_html
    assert "Zero conflitos ou divergências" in empty_html
    assert "Nenhuma pendência documental" in empty_html
