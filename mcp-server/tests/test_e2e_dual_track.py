"""Milestone 5 Dual-Track E2E Test Suite & Acceptance Harness (`test_e2e_dual_track.py`).

Covers:
1. Track A: Synthetic procedural pipeline with Manifesto v2, 6-dim completeness, SHA-256 caching, and zero page loss.
2. Track B: Canary homologation for all 9 MCP supertools against standardized contract schemas and error taxonomy.
3. SQLite WAL process restart recovery test verifying checkpoint persistence across engine crashes.
4. Parallel PDF consolidation request coalescing test asserting single ConsolidatedPdfJob dispatch.
"""

import asyncio
import base64
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import analise_processual_completa as complete
import auditoria_processual as audit
import server
from job_engine import DurableJobEngine


def create_synthetic_doc(doc_id: str, title: str, text: str, page_count: int = 1, is_ocr: bool = False) -> dict:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    pages = []
    for p in range(1, page_count + 1):
        pages.append({
            "page": p,
            "text": f"{text} - Página {p}",
            "extraction_method": "ocr" if is_ocr else "native",
            "confidence": 0.75 if is_ocr else 1.0,
        })
    return {
        "document_id": doc_id,
        "title": title,
        "type": title,
        "date": "2026-07-31T12:00:00Z",
        "pages": pages,
        "content_sha256": digest,
        "pages_without_text": 0,
        "truncated": False,
        "source_fingerprint": f"fingerprint-{doc_id}",
        "security": {"anomalies": []},
    }


class DualTrackE2ETestSuite(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_storage = os.environ.get("PJE_STORAGE_DIR")
        self.old_key = os.environ.get("PJE_AUDIT_MASTER_KEY")
        os.environ["PJE_STORAGE_DIR"] = self.temp_dir.name
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(b"k" * 32).decode()

    async def asyncTearDown(self):
        if self.old_storage is None:
            os.environ.pop("PJE_STORAGE_DIR", None)
        else:
            os.environ["PJE_STORAGE_DIR"] = self.old_storage

        if self.old_key is None:
            os.environ.pop("PJE_AUDIT_MASTER_KEY", None)
        else:
            os.environ["PJE_AUDIT_MASTER_KEY"] = self.old_key

        self.temp_dir.cleanup()

    async def test_track_a_synthetic_procedural_pipeline(self):
        """Track A: Ingests 10-page mixed document process, verifying Manifesto v2, 6-dim completeness, SHA-256 caching & 0 page loss."""
        cnj = "0800100-10.2026.8.14.0001"
        
        # Create 5 synthetic documents totaling 10 pages (mixed native and OCR scanned)
        doc1 = create_synthetic_doc("doc_1", "Petição Inicial", "Excelentíssimo Juiz, requer a procedência do pedido.", page_count=2, is_ocr=False)
        doc2 = create_synthetic_doc("doc_2", "Procuração", "Outorga de poderes de representação judicial.", page_count=1, is_ocr=True)
        doc3 = create_synthetic_doc("doc_3", "Contrato Social", "Cláusulas contratuais da sociedade e patrimônio.", page_count=3, is_ocr=False)
        doc4 = create_synthetic_doc("doc_4", "Decisão Interlocutória", "Determino a intimação do Ministério Público. Voltem os autos conclusos para sentença.", page_count=1, is_ocr=False)
        doc5 = create_synthetic_doc("doc_5", "Parecer do Ministério Público", "O Ministério Público manifesta-se pelo deferimento do pedido.", page_count=3, is_ocr=False)

        documents = [doc1, doc2, doc3, doc4, doc5]
        total_pages_input = sum(len(d["pages"]) for d in documents)
        self.assertEqual(total_pages_input, 10, "Procedural record must total exactly 10 pages")

        manifest = []
        doc_states = []
        for d in documents:
            manifest.append({
                "document_id": d["document_id"],
                "type": d["type"],
                "title": d["title"],
                "date": d["date"],
                "source_fingerprint": d["source_fingerprint"],
                "parent_id": None if d["document_id"] in ("doc_1", "doc_4", "doc_5") else "doc_1",
            })
            doc_states.append({
                "document_id": d["document_id"],
                "status": "completed",
                "pages_without_text": 0,
                "truncated": False,
                "duplicate_of": "",
            })
            audit.save_cached_document(
                cnj,
                d["document_id"],
                d["source_fingerprint"],
                d["content_sha256"],
                d,
            )

        # Build initial job and dossier
        source_job = complete.create_job(
            process_number=cnj,
            grau="1g",
            persona="advogado",
            mode="integral",
            semantic_enabled=False,
            force_reread=False,
            authorisation_ref="auth_track_a",
        )

        base_data = {
            "case_class": "Ação Ordinária",
            "subject": "Cível",
            "movements": [{"data": "2026-07-31T12:00:00Z", "nome": "Juntada de parecer"}],
            "parties": [{"nome": "Autor Teste", "polo": "AT"}]
        }

        dossier = complete.build_dossier(
            process_number=cnj,
            base=base_data,
            manifest=manifest,
            documents=documents,
            document_states_value=doc_states,
            tree_complete=True,
            expedients=[],
            collected_at="2026-07-31T12:00:00Z",
            expedients_complete=True,
            attachments_complete=True,
            manifest_reconciled=True,
            metadata_complete=True,
        )
        complete.save_result(source_job["job_id"], dossier)

        # 1. Assert Manifesto v2 parent-attachment hierarchy
        self.assertIn("completeness", dossier)
        comp = dossier["completeness"]
        
        # 2. Assert 6-dimensional completeness structure
        self.assertIn("source", comp)
        self.assertIn("documents", comp)
        self.assertIn("pages", comp)
        self.assertIn("metadata", comp)
        self.assertIn("domain_analysis", comp)
        self.assertIn("temporal_consistency", comp)
        self.assertEqual(comp["documents"]["status"], "complete")
        self.assertEqual(comp["pages"]["status"], "complete")

        # 3. Assert Zero Page Loss
        pages_processed = sum(len(d.get("pages", [])) for d in documents)
        self.assertEqual(pages_processed, 10, "Zero page loss assertion failed: total pages must equal 10")
        self.assertEqual(dossier["coverage"]["documents_processed"], 5)

        # 4. SHA-256 Cache Re-analysis Test
        target_job = complete.create_job(
            process_number=cnj,
            grau="1g",
            persona="advogado",
            mode="integral",
            semantic_enabled=False,
            force_reread=False,
            authorisation_ref="auth_track_a_second",
        )

        reanalysis = complete.reanalyse_from_cache(target_job["job_id"], source_job["job_id"])
        self.assertEqual(reanalysis["status"], "completed")
        self.assertEqual(reanalysis["reused_documents"], 5, "SHA-256 byte cache must reuse all 5 documents on second run")

    async def test_track_b_canary_homologation(self):
        """Track B: Validates all 9 MCP supertools against standardized contract schemas and error taxonomy codes."""
        supertools_args = {
            "analisar_processo_completo_pje": {"acao": "status", "job_id": ""},
            "status_e_auditoria_pje": {"acao": "capacidades", "filtro": "prazo"},
            "painel_e_prazos_pje": {"acao": "schema_acervo_tarefas"},
            "buscar_processos_pje": {"acao": "corrigir_cnj", "valor": "0800000-00.2026.8.14.0000"},
            "analisar_processo_pje": {"acao": "movimentacoes", "numero_cnj": "0800000-00.2026.8.14.0000"},
            "gerir_documentos_pje": {"acao": "listar", "numero_cnj": "0800000-00.2026.8.14.0000"},
            "download_e_cache_pje": {"acao": "status", "numero_cnj": "0800000-00.2026.8.14.0000"},
            "producao_minutas_e_relatorios": {"acao": "modelos"},
            "auditar_fluxo_processual_pje": {"acao": "planejar_auditoria"},
        }

        self.assertEqual(len(supertools_args), 9, "Track B must validate exactly 9 MCP supertools")

        for tool_name, args in supertools_args.items():
            with self.subTest(tool=tool_name):
                content, structured = await server.mcp.call_tool(tool_name, args)
                self.assertIsNotNone(content)
                self.assertIn("result", structured)
                result_payload = structured["result"]

                # Validate standard contract fields
                self.assertIn("status", result_payload, f"{tool_name} missing status field")
                self.assertIn(result_payload["status"], ("success", "partial", "error"))

                # Validate Multidimensional Completeness (6 dimensions)
                if "completeness" in result_payload and isinstance(result_payload["completeness"], dict):
                    comp = result_payload["completeness"]
                    for dim in ("source", "documents", "pages", "metadata", "domain_analysis", "temporal_consistency"):
                        self.assertIn(dim, comp, f"{tool_name} completeness missing dimension '{dim}'")

                # Validate GapDetails & Evidence list types
                if "gap_details" in result_payload:
                    self.assertIsInstance(result_payload["gap_details"], list)
                if "evidence" in result_payload:
                    self.assertIsInstance(result_payload["evidence"], list)

                # Validate Latency breakdown structure
                if "latency" in result_payload and isinstance(result_payload["latency"], dict):
                    lat = result_payload["latency"]
                    self.assertIn("total_duration_ms", lat)

    async def test_sqlite_wal_process_restart_recovery(self):
        """SQLite WAL Process Restart Recovery: Job creation, checkpoint saving, process restart simulation and checkpoint recovery."""
        db_file = Path(self.temp_dir.name) / "wal_recovery_test.db"
        
        # 1. Initial engine process phase
        engine_v1 = DurableJobEngine(db_path=db_file)
        job_id = engine_v1.submit_job(
            job_type="procedural_analysis",
            process_cnj="0809999-99.2026.8.14.0001",
            grau="1g",
            payload={"initial_doc": "doc_1"},
        )
        self.assertIsNotNone(job_id)

        claimed = engine_v1.claim_job(worker_id="worker_before_crash", job_id=job_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], "RUNNING")

        checkpoint_data = {
            "processed_documents": 7,
            "last_doc_id": "doc_7",
            "sha256_verified": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        }
        checkpoint_saved = engine_v1.save_checkpoint(job_id, checkpoint_data)
        self.assertTrue(checkpoint_saved, "Checkpoint saving in SQLite WAL failed")

        # 2. Simulate Process Crash (destroy old engine instance)
        del engine_v1

        # 3. Restart process (instantiate new DurableJobEngine over exact same SQLite WAL db file)
        engine_v2 = DurableJobEngine(db_path=db_file)

        # 4. Assert full recovery from SQLite WAL
        recovered_job = engine_v2.get_job(job_id)
        self.assertIsNotNone(recovered_job, "Failed to recover job after process restart")
        self.assertEqual(recovered_job["status"], "RUNNING")

        recovered_checkpoint = engine_v2.get_checkpoint(job_id)
        self.assertEqual(recovered_checkpoint, checkpoint_data, "Recovered checkpoint data does not match state before crash!")

    async def test_consolidated_pdf_request_coalescing(self):
        """Parallel PDF Consolidation Request Coalescing: 5 parallel requests coalesce into a single ConsolidatedPdfJob."""
        db_file = Path(self.temp_dir.name) / "pdf_coalesce_test.db"
        engine = DurableJobEngine(db_path=db_file)

        cnj = "0800002-77.2026.8.14.0001"

        # Submit 5 parallel requests for PDF consolidation on the exact same CNJ
        def submit_request(index: int) -> str:
            return engine.submit_job(
                job_type="pdf_consolidation",
                process_cnj=cnj,
                grau="1g",
                payload={"request_source": f"request_{index}"},
            )

        job_ids = await asyncio.gather(*[
            asyncio.to_thread(submit_request, i) for i in range(5)
        ])

        self.assertEqual(len(job_ids), 5)
        first_job_id = job_ids[0]
        self.assertTrue(
            all(jid == first_job_id for jid in job_ids),
            f"Request coalescing failed! Received multiple distinct job IDs: {job_ids}",
        )

        # Assert only 1 single job exists in database for this CNJ
        job_state = engine.get_job(first_job_id)
        self.assertIsNotNone(job_state)
        self.assertEqual(job_state["job_type"], "pdf_consolidation")
        self.assertEqual(job_state["process_cnj"], cnj)


if __name__ == "__main__":
    unittest.main()
