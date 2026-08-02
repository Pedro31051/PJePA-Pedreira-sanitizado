"""Contrato de todas as ferramentas publicadas pelo servidor MCP."""

import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server

EXPECTED_TOOLS = {
    "analisar_processo_completo_pje": (False, True, False, True),
    "status_e_auditoria_pje": (True, False, True, True),
    "painel_e_prazos_pje": (False, False, False, True),
    "buscar_processos_pje": (True, False, True, True),
    "analisar_processo_pje": (True, False, True, True),
    "gerir_documentos_pje": (True, False, True, True),
    "download_e_cache_pje": (False, True, False, True),
    "producao_minutas_e_relatorios": (False, False, False, False),
    "auditar_fluxo_processual_pje": (False, False, False, True),
    "pje_ler_autos_digitais": (True, False, True, True),
    "pje_rastrear_ar_correios": (True, False, True, True),
    "atuar_fluxo_tarefas_pje": (False, False, False, True),
    "pje_capturar_evidencia": (True, False, True, True),
    "pje_consultar_processo": (True, False, True, True),
    "pje_analisar_autos_lote": (True, False, True, True),
    "automacao_navegador_pje": (True, False, True, True),
    "gerenciar_etiqueta_processo_pje": (False, True, False, True),
    "retificar_autuacao_pje": (False, True, False, True),
}


class ToolCatalogContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalogo_publica_exatamente_as_ferramentas_contratadas(self):
        tools = await server.mcp.list_tools()

        self.assertEqual({tool.name for tool in tools}, set(EXPECTED_TOOLS))

    async def test_todas_publicam_output_schema_de_objeto(self):
        tools = await server.mcp.list_tools()

        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.outputSchema["type"], "object")
                self.assertEqual(tool.outputSchema["required"], ["result"])
                result_schema = tool.outputSchema["properties"]["result"]
                self.assertEqual(result_schema["type"], "object")
                self.assertTrue(result_schema["additionalProperties"])

    async def test_annotations_conservadoras_permanecem_estaveis(self):
        tools = await server.mcp.list_tools()

        for tool in tools:
            with self.subTest(tool=tool.name):
                expected = EXPECTED_TOOLS[tool.name]
                annotations = tool.annotations
                actual = (
                    annotations.readOnlyHint,
                    annotations.destructiveHint,
                    annotations.idempotentHint,
                    annotations.openWorldHint,
                )
                self.assertEqual(actual, expected)


class StructuredContentCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    SAFE_CALLS = {
        "analisar_processo_completo_pje": {
            "acao": "status",
            "job_id": "",
        },
        "status_e_auditoria_pje": {
            "acao": "capacidades",
            "filtro": "prazo",
        },
        "painel_e_prazos_pje": {
            "acao": "schema_acervo_tarefas",
        },
        "buscar_processos_pje": {
            "acao": "corrigir_cnj",
            "valor": "0000000-00.0000.8.14.0000",
        },
        "analisar_processo_pje": {
            "acao": "movimentacoes",
            "numero_cnj": "invalido",
        },
        "gerir_documentos_pje": {
            "acao": "acao_inexistente",
            "numero_cnj": "invalido",
        },
        "download_e_cache_pje": {
            "acao": "acao_inexistente",
        },
        "producao_minutas_e_relatorios": {
            "acao": "acao_inexistente",
        },
        "auditar_fluxo_processual_pje": {
            "acao": "planejar_auditoria",
        },
        "pje_ler_autos_digitais": {
            "acao": "resumo",
            "numero_cnj": "invalido",
        },
        "pje_rastrear_ar_correios": {
            "numero_cnj": "invalido",
        },
        "pje_capturar_evidencia": {
            "numero_processo": "invalido",
            "descricao_acao": "teste_evidencia",
        },
        "pje_consultar_processo": {
            "numero_processo": "invalido",
        },
        "pje_analisar_autos_lote": {
            "numero_processo": "invalido",
        },
        "automacao_navegador_pje": {
            "acao": "acao_inexistente",
        },
        "gerenciar_etiqueta_processo_pje": {
            "acao": "acao_inexistente",
            "numero_cnj": "invalido",
            "operacao": "adicionar",
            "etiqueta": "sintetica",
        },
        "retificar_autuacao_pje": {
            "acao": "acao_inexistente",
            "numero_cnj": "invalido",
        },
    }

    async def test_payload_textual_legado_e_structured_content_sao_equivalentes(self):
        for tool_name, arguments in self.SAFE_CALLS.items():
            with self.subTest(tool=tool_name):
                content, structured = await server.mcp.call_tool(
                    tool_name,
                    arguments,
                )

                self.assertEqual(len(content), 1)
                legacy_payload = json.loads(content[0].text)
                self.assertEqual(structured, {"result": legacy_payload})


if __name__ == "__main__":
    unittest.main()
