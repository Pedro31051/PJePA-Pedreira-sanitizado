"""Contrato seguro do cadastro e da preparação de protocolo."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import mapa_pje
import server


class RegistrationMapTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_redacts_query_and_blocks_final_controls(self):
        page = SimpleNamespace(
            url=(
                "https://pje.tjpa.jus.br/pje/Processo/cadastrar.seam"
                "?newInstance=true&token=segredo"
            ),
            evaluate=AsyncMock(
                return_value={
                    "selects": [
                        {
                            "id": (
                                "formInserirParteProcesso:nomeParteDecoration:nomeParte"
                            ),
                            "name": "",
                            "options": [{"label": "NOME PESSOAL", "value": "1"}],
                        }
                    ],
                    "inputs": [],
                    "controls": [
                        {
                            "tag": "button",
                            "id": "final",
                            "name": "",
                            "text": "Protocolar",
                            "type": "submit",
                        }
                    ],
                    "steps": ["Dados iniciais"],
                    "headings": ["Cadastro de processo"],
                }
            ),
        )
        registration = mapa_pje.ProcessRegistrationPage(
            page, "https://pje.tjpa.jus.br/pje"
        )

        result = await registration.snapshot()

        self.assertEqual(result["rota"], "/pje/Processo/cadastrar.seam")
        self.assertTrue(result["etapa_final_detectada"])
        self.assertEqual(result["controles_finais_bloqueados"], ["Protocolar"])
        self.assertNotIn("segredo", str(result))
        self.assertNotIn("NOME PESSOAL", str(result))
        self.assertTrue(result["selects"][0]["opcoes_omitidas_por_privacidade"])


@unittest.skip("fluxo legado preservado apenas como referência offline")
class RegistrationToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapear_routes_to_authenticated_client_without_protocol(self):
        client = SimpleNamespace(
            mapear_cadastro_processo=AsyncMock(
                return_value={
                    "status": "mapeado",
                    "protocolo_executado": False,
                }
            )
        )
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(return_value=client),
        ) as get_client:
            result = await server.protocolar_processo_pje(
                acao="mapear",
                persona="advogado",
                grau="1",
            )

        get_client.assert_awaited_once_with("advogado", "1g")
        client.mapear_cadastro_processo.assert_awaited_once_with(
            materia="",
            jurisdicao="",
            classe_judicial="",
            assunto="",
            codigo_assunto="",
            etapa="",
            polo="",
            cpf_parte="",
            adicionar_parte=False,
            etapa_apos_parte="",
            caminho_pdf="",
            descricao_documento="Documento sintético para teste",
            tipo_documento="Petição Inicial",
            avancar=False,
        )
        self.assertFalse(result["protocolo_executado"])
        self.assertEqual(result["grau"], "1º grau")

    async def test_advancing_initial_data_requires_explicit_confirmation(self):
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(),
        ) as get_client:
            res = await server.protocolar_processo_pje(
                acao="avancar_dados_iniciais",
                materia="Direito Civil",
                jurisdicao="Belém",
                classe_judicial="Procedimento Comum Cível",
                confirmar_preparacao=False,
            )

        get_client.assert_not_awaited()
        self.assertEqual(res["status"], "desabilitado_por_politica")


class ProductionReadOnlyContractTests(unittest.IsolatedAsyncioTestCase):
    def test_protocol_tool_is_not_published(self):
        tools = server.mcp._tool_manager.list_tools()
        names = {tool.name for tool in tools}

        self.assertNotIn("protocolar_processo_pje", names)
        self.assertNotIn(
            "protocolar_processo_pje",
            {
                item["ferramenta"]
                for item in __import__("asyncio").run(server.inventario_capacidades())[
                    "ferramentas"
                ]
            },
        )

    async def test_legacy_entrypoint_fails_closed_without_browser(self):
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(),
        ) as get_client:
            result = await server.protocolar_processo_pje(
                acao="adicionar_parte_cpf",
                confirmar_preparacao=True,
            )

        get_client.assert_not_awaited()
        self.assertEqual(result["status"], "desabilitado_por_politica")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["protocolo_executado"])

    def test_all_published_tools_have_explicit_annotations(self):
        tools = server.mcp._tool_manager.list_tools()

        self.assertEqual(len(tools), 18)
        for tool in tools:
            self.assertIsNotNone(tool.annotations, tool.name)
            self.assertIsNotNone(tool.annotations.readOnlyHint, tool.name)
            self.assertIsNotNone(tool.annotations.destructiveHint, tool.name)
            self.assertIsNotNone(tool.annotations.idempotentHint, tool.name)
            self.assertIsNotNone(tool.annotations.openWorldHint, tool.name)

    async def test_painel_publica_schema_e_conteudo_estruturado(self):
        tools = server.mcp._tool_manager.list_tools()
        painel = next(tool for tool in tools if tool.name == "painel_e_prazos_pje")

        self.assertEqual(painel.output_schema["type"], "object")
        self.assertIn("result", painel.output_schema["properties"])

        content, structured = await server.mcp._tool_manager.call_tool(
            "painel_e_prazos_pje",
            {"acao": "schema_acervo_tarefas"},
            convert_result=True,
        )
        self.assertTrue(content)
        self.assertEqual(
            structured["result"]["schema_version"],
            "pje.acervo-tarefas/v2",
        )

    async def test_metricas_do_acervo_sao_offline_e_minimizadas(self):
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(),
        ) as get_client:
            resultado = await server.painel_e_prazos_pje(
                acao="metricas_desempenho_acervo"
            )

        get_client.assert_not_awaited()
        self.assertEqual(
            resultado["schema_version"],
            "pje.acervo-performance/v1",
        )
        self.assertFalse(resultado["dados_processuais_incluidos"])


if __name__ == "__main__":
    unittest.main()
