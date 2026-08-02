"""Contrato padronizado do relatório processual, com dados sintéticos."""

import importlib
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

report_format = importlib.import_module("report_format")


def _result():
    evidence = {
        "document_id": "doc-1",
        "page": 2,
        "excerpt": "trecho literal sintético dos autos",
        "sha256": "a" * 64,
    }
    return {
        "provider": "vertex_ai_direct",
        "model": "gemini-3.5-flash",
        "capsule_id": "0" * 32,
        "source": "ephemeral_capsule",
        "summary": "Resumo bruto.",
        "findings": [
            {
                "finding_id": "full:f-1",
                "category": "decisao",
                "title": "Nomeação da inventariante",
                "conclusion": "A inventariante sintética foi nomeada.",
                "caveat": "",
                "evidence": [evidence],
            },
            {
                "finding_id": "full:f-2",
                "category": "contradicao",
                "title": "Datas divergentes",
                "conclusion": "Certidão e mandado apontam anos diferentes.",
                "caveat": "provável erro material",
                "evidence": [evidence],
            },
            {
                "finding_id": "full:f-3",
                "category": "pendencia",
                "title": "ITCD não recolhido",
                "conclusion": "Não há guia de ITCD nos autos.",
                "caveat": "",
                "evidence": [evidence],
            },
        ],
        "unknowns": ["Sem manifestação do MP no recorte."],
        "recommended_human_reviews": ["Conferir representação da curadora."],
        "sintese": {
            "status": "completed",
            "fail_closed": True,
            "executive_summary": "Síntese executiva sintética.",
            "current_state": "Inventário em fase de declarações.",
            "suggestions": [
                {
                    "item": "Intimar o Ministério Público.",
                    "rationale": "Há interesse de incapaz.",
                    "priority": "alta",
                    "supporting_finding_ids": ["full:f-1"],
                }
            ],
            "rejected_suggestions": [
                {"item": "Sugestão sem amparo.", "reason": "finding inexistente"}
            ],
            "open_questions": ["Falta avaliação consolidada."],
        },
        "verification": {
            "fail_closed": True,
            "verified_findings": 3,
            "rejected_findings": 0,
            "rejections": [],
            "literal_grounding": {"evidence_total": 3},
        },
    }


class ReportFormatTests(unittest.TestCase):
    def test_sections_and_schema(self):
        report = report_format.build_standard_report(_result())
        self.assertEqual(report["schema_version"], "pje.relatorio-processual/v1")
        self.assertTrue(report["read_only"])
        self.assertEqual(len(report["atos_e_fatos"]), 1)
        self.assertEqual(len(report["contradicoes"]), 1)
        self.assertEqual(len(report["pendencias"]), 1)
        self.assertEqual(report["sintese_executiva"], "Síntese executiva sintética.")
        self.assertEqual(report["estado_atual"], "Inventário em fase de declarações.")
        self.assertEqual(report["sintese_status"], "completed")
        self.assertEqual(report["avisos"], report_format.AVISOS_PADRAO)

    def test_citation_format(self):
        report = report_format.build_standard_report(_result())
        citation = report["atos_e_fatos"][0]["provas"][0]["citacao"]
        self.assertEqual(citation, f"[doc doc-1 p.2 sha256:{'a' * 8}]")

    def test_suggestions_never_auto_executed(self):
        report = report_format.build_standard_report(_result())
        self.assertEqual(len(report["sugestoes"]), 2)
        for suggestion in report["sugestoes"]:
            self.assertFalse(suggestion["execucao_automatica"])
            self.assertTrue(suggestion["exige_conferencia_humana"])
        self.assertEqual(report["sugestoes"][0]["prioridade"], "alta")
        self.assertEqual(
            report["sugestoes"][0]["achados_suporte"], ["full:f-1"]
        )
        self.assertEqual(len(report["sugestoes_rejeitadas"]), 1)

    def test_fallback_without_synthesis(self):
        result = _result()
        result["sintese"] = {"status": "failed", "safe_error": "indisponível"}
        report = report_format.build_standard_report(result)
        self.assertEqual(report["sintese_executiva"], "Resumo bruto.")
        self.assertEqual(report["sintese_status"], "failed")
        self.assertEqual(len(report["sugestoes"]), 1)
        self.assertEqual(
            report["sugestoes"][0]["item"], "Conferir representação da curadora."
        )

    def test_markdown_render(self):
        report = report_format.build_standard_report(_result())
        markdown = report_format.render_markdown(report)
        for heading in (
            "# Relatório processual padronizado",
            "## Síntese executiva",
            "## Estado atual",
            "## Atos e fatos verificados",
            "## Contradições",
            "## Pendências",
            "## Lacunas e desconhecidos",
            "## Sugestões (não executadas automaticamente)",
            "## Verificação",
            "## Avisos",
        ):
            self.assertIn(heading, markdown)
        self.assertIn("[doc doc-1 p.2 sha256:aaaaaaaa]", markdown)
        self.assertIn("[alta] Intimar o Ministério Público.", markdown)


if __name__ == "__main__":
    unittest.main()
