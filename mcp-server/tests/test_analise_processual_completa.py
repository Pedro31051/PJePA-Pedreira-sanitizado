"""Testes sintéticos da análise processual completa e retomável."""

import asyncio
import base64
import hashlib
import hmac
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

complete = importlib.import_module("analise_processual_completa")
audit = importlib.import_module("auditoria_processual")
pje_client_module = importlib.import_module("pje_client")
PJeClient = pje_client_module.PJeClient

SYNTHETIC_CNJ = "0000000-00.0000.0.00.0000"


def synthetic_document(
    document_id: str,
    text: str,
    *,
    title: str = "Petição sintética",
) -> dict:
    digest = __import__("hashlib").sha256(text.encode()).hexdigest()
    return {
        "document_id": document_id,
        "title": title,
        "type": title,
        "date": "01/01/2026",
        "pages": [{"page": 1, "text": text}],
        "content_sha256": digest,
        "pages_without_text": 0,
        "truncated": False,
        "source_fingerprint": f"fingerprint-{document_id}",
    }


class CompleteAnalysisStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_storage = os.environ.get("PJE_STORAGE_DIR")
        self.old_key = os.environ.get("PJE_AUDIT_MASTER_KEY")
        self.old_credentials_dir = os.environ.pop("CREDENTIALS_DIRECTORY", None)
        os.environ["PJE_STORAGE_DIR"] = self.temp_dir.name
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"s" * 32
        ).decode()

    def tearDown(self):
        if self.old_storage is None:
            os.environ.pop("PJE_STORAGE_DIR", None)
        else:
            os.environ["PJE_STORAGE_DIR"] = self.old_storage
        if self.old_key is None:
            os.environ.pop("PJE_AUDIT_MASTER_KEY", None)
        else:
            os.environ["PJE_AUDIT_MASTER_KEY"] = self.old_key
        if self.old_credentials_dir is not None:
            os.environ["CREDENTIALS_DIRECTORY"] = self.old_credentials_dir
        self.temp_dir.cleanup()

    def create_job(self):
        return complete.create_job(
            process_number=SYNTHETIC_CNJ,
            grau="1g",
            persona="advogado",
            mode="integral",
            semantic_enabled=False,
            force_reread=False,
            authorisation_ref="autorização sintética",
        )

    def test_crypto_uses_systemd_credential_before_environment(self):
        credential_dir = Path(self.temp_dir.name) / "credentials"
        credential_dir.mkdir()
        (credential_dir / "audit_master_key").write_text(
            base64.urlsafe_b64encode(b"c" * 32).decode(),
            encoding="ascii",
        )
        os.environ["CREDENTIALS_DIRECTORY"] = str(credential_dir)
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"e" * 32
        ).decode()

        crypto = audit.CryptoBox()

        expected = hmac.new(
            b"c" * 32, b"process-reference", hashlib.sha256
        ).hexdigest()
        self.assertEqual(crypto.reference("process-reference"), expected)

    def test_job_persists_progress_and_hides_process_number(self):
        job = self.create_job()
        updated = complete.update_job(
            job["job_id"],
            status="running",
            phase="extraction",
            total_documents=25,
            processed_documents=5,
        )
        self.assertEqual(updated["progress_percent"], 20.0)
        self.assertNotIn("process_ref", updated)
        self.assertNotIn("encrypted_identity", updated)

        database = Path(self.temp_dir.name) / ("auditoria_processual/auditoria.sqlite3")
        raw = database.read_bytes()
        self.assertNotIn(SYNTHETIC_CNJ.encode(), raw)
        self.assertEqual(
            complete.identity_for_job(job["job_id"])["process_number"],
            SYNTHETIC_CNJ,
        )

    def test_cancelled_job_remains_resumable(self):
        job = self.create_job()
        cancelled = complete.request_cancel(job["job_id"])
        self.assertEqual(cancelled["status"], "cancel_requested")
        self.assertTrue(complete.cancellation_requested(job["job_id"]))
        resumable = complete.find_latest_job(
            SYNTHETIC_CNJ,
            "1g",
            resumable_only=True,
        )
        self.assertEqual(resumable["job_id"], job["job_id"])

    def test_supported_modes_and_mode_scoped_lookup(self):
        jobs = {}
        for mode in ("inventario", "rapida", "integral"):
            jobs[mode] = complete.create_job(
                process_number=SYNTHETIC_CNJ,
                grau="1g",
                persona="servidor",
                mode=mode,
                semantic_enabled=False,
                force_reread=False,
                authorisation_ref="autorização sintética",
            )
        found = complete.find_latest_job(
            SYNTHETIC_CNJ,
            "1g",
            mode="rapida",
        )
        self.assertEqual(found["job_id"], jobs["rapida"]["job_id"])
        with self.assertRaises(complete.CompleteAnalysisError):
            complete.create_job(
                process_number=SYNTHETIC_CNJ,
                grau="1g",
                persona="servidor",
                mode="desconhecido",
                semantic_enabled=False,
                force_reread=False,
                authorisation_ref="autorização sintética",
            )

    def test_latest_snapshot_does_not_expose_encrypted_payload(self):
        job = self.create_job()
        dossier = {"manifest": [], "marker": "conteúdo protegido"}
        complete.save_result(job["job_id"], dossier)

        snapshot = complete.find_latest_available_result(SYNTHETIC_CNJ, "1g")

        self.assertEqual(snapshot["dossier"], dossier)
        self.assertNotIn("encrypted_payload", snapshot["job"])
        self.assertNotIn("dossier_sha256", snapshot["job"])

    def test_result_is_encrypted_and_explainable(self):
        job = self.create_job()
        dossier = {
            "schema_version": complete.SCHEMA_VERSION,
            "next_steps": [
                {
                    "id": "next-step-synthetic",
                    "act": "Ato inteiramente sintético",
                    "citations": [],
                }
            ],
        }
        complete.save_result(job["job_id"], dossier)
        result = complete.get_result(job["job_id"])
        self.assertTrue(result["available"])
        explained = complete.explain(
            job["job_id"],
            finding_id="next-step-synthetic",
        )
        self.assertEqual(
            explained["finding"]["act"],
            "Ato inteiramente sintético",
        )

        database = Path(self.temp_dir.name) / ("auditoria_processual/auditoria.sqlite3")
        connection = sqlite3.connect(database)
        try:
            encrypted = connection.execute(
                """
                SELECT encrypted_payload FROM complete_analysis_results
                 WHERE job_id=?
                """,
                (job["job_id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn(b"Ato inteiramente", encrypted)

    def test_reanalysis_reuses_encrypted_cache_without_browser(self):
        source_job = self.create_job()
        documents = [
            synthetic_document(
                "doc-opinion",
                "O Ministério Público apresenta manifestação final.",
                title="Manifestação sintética",
            ),
            synthetic_document(
                "doc-order",
                "Determino que voltem os autos conclusos para sentença.",
                title="Decisão sintética",
            ),
        ]
        manifest = []
        states = []
        for document in documents:
            manifest.append(
                {
                    "document_id": document["document_id"],
                    "type": document["type"],
                    "title": document["title"],
                    "date": document["date"],
                    "source_fingerprint": document["source_fingerprint"],
                }
            )
            states.append(
                {
                    "document_id": document["document_id"],
                    "status": "completed",
                    "pages_without_text": 0,
                    "truncated": False,
                    "duplicate_of": "",
                }
            )
            audit.save_cached_document(
                SYNTHETIC_CNJ,
                document["document_id"],
                document["source_fingerprint"],
                document["content_sha256"],
                document,
            )
        source_dossier = complete.build_dossier(
            process_number=SYNTHETIC_CNJ,
            base={"movements": [], "parties": []},
            manifest=manifest,
            documents=documents,
            document_states_value=states,
            tree_complete=True,
            expedients=[],
            collected_at="2026-01-01T00:00:00+00:00",
        )
        complete.save_result(source_job["job_id"], source_dossier)
        target_job = self.create_job()

        finished = complete.reanalyse_from_cache(
            target_job["job_id"],
            source_job["job_id"],
        )

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["reused_documents"], 2)
        result = complete.get_result(target_job["job_id"])
        self.assertEqual(
            result["dossier"]["next_steps"][0]["act"],
            "Conclusão para sentença",
        )


class CompleteAnalysisDossierTests(unittest.TestCase):
    def test_manifest_delta_and_rapid_legal_priority(self):
        previous = [
            {"document_id": "1", "source_fingerprint": "same"},
            {"document_id": "2", "source_fingerprint": "old"},
            {"document_id": "removed", "source_fingerprint": "gone"},
        ]
        current = [
            {"document_id": "1", "source_fingerprint": "same"},
            {"document_id": "2", "source_fingerprint": "new"},
            {"document_id": "added", "source_fingerprint": "fresh"},
        ]
        delta = complete.compare_manifests(previous, current)
        self.assertEqual(delta["unchanged_document_ids"], ["1"])
        self.assertEqual(delta["modified_document_ids"], ["2"])
        self.assertEqual(delta["added_document_ids"], ["added"])
        self.assertEqual(delta["removed_document_ids"], ["removed"])

        selected = complete.prioritise_documents(
            [
                {"id": "admin", "tipo": "Procuração", "titulo": "Anexo"},
                {"id": "initial", "tipo": "Petição Inicial", "titulo": "Inicial"},
                {"id": "decision", "tipo": "Decisão", "titulo": "Tutela"},
            ],
            limit=2,
        )
        self.assertEqual({item["id"] for item in selected}, {"initial", "decision"})

    def test_inventory_dossier_never_claims_content_was_read(self):
        dossier = complete.build_inventory_dossier(
            process_number=SYNTHETIC_CNJ,
            base={"parties": [], "movements": [{"description": "Distribuído"}]},
            manifest_value=[{"document_id": "1", "status": "not_attempted"}],
            tree_complete=True,
        )
        self.assertEqual(dossier["analysis_level"], "inventory")
        self.assertFalse(dossier["coverage"]["content_read"])
        self.assertEqual(dossier["next_recommended_action"], "iniciar_rapida")

    def test_reconciliacao_detecta_peca_nova_durante_job(self):
        initial = [{"id": "1", "tipo": "Petição", "titulo": "Inicial"}]
        final = [
            *initial,
            {"id": "2", "tipo": "Certidão", "titulo": "Juntada posterior"},
        ]

        delta = pje_client_module.reconciliar_manifestos_documentais(
            initial,
            final,
        )

        self.assertFalse(delta["reconciled"])
        self.assertEqual(delta["added_document_ids"], ["2"])
        self.assertEqual(delta["removed_document_ids"], [])

    def test_manifesto_v2_preserva_vinculo_ordem_e_campos_desconhecidos(self):
        entry = complete.build_manifest_entry(
            {
                "id": "anexo-2",
                "documento_pai_id": "principal-1",
                "tipo": "Matrícula",
                "titulo": "Anexo",
                "data": "01/01/2026",
                "campo_novo_pje": {"valor": 7},
            },
            canonical_order=2,
        )

        self.assertEqual(entry["schema_version"], "pje.document-manifest/v2")
        self.assertEqual(entry["parent_document_id"], "principal-1")
        self.assertEqual(entry["canonical_order"], 2)
        self.assertEqual(
            entry["source_fields"]["campo_novo_pje"],
            {"valor": 7},
        )

    def test_payload_separa_hash_dos_bytes_do_hash_do_texto(self):
        source_hash = hashlib.sha256(b"%PDF-fixture").hexdigest()
        payload = complete.normalise_document_payload(
            {
                "id": "doc-hash",
                "tipo": "PDF",
                "titulo": "Fixture",
            },
            {
                "texto": "--- Página 1 ---\nTexto extraído",
                "source_bytes_sha256": source_hash,
            },
        )

        self.assertEqual(payload["source_bytes_sha256"], source_hash)
        self.assertEqual(payload["content_sha256"], source_hash)
        self.assertNotEqual(payload["extracted_text_sha256"], source_hash)

    def test_pdf_misto_marca_apenas_pagina_sem_texto_para_ocr(self):
        payload = complete.normalise_document_payload(
            {"id": "doc-misto", "tipo": "PDF", "titulo": "Misto"},
            {
                "texto": (
                    "--- Página 1 ---\nTexto nativo\n\n"
                    "--- Página 2 ---\n"
                ),
                "num_paginas": 2,
                "paginas_sem_texto": [2],
                "paginas_estruturadas": [
                    {
                        "page": 1,
                        "tables": [[["bem", "valor"], ["imóvel", "100"]]],
                        "extraction_method": "native_text",
                        "status": "extracted",
                        "confidence": 1.0,
                    },
                    {
                        "page": 2,
                        "tables": [],
                        "extraction_method": "none",
                        "status": "ocr_required_unavailable",
                        "confidence": 0.0,
                    },
                ],
                "ocr_status": "required_unavailable",
            },
        )

        self.assertEqual(payload["page_coverage"]["native_text"], 1)
        self.assertEqual(payload["page_coverage"]["failed_or_unavailable"], 1)
        self.assertEqual(payload["pages"][0]["tables"][0][1][0], "imóvel")
        self.assertEqual(
            payload["pages"][1]["status"],
            "ocr_required_unavailable",
        )

    def test_all_documents_duplicates_contradiction_and_next_act(self):
        order = synthetic_document(
            "doc-order",
            (
                "Determino que, após a manifestação do Ministério Público, "
                "voltem os autos conclusos para sentença. A assertiva I da "
                "questão 92 é verdadeira."
            ),
            title="Decisão sintética",
        )
        opinion = synthetic_document(
            "doc-opinion",
            (
                "Ação: Mandado de Segurança Sintético\n"
                "Ao Douto Juízo da Vara Sintética\n"
                "O Ministério Público apresenta manifestação final. "
                "A assertiva I da questão 92 é falsa."
            ),
            title="Manifestação sintética",
        )
        documents = [opinion, order]
        for index in range(23):
            documents.append(
                synthetic_document(
                    f"doc-{index}",
                    f"Conteúdo sintético exclusivo do documento {index}.",
                )
            )
        duplicate = dict(documents[-1])
        duplicate["document_id"] = "doc-duplicate"
        duplicate["source_fingerprint"] = "fingerprint-duplicate"
        manifest = [
            {
                "document_id": item["document_id"],
                "type": item["type"],
                "title": item["title"],
                "date": item["date"],
                "source_fingerprint": item["source_fingerprint"],
            }
            for item in [*documents, duplicate]
        ]
        states = [
            {
                "document_id": item["document_id"],
                "status": "completed",
                "pages_without_text": 0,
                "truncated": False,
                "duplicate_of": "",
            }
            for item in documents
        ]
        states.append(
            {
                "document_id": duplicate["document_id"],
                "status": "duplicate",
                "pages_without_text": 0,
                "truncated": False,
                "duplicate_of": documents[-1]["document_id"],
            }
        )
        dossier = complete.build_dossier(
            process_number=SYNTHETIC_CNJ,
            base={"movements": [], "parties": []},
            manifest=manifest,
            documents=documents,
            document_states_value=states,
            tree_complete=True,
            expedients=[],
            collected_at="2026-01-01T00:00:00+00:00",
        )
        self.assertTrue(dossier["coverage"]["complete"])
        self.assertEqual(dossier["coverage"]["documents_discovered"], 26)
        self.assertEqual(dossier["coverage"]["documents_deduplicated"], 1)
        self.assertEqual(
            dossier["next_steps"][0]["act"],
            "Conclusão para sentença",
        )
        self.assertEqual(len(dossier["contradictions"]), 1)
        self.assertEqual(
            dossier["case"]["case_class"],
            "Mandado de Segurança Sintético",
        )
        self.assertEqual(
            set(dossier["case"]["evidence"]["case_class"][0]),
            {
                "document_id",
                "page",
                "excerpt",
                "sha256",
                "source_bytes_sha256",
            },
        )
        citation = dossier["contradictions"][0]["positive_evidence"][0]
        self.assertEqual(
            set(citation),
            {
                "document_id",
                "page",
                "excerpt",
                "sha256",
                "source_bytes_sha256",
            },
        )

    def test_missing_text_prevents_complete_result(self):
        document = synthetic_document("doc-one", "Texto sintético.")
        dossier = complete.build_dossier(
            process_number=SYNTHETIC_CNJ,
            base={"movements": [], "parties": []},
            manifest=[
                {
                    "document_id": "doc-one",
                    "type": "Petição",
                    "title": "Petição",
                    "date": None,
                    "source_fingerprint": "fingerprint-doc-one",
                }
            ],
            documents=[document],
            document_states_value=[
                {
                    "document_id": "doc-one",
                    "status": "completed",
                    "pages_without_text": 1,
                    "truncated": False,
                    "duplicate_of": "",
                }
            ],
            tree_complete=True,
            expedients=[],
            collected_at="2026-01-01T00:00:00+00:00",
        )
        self.assertFalse(dossier["coverage"]["complete"])
        self.assertIn("OCR não estava disponível", " ".join(dossier["gaps"]))

    def test_expediente_indisponivel_nao_equivale_a_zero_confirmado(self):
        document = synthetic_document("doc-one", "Texto sintético.")
        common = {
            "process_number": SYNTHETIC_CNJ,
            "base": {"movements": [], "parties": []},
            "manifest": [
                {
                    "document_id": "doc-one",
                    "type": "Petição",
                    "title": "Petição",
                    "date": None,
                    "source_fingerprint": "fingerprint-doc-one",
                }
            ],
            "documents": [document],
            "document_states_value": [
                {
                    "document_id": "doc-one",
                    "status": "completed",
                    "pages_without_text": 0,
                    "truncated": False,
                    "duplicate_of": "",
                }
            ],
            "tree_complete": True,
            "expedients": [],
            "collected_at": "2026-01-01T00:00:00+00:00",
        }

        unavailable = complete.build_dossier(
            **common,
            expedients_complete=False,
            expedients_safe_error="aba indisponível",
        )
        confirmed_empty = complete.build_dossier(
            **common,
            expedients_complete=True,
        )

        self.assertFalse(unavailable["coverage"]["complete"])
        self.assertEqual(unavailable["expedients"]["total"], 0)
        self.assertFalse(unavailable["expedients"]["complete"])
        self.assertIn(
            "SOURCE_EXPEDIENTS_UNAVAILABLE",
            {item["code"] for item in unavailable["gap_details"]},
        )
        self.assertTrue(confirmed_empty["coverage"]["complete"])
        self.assertTrue(confirmed_empty["expedients"]["complete"])


class SingleOpenCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_five_documents_use_one_open(self):
        client = PJeClient.__new__(PJeClient)
        client._op_lock = asyncio.Lock()
        client._ultimo_processo_foi_terceiro = False
        client._ultima_arvore_completa = True

        class SyntheticPage:
            async def inner_text(self, _selector):
                return "Processo sintético"

            async def content(self):
                return "<html></html>"

            def locator(self, _selector):
                locator = AsyncMock()
                locator.click.side_effect = RuntimeError("sem expedientes sintéticos")
                return locator

        page = SyntheticPage()
        client._abrir_autos_processo = AsyncMock(return_value=page)
        client._fechar_aba_autos = AsyncMock()
        client._extrair_partes = lambda *_: {}
        client._extrair_movimentacoes_dom = lambda *_: []
        client._extrair_movimentacoes = lambda *_: []
        documents = [
            {
                "id": str(index),
                "tipo": "Peça sintética",
                "titulo": f"Peça sintética {index}",
                "data": "",
            }
            for index in range(25)
        ]
        client._extrair_documentos_da_aba = AsyncMock(return_value=documents)
        client._ler_documento_rest = AsyncMock(
            return_value={"texto": "--- Página 1 ---\nTexto inteiramente sintético."}
        )
        seen = []

        async def on_manifest(base, manifest, complete_tree):
            self.assertTrue(complete_tree)
            self.assertEqual(len(manifest), 25)
            return {}

        async def on_document(document, cached, response, source):
            seen.append(document["id"])

        async def should_cancel():
            return False

        result = await client.coletar_processo_integral(
            SYNTHETIC_CNJ,
            on_manifest,
            on_document,
            should_cancel,
        )
        self.assertTrue(result["single_open"])
        self.assertFalse(result["expedients_complete"])
        self.assertTrue(result["expedients_safe_error"])
        self.assertEqual(len(seen), 25)
        client._abrir_autos_processo.assert_awaited_once()
        client._fechar_aba_autos.assert_awaited_once()
        self.assertEqual(client._ler_documento_rest.await_count, 25)

    async def test_selector_reads_only_priority_subset(self):
        client = PJeClient.__new__(PJeClient)
        client._op_lock = asyncio.Lock()
        client._ultimo_processo_foi_terceiro = False
        client._ultima_arvore_completa = True

        class SyntheticPage:
            async def inner_text(self, _selector):
                return "Processo sintético"

            async def content(self):
                return "<html></html>"

            def locator(self, _selector):
                locator = AsyncMock()
                locator.click.side_effect = RuntimeError("não deveria abrir expedientes")
                return locator

        documents = [
            {"id": str(index), "tipo": "Peça", "titulo": f"Peça {index}"}
            for index in range(10)
        ]
        client._abrir_autos_processo = AsyncMock(return_value=SyntheticPage())
        client._fechar_aba_autos = AsyncMock()
        client._extrair_partes = lambda *_: {}
        client._extrair_movimentacoes_dom = lambda *_: []
        client._extrair_movimentacoes = lambda *_: []
        client._extrair_documentos_da_aba = AsyncMock(return_value=documents)
        client._ler_documento_rest = AsyncMock(return_value={"texto": "texto"})
        seen = []

        async def on_manifest(_base, _manifest, _complete_tree):
            return {}

        async def on_document(document, _cached, _response, _source):
            seen.append(document["id"])

        async def should_cancel():
            return False

        result = await client.coletar_processo_integral(
            SYNTHETIC_CNJ,
            on_manifest,
            on_document,
            should_cancel,
            document_selector=lambda items: items[-3:],
            concurrency=9,
            collect_expedients=False,
        )
        self.assertEqual(seen, ["7", "8", "9"])
        self.assertEqual(result["documents_selected"], 3)
        self.assertEqual(result["documents_skipped"], 7)
        self.assertEqual(result["concurrency"], 4)
        self.assertEqual(client._ler_documento_rest.await_count, 3)

    async def test_parallel_processes_use_isolated_pages_and_overlap(self):
        client = PJeClient.__new__(PJeClient)
        client._op_lock = asyncio.Lock()
        client._parallel_open_lock = asyncio.Lock()
        client._parallel_process_semaphore = asyncio.Semaphore(3)
        client._ultimo_processo_foi_terceiro = False
        client._ultima_arvore_completa = True
        active = 0
        peak = 0
        both_entered = asyncio.Event()

        class SyntheticPage:
            async def inner_text(self, _selector):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_entered.set()
                await asyncio.wait_for(both_entered.wait(), timeout=1)
                active -= 1
                return "Processo sintético"

            async def content(self):
                return "<html></html>"

        pages = [SyntheticPage(), SyntheticPage()]
        client._abrir_autos_processo = AsyncMock(side_effect=pages)
        client._fechar_aba_autos = AsyncMock()
        client._extrair_partes = lambda *_: {}
        client._extrair_movimentacoes_dom = lambda *_: []
        client._extrair_movimentacoes = lambda *_: []
        client._extrair_documentos_da_aba = AsyncMock(return_value=[])

        async def on_manifest(_base, _manifest, _complete_tree):
            return {}

        async def on_document(_document, _cached, _response, _source):
            raise AssertionError("não há documentos neste fixture")

        async def should_cancel():
            return False

        results = await asyncio.gather(
            *[
                client.coletar_processo_integral(
                    f"processo-{index}",
                    on_manifest,
                    on_document,
                    should_cancel,
                    collect_expedients=False,
                    parallel_process=True,
                )
                for index in range(2)
            ]
        )
        self.assertEqual(peak, 2)
        self.assertTrue(all(item["single_open"] for item in results))
        self.assertEqual(client._abrir_autos_processo.await_count, 2)
        self.assertEqual(client._fechar_aba_autos.await_count, 2)


class DomainExtractorTests(unittest.TestCase):
    def test_inventario_extractor(self):
        docs = [
            synthetic_document("doc-1", (
                "Inicial de inventário.\n"
                "Falecido: João da Silva Santos.\n"
                "Meeira: Maria Souza Santos.\n"
                "Herdeiro: Pedro Santos.\n"
                "Herdeiro: Ana Santos.\n"
                "Não deixou testamento.\n"
                "Bens a inventariar: Imóvel residencial localizado em Belém.\n"
                "O imposto ITCD foi pago integralmente."
            ), title="Petição Inicial")
        ]
        res = complete._extract_inventario_data(docs)
        self.assertEqual(res["falecido"], "João da Silva Santos")
        self.assertEqual(res["meeiro"], "Maria Souza Santos")
        self.assertIn("Pedro Santos", res["herdeiros"])
        self.assertIn("Ana Santos", res["herdeiros"])
        self.assertFalse(res["testamento"])
        self.assertTrue(res["itcd_pago"])
        self.assertTrue(any("Imóvel" in b for b in res["bens"]))

    def test_usucapiao_extractor(self):
        docs = [
            synthetic_document("doc-1", (
                "Ação de usucapião ordinária.\n"
                "Confrontante: Carlos de Alencar.\n"
                "Confrontante: Roberto de Oliveira.\n"
                "Os réus encontram-se em local incerto.\n"
                "Planta e memorial descritivo anexados na folha 45.\n"
                "A União manifestou que não tem interesse na área."
            ), title="Petição Inicial")
        ]
        res = complete._extract_usucapiao_data(docs)
        self.assertIn("Carlos de Alencar", res["confrontantes"])
        self.assertIn("Roberto de Oliveira", res["confrontantes"])
        self.assertTrue(res["local_incerto"])
        self.assertTrue(res["planta_memorial"])
        self.assertEqual(res["manifestacoes"]["uniao"], "sem_interesse")


if __name__ == "__main__":
    unittest.main()
