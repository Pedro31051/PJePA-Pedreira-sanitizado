"""Adversarial stress-testing and empirical verification for M2.

Covers:
1. Corrupt piece SHA-256 detection and missing acquisition hash edge cases.
2. Zero-byte payload integrity detection under both 'tamanho_bytes' and 'size_bytes'.
3. Missing metadata fingerprints and auto-generation fallbacks.
4. Missing attachments evaluation in 6D completeness engine.
5. Explicit ERROR/PARTIAL status flags in tab and document download failures.
6. Reconcile manifest and dossier with deliberate file corruptions/deletions and SQLite re-queuing.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import analise_processual_completa as complete
import auditoria_processual
import manifest
import pje_downloader


class AdversarialM2IntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_key = os.environ.get("PJE_AUDIT_MASTER_KEY")
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")

    @classmethod
    def tearDownClass(cls):
        if cls.old_key is None:
            os.environ.pop("PJE_AUDIT_MASTER_KEY", None)
        else:
            os.environ["PJE_AUDIT_MASTER_KEY"] = cls.old_key

    def test_corrupt_piece_sha256_detection(self):
        """Test SHA-256 mismatch detection and explicit error status handling."""
        entry = {
            "document_id": "doc_101",
            "source_bytes_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "type": "Petição",
        }

        # Case 1: Mismatched SHA-256 hash
        bad_hash_payload = {
            "document_id": "doc_101",
            "source_bytes_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
            "tamanho_bytes": 128,
            "status": "ok",
        }
        is_valid, reason = manifest.verify_piece_integrity(entry, bad_hash_payload)
        self.assertFalse(is_valid)
        self.assertIn("divergência de sha256", reason)

        # Case 2: Explicit error/failed/corrupted status
        for status_val in ("failed", "ERROR", "corrupted"):
            error_payload = {
                "document_id": "doc_101",
                "status": status_val,
                "safe_error": f"Failure with {status_val}",
            }
            is_valid, reason = manifest.verify_piece_integrity(entry, error_payload)
            self.assertFalse(is_valid)
            self.assertEqual(reason, f"Failure with {status_val}")

        # Case 3: Valid hash and size
        valid_payload = {
            "document_id": "doc_101",
            "source_bytes_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "tamanho_bytes": 1024,
            "status": "ok",
        }
        is_valid, reason = manifest.verify_piece_integrity(entry, valid_payload)
        self.assertTrue(is_valid)
        self.assertIsNone(reason)

    def test_zero_byte_payload_detection_bug_analysis(self):
        """Empirically test zero-byte detection behavior for 'size_bytes' vs 'tamanho_bytes'.

        REMEDIATED:
        manifest.py verify_piece_integrity correctly evaluates tamanho_bytes=0 as corrupt (0-byte file).
        """
        entry = {"document_id": "doc_zero", "type": "Anexo"}

        # Using size_bytes=0 (English key) works because None or 0 -> 0
        payload_size_bytes = {"document_id": "doc_zero", "size_bytes": 0, "status": "ok"}
        is_valid_sb, reason_sb = manifest.verify_piece_integrity(entry, payload_size_bytes)
        self.assertFalse(is_valid_sb, "size_bytes=0 should be detected as corrupt")
        self.assertIn("tamanho nulo", reason_sb)

        # Using tamanho_bytes=0 (Portuguese key used in PJe) now correctly detects corrupt file
        payload_tamanho_bytes = {"document_id": "doc_zero", "tamanho_bytes": 0, "status": "ok"}
        is_valid_tb, reason_tb = manifest.verify_piece_integrity(entry, payload_tamanho_bytes)
        self.assertFalse(is_valid_tb, "REMEDIATED: tamanho_bytes=0 is evaluated as 0 and detected as corrupt")
        self.assertIn("tamanho nulo", reason_tb)

    def test_missing_acquired_sha256_bug_analysis(self):
        """Empirically test missing acquired SHA-256 hash when manifest expects SHA-256.

        REMEDIATED:
        manifest.py line 139 enforces that acquired_sha MUST be present when manifest specifies SHA-256.
        """
        entry = {
            "document_id": "doc_sha_req",
            "source_bytes_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "type": "Petição",
        }
        payload_no_sha = {
            "document_id": "doc_sha_req",
            "tamanho_bytes": 1024,
            "status": "ok",
        }

        is_valid, reason = manifest.verify_piece_integrity(entry, payload_no_sha)
        self.assertFalse(is_valid, "REMEDIATED: missing acquired SHA-256 fails integrity check when manifest specifies hash")
        self.assertIn("divergência de sha256", reason)

    def test_missing_metadata_fingerprints_fallback(self):
        """Test missing source_fingerprint generation fallback and modification detection."""
        doc_raw = {
            "id": "doc_202",
            "tipo": "Decisão Interlocutória",
            "data": "2026-03-01T14:30:00+00:00",
            "tamanho_bytes": 5000,
        }

        entry = manifest.build_manifest_entry(doc_raw, canonical_order=1)
        self.assertIn("source_fingerprint", entry)
        self.assertTrue(len(entry["source_fingerprint"]) > 0)

        # Acquired document without explicit source_fingerprint should auto-generate fingerprint
        acquired_same = {
            "id": "doc_202",
            "tipo": "Decisão Interlocutória",
            "data": "2026-03-01T14:30:00+00:00",
            "tamanho_bytes": 5000,
            "status": "ok",
        }

        recon_ok = manifest.reconcile_manifest_and_dossier([entry], [acquired_same])
        self.assertTrue(recon_ok["reconciled"])
        self.assertEqual(len(recon_ok["manifest_delta"]["modified"]), 0)

        # Acquired document with changed metadata should trigger fingerprint modification flag
        acquired_modified = {
            "id": "doc_202",
            "tipo": "Decisão Alterada",
            "data": "2026-03-02T10:00:00+00:00",
            "tamanho_bytes": 5000,
            "status": "ok",
        }

        recon_mod = manifest.reconcile_manifest_and_dossier([entry], [acquired_modified])
        self.assertIn("doc_202", recon_mod["manifest_delta"]["modified"])

    def test_missing_attachments_in_6d_completeness_engine(self):
        """Test that missing or failed attachment documents produce an ATTACHMENTS_INCOMPLETE gap."""
        manifest_docs = [
            {
                "document_id": "1001",
                "type": "Petição Inicial",
                "source_fingerprint": "fp_1001",
            },
            {
                "document_id": "1002",
                "type": "Anexo - Procuração",
                "parent_document_id": "1001",
                "type_code": 2,
                "source_fingerprint": "fp_1002",
            },
            {
                "document_id": "1003",
                "type": "Documento Comprovante",
                "parent_document_id": "1001",
                "source_fingerprint": "fp_1003",
            },
        ]

        doc_1001 = {
            "document_id": "1001",
            "title": "Petição Inicial",
            "type": "Petição Inicial",
            "pages": [{"page": 1, "text": "Excelentíssimo...", "extraction_method": "native_text", "status": "extracted"}],
            "content_sha256": "sha_1001",
            "pages_without_text": 0,
            "truncated": False,
            "source_fingerprint": "fp_1001",
        }
        doc_1003 = {
            "document_id": "1003",
            "title": "Documento Comprovante",
            "type": "Documento Comprovante",
            "pages": [{"page": 1, "text": "Comprovante...", "extraction_method": "native_text", "status": "extracted"}],
            "content_sha256": "sha_1003",
            "pages_without_text": 0,
            "truncated": False,
            "source_fingerprint": "fp_1003",
        }

        states = [
            {"document_id": "1001", "status": "completed", "pages": 1, "pages_without_text": 0, "truncated": False, "duplicate_of": ""},
            {"document_id": "1002", "status": "failed", "safe_error": "Connection timeout", "pages": 0, "pages_without_text": 0, "truncated": False, "duplicate_of": ""},
            {"document_id": "1003", "status": "completed", "pages": 1, "pages_without_text": 0, "truncated": False, "duplicate_of": ""},
        ]

        base = {"movements": [], "parties": []}

        dossier = complete.build_dossier(
            process_number="0000999-99.2026.8.14.0001",
            base=base,
            manifest=manifest_docs,
            documents=[doc_1001, doc_1003],
            document_states_value=states,
            tree_complete=True,
            expedients=[],
            collected_at="2026-07-31T20:00:00+00:00",
        )

        attachments_dim = dossier["completeness"]["attachments"]
        self.assertEqual(attachments_dim["status"], "partial")
        self.assertEqual(attachments_dim["discovered_attachments"], 2)
        self.assertEqual(attachments_dim["failed_attachments"], 1)

        self.assertFalse(dossier["coverage"]["complete"])
        self.assertEqual(dossier["coverage"]["status"], "partial_with_gaps")

        gap_codes = [g["code"] for g in dossier["gap_details"]]
        self.assertIn("ATTACHMENTS_INCOMPLETE", gap_codes)
        self.assertIn("DOCUMENT_READ_FAILED", gap_codes)


class AdversarialDownloaderStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_tab_listing_failure_sets_explicit_error_status(self):
        """Verify tab listing failure sets explicit status='ERROR' and error details."""
        mock_client = MagicMock()
        mock_client.listar_documentos = AsyncMock(
            side_effect=RuntimeError("PJe Tab DOM crash: Session lost")
        )

        res = await pje_downloader.baixar_processo_doc_a_doc(
            mock_client,
            "0000555-55.2026.8.14.0001",
        )

        self.assertEqual(res["status"], "ERROR")
        self.assertIn("PJe Tab DOM crash", res["safe_error"])
        self.assertEqual(res["erros"], 1)
        self.assertEqual(res["total_documentos"], 0)
        self.assertEqual(len(res["documentos_salvos"]), 0)
        self.assertIsNotNone(res["erros_detalhados"])
        self.assertEqual(res["erros_detalhados"][0]["status"], "ERROR")

    async def test_partial_download_failures_set_explicit_partial_status(self):
        """Verify partial piece failures set status='PARTIAL' with detailed error tracking."""
        mock_client = MagicMock()
        mock_client.grau = "1g"
        mock_client.listar_documentos = AsyncMock(
            return_value={
                "arvore_completa": True,
                "documentos": [
                    {"id": "doc_1", "tipo": "Petição Inicial"},
                    {"id": "doc_2", "tipo": "Anexo 1"},
                    {"id": "doc_3", "tipo": "Anexo 2"},
                ],
            }
        )

        async def mock_salvar(client, cnj, doc_id, tipo="", *args, **kwargs):
            if doc_id == "doc_2":
                raise TimeoutError("HTTP 504 Gateway Timeout")
            return {
                "numero_cnj": cnj,
                "documento_id": doc_id,
                "caminho": f"/tmp/{doc_id}.pdf",
                "tamanho_bytes": 2048,
                "tamanho_kb": 2.0,
                "formato": "pdf",
                "sha256": f"hash_{doc_id}",
                "source_fingerprint": f"fp_{doc_id}",
            }

        with patch("pje_downloader.salvar_documento", side_effect=mock_salvar):
            res = await pje_downloader.baixar_processo_doc_a_doc(
                mock_client,
                "0000555-55.2026.8.14.0001",
                concurrency_limit=3,
            )

            self.assertEqual(res["status"], "PARTIAL")
            self.assertEqual(res["total_documentos"], 3)
            self.assertEqual(res["baixados"], 2)
            self.assertEqual(res["erros"], 1)
            self.assertEqual(len(res["documentos_salvos"]), 2)
            self.assertEqual(res["erros_detalhados"][0]["id"], "doc_2")
            self.assertIn("HTTP 504", res["erros_detalhados"][0]["erro"])
            self.assertEqual(res["erros_detalhados"][0]["status"], "ERROR")

    async def test_all_pieces_failing_sets_explicit_error_status(self):
        """Verify that when all document downloads fail, status is 'ERROR'."""
        mock_client = MagicMock()
        mock_client.grau = "1g"
        mock_client.listar_documentos = AsyncMock(
            return_value={
                "arvore_completa": True,
                "documentos": [
                    {"id": "doc_1", "tipo": "Petição"},
                    {"id": "doc_2", "tipo": "Anexo"},
                ],
            }
        )

        with patch("pje_downloader.salvar_documento", side_effect=RuntimeError("Storage offline")):
            res = await pje_downloader.baixar_processo_doc_a_doc(
                mock_client,
                "0000555-55.2026.8.14.0001",
            )

            self.assertEqual(res["status"], "ERROR")
            self.assertEqual(res["total_documentos"], 2)
            self.assertEqual(res["baixados"], 0)
            self.assertEqual(res["erros"], 2)
            self.assertEqual(len(res["erros_detalhados"]), 2)


class AdversarialReconciliationDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_key = os.environ.get("PJE_AUDIT_MASTER_KEY")
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(b"d" * 32).decode("ascii")

    @classmethod
    def tearDownClass(cls):
        if cls.old_key is None:
            os.environ.pop("PJE_AUDIT_MASTER_KEY", None)
        else:
            os.environ["PJE_AUDIT_MASTER_KEY"] = cls.old_key

    def test_reconcile_manifest_deliberate_corruption_and_sqlite_requeue(self):
        """Test reconcile_manifest_and_dossier with missing/corrupted files and verify SQLite re-queuing."""
        # 1. Create a persistent analysis job in SQLite
        job = complete.create_job(
            process_number="0000777-77.2026.8.14.0001",
            grau="1g",
            persona="advogado",
            mode="integral",
            semantic_enabled=False,
            force_reread=False,
            authorisation_ref="auth-ref-777",
        )
        job_id = job["job_id"]

        # 2. Build initial manifest with 3 items
        manifest_items = [
            {
                "document_id": "piece_1",
                "type": "Petição Inicial",
                "source_bytes_sha256": "sha_valid_1",
                "source_fingerprint": "fp_piece_1",
            },
            {
                "document_id": "piece_2",
                "type": "Anexo Deletado",
                "source_bytes_sha256": "sha_valid_2",
                "source_fingerprint": "fp_piece_2",
            },
            {
                "document_id": "piece_3",
                "type": "Certidão Corrompida",
                "source_bytes_sha256": "sha_valid_3",
                "source_fingerprint": "fp_piece_3",
            },
        ]

        # 3. Simulate acquired documents:
        # - piece_1: valid
        # - piece_2: deleted / missing
        # - piece_3: corrupt SHA-256
        acquired_docs = [
            {
                "document_id": "piece_1",
                "type": "Petição Inicial",
                "source_bytes_sha256": "sha_valid_1",
                "source_fingerprint": "fp_piece_1",
                "tamanho_bytes": 1024,
                "status": "ok",
            },
            # piece_2 is omitted (missing)
            {
                "document_id": "piece_3",
                "type": "Certidão Corrompida",
                "source_bytes_sha256": "sha_WRONG_3",
                "source_fingerprint": "fp_piece_3",
                "tamanho_bytes": 512,
                "status": "ok",
            },
        ]

        # 4. Perform reconciliation with automatic SQLite re-queuing
        recon = manifest.reconcile_manifest_and_dossier(
            manifest_items, acquired_docs, job_id=job_id
        )

        # 5. Assertions on reconciliation result
        self.assertFalse(recon["reconciled"])
        self.assertEqual(recon["status"], "reconciliation_failed")
        self.assertEqual(sorted(recon["missing_pieces"]), ["piece_2"])
        self.assertEqual(sorted(recon["corrupted_pieces"]), ["piece_3"])
        self.assertEqual(sorted(recon["requeued_pieces"]), ["piece_2", "piece_3"])

        # 6. Verify SQLite database persistence of requeued pieces
        states = complete.document_states(job_id)
        states_by_id = {s["document_id"]: s for s in states}

        self.assertEqual(len(states), 2)

        # Check piece_2 (missing/deleted) -> status='queued'
        self.assertIn("piece_2", states_by_id)
        self.assertEqual(states_by_id["piece_2"]["status"], "queued")
        self.assertEqual(states_by_id["piece_2"]["safe_error"], "requeued_for_reacquisition")

        # Check piece_3 (corrupt SHA-256) -> status='queued'
        self.assertIn("piece_3", states_by_id)
        self.assertEqual(states_by_id["piece_3"]["status"], "queued")
        self.assertEqual(states_by_id["piece_3"]["safe_error"], "requeued_for_reacquisition")


class ConsolidatedPdfJobWALTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_wal_job_persistence_and_coalescing(self):
        """Test persistent SQLite WAL job creation, request coalescing, status queries and PDF integrity."""
        import asyncio
        mock_client = MagicMock()
        mock_client.grau = "1g"

        async def slow_download(*args, **kwargs):
            await asyncio.sleep(0.3)
            return {
                "caminho": "/tmp/test_coalesce.pdf",
                "tamanho_bytes": 2048,
                "tamanho_mb": 0.0,
                "sha256": "abc_hash_123",
            }

        cnj = "0000888-88.2026.8.14.0001"

        with pje_downloader._downloader_db() as conn:
            conn.execute("DELETE FROM consolidated_pdf_jobs WHERE process_cnj = ?", (pje_downloader.cnj_safe(cnj),))

        with patch("pje_downloader.GRACA_SINCRONA_S", 0.1), \
             patch("pje_downloader.baixar_processo_completo", side_effect=slow_download):
            res1 = await pje_downloader.baixar_processo_background(
                mock_client, cnj, cronologia="decrescente"
            )
            self.assertIn("job_id", res1)
            job_id_1 = res1["job_id"]
            self.assertEqual(res1["status"], "em_andamento")

            res2 = await pje_downloader.baixar_processo_background(
                mock_client, cnj, cronologia="decrescente"
            )
            self.assertTrue(res2.get("ja_estava_em_andamento"))
            self.assertEqual(res2["job_id"], job_id_1)

            jobs = pje_downloader.listar_jobs(incluir_concluidos=True)
            self.assertGreaterEqual(jobs["total_jobs"], 1)
            active_jobs = [j for j in jobs["jobs"] if j["job_id"] == job_id_1]
            self.assertEqual(len(active_jobs), 1)

            if job_id_1 in pje_downloader._ACTIVE_TASKS:
                await pje_downloader._ACTIVE_TASKS[job_id_1]
            else:
                await asyncio.sleep(0.5)

            status_after = pje_downloader.status_download(cnj, "1g")
            self.assertEqual(status_after["status"], "concluido")
            self.assertEqual(status_after["job_id"], job_id_1)
            self.assertEqual(status_after["sha256"], "abc_hash_123")

    def test_physical_pdf_integrity_validation(self):
        """Test physical PDF validation refusing invalid headers, missing EOF, or truncated bytes."""
        valid_pdf = b"%PDF-1.4\n" + b"A" * 1050 + b"\n%%EOF\n"
        integro, motivo = pje_downloader.conferir_conteudo(valid_pdf, "application/pdf")
        self.assertTrue(integro)
        self.assertIsNone(motivo)

        invalid_header = b"NOT_PDF_HEADER\n" + b"A" * 1050 + b"\n%%EOF\n"
        integro_h, motivo_h = pje_downloader.conferir_conteudo(invalid_header, "application/pdf")
        self.assertFalse(integro_h)
        self.assertIn("não começa com '%PDF-'", motivo_h)

        missing_eof = b"%PDF-1.4\n" + b"A" * 1050 + b"\nNO_FOOTER\n"
        integro_f, motivo_f = pje_downloader.conferir_conteudo(missing_eof, "application/pdf")
        self.assertFalse(integro_f)
        self.assertIn("sem marcador de fim '%%EOF'", motivo_f)

        truncated_pdf = b"%PDF-1.4\n%%EOF\n"
        integro_t, motivo_t = pje_downloader.conferir_conteudo(truncated_pdf, "application/pdf")
        self.assertFalse(integro_t)
        self.assertIn("truncado", motivo_t)


class EmpiricalProcessRestartAndWALTests(unittest.TestCase):
    """Empirical verification of SQLite WAL persistence across process restarts."""

    @classmethod
    def setUpClass(cls):
        cls.old_key = os.environ.get("PJE_AUDIT_MASTER_KEY")
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(b"w" * 32).decode("ascii")

    @classmethod
    def tearDownClass(cls):
        if cls.old_key is None:
            os.environ.pop("PJE_AUDIT_MASTER_KEY", None)
        else:
            os.environ["PJE_AUDIT_MASTER_KEY"] = cls.old_key

    def test_sqlite_wal_persistence_across_process_restart(self):
        """Verify background PDF jobs persist in SQLite WAL database and survive process restart."""
        cnj = "0000999-11.2026.8.14.0001"
        cnj_clean = pje_downloader.cnj_safe(cnj)
        job_id = "job_restart_test_123"
        now_iso = pje_downloader._now_iso()

        # Insert a job directly into SQLite WAL table
        with pje_downloader._downloader_db() as conn:
            conn.execute("DELETE FROM consolidated_pdf_jobs WHERE process_cnj = ?", (cnj_clean,))
            conn.execute(
                """
                INSERT INTO consolidated_pdf_jobs
                    (job_id, process_cnj, grau, cronologia, status, caminho_pdf, tamanho_bytes, tamanho_mb, sha256, created_at, updated_at, completed_at, worker_id)
                VALUES (?, ?, '1g', 'decrescente', 'completed', '/tmp/pdf_restart.pdf', 4096, 0.004, 'sha256_restart_val', ?, ?, ?, 'worker_m2')
                """,
                (job_id, cnj_clean, now_iso, now_iso, now_iso),
            )

        # Clear in-memory cache structures
        pje_downloader._JOBS.clear()
        pje_downloader._ACTIVE_TASKS.clear()

        # Inspect raw SQLite database directly using independent sqlite3 connection
        db_path = auditoria_processual.database_path()
        raw_conn = sqlite3.connect(db_path)
        raw_conn.row_factory = sqlite3.Row
        try:
            cursor = raw_conn.execute(
                "SELECT * FROM consolidated_pdf_jobs WHERE job_id = ?", (job_id,)
            )
            row = cursor.fetchone()
            self.assertIsNotNone(row, "Job must exist in SQLite database")
            self.assertEqual(row["process_cnj"], cnj_clean)
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["sha256"], "sha256_restart_val")
            self.assertEqual(row["worker_id"], "worker_m2")
        finally:
            raw_conn.close()

        # Verify status_download recovers job from SQLite WAL without memory state
        status = pje_downloader.status_download(cnj, "1g")
        self.assertEqual(status["status"], "concluido")
        self.assertEqual(status["job_id"], job_id)
        self.assertEqual(status["sha256"], "sha256_restart_val")

        # Verify listar_jobs recovers job from SQLite WAL
        jobs_list = pje_downloader.listar_jobs(incluir_concluidos=True)
        found = [j for j in jobs_list["jobs"] if j["job_id"] == job_id]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "concluido")
        self.assertEqual(found[0]["caminho"], "/tmp/pdf_restart.pdf")


class EmpiricalHighConcurrencyRequestCoalescingTests(unittest.IsolatedAsyncioTestCase):
    """Empirical verification of Request Coalescing under high concurrency (N=50)."""

    async def test_n_50_parallel_requests_coalesce_to_single_job(self):
        """Verify N=50 parallel requests for same process PDF return exact same job_id without duplicate downloads."""
        import asyncio
        mock_client = MagicMock()
        mock_client.grau = "1g"

        download_counter = {"count": 0}

        async def mock_download_func(*args, **kwargs):
            download_counter["count"] += 1
            await asyncio.sleep(0.4)
            return {
                "caminho": "/tmp/coalesce_50.pdf",
                "tamanho_bytes": 8192,
                "tamanho_mb": 0.01,
                "sha256": "coalesce_50_hash",
            }

        cnj = "0000777-55.2026.8.14.0001"
        cnj_clean = pje_downloader.cnj_safe(cnj)

        with pje_downloader._downloader_db() as conn:
            conn.execute("DELETE FROM consolidated_pdf_jobs WHERE process_cnj = ?", (cnj_clean,))

        with patch("pje_downloader.GRACA_SINCRONA_S", 0.05), \
             patch("pje_downloader.baixar_processo_completo", side_effect=mock_download_func):
            
            # Fire 50 simultaneous parallel requests
            tasks = [
                pje_downloader.baixar_processo_background(mock_client, cnj, cronologia="decrescente")
                for _ in range(50)
            ]
            responses = await asyncio.gather(*tasks)

        self.assertEqual(len(responses), 50)
        job_ids = set(r["job_id"] for r in responses)
        self.assertEqual(len(job_ids), 1, f"Expected 1 unique job_id across 50 requests, got {len(job_ids)}: {job_ids}")

        coalesced_count = sum(1 for r in responses if r.get("ja_estava_em_andamento") is True)
        self.assertGreaterEqual(coalesced_count, 49, f"Expected at least 49 coalesced responses, got {coalesced_count}")

        # Wait for background task to finish
        job_id_val = list(job_ids)[0]
        if job_id_val in pje_downloader._ACTIVE_TASKS:
            await pje_downloader._ACTIVE_TASKS[job_id_val]

        self.assertEqual(download_counter["count"], 1, "baixar_processo_completo must be called exactly ONCE")

        # Verify only 1 row exists in SQLite table
        with pje_downloader._downloader_db() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM consolidated_pdf_jobs WHERE process_cnj = ?", (cnj_clean,))
            count = cursor.fetchone()[0]
            self.assertEqual(count, 1)

    async def test_degree_isolation_coalescing(self):
        """Verify 1g and 2g requests for same CNJ form isolated jobs without cross-degree collision."""
        import asyncio
        client_1g = MagicMock()
        client_1g.grau = "1g"
        client_2g = MagicMock()
        client_2g.grau = "2g"

        async def mock_download(*args, **kwargs):
            await asyncio.sleep(0.2)
            return {"caminho": "/tmp/dummy.pdf", "tamanho_bytes": 1024, "tamanho_mb": 0.0, "sha256": "h"}

        cnj = "0000333-22.2026.8.14.0001"
        cnj_clean = pje_downloader.cnj_safe(cnj)

        with pje_downloader._downloader_db() as conn:
            conn.execute("DELETE FROM consolidated_pdf_jobs WHERE process_cnj = ?", (cnj_clean,))

        with patch("pje_downloader.GRACA_SINCRONA_S", 0.01), \
             patch("pje_downloader.baixar_processo_completo", side_effect=mock_download):
            
            res_1g, res_2g = await asyncio.gather(
                pje_downloader.baixar_processo_background(client_1g, cnj),
                pje_downloader.baixar_processo_background(client_2g, cnj),
            )

        self.assertNotEqual(res_1g["job_id"], res_2g["job_id"], "1g and 2g must have distinct job_ids")

    async def test_retry_after_failed_job(self):
        """Verify that after a job fails, a new request launches a new job rather than returning the failed job."""
        import asyncio
        mock_client = MagicMock()
        mock_client.grau = "1g"

        call_count = {"count": 0}

        async def failing_then_success_download(*args, **kwargs):
            call_count["count"] += 1
            if call_count["count"] == 1:
                raise RuntimeError("TJPA server 503")
            return {"caminho": "/tmp/retry_success.pdf", "tamanho_bytes": 2048, "tamanho_mb": 0.0, "sha256": "retry_sha"}

        cnj = "0000444-11.2026.8.14.0001"
        cnj_clean = pje_downloader.cnj_safe(cnj)

        with pje_downloader._downloader_db() as conn:
            conn.execute("DELETE FROM consolidated_pdf_jobs WHERE process_cnj = ?", (cnj_clean,))

        with patch("pje_downloader.GRACA_SINCRONA_S", 0.01), \
             patch("pje_downloader.baixar_processo_completo", side_effect=failing_then_success_download):
            
            res1 = await pje_downloader.baixar_processo_background(mock_client, cnj)
            job_1 = res1["job_id"]
            await asyncio.sleep(0.1)  # allow task 1 to finish and fail

            st1 = pje_downloader.status_download(cnj, "1g")
            self.assertEqual(st1["status"], "erro")

            # Second request should NOT coalesce into failed job, but start a new job
            res2 = await pje_downloader.baixar_processo_background(mock_client, cnj)
            job_2 = res2["job_id"]
            self.assertNotEqual(job_1, job_2, "Retry request must spawn a new job_id after failure")


if __name__ == "__main__":
    unittest.main()
