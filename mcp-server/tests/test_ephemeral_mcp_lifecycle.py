from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fitz

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import analysis_capsule
import server


class EphemeralMcpLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_pdf_runs_end_to_end_then_is_purged(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("PJE_ANALYSIS_CAPSULE_DIR")
            os.environ["PJE_ANALYSIS_CAPSULE_DIR"] = temporary
            try:
                source = Path(temporary) / "source.pdf"
                document = fitz.open()
                page = document.new_page()
                page.insert_text((72, 72), "ID do documento: 300001 PETICAO SINTETICA " * 3)
                document.save(source)
                document.close()

                class FakeClient:
                    async def listar_documentos(self, _numero):
                        return {
                            "arvore_completa": True,
                            "documentos": [{"id": "300001", "tipo": "Petição"}],
                        }

                    async def baixar_processo_nativo(self, **kwargs):
                        shutil.copyfile(source, kwargs["caminho_destino"])
                        return {"integridade_validada": True}

                capsule = analysis_capsule.create_capsule(mode="training")
                job_id = uuid.uuid4().hex
                server._ephemeral_pdf_jobs[job_id] = {
                    "job_id": job_id,
                    "capsule_id": capsule.capsule_id,
                    "status": "queued",
                    "mode": "training",
                    "complete": False,
                }
                with patch.object(
                    server.cliente_singleton,
                    "get_cliente",
                    new=AsyncMock(return_value=FakeClient()),
                ):
                    await server._executar_pdf_integral_efemero(
                        job_id,
                        capsule=capsule,
                        numero_cnj="00000000000000000000",
                        persona="advogado",
                        grau="1g",
                    )
                self.assertEqual(server._ephemeral_pdf_jobs[job_id]["status"], "completed")

                result = await server.analisar_processo_completo_pje(
                    acao="resultado_pdf_integral",
                    job_id=job_id,
                    autorizacao_leitura=True,
                    autorizacao_ref="teste sintético local",
                    persona="advogado",
                )
                self.assertTrue(result["manifest"]["complete"])
                self.assertEqual(result["manifest"]["page_count"], 1)
                self.assertEqual(len(result["shards"]), 1)

                receipt = await server.analisar_processo_completo_pje(
                    acao="registrar_resultado_teste",
                    job_id=job_id,
                    teste_aprovado=True,
                    persona="advogado",
                )
                self.assertTrue(receipt["verified_absent"])
                self.assertFalse(capsule.path.exists())
            finally:
                if previous is None:
                    os.environ.pop("PJE_ANALYSIS_CAPSULE_DIR", None)
                else:
                    os.environ["PJE_ANALYSIS_CAPSULE_DIR"] = previous

    async def test_passing_test_through_mcp_action_erases_capsule(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("PJE_ANALYSIS_CAPSULE_DIR")
            os.environ["PJE_ANALYSIS_CAPSULE_DIR"] = temporary
            try:
                capsule = analysis_capsule.create_capsule(mode="training")
                capsule.file("source/process.pdf").write_bytes(b"synthetic process")
                job_id = uuid.uuid4().hex
                server._ephemeral_pdf_jobs[job_id] = {
                    "job_id": job_id,
                    "capsule_id": capsule.capsule_id,
                    "status": "completed",
                    "mode": "training",
                    "complete": True,
                }

                result = await server.analisar_processo_completo_pje(
                    acao="registrar_resultado_teste",
                    job_id=job_id,
                    teste_aprovado=True,
                    persona="advogado",
                )

                self.assertTrue(result["purged"])
                self.assertTrue(result["verified_absent"])
                self.assertFalse(capsule.path.exists())
                self.assertNotIn(job_id, server._ephemeral_pdf_jobs)
            finally:
                if previous is None:
                    os.environ.pop("PJE_ANALYSIS_CAPSULE_DIR", None)
                else:
                    os.environ["PJE_ANALYSIS_CAPSULE_DIR"] = previous


    async def test_ephemeral_agent_runs_from_capsule_and_dies_with_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("PJE_ANALYSIS_CAPSULE_DIR")
            os.environ["PJE_ANALYSIS_CAPSULE_DIR"] = temporary
            try:
                import hashlib
                import json

                text = (
                    "ID do documento: 300001 A petição sintética requereu a "
                    "abertura do inventário do espólio fictício."
                )
                digest = hashlib.sha256(text.encode()).hexdigest()
                capsule = analysis_capsule.create_capsule(mode="training")
                manifest = {
                    "schema_version": "pje.consolidated-process/v1",
                    "capsule_id": capsule.capsule_id,
                    "complete": True,
                    "fail_closed": True,
                    "page_count": 1,
                    "documents": [
                        {
                            "document_id": "300001",
                            "title": "Petição sintética",
                            "type": "Petição",
                            "date": "01/08/2026",
                            "content_sha256": digest,
                            "pages": [
                                {"page": 1, "text": text, "text_sha256": digest}
                            ],
                        }
                    ],
                    "coverage": {
                        "pages_expected": 1,
                        "pages_extracted": 1,
                        "pages_mapped": 1,
                        "empty_pages": [],
                        "truncated": False,
                    },
                }
                shards = [
                    {
                        "shard_id": "shard-0001",
                        "characters": len(text),
                        "pages": [
                            {
                                "document_id": "300001",
                                "page": 1,
                                "text": text,
                                "text_sha256": digest,
                            }
                        ],
                    }
                ]
                capsule.file("manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
                )
                capsule.file("shards.json").write_text(
                    json.dumps(shards, ensure_ascii=False), encoding="utf-8"
                )
                job_id = uuid.uuid4().hex
                server._ephemeral_pdf_jobs[job_id] = {
                    "job_id": job_id,
                    "capsule_id": capsule.capsule_id,
                    "status": "completed",
                    "mode": "training",
                    "complete": True,
                }

                import vertex_process_agent as agent_module

                def fake_call(model, model_input):
                    payload = json.loads(model_input)
                    document = payload["documents"][0]
                    page = document["pages"][0]
                    draft = agent_module.AgentDraft(
                        findings=[
                            agent_module.AgentFinding(
                                finding_id="f-1",
                                category="fato",
                                title="Petição sintética juntada",
                                conclusion="A petição sintética abriu o inventário.",
                                confidence=0.9,
                                evidence=[
                                    agent_module.AgentEvidence(
                                        document_id=document["document_id"],
                                        page=page["page"],
                                        excerpt=page["text"][:60],
                                        sha256=document["sha256"],
                                    )
                                ],
                            )
                        ]
                    )
                    return draft, {"model_version": model}

                def fake_synthesis(model, contents):
                    payload = json.loads(contents)
                    finding_ids = [
                        item["finding_id"]
                        for item in payload["verified_findings"]
                    ]
                    return (
                        agent_module.AgentSynthesis(
                            executive_summary=(
                                "Síntese sintética consolidada dos achados "
                                "verificados do processo fictício."
                            ),
                            current_state="Inventário sintético recém-aberto.",
                            suggestions=[
                                agent_module.AgentSuggestion(
                                    item="Conferir a petição sintética.",
                                    rationale="Amparada no achado verificado.",
                                    priority="alta",
                                    supporting_finding_ids=[finding_ids[0]],
                                )
                            ],
                        ),
                        {"model_version": model},
                    )

                os.environ.pop("PJE_ADK_AGENT_URL", None)
                with (
                    patch.object(agent_module, "_call_vertex", new=fake_call),
                    patch.object(
                        agent_module,
                        "_call_vertex_synthesis",
                        new=fake_synthesis,
                    ),
                ):
                    started = await server.analisar_processo_completo_pje(
                        acao="iniciar_agente",
                        job_id=job_id,
                        autorizacao_leitura=True,
                        autorizacao_ref="teste sintético local",
                        persona="advogado",
                    )
                    run_id = started["agent_run_id"]
                    self.assertEqual(started["source"], "ephemeral_capsule")
                    task = server._ephemeral_agent_tasks.get(run_id)
                    self.assertIsNotNone(task)
                    await task

                status = await server.analisar_processo_completo_pje(
                    acao="status_agente",
                    agent_run_id=run_id,
                    persona="advogado",
                )
                self.assertEqual(status["status"], "completed")
                self.assertFalse(status["process_data_persisted"])

                result = await server.analisar_processo_completo_pje(
                    acao="resultado_agente",
                    agent_run_id=run_id,
                    autorizacao_leitura=True,
                    autorizacao_ref="teste sintético local",
                    persona="advogado",
                )
                self.assertTrue(result["available"])
                self.assertEqual(
                    result["result"]["verification"]["verified_findings"], 1
                )
                self.assertTrue(
                    capsule.file("agent/result.json").is_file()
                )
                self.assertEqual(
                    result["result"]["sintese"]["status"], "completed"
                )
                relatorio = result["relatorio"]
                self.assertEqual(
                    relatorio["schema_version"], "pje.relatorio-processual/v1"
                )
                self.assertEqual(len(relatorio["sugestoes"]), 1)
                self.assertTrue(
                    relatorio["sugestoes"][0]["exige_conferencia_humana"]
                )
                self.assertFalse(
                    relatorio["sugestoes"][0]["execucao_automatica"]
                )
                self.assertIn("## Sugestões", result["relatorio_markdown"])
                self.assertIn("delivery_receipt", result)

                receipt = await server.analisar_processo_completo_pje(
                    acao="registrar_resultado_teste",
                    job_id=job_id,
                    teste_aprovado=True,
                    persona="advogado",
                )
                self.assertTrue(receipt["verified_absent"])
                self.assertFalse(capsule.path.exists())
                self.assertNotIn(run_id, server._ephemeral_agent_runs)
                self.assertNotIn(job_id, server._ephemeral_pdf_jobs)
            finally:
                if previous is None:
                    os.environ.pop("PJE_ANALYSIS_CAPSULE_DIR", None)
                else:
                    os.environ["PJE_ANALYSIS_CAPSULE_DIR"] = previous


    async def test_production_discard_requires_delivery_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("PJE_ANALYSIS_CAPSULE_DIR")
            os.environ["PJE_ANALYSIS_CAPSULE_DIR"] = temporary
            try:
                capsule = analysis_capsule.create_capsule(mode="production")
                job_id = uuid.uuid4().hex
                token = uuid.uuid4().hex
                server._ephemeral_pdf_jobs[job_id] = {
                    "job_id": job_id,
                    "capsule_id": capsule.capsule_id,
                    "status": "completed",
                    "mode": "production",
                    "complete": True,
                    "discard_token": token,
                }

                blocked = await server.analisar_processo_completo_pje(
                    acao="confirmar_descarte",
                    job_id=job_id,
                    persona="advogado",
                )
                self.assertIn("discard_token", blocked["erro"])
                self.assertTrue(capsule.path.exists())

                receipt = await server.analisar_processo_completo_pje(
                    acao="confirmar_descarte",
                    job_id=job_id,
                    confirmation_token=token,
                    persona="advogado",
                )
                self.assertTrue(receipt["verified_absent"])
                self.assertFalse(capsule.path.exists())
                self.assertNotIn(job_id, server._ephemeral_pdf_jobs)
            finally:
                if previous is None:
                    os.environ.pop("PJE_ANALYSIS_CAPSULE_DIR", None)
                else:
                    os.environ["PJE_ANALYSIS_CAPSULE_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
