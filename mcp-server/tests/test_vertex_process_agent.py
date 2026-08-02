"""Testes do agente Vertex AI sem enviar autos reais ao provedor."""

import base64
import hashlib
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

complete = importlib.import_module("analise_processual_completa")
audit = importlib.import_module("auditoria_processual")
agent = importlib.import_module("vertex_process_agent")

CNJ = "0000000-00.0000.0.00.0000"


class VertexProcessAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "PJE_STORAGE_DIR",
                "PJE_AUDIT_MASTER_KEY",
                "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_CLOUD_LOCATION",
                "PJE_ADK_AGENT_URL",
            )
        }
        os.environ["PJE_STORAGE_DIR"] = self.temp_dir.name
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"v" * 32
        ).decode()
        os.environ["GOOGLE_CLOUD_PROJECT"] = "projeto-sintetico"
        os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

        self.text = (
            "O requerido apresentou contestação e alegou pagamento integral. "
            "O comprovante ainda não foi juntado aos autos."
        )
        self.digest = hashlib.sha256(self.text.encode()).hexdigest()
        self.document = {
            "document_id": "doc-1",
            "title": "Contestação sintética",
            "type": "Contestação",
            "date": "01/08/2026",
            "pages": [{"page": 1, "text": self.text}],
            "content_sha256": self.digest,
            "source_bytes_sha256": self.digest,
            "source_fingerprint": "fingerprint-doc-1",
        }
        self.job = complete.create_job(
            process_number=CNJ,
            grau="1g",
            persona="servidor",
            mode="rapida",
            semantic_enabled=False,
            force_reread=False,
            authorisation_ref="autorização sintética",
        )
        audit.save_cached_document(
            CNJ,
            "doc-1",
            "fingerprint-doc-1",
            self.digest,
            self.document,
        )
        complete.save_result(
            self.job["job_id"],
            {
                "manifest": [
                    {
                        "document_id": "doc-1",
                        "type": "Contestação",
                        "title": "Contestação sintética",
                        "date": "01/08/2026",
                        "source_fingerprint": "fingerprint-doc-1",
                        "status": "completed",
                    }
                ],
                "case": {},
                "timeline": [],
                "coverage": {
                    "complete": True,
                    "documents_failed": 0,
                    "pages_without_text": 0,
                    "truncated_documents": 0,
                },
                "gaps": [],
            },
        )

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def valid_draft(self):
        return agent.AgentDraft(
            findings=[
                agent.AgentFinding(
                    finding_id="defesa-pagamento",
                    category="defesa",
                    title="Alegação de pagamento",
                    conclusion="O requerido alegou pagamento integral.",
                    confidence=0.95,
                    evidence=[
                        agent.AgentEvidence(
                            document_id="doc-1",
                            page=1,
                            excerpt="O requerido apresentou contestação e alegou pagamento integral.",
                            sha256=self.digest,
                        )
                    ],
                )
            ],
            unknowns=["Não há comprovante de pagamento no recorte analisado."],
        )

    def test_verifier_rejects_invented_excerpt_and_hash(self):
        _, documents = agent._load_source_documents(self.job["job_id"])
        valid = agent.verify_draft(self.valid_draft(), documents)
        self.assertEqual(valid["verification"]["verified_findings"], 1)

        invalid = self.valid_draft()
        invalid.findings[0].evidence[0].excerpt = "Trecho que não existe nos autos."
        invalid.findings[0].evidence[0].sha256 = "0" * 64
        rejected = agent.verify_draft(invalid, documents)
        self.assertEqual(rejected["findings"], [])
        self.assertEqual(rejected["verification"]["rejected_findings"], 1)

    def test_verifier_grounds_paraphrase_and_altered_hash(self):
        _, documents = agent._load_source_documents(self.job["job_id"])
        draft = self.valid_draft()
        draft.findings[0].evidence[0].excerpt = (
            "O requerido apresentou a contestação alegando o pagamento integral"
        )
        draft.findings[0].evidence[0].sha256 = "f" * 64
        result = agent.verify_draft(draft, documents)
        self.assertEqual(result["verification"]["verified_findings"], 1)
        grounding = result["verification"]["literal_grounding"]
        self.assertEqual(grounding["excerpts_recovered"], 1)
        self.assertEqual(grounding["hashes_corrected"], 1)
        published = result["findings"][0]["evidence"][0]
        self.assertEqual(published["sha256"], self.digest)
        self.assertIn(published["excerpt"].casefold(), self.text.casefold())

    def test_run_is_persisted_encrypted_and_explainable(self):
        run = agent.create_run(self.job["job_id"])
        with patch.object(
            agent,
            "_call_vertex",
            return_value=(
                self.valid_draft(),
                {"model_version": "gemini-3.5-flash", "total_tokens": 123},
            ),
        ):
            completed = agent.execute_run(run["run_id"])

        self.assertEqual(completed["status"], "completed")
        result = agent.get_result(run["run_id"])
        self.assertTrue(result["available"])
        self.assertEqual(result["result"]["verification"]["verified_findings"], 1)
        explained = agent.explain(run["run_id"], "defesa-pagamento")
        self.assertTrue(explained["verified"])
        self.assertEqual(explained["finding"]["evidence"][0]["page"], 1)

        database = Path(self.temp_dir.name) / "auditoria_processual/auditoria.sqlite3"
        connection = sqlite3.connect(database)
        try:
            encrypted = connection.execute(
                "SELECT encrypted_result FROM vertex_process_agent_runs WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn(b"pagamento integral", encrypted)

    def test_inventory_cannot_start_agent(self):
        inventory = complete.create_job(
            process_number=CNJ,
            grau="1g",
            persona="servidor",
            mode="inventario",
            semantic_enabled=False,
            force_reread=False,
            authorisation_ref="autorização sintética",
        )
        complete.save_result(inventory["job_id"], {"manifest": []})
        with self.assertRaises(agent.VertexAgentError):
            agent.create_run(inventory["job_id"])

    def test_incomplete_source_never_creates_agent_run(self):
        source = complete.get_result(self.job["job_id"])["dossier"]
        source["coverage"]["complete"] = False
        complete.save_result(self.job["job_id"], source)
        with patch.object(agent, "_call_vertex") as model_call:
            with self.assertRaisesRegex(agent.VertexAgentError, "incompleto"):
                agent.create_run(self.job["job_id"])
        model_call.assert_not_called()

    def test_context_truncation_blocks_model_call(self):
        run = agent.create_run(self.job["job_id"])
        with patch.dict(os.environ, {"PJE_VERTEX_CONTEXT_CHARS": "50000"}):
            _, documents = agent._load_source_documents(self.job["job_id"])
            documents[0]["pages"][0]["text"] = "x" * 50001
            with self.assertRaisesRegex(agent.VertexAgentError, "divisão local"):
                agent._build_model_input(
                    complete.get_result(self.job["job_id"]), documents
                )

    def test_run_uses_adk_when_url_is_configured(self):
        run = agent.create_run(self.job["job_id"])
        report = {
            "read_only": True,
            "documents_reviewed": ["doc-1"],
            "acts": [
                {
                    "sequence": 1,
                    "act_type": "peticao",
                    "summary": "Contestação com alegação de pagamento",
                    "procedural_effect": "A defesa alegou pagamento integral.",
                    "evidence": [
                        {
                            "document_id": "doc-1",
                            "page": 1,
                            "excerpt": "O requerido apresentou contestação e alegou pagamento integral.",
                            "sha256": self.digest,
                        }
                    ],
                }
            ],
            "unknowns": ["Comprovante não localizado."],
            "recommended_human_reviews": [],
        }
        os.environ["PJE_ADK_AGENT_URL"] = "http://127.0.0.1:8000"
        with patch.object(
            agent.adk_process_client,
            "analyze_inventory",
            return_value=(report, {"transport": "adk_http_sse", "total_tokens": 10}),
        ):
            agent.execute_run(run["run_id"])
        result = agent.get_result(run["run_id"])["result"]
        self.assertEqual(result["provider"], "vertex_ai_adk")
        self.assertEqual(result["verification"]["verified_findings"], 1)
        self.assertEqual(result["inventory_report"]["acts"][0]["sequence"], 1)


class EphemeralManifestOrchestratorTests(unittest.TestCase):
    """Orquestração pelo manifesto efêmero, sem tocar o banco persistente."""

    def setUp(self):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "projeto-sintetico"
        os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
        os.environ.pop("PJE_ADK_AGENT_URL", None)
        self.text_1 = (
            "A petição inicial sintética requereu a abertura do inventário "
            "e indicou os herdeiros conhecidos."
        )
        self.text_2 = (
            "A decisão sintética nomeou a inventariante e determinou o "
            "compromisso legal em cinco dias."
        )

    def _manifest(self):
        documents = []
        for index, text in enumerate([self.text_1, self.text_2], start=1):
            documents.append(
                {
                    "document_id": f"doc-{index}",
                    "title": f"Peça {index}",
                    "type": "peça",
                    "date": "01/08/2026",
                    "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "pages": [
                        {
                            "page": index,
                            "text": text,
                            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        }
                    ],
                }
            )
        return {
            "schema_version": "pje.consolidated-process/v1",
            "capsule_id": "0" * 32,
            "complete": True,
            "fail_closed": True,
            "page_count": 2,
            "documents": documents,
            "coverage": {
                "pages_expected": 2,
                "pages_extracted": 2,
                "pages_mapped": 2,
                "empty_pages": [],
                "truncated": False,
            },
        }

    def _shards(self, manifest):
        shards = []
        for index, document in enumerate(manifest["documents"], start=1):
            page = document["pages"][0]
            shards.append(
                {
                    "shard_id": f"shard-{index:04d}",
                    "characters": len(page["text"]),
                    "pages": [
                        {
                            "document_id": document["document_id"],
                            "page": page["page"],
                            "text": page["text"],
                            "text_sha256": page["text_sha256"],
                        }
                    ],
                }
            )
        return shards

    @staticmethod
    def _fake_synthesis(model, contents):
        import json as _json

        payload = _json.loads(contents)
        finding_ids = [
            item["finding_id"] for item in payload["verified_findings"]
        ]
        synthesis = agent.AgentSynthesis(
            executive_summary=(
                "Síntese sintética consolidada a partir dos achados verificados."
            ),
            current_state="Processo sintético em fase inicial de instrução.",
            suggestions=[
                agent.AgentSuggestion(
                    item="Conferir a juntada da peça sintética.",
                    rationale="Amparada em achado verificado.",
                    priority="alta",
                    supporting_finding_ids=[finding_ids[0]],
                ),
                agent.AgentSuggestion(
                    item="Sugestão sem amparo em achado verificado.",
                    rationale="Aponta achado inexistente.",
                    priority="baixa",
                    supporting_finding_ids=["finding-inexistente"],
                ),
            ],
            open_questions=["Falta manifestação sintética do MP."],
        )
        return synthesis, {"model_version": model}

    @staticmethod
    def _fake_call(model, model_input):
        import json as _json

        payload = _json.loads(model_input)
        findings = []
        for document in payload["documents"]:
            page = document["pages"][0]
            findings.append(
                agent.AgentFinding(
                    finding_id=f"f-{document['document_id']}",
                    category="fato",
                    title=f"Ato da peça {document['document_id']}",
                    conclusion=page["text"][:100],
                    confidence=0.9,
                    evidence=[
                        agent.AgentEvidence(
                            document_id=document["document_id"],
                            page=page["page"],
                            excerpt=page["text"][:60],
                            sha256=document["sha256"],
                        )
                    ],
                )
            )
        return agent.AgentDraft(findings=findings), {"model_version": model}

    def test_single_call_when_context_fits(self):
        manifest = self._manifest()
        with (
            patch.object(agent, "_call_vertex", side_effect=self._fake_call),
            patch.object(
                agent, "_call_vertex_synthesis", side_effect=self._fake_synthesis
            ),
        ):
            result = agent.run_ephemeral_manifest(manifest, self._shards(manifest))
        self.assertEqual(result["provider"], "vertex_ai_direct")
        self.assertEqual(result["source"], "ephemeral_capsule")
        self.assertFalse(result["process_data_persisted"])
        self.assertEqual(result["input_coverage"]["shards_used"], 1)
        self.assertEqual(result["verification"]["verified_findings"], 2)

    def test_shard_workers_when_context_exceeded(self):
        manifest = self._manifest()
        with (
            patch.object(agent, "_context_limit", return_value=100),
            patch.object(agent, "_call_vertex", side_effect=self._fake_call),
            patch.object(
                agent, "_call_vertex_synthesis", side_effect=self._fake_synthesis
            ),
        ):
            result = agent.run_ephemeral_manifest(manifest, self._shards(manifest))
        self.assertEqual(result["provider"], "vertex_ai_direct_sharded")
        self.assertEqual(result["input_coverage"]["shards_used"], 2)
        self.assertEqual(result["verification"]["verified_findings"], 2)
        finding_ids = {item["finding_id"] for item in result["findings"]}
        self.assertEqual(
            finding_ids, {"shard-0001:f-doc-1", "shard-0002:f-doc-2"}
        )

    def test_specialist_synthesis_with_deterministic_reviewer(self):
        manifest = self._manifest()
        with (
            patch.object(agent, "_call_vertex", side_effect=self._fake_call),
            patch.object(
                agent, "_call_vertex_synthesis", side_effect=self._fake_synthesis
            ) as synthesis_call,
        ):
            result = agent.run_ephemeral_manifest(manifest, self._shards(manifest))
        synthesis = result["sintese"]
        self.assertEqual(synthesis["status"], "completed")
        self.assertTrue(synthesis["fail_closed"])
        self.assertEqual(len(synthesis["suggestions"]), 1)
        self.assertEqual(len(synthesis["rejected_suggestions"]), 1)
        self.assertIn(
            "finding_id não verificado",
            synthesis["rejected_suggestions"][0]["reason"],
        )
        # O especialista recebe apenas achados estruturados, nunca as páginas.
        (_, contents), _kwargs = synthesis_call.call_args
        self.assertIn('"verified_findings"', contents)
        self.assertNotIn('"pages"', contents)
        self.assertNotIn('"documents"', contents)

    def test_synthesis_failure_keeps_verified_findings(self):
        manifest = self._manifest()
        with (
            patch.object(agent, "_call_vertex", side_effect=self._fake_call),
            patch.object(
                agent,
                "_call_vertex_synthesis",
                side_effect=RuntimeError("indisponível"),
            ),
        ):
            result = agent.run_ephemeral_manifest(manifest, self._shards(manifest))
        self.assertEqual(result["sintese"]["status"], "failed")
        self.assertEqual(result["verification"]["verified_findings"], 2)

    def test_context_exceeded_without_shards_blocks(self):
        manifest = self._manifest()
        with (
            patch.object(agent, "_context_limit", return_value=100),
            patch.object(agent, "_call_vertex") as call,
        ):
            with self.assertRaisesRegex(agent.VertexAgentError, "shards"):
                agent.run_ephemeral_manifest(manifest, [])
        call.assert_not_called()

    def test_incomplete_manifest_never_calls_model(self):
        manifest = self._manifest()
        manifest["complete"] = False
        with patch.object(agent, "_call_vertex") as call:
            with self.assertRaisesRegex(agent.VertexAgentError, "incompleto"):
                agent.run_ephemeral_manifest(manifest, self._shards(manifest))
        call.assert_not_called()

    def test_unknown_model_is_rejected(self):
        manifest = self._manifest()
        with patch.object(agent, "_call_vertex") as call:
            with self.assertRaisesRegex(agent.VertexAgentError, "modelo"):
                agent.run_ephemeral_manifest(
                    manifest, self._shards(manifest), model="gemini-3.6-flash"
                )
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
