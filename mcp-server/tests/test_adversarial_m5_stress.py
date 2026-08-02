"""Adversarial & Stress Test Suite for M5 Modules (`test_adversarial_m5_stress.py`).

Stress tests targeting M5 modules:
1. StructuredCache: race conditions, invalidation under concurrency, expired negative cache keys, corrupted disk cache files, invalid/string created_at timestamps.
2. Durable Jobs: job crashes, interrupted jobs, concurrent job updates, corrupted state JSON files, PDF consolidation edge cases.
3. Dashboard UI & CLI Review Panel: rendering empty, extreme (NaN/Inf), special character / XSS, unicode inputs.
4. MetricsCollector: zero, extreme, NaN telemetry tracking and summary reporting.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from pathlib import Path

import pytest

from durable_jobs import Job, JobManager, JobStatus, consolidate_process_pdf_job
from generate_pje_dashboard import generate_html_dashboard
from metrics_collector import MetricsCollector
from painel_revisao import PainelRevisao
from structured_cache import CacheEntry, StructuredCache

# ============================================================================
# 1. StructuredCache Stress & Adversarial Tests
# ============================================================================

def test_cache_invalidation_race_conditions():
    """Stress test StructuredCache under high concurrent reads, writes, invalidations, and clears."""
    cache = StructuredCache()
    errors = []

    def writer():
        for i in range(500):
            try:
                cache.put(f"key_{i % 20}", f"byte_hash_{i % 5}", f"ext_hash_{i % 5}", f"value_{i}")
                cache.put_negative(f"neg_{i % 20}", f"byte_hash_{i % 5}", f"ext_hash_{i % 5}", error_reason=f"err_{i}")
            except Exception as e:
                errors.append(e)

    def invalidator():
        for i in range(500):
            try:
                cache.invalidate(f"key_{i % 20}")
                if i % 10 == 0:
                    cache.clear()
            except Exception as e:
                errors.append(e)

    def reader():
        for i in range(500):
            try:
                cache.get(f"key_{i % 20}", f"byte_hash_{i % 5}", f"ext_hash_{i % 5}")
                cache.get_with_status(f"neg_{i % 20}", f"byte_hash_{i % 5}", f"ext_hash_{i % 5}")
                cache.is_negative(f"neg_{i % 20}", f"byte_hash_{i % 5}", f"ext_hash_{i % 5}")
                cache.get_cache_stats()
            except Exception as e:
                errors.append(e)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=invalidator),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Concurrent StructuredCache access produced errors: {errors[:5]}"


def test_cache_key_error_race_invalidation():
    """Test race condition where key is invalidated concurrently while get_with_status is checking hash mismatch."""
    cache = StructuredCache()
    errors = []

    class SlowHashEntry(CacheEntry):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._bh = kwargs.get("byte_hash", "hash_slow")

        @property
        def byte_hash(self):
            time.sleep(0.002)
            return self._bh

        @byte_hash.setter
        def byte_hash(self, val):
            self._bh = val

    entry = SlowHashEntry(key="race_key", value="val", byte_hash="hash_slow", extractor_hash="ext")
    cache._memory_cache["race_key"] = entry

    def reader():
        try:
            cache.get_with_status("race_key", "hash_mismatch", "ext")
        except Exception as e:
            errors.append(e)

    def invalidator():
        time.sleep(0.001)
        cache.invalidate("race_key")

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=invalidator)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # If KeyError is raised, it indicates unguarded dict deletion during concurrent invalidation
    key_errors = [e for e in errors if isinstance(e, KeyError)]
    assert len(key_errors) == 0, f"KeyError raised during cache invalidation race condition: {key_errors}"


def test_cache_expired_negative_keys():
    """Test behavior of expired negative cache keys and invalid TTL values."""
    cache = StructuredCache(default_negative_ttl=0.01)

    # 1. Short TTL negative entry
    cache.put_negative("neg_key_1", "b_hash", "e_hash", error_reason="Temporary timeout", ttl=0.01)

    assert cache.is_negative("neg_key_1", "b_hash", "e_hash") is True
    val, status = cache.get_with_status("neg_key_1", "b_hash", "e_hash")
    assert status == "NEGATIVE_HIT"

    # Wait for TTL expiration
    time.sleep(0.02)

    assert cache.is_negative("neg_key_1", "b_hash", "e_hash") is False
    val, status = cache.get_with_status("neg_key_1", "b_hash", "e_hash")
    assert status == "MISS"
    assert val is None

    # 2. Negative TTL & zero TTL
    cache.put_negative("neg_key_zero", "b_hash", "e_hash", ttl=0.0)
    assert cache.is_negative("neg_key_zero", "b_hash", "e_hash") is False
    val, status = cache.get_with_status("neg_key_zero", "b_hash", "e_hash")
    assert status == "MISS"


def test_cache_corrupted_disk_files():
    """Test StructuredCache robustness against corrupted, malformed, or invalid disk cache files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_dir = Path(tmpdir)

        # Write corrupted files
        (cache_dir / "malformed.json").write_text("{corrupted_json: [", encoding="utf-8")
        (cache_dir / "empty.json").write_text("", encoding="utf-8")
        (cache_dir / "array.json").write_text("[1, 2, 3]", encoding="utf-8")
        (cache_dir / "scalar.json").write_text('"just a string"', encoding="utf-8")
        (cache_dir / "missing_fields.json").write_text('{"value": 123}', encoding="utf-8")
        (cache_dir / "binary.json").write_bytes(b"\x00\xff\xfe\xfd\x12\x34")

        # Initializing cache should not raise unhandled exceptions
        cache = StructuredCache(cache_dir=cache_dir)
        stats = cache.get_cache_stats()
        assert stats is not None

        # Operations should succeed without crashing
        cache.put("valid_key", "bh", "eh", {"data": "test"})
        val, status = cache.get_with_status("valid_key", "bh", "eh")
        assert status == "HIT"
        assert val == {"data": "test"}

        # Clearing cache should clean up valid & invalid files safely
        cache.clear()


def test_cache_string_created_at_deserialization():
    """Test CacheEntry behavior when created_at is deserialized as ISO string from JSON."""
    entry = CacheEntry.from_dict({
        "key": "k_iso",
        "value": "val",
        "byte_hash": "bh",
        "extractor_hash": "eh",
        "created_at": "2026-07-31T20:00:00Z",
        "ttl": 600.0,
    })

    # is_expired should handle string created_at or raise TypeError
    try:
        expired = entry.is_expired()
        assert isinstance(expired, bool)
    except TypeError as e:
        pytest.fail(f"CacheEntry.is_expired raised TypeError for string created_at: {e}")


# ============================================================================
# 2. Durable Jobs Stress & Adversarial Tests
# ============================================================================

@pytest.mark.asyncio
async def test_durable_job_crashes_and_interrupted_jobs():
    """Test JobManager under worker exception crashes and recovery of interrupted jobs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = JobManager(storage_dir=tmpdir)

        # Register handler that throws arbitrary exceptions
        async def crashing_job(manager, job_id, fail_type="runtime"):
            if fail_type == "runtime":
                raise RuntimeError("Worker process crashed unexpectedly!")
            elif fail_type == "zero_div":
                _ = 1 / 0
            elif fail_type == "custom":
                raise ValueError("Invalid payload state")

        mgr.register_job_type("crashing", crashing_job)

        # 1. Submit crashing jobs
        job1_id = mgr.submit_job("crashing", crashing_job, payload={"fail_type": "runtime"})
        job2_id = mgr.submit_job("crashing", crashing_job, payload={"fail_type": "zero_div"})

        # Let background tasks run
        await asyncio.sleep(0.05)

        j1 = mgr.get_job(job1_id)
        assert j1 is not None
        assert j1.status == JobStatus.FAILED
        assert "Worker process crashed unexpectedly!" in str(j1.error)

        j2 = mgr.get_job(job2_id)
        assert j2 is not None
        assert j2.status == JobStatus.FAILED

        # 2. Interrupted job recovery across manager restarts
        interrupted_job = Job(
            job_id="interrupted_123",
            job_type="crashing",
            status=JobStatus.PENDING,
            payload={"fail_type": "none"}
        )
        mgr.save_job_to_disk(interrupted_job)

        # Re-initialize JobManager (simulating service restart)
        mgr2 = JobManager(storage_dir=tmpdir)

        async def safe_job(fail_type="none"):
            return "recovered_ok"

        mgr2.register_job_type("crashing", safe_job)
        resumed_ids = mgr2.resume_pending_jobs()

        assert "interrupted_123" in resumed_ids
        await asyncio.sleep(0.05)

        j_recovered = mgr2.get_job("interrupted_123")
        assert j_recovered is not None
        assert j_recovered.status == JobStatus.COMPLETED
        assert j_recovered.result == "recovered_ok"


@pytest.mark.asyncio
async def test_concurrent_job_updates():
    """Stress test JobManager with concurrent async job submissions, updates, and cancellations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = JobManager(storage_dir=tmpdir)

        async def async_worker(manager, job_id, steps=5):
            for i in range(steps):
                manager.update_job_progress(job_id, (i + 1) / steps, f"Step {i+1}")
                await asyncio.sleep(0.005)
            return f"job_{job_id}_done"

        mgr.register_job_type("async_task", async_worker)
        job_ids = []

        # Concurrently submit jobs
        for i in range(20):
            jid = mgr.submit_job("async_task", async_worker, steps=5)
            job_ids.append(jid)

        await asyncio.sleep(0.01)
        for jid in job_ids[:5]:
            mgr.cancel_job(jid)

        await asyncio.sleep(0.05)

        for jid in job_ids[5:]:
            job = mgr.get_job(jid)
            assert job is not None
            assert job.status in (JobStatus.RUNNING, JobStatus.COMPLETED)


def test_corrupted_state_json_files():
    """Test JobManager loading corrupted, malformed, or partial state JSON files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        storage_dir = Path(tmpdir)

        # Write corrupted job files
        (storage_dir / "bad1.json").write_text("{invalid_json...", encoding="utf-8")
        (storage_dir / "bad2.json").write_text("", encoding="utf-8")
        (storage_dir / "bad3.json").write_text("[]", encoding="utf-8")
        (storage_dir / "bad4.json").write_text('{"no_job_id": true}', encoding="utf-8")
        (storage_dir / "bad5.json").write_text(
            '{"job_id": "job_bad_enum", "job_type": "test", "status": "UNKNOWN_ENUM_VAL", "progress": "bad_float"}',
            encoding="utf-8"
        )

        # Manager should load valid jobs and skip corrupted files cleanly
        mgr = JobManager(storage_dir=storage_dir)
        all_jobs = mgr.list_jobs()
        assert isinstance(all_jobs, list)


@pytest.mark.asyncio
async def test_pdf_consolidation_corrupted_documents():
    """Test PDF consolidation background job under corrupted, empty, or missing PDF files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = JobManager(storage_dir=tmpdir)
        base = Path(tmpdir)

        # Prepare corrupted document files
        missing_file = base / "non_existent.pdf"
        zero_byte_file = base / "zero_byte.pdf"
        zero_byte_file.write_bytes(b"")

        garbage_file = base / "garbage.pdf"
        garbage_file.write_bytes(b"THIS IS NOT A VALID PDF CONTENT AT ALL")

        output_pdf = base / "output_consolidated.pdf"

        # Execute consolidation job
        result = await consolidate_process_pdf_job(
            job_manager=mgr,
            job_id="test_pdf_job",
            process_number="0001234-56.2026.8.14.0001",
            document_files=[missing_file, zero_byte_file, garbage_file],
            output_pdf_path=output_pdf
        )

        assert result is not None
        assert result["process_number"] == "0001234-56.2026.8.14.0001"
        assert output_pdf.exists()
        assert output_pdf.stat().st_size > 0


# ============================================================================
# 3. HTML Dashboard UI & PainelRevisao Stress & Adversarial Tests
# ============================================================================

def test_dashboard_empty_inputs():
    """Test generate_html_dashboard and PainelRevisao with completely empty dictionary inputs."""
    empty_report = {}

    # 1. HTML Dashboard
    html_out = generate_html_dashboard(empty_report)
    assert isinstance(html_out, str)
    assert "<!DOCTYPE html>" in html_out
    assert "N/A" in html_out or "Desconhecido" in html_out or "0%" in html_out

    # 2. PainelRevisao CLI
    panel = PainelRevisao(empty_report)
    summary = panel.render_summary()
    completeness = panel.render_completeness()
    domain = panel.render_domain_profile()
    conflicts = panel.render_conflicts()
    timeline = panel.render_timeline()
    full_report = panel.render_full_report()

    assert "PAINEL DE REVISÃO PROCESSUAL" in summary
    assert "0%" in completeness
    assert isinstance(full_report, str)


def test_dashboard_extreme_and_special_character_inputs():
    """Test dashboard generator and PainelRevisao under extreme numerical and XSS injection inputs."""
    adversarial_report = {
        "process_number": "<script>alert('XSS_PROC')</script>",
        "status": "completed_with_<b>bold</b>",
        "completeness": {
            "overall_score": 0.85,
            "dimensions": {
                "origin": 0.95,
                "documents": 0.80,
                "attachments": 0.70,
                "pages": 0.90,
                "metadata": 1.0,
                "temporal": 0.88,
            }
        },
        "domain_profile": {
            "profile_name": "inventory/v1_<img src=x onerror=alert(1)>",
            "details": {
                "falecido": "João da Silva & Cia <script>",
                "bens": ["Imóvel R$ 500.000,00", "<svg onload=alert(2)>"]
            }
        },
        "missing_documents": [
            "<script>alert('MISSING')</script>",
            {"description": "Certidão de Óbito & Registro '<>"}
        ],
        "timeline": [
            {
                "date": "01/01/2026",
                "title": "Petição Inicial <iframe src='javascript:alert(1)'>",
                "description": "Ajuizamento com texto especial: ⚖️ 🚀 🔥 & < > ' \""
            }
        ],
        "conflicts": [
            {
                "category": "Conflito de Hereditariedade",
                "topic": "Divergência de herdeiros <script>",
                "status": "abstained",
                "abstention_reason": "Provas documentais colidentes & irreconciliáveis"
            }
        ]
    }

    # 1. HTML Dashboard rendering
    html_out = generate_html_dashboard(adversarial_report)

    # Verification: Raw script tags MUST be escaped in HTML output
    assert "<script>alert('XSS_PROC')</script>" not in html_out
    assert "&lt;script&gt;alert(&#x27;XSS_PROC&#x27;)&lt;/script&gt;" in html_out or "&lt;script&gt;alert(&#39;XSS_PROC&#39;)&lt;/script&gt;" in html_out or "alert" in html_out
    assert "<iframe src=" not in html_out
    assert "<svg onload=" not in html_out

    # 2. PainelRevisao CLI rendering
    panel = PainelRevisao(adversarial_report)
    full_text = panel.render_full_report()

    assert "João da Silva" in full_text
    assert "Motivo da Abstenção Explícita:" in full_text
    assert "Provas documentais colidentes" in full_text


def test_dashboard_nan_and_inf_handling():
    """Test dashboard generator and PainelRevisao with float NaN and Inf completeness scores."""
    nan_report = {"completeness": float("nan")}
    inf_report = {"completeness": float("inf")}

    # Dashboard should handle NaN and Inf gracefully without throwing ValueError / OverflowError
    try:
        html_nan = generate_html_dashboard(nan_report)
        assert isinstance(html_nan, str)
    except Exception as e:
        pytest.fail(f"generate_html_dashboard raised exception on NaN completeness: {e}")

    try:
        html_inf = generate_html_dashboard(inf_report)
        assert isinstance(html_inf, str)
    except Exception as e:
        pytest.fail(f"generate_html_dashboard raised exception on Inf completeness: {e}")

    try:
        panel_nan = PainelRevisao(nan_report).render_full_report()
        assert isinstance(panel_nan, str)
    except Exception as e:
        pytest.fail(f"PainelRevisao raised exception on NaN completeness: {e}")


# ============================================================================
# 4. MetricsCollector Stress Tests
# ============================================================================

def test_metrics_collector_stress():
    """Test MetricsCollector under zero, extreme, and negative values."""
    collector = MetricsCollector()

    # Empty summary
    summary_empty = collector.get_metrics_summary()
    assert summary_empty["technical_metrics"]["ocr_fallback_rate"] == 0.0
    assert summary_empty["technical_metrics"]["cache_efficiency"] == 0.0
    assert summary_empty["domain_metrics"]["average_completeness_score"] == 0.0

    # Record data
    collector.record_extraction_time("tier1_native", 0.12)
    collector.record_extraction_time("tier1_native", 0.24)
    collector.record_ocr_execution(fallback_occurred=False)
    collector.record_ocr_execution(fallback_occurred=True)
    collector.record_cache_access("hit")
    collector.record_cache_access("miss")
    collector.record_cache_access("negative_hit")
    collector.record_completeness_score(0.85)
    collector.record_completeness_score(0.95)
    collector.record_missing_requirements(["Certidão de Óbito", "Procuração"])
    collector.record_domain_profile("inventory/v1")

    summary = collector.get_metrics_summary()
    tech = summary["technical_metrics"]
    dom = summary["domain_metrics"]

    assert tech["ocr_fallback_rate"] == 0.5
    assert tech["cache_efficiency"] == round(2 / 3, 4)
    assert dom["average_completeness_score"] == 0.90
    assert len(dom["top_missing_requirements"]) == 2

    # Reset
    collector.reset()
    assert collector.get_metrics_summary()["domain_metrics"]["total_analyses_evaluated"] == 0
