"""Retenção zero: caminhos legados de persistência ficam bloqueados."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

retention_policy = importlib.import_module("retention_policy")
complete = importlib.import_module("analise_processual_completa")
audit = importlib.import_module("auditoria_processual")
agent = importlib.import_module("vertex_process_agent")
batch_engine = importlib.import_module("batch_engine")
caixas = importlib.import_module("caixas_tarefas")
server = importlib.import_module("server")

CNJ = "0000000-00.0000.0.00.0000"


class RetentionPolicyUnitTests(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.get("PJE_ZERO_RETENTION")

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("PJE_ZERO_RETENTION", None)
        else:
            os.environ["PJE_ZERO_RETENTION"] = self._previous

    def test_disabled_by_default(self):
        os.environ.pop("PJE_ZERO_RETENTION", None)
        self.assertFalse(retention_policy.zero_retention_enabled())
        retention_policy.require_legacy_persistence("teste")

    def test_enabled_variants(self):
        for value in ("1", "true", "YES", " On "):
            os.environ["PJE_ZERO_RETENTION"] = value
            self.assertTrue(retention_policy.zero_retention_enabled())
        os.environ["PJE_ZERO_RETENTION"] = "0"
        self.assertFalse(retention_policy.zero_retention_enabled())

    def test_require_raises_when_enabled(self):
        os.environ["PJE_ZERO_RETENTION"] = "1"
        with self.assertRaisesRegex(
            retention_policy.ZeroRetentionError, "fluxo efêmero"
        ):
            retention_policy.require_legacy_persistence("o recurso legado")


class LegacyWritePathsBlockedTests(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.get("PJE_ZERO_RETENTION")
        os.environ["PJE_ZERO_RETENTION"] = "1"

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("PJE_ZERO_RETENTION", None)
        else:
            os.environ["PJE_ZERO_RETENTION"] = self._previous

    def test_complete_analysis_job_blocked(self):
        with self.assertRaises(retention_policy.ZeroRetentionError):
            complete.create_job(
                process_number=CNJ,
                grau="1g",
                persona="servidor",
                mode="integral",
                semantic_enabled=False,
                force_reread=False,
                authorisation_ref="teste",
            )

    def test_complete_analysis_result_blocked(self):
        with self.assertRaises(retention_policy.ZeroRetentionError):
            complete.save_result("job-sintetico", {})

    def test_document_cache_blocked(self):
        with self.assertRaises(retention_policy.ZeroRetentionError):
            audit.save_cached_document(CNJ, "doc-1", "fp", "sha", {})

    def test_audit_dossier_blocked(self):
        with self.assertRaises(retention_policy.ZeroRetentionError):
            audit.save_dossier({"schema_version": audit.DOSSIER_SCHEMA})

    def test_vertex_persistent_run_blocked(self):
        with self.assertRaises(retention_policy.ZeroRetentionError):
            agent.create_run("job-sintetico")

    def test_batch_blocked(self):
        with self.assertRaises(retention_policy.ZeroRetentionError):
            batch_engine.criar_lote()

    def test_acervo_export_blocked(self):
        with self.assertRaises(retention_policy.ZeroRetentionError):
            caixas.exportar_acervo(autorizacao_ref="teste")


class ServerActionBlockedTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_actions_redirect_to_ephemeral_flow(self):
        previous = os.environ.get("PJE_ZERO_RETENTION")
        os.environ["PJE_ZERO_RETENTION"] = "1"
        try:
            for acao in ("iniciar", "analisar_varios", "iniciar_agente"):
                result = await server.analisar_processo_completo_pje(
                    acao=acao,
                    numero_cnj=CNJ,
                    job_id="job-legado",
                    autorizacao_leitura=True,
                    autorizacao_ref="teste",
                    persona="advogado",
                )
                self.assertIn("retenção zero", result["erro"])
                self.assertIn("preparar_pdf_integral", result["alternativa"])
        finally:
            if previous is None:
                os.environ.pop("PJE_ZERO_RETENTION", None)
            else:
                os.environ["PJE_ZERO_RETENTION"] = previous

    async def test_status_reads_remain_allowed(self):
        previous = os.environ.get("PJE_ZERO_RETENTION")
        os.environ["PJE_ZERO_RETENTION"] = "1"
        try:
            result = await server.analisar_processo_completo_pje(
                acao="status_pdf_integral",
                job_id="inexistente",
                persona="advogado",
            )
            self.assertNotIn("retenção zero", str(result.get("erro") or ""))
        finally:
            if previous is None:
                os.environ.pop("PJE_ZERO_RETENTION", None)
            else:
                os.environ["PJE_ZERO_RETENTION"] = previous


if __name__ == "__main__":
    unittest.main()
