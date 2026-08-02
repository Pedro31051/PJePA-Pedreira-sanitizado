"""Unit and integration tests for Manifest V2, Individual Download, 6D Completeness Engine, and Final Reconciliation.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import analise_processual_completa as complete
import manifest
import pje_downloader


class ManifestV2CreationTests(unittest.TestCase):
    def test_manifest_v2_schema_and_fingerprint(self):
        doc = {
            "id": "100200",
            "tipo": "Petição Inicial",
            "tipo_codigo": 1,
            "titulo": "Petição Inicial com Anexo",
            "data": "2026-01-15T10:00:00+00:00",
            "autor": "Advogado Autor",
            "mime_type": "application/pdf",
            "tamanho_bytes": 10240,
            "sigilo": False,
            "ativo": True,
            "assinado": True,
            "custom_metadata_field": "test_value",
        }
        entry = manifest.build_manifest_entry(doc, canonical_order=1)

        self.assertEqual(entry["schema_version"], "pje.document-manifest/v2")
        self.assertEqual(entry["document_id"], "100200")
        self.assertIsNone(entry["parent_document_id"])
        self.assertEqual(entry["canonical_order"], 1)
        self.assertEqual(entry["type"], "Petição Inicial")
        self.assertEqual(entry["title"], "Petição Inicial com Anexo")
        self.assertEqual(entry["size_bytes"], 10240)
        self.assertIsNotNone(entry["source_fingerprint"])
        self.assertEqual(entry["source_fields"]["custom_metadata_field"], "test_value")

    def test_build_manifest_from_tree(self):
        docs = [
            {"id": "doc1", "tipo": "Petição"},
            {"id": "doc2", "tipo": "Certidão", "parent_document_id": "doc1"},
        ]
        manifest_tree = manifest.build_manifest_from_tree(docs)

        self.assertEqual(len(manifest_tree), 2)
        self.assertEqual(manifest_tree[0]["canonical_order"], 1)
        self.assertEqual(manifest_tree[1]["canonical_order"], 2)
        self.assertEqual(manifest_tree[1]["parent_document_id"], "doc1")

    def test_compute_manifest_sha256_is_deterministic(self):
        docs = [{"id": "1", "tipo": "Petição"}, {"id": "2", "tipo": "Sentença"}]
        manifest_a = manifest.build_manifest_from_tree(docs)
        manifest_b = manifest.build_manifest_from_tree(docs)

        hash_a = manifest.compute_manifest_sha256(manifest_a)
        hash_b = manifest.compute_manifest_sha256(manifest_b)

        self.assertEqual(hash_a, hash_b)
        self.assertEqual(len(hash_a), 64)


class IndividualDownloadAndTabErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_salvar_documento_captures_sha256_and_fingerprint(self):
        mock_client = MagicMock()
        mock_client.grau = "1g"
        mock_client.garantir_processo_id = AsyncMock()

        sample_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n" + b"X" * 1050 + b"\ntrailer\n<<>>\nstartxref\n100\n%%EOF\n"
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.headers = {"content-type": "application/pdf"}
        mock_response.body = AsyncMock(return_value=sample_bytes)
        mock_response.dispose = AsyncMock()

        mock_client._context.request.get = AsyncMock(return_value=mock_response)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("pje_downloader._PASTAS", {"1g": Path(tmpdir), "2g": Path(tmpdir)}):
                result = await pje_downloader.salvar_documento(
                    mock_client,
                    "0000123-45.2026.8.14.0001",
                    "1001",
                    "Petição",
                )

                self.assertEqual(result["documento_id"], "1001")
                self.assertEqual(result["tamanho_bytes"], len(sample_bytes))
                self.assertEqual(result["sha256"], hashlib.sha256(sample_bytes).hexdigest())
                self.assertIsNotNone(result.get("source_fingerprint"))
                self.assertTrue(Path(result["caminho"]).exists())

    async def test_tab_listing_failure_produces_explicit_error_status(self):
        mock_client = MagicMock()
        mock_client.listar_documentos = AsyncMock(side_effect=RuntimeError("Tab autos crash"))

        res = await pje_downloader.baixar_processo_doc_a_doc(
            mock_client,
            "0000123-45.2026.8.14.0001",
        )

        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["erros"], 1)
        self.assertEqual(res["total_documentos"], 0)
        self.assertIn("Tab autos crash", res["safe_error"])
        self.assertIsNotNone(res["erros_detalhados"])

    async def test_partial_download_failure_produces_explicit_partial_status(self):
        mock_client = MagicMock()
        mock_client.grau = "1g"
        mock_client.listar_documentos = AsyncMock(return_value={
            "arvore_completa": True,
            "documentos": [
                {"id": "1", "tipo": "Petição"},
                {"id": "2", "tipo": "Documento Corrompido"},
            ],
        })

        async def fake_salvar(client, cnj, doc_id, tipo="", *args, **kwargs):
            if doc_id == "2":
                raise RuntimeError("Download timeout on piece 2")
            return {
                "numero_cnj": cnj,
                "documento_id": doc_id,
                "caminho": f"/tmp/{doc_id}.pdf",
                "tamanho_bytes": 500,
                "tamanho_kb": 0.5,
                "formato": "pdf",
                "sha256": "abc123hash",
                "source_fingerprint": "fp123",
            }

        with patch("pje_downloader.salvar_documento", side_effect=fake_salvar):
            res = await pje_downloader.baixar_processo_doc_a_doc(
                mock_client,
                "0000123-45.2026.8.14.0001",
                concurrency_limit=2,
            )

            self.assertEqual(res["status"], "PARTIAL")
            self.assertEqual(res["total_documentos"], 2)
            self.assertEqual(res["baixados"], 1)
            self.assertEqual(res["erros"], 1)
            self.assertEqual(res["erros_detalhados"][0]["id"], "2")
            self.assertIn("Download timeout", res["erros_detalhados"][0]["erro"])


class Completeness6DEngineTests(unittest.TestCase):
    def test_6d_completeness_calculation(self):
        doc1 = {
            "document_id": "101",
            "title": "Petição Inicial",
            "type": "Petição",
            "date": "2026-01-01T10:00:00+00:00",
            "pages": [{"page": 1, "text": "Excelentíssimo Juiz", "extraction_method": "native_text", "status": "extracted"}],
            "content_sha256": "sha101",
            "source_bytes_sha256": "sha101",
            "pages_without_text": 0,
            "truncated": False,
            "source_fingerprint": "fp101",
            "security": {"anomalies": []},
        }

        manifest_docs = [
            {
                "document_id": "101",
                "type": "Petição",
                "title": "Petição Inicial",
                "date": "2026-01-01T10:00:00+00:00",
                "source_fingerprint": "fp101",
                "source_bytes_sha256": "sha101",
            }
        ]

        states = [
            {
                "document_id": "101",
                "status": "completed",
                "source_fingerprint": "fp101",
                "content_sha256": "sha101",
                "pages": 1,
                "pages_without_text": 0,
                "truncated": False,
                "duplicate_of": "",
            }
        ]

        base = {
            "case_class": "Ação Ordinária",
            "subject": "Contratos",
            "court": "1a Vara Cível",
            "distribution_date": "2026-01-01T08:00:00+00:00",
            "movements": [{"data": "2026-01-02T12:00:00+00:00", "nome": "Juntada"}],
            "parties": [{"nome": "Autor Teste", "polo": "AT"}],
        }

        dossier = complete.build_dossier(
            process_number="0000123-45.2026.8.14.0001",
            base=base,
            manifest=manifest_docs,
            documents=[doc1],
            document_states_value=states,
            tree_complete=True,
            expedients=[],
            collected_at="2026-01-10T00:00:00+00:00",
        )

        completeness = dossier["completeness"]
        self.assertIn("source", completeness)
        self.assertIn("documents", completeness)
        self.assertIn("attachments", completeness)
        self.assertIn("pages", completeness)
        self.assertIn("metadata", completeness)
        self.assertIn("temporal_consistency", completeness)

        self.assertEqual(completeness["source"]["status"], "complete")
        self.assertEqual(completeness["documents"]["status"], "complete")
        self.assertEqual(completeness["attachments"]["status"], "complete")
        self.assertEqual(completeness["pages"]["status"], "complete")
        self.assertEqual(completeness["metadata"]["status"], "complete")
        self.assertEqual(completeness["temporal_consistency"]["status"], "complete")
        self.assertTrue(dossier["coverage"]["complete"])

    def test_missing_attachment_produces_attachment_gap(self):
        doc1 = {
            "document_id": "101",
            "title": "Petição Inicial",
            "type": "Petição",
            "pages": [{"page": 1, "text": "Texto", "extraction_method": "native_text", "status": "extracted"}],
            "content_sha256": "sha101",
            "pages_without_text": 0,
            "truncated": False,
            "source_fingerprint": "fp101",
        }

        manifest_docs = [
            {"document_id": "101", "type": "Petição", "source_fingerprint": "fp101"},
            {"document_id": "102", "type": "Anexo RG", "parent_document_id": "101", "source_fingerprint": "fp102"},
        ]

        states = [
            {"document_id": "101", "status": "completed", "pages": 1, "pages_without_text": 0, "truncated": False, "duplicate_of": ""},
            {"document_id": "102", "status": "failed", "pages": 0, "pages_without_text": 0, "truncated": False, "duplicate_of": ""},
        ]

        base = {"movements": [], "parties": []}

        dossier = complete.build_dossier(
            process_number="0000123-45.2026.8.14.0001",
            base=base,
            manifest=manifest_docs,
            documents=[doc1],
            document_states_value=states,
            tree_complete=True,
            expedients=[],
            collected_at="2026-01-10T00:00:00+00:00",
        )

        completeness = dossier["completeness"]
        self.assertEqual(completeness["attachments"]["status"], "partial")
        self.assertFalse(dossier["coverage"]["complete"])
        gap_codes = {g["code"] for g in dossier["gap_details"]}
        self.assertIn("ATTACHMENTS_INCOMPLETE", gap_codes)

    def test_temporal_inconsistency_produces_temporal_gap(self):
        doc1 = {
            "document_id": "101",
            "type": "Petição",
            "date": "2099-01-01T00:00:00+00:00",  # Future date!
            "pages": [{"page": 1, "text": "Texto", "extraction_method": "native_text", "status": "extracted"}],
            "content_sha256": "sha101",
            "pages_without_text": 0,
            "truncated": False,
        }

        manifest_docs = [{"document_id": "101", "type": "Petição", "source_fingerprint": "fp101"}]
        states = [{"document_id": "101", "status": "completed", "pages": 1, "pages_without_text": 0, "truncated": False, "duplicate_of": ""}]

        base = {
            "distribution_date": "2026-01-01T00:00:00+00:00",
            "movements": [{"data": "2026-01-02T00:00:00+00:00"}],
            "parties": [],
        }

        dossier = complete.build_dossier(
            process_number="0000123-45.2026.8.14.0001",
            base=base,
            manifest=manifest_docs,
            documents=[doc1],
            document_states_value=states,
            tree_complete=True,
            expedients=[],
            collected_at="2026-01-10T00:00:00+00:00",
        )

        completeness = dossier["completeness"]
        self.assertEqual(completeness["temporal_consistency"]["status"], "partial")
        gap_codes = {g["code"] for g in dossier["gap_details"]}
        self.assertIn("TEMPORAL_INCONSISTENCY_DETECTED", gap_codes)


class PostAcquisitionReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import base64
        import os
        cls.old_key = os.environ.get("PJE_AUDIT_MASTER_KEY")
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(b"a" * 32).decode("ascii")

    @classmethod
    def tearDownClass(cls):
        import os
        if cls.old_key is None:
            os.environ.pop("PJE_AUDIT_MASTER_KEY", None)
        else:
            os.environ["PJE_AUDIT_MASTER_KEY"] = cls.old_key

    def test_reconcile_manifest_and_dossier_success(self):
        manifest_items = [
            {"document_id": "1", "type": "Petição", "source_bytes_sha256": "hash1", "source_fingerprint": "fp1"},
            {"document_id": "2", "type": "Sentença", "source_bytes_sha256": "hash2", "source_fingerprint": "fp2"},
        ]
        acquired = [
            {"document_id": "1", "type": "Petição", "source_bytes_sha256": "hash1", "source_fingerprint": "fp1", "tamanho_bytes": 1000},
            {"document_id": "2", "type": "Sentença", "source_bytes_sha256": "hash2", "source_fingerprint": "fp2", "tamanho_bytes": 2000},
        ]

        recon = manifest.reconcile_manifest_and_dossier(manifest_items, acquired)

        self.assertTrue(recon["reconciled"])
        self.assertEqual(recon["status"], "reconciled")
        self.assertEqual(len(recon["missing_pieces"]), 0)
        self.assertEqual(len(recon["corrupted_pieces"]), 0)
        self.assertEqual(recon["total_manifest"], 2)
        self.assertEqual(recon["total_acquired"], 2)

    def test_reconcile_manifest_detects_missing_and_corrupted_pieces(self):
        manifest_items = [
            {"document_id": "1", "type": "Petição", "source_bytes_sha256": "hash1", "source_fingerprint": "fp1"},
            {"document_id": "2", "type": "Anexo", "source_bytes_sha256": "hash2", "source_fingerprint": "fp2"},
            {"document_id": "3", "type": "Certidão", "source_bytes_sha256": "hash3", "source_fingerprint": "fp3"},
        ]

        acquired = [
            {"document_id": "1", "type": "Petição", "source_bytes_sha256": "hash1", "source_fingerprint": "fp1", "tamanho_bytes": 1000},
            {"document_id": "2", "type": "Anexo", "source_bytes_sha256": "HASH_MISMATCH", "source_fingerprint": "fp2", "tamanho_bytes": 500},
            # Document 3 is missing
        ]

        recon = manifest.reconcile_manifest_and_dossier(manifest_items, acquired)

        self.assertFalse(recon["reconciled"])
        self.assertEqual(recon["status"], "reconciliation_failed")
        self.assertEqual(recon["missing_pieces"], ["3"])
        self.assertEqual(recon["corrupted_pieces"], ["2"])
        self.assertIn("3", recon["requeued_pieces"])
        self.assertIn("2", recon["requeued_pieces"])

    def test_reconcile_manifest_auto_requeues_failed_pieces_in_db(self):
        job = complete.create_job(
            process_number="0000123-45.2026.8.14.0001",
            grau="1g",
            persona="advogado",
            mode="integral",
            semantic_enabled=False,
            force_reread=False,
            authorisation_ref="auth-ref-123",
        )
        job_id = job["job_id"]

        manifest_items = [
            {"document_id": "100", "type": "Petição", "source_fingerprint": "fp100"},
        ]
        acquired = [
            {"document_id": "100", "status": "failed", "safe_error": "Read failure", "source_fingerprint": "fp100"},
        ]

        recon = manifest.reconcile_manifest_and_dossier(manifest_items, acquired, job_id=job_id)

        self.assertFalse(recon["reconciled"])
        self.assertIn("100", recon["requeued_pieces"])

        states = complete.document_states(job_id)
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["document_id"], "100")
        self.assertEqual(states[0]["status"], "queued")
        self.assertEqual(states[0]["safe_error"], "requeued_for_reacquisition")


class RemediationM2GateFixesTests(unittest.IsolatedAsyncioTestCase):
    """Explicit unit tests covering all 5 M2 Gate remediation fixes."""

    def test_fix1_key_normalization_documento_id(self):
        """Fix 1: verify reconcile_manifest_and_dossier extracts 'documento_id' key from acquired items."""
        manifest_items = [
            {"document_id": "100", "type": "Petição Inicial", "source_fingerprint": "fp100", "tamanho_bytes": 1024},
        ]
        acquired_from_salvar = [
            {"documento_id": "100", "caminho": "/tmp/100.pdf", "tamanho_bytes": 1024, "status": "ok", "source_fingerprint": "fp100"},
        ]
        recon = manifest.reconcile_manifest_and_dossier(manifest_items, acquired_from_salvar)
        self.assertTrue(recon["reconciled"])
        self.assertEqual(len(recon["missing_pieces"]), 0)
        self.assertEqual(recon["total_acquired"], 1)

    async def test_fix2_harmonize_source_fingerprint_salvar_documento(self):
        """Fix 2: verify salvar_documento includes titulo and data in source_fingerprint computation."""
        mock_client = MagicMock()
        mock_client.grau = "1g"
        mock_client.garantir_processo_id = AsyncMock()

        sample_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n" + b"X" * 1050 + b"\ntrailer\n<<>>\nstartxref\n100\n%%EOF\n"
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.headers = {"content-type": "application/pdf"}
        mock_response.body = AsyncMock(return_value=sample_bytes)
        mock_response.dispose = AsyncMock()
        mock_client._context.request.get = AsyncMock(return_value=mock_response)

        doc_metadata = {
            "id": "200",
            "tipo": "Decisão",
            "titulo": "Decisão Interlocutória",
            "data": "2026-01-15T12:00:00Z",
            "tamanho_bytes": len(sample_bytes),
        }
        manifest_entry = manifest.build_manifest_entry(doc_metadata, canonical_order=1)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("pje_downloader._PASTAS", {"1g": Path(tmpdir), "2g": Path(tmpdir)}):
                acquired = await pje_downloader.salvar_documento(
                    mock_client,
                    "0000123-45.2026.8.14.0001",
                    "200",
                    tipo_descritivo="Decisão",
                    titulo="Decisão Interlocutória",
                    data="2026-01-15T12:00:00Z",
                )
                self.assertEqual(acquired["source_fingerprint"], manifest_entry["source_fingerprint"])

    def test_fix3_attachment_completeness_missing_state(self):
        """Fix 3: verify missing attachment state in document_states_value causes attachments_complete=False."""
        doc1 = {
            "document_id": "101",
            "title": "Petição Inicial",
            "type": "Petição",
            "pages": [{"page": 1, "text": "Excelentíssimo", "extraction_method": "native_text", "status": "extracted"}],
            "content_sha256": "s101",
            "pages_without_text": 0,
            "truncated": False,
            "source_fingerprint": "fp101",
        }
        manifest_docs = [
            {"document_id": "101", "type": "Petição", "source_fingerprint": "fp101"},
            {"document_id": "102", "type": "Anexo Documento", "parent_document_id": "101", "source_fingerprint": "fp102"},
        ]
        # Only doc 101 is in states; 102 (attachment) is completely missing from document_states_value
        states = [
            {"document_id": "101", "status": "completed", "pages": 1, "pages_without_text": 0, "truncated": False, "duplicate_of": ""},
        ]
        base = {"movements": [], "parties": []}

        dossier = complete.build_dossier(
            process_number="0000123-45.2026.8.14.0001",
            base=base,
            manifest=manifest_docs,
            documents=[doc1],
            document_states_value=states,
            tree_complete=True,
            expedients=[],
            collected_at="2026-01-10T00:00:00Z",
        )

        attachments_dim = dossier["completeness"]["attachments"]
        self.assertEqual(attachments_dim["status"], "partial")
        self.assertEqual(attachments_dim["failed_attachments"], 1)
        self.assertFalse(dossier["coverage"]["complete"])
        gap_codes = {g["code"] for g in dossier["gap_details"]}
        self.assertIn("ATTACHMENTS_INCOMPLETE", gap_codes)

    async def test_fix4_empty_listing_failure_status(self):
        """Fix 4: verify empty document list or error in tab listing sets ERROR/PARTIAL status and erros_detalhados."""
        mock_client = MagicMock()
        mock_client.listar_documentos = AsyncMock(return_value={"arvore_completa": True, "documentos": []})

        res = await pje_downloader.baixar_processo_doc_a_doc(
            mock_client,
            "0000123-45.2026.8.14.0001",
        )

        self.assertEqual(res["status"], "ERROR")
        self.assertGreater(res["erros"], 0)
        self.assertIsNotNone(res["erros_detalhados"])
        self.assertEqual(res["erros_detalhados"][0]["status"], "ERROR")

    def test_fix5_sha256_and_zero_byte_integrity_checks(self):
        """Fix 5: verify missing acquired SHA-256 and zero-byte payloads fail verify_piece_integrity."""
        entry_with_sha = {
            "document_id": "doc1",
            "source_bytes_sha256": "expected_sha_256_hash",
            "type": "Petição",
        }
        payload_no_sha = {
            "document_id": "doc1",
            "tamanho_bytes": 1024,
            "status": "ok",
        }
        is_valid_sha, reason_sha = manifest.verify_piece_integrity(entry_with_sha, payload_no_sha)
        self.assertFalse(is_valid_sha)
        self.assertIn("divergência de sha256", reason_sha)

        entry_any = {"document_id": "doc2", "type": "Anexo"}
        payload_zero = {
            "document_id": "doc2",
            "tamanho_bytes": 0,
            "status": "ok",
        }
        is_valid_zero, reason_zero = manifest.verify_piece_integrity(entry_any, payload_zero)
        self.assertFalse(is_valid_zero)
        self.assertIn("tamanho nulo", reason_zero)


if __name__ == "__main__":
    unittest.main()
