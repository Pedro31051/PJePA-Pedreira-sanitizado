"""Regressões da busca pelo menu lateral Consulta processual."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import mapa_pje
import server


class GeneralSearchContractTests(unittest.TestCase):
    def test_eleven_digits_with_valid_cpf_are_mapped_to_cpf(self):
        detected = server._detectar_tipo_busca("00000000000")

        self.assertEqual(detected["acao"], "cpf")
        self.assertEqual(detected["valor_normalizado"], "00000000000")
        self.assertIn("CPF", detected["motivo"])

    def test_invalid_eleven_digits_still_preserve_general_search(self):
        # 11 dígitos sem DV válido continuam ambíguos para não inferência forçada.
        detected = server._detectar_tipo_busca("11111111111")

        self.assertEqual(detected["acao"], "busca_geral")
        self.assertIn("ambíguos", detected["motivo"])

    def test_general_value_preserves_format_and_rejects_controls(self):
        self.assertEqual(
            mapa_pje.validate_general_search_value("  00000000000  "),
            "00000000000",
        )
        with self.assertRaises(ValueError):
            mapa_pje.validate_general_search_value("abc\x00def")

    def test_result_normalization_deduplicates_by_cnj(self):
        raw = [
            {
                "text": (
                    "0800001-61.2024.8.14.0028 Classe sintética "
                    "Unidade sintética"
                ),
                "href_path": (
                    "/pje/Processo/ConsultaProcesso/Detalhe/"
                    "#/0800001-61.2024.8.14.0028"
                ),
            },
            {
                "text": "0800001-61.2024.8.14.0028 repetido",
                "href_path": "",
            },
        ]

        result = mapa_pje.normalize_general_search_results(raw, 20)

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["numero_cnj"],
            "0800001-61.2024.8.14.0028",
        )
        self.assertTrue(result[0]["rota_resultado"].startswith("/pje/"))

    def test_native_grid_uses_actual_nine_column_headers(self):
        raw = [{
            "headers": [
                "Processo",
                "Características",
                "Órgão julgador",
                "Autuado em",
                "Classe judicial",
                "Polo ativo",
                "Polo passivo",
                "Nó(s) atual(is)",
                "Última moviment.",
            ],
            "columns": [
                "0800001-61.2024.8.14.0028",
                "Segredo: não",
                "Unidade sintética",
                "01/01/2024",
                "Classe sintética",
                "Parte ativa",
                "Parte passiva",
                "Tarefa atual",
                "Movimento mais recente",
            ],
            "href_path": "/pje/Processo/ConsultaProcesso/Detalhe/listView.seam",
        }]

        result = mapa_pje.normalize_native_search_rows(raw, 20)

        self.assertEqual(result[0]["nos_atuais"], "Tarefa atual")
        self.assertEqual(
            result[0]["ultima_movimentacao"],
            "Movimento mais recente",
        )
        self.assertNotIn("raw", result[0])

    def test_sensitive_search_values_are_masked(self):
        self.assertEqual(
            mapa_pje.mask_process_search_value("cpf", "00000000000"),
            "***.***.***-33",
        )
        self.assertEqual(
            mapa_pje.mask_process_search_value("oab", ("12345", "PA")),
            "***45/PA",
        )
        self.assertEqual(
            mapa_pje.mask_process_search_value(
                "oab", {"numero": "12345", "letra": "A", "uf": "PA"}
            ),
            "***45A/PA",
        )
        self.assertNotIn(
            "PEDRO",
            mapa_pje.mask_process_search_value(
                "nome_parte",
                "Pedro Felipe Alves Rocha",
            ),
        )

    def test_party_role_filter_distinguishes_required_from_requester(self):
        rows = [
            {
                "numero_cnj": "0800001-61.2024.8.14.0028",
                "polo_ativo": "Maria da Silva",
                "polo_passivo": "João de Souza",
            },
            {
                "numero_cnj": "0800002-46.2024.8.14.0028",
                "polo_ativo": "João de Souza",
                "polo_passivo": "Empresa Sintética",
            },
        ]

        required, field = mapa_pje.filter_native_results_by_party_role(
            rows, "nome_requerido", "João de Souza"
        )

        self.assertEqual(field, "polo_passivo")
        self.assertEqual(
            [item["numero_cnj"] for item in required],
            ["0800001-61.2024.8.14.0028"],
        )

    def test_oab_parser_preserves_complementary_letter(self):
        self.assertEqual(
            server._parse_oab_detalhada("OAB/PA 12345-A"),
            ("12345", "A", "PA"),
        )


class GeneralSearchRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _consultar_confirmado(self, **kwargs):
        return await server.buscar_processos_pje(**kwargs)

    async def test_auto_routes_valid_cpf_to_cpf_search(self):
        expected = {
            "resultados": [],
        }
        with patch.object(
            server,
            "buscar_por_cpf",
            AsyncMock(return_value=expected),
        ) as cpf_search:
            result = await self._consultar_confirmado(
                acao="auto",
                valor="00000000000",
                persona="advogado",
                grau="1",
            )

        cpf_search.assert_awaited_once_with(
            "00000000000",
            20,
            "advogado",
            "1g",
        )
        self.assertEqual(result, expected)

    async def test_auto_routes_invalid_eleven_digits_to_general_menu(self):
        expected = {
            "fonte": "Menu lateral do PJe > Consulta processual",
            "valor_busca": "11111111111",
            "resultados": [],
        }
        with patch.object(
            server,
            "buscar_processo_geral",
            AsyncMock(return_value=expected),
        ) as general:
            result = await self._consultar_confirmado(
                acao="auto",
                valor="11111111111",
                persona="advogado",
                grau="1",
            )

        general.assert_awaited_once_with(
            "11111111111",
            20,
            "advogado",
            "1g",
        )
        self.assertEqual(result["criterio_detectado"], "busca_geral")
        self.assertEqual(result["valor_busca"], "11111111111")

    async def test_explicit_cpf_action_still_uses_cpf_search(self):
        with patch.object(
            server,
            "buscar_por_cpf",
            AsyncMock(return_value={"resultados": []}),
        ) as cpf_search:
            result = await self._consultar_confirmado(
                acao="cpf",
                valor="00000000000",
                persona="advogado",
            )

        cpf_search.assert_awaited_once_with("00000000000", 20, "advogado", "1g")
        self.assertEqual(result, {"resultados": []})

    async def test_requerido_alias_routes_to_passive_party_search(self):
        expected = {"resultados": [], "filtro_polo_aplicado": "polo_passivo"}
        with patch.object(
            server,
            "buscar_por_nome_requerido",
            AsyncMock(return_value=expected),
        ) as required_search:
            result = await self._consultar_confirmado(
                acao="requerido",
                valor="Pessoa Sintética",
                persona="advogado",
            )

        required_search.assert_awaited_once_with(
            "Pessoa Sintética", 20, "advogado", "1g"
        )
        self.assertEqual(result, expected)

    async def test_advanced_date_range_is_normalized_and_routed(self):
        expected = {"resultados": [], "criterio_aplicado": "data_autuacao"}
        with patch.object(
            server,
            "buscar_por_criterio_avancado",
            AsyncMock(return_value=expected),
        ) as advanced_search:
            result = await self._consultar_confirmado(
                acao="data_autuacao",
                valor="01/01/2024",
                valor_final="31/12/2024",
                persona="advogado",
            )

        advanced_search.assert_awaited_once_with(
            "data_autuacao",
            {"inicio": "01/01/2024", "fim": "31/12/2024"},
            20,
            "advogado",
            "1g",
        )
        self.assertEqual(result, expected)

    async def test_advanced_invalid_inverted_range_is_rejected_before_pje(self):
        with patch.object(
            server,
            "buscar_por_criterio_avancado",
            AsyncMock(),
        ) as advanced_search:
            result = await self._consultar_confirmado(
                acao="valor_causa",
                valor="5.000,00",
                valor_final="1.000,00",
                persona="advogado",
            )

        advanced_search.assert_not_awaited()
        self.assertEqual(result["codigo"], "INVALID_REQUEST")

    async def test_process_reference_is_intentionally_not_an_action(self):
        result = await self._consultar_confirmado(
            acao="processo_referencia",
            valor="0000000-00.0000.0.00.0000",
            persona="advogado",
        )

        self.assertIn("erro", result)
        self.assertNotIn("processo_referencia", result["acoes"])

    async def test_second_degree_is_not_claimed_without_mapping(self):
        result = await server.buscar_processo_geral(
            "00000000000",
            grau="2",
        )

        self.assertIn("somente no 1º grau", result["erro"])
        self.assertTrue(result["somente_leitura"])

    async def test_batch_only_claims_zero_when_pje_confirms_it(self):
        with patch.object(
            server,
            "_executar_busca",
            AsyncMock(
                side_effect=[
                    {
                        "resultados": [],
                        "sem_resultado_confirmado": True,
                    },
                    {
                        "resultados": [],
                        "resposta_confirmada": False,
                        "sem_resultado_confirmado": False,
                    },
                ]
            ),
        ):
            result = await server.buscar_em_lote(
                "00000000000;11111111111",
                persona="advogado",
            )

        self.assertEqual(result["por_item"][0]["status"], "sem_resultado")
        self.assertFalse(result["por_item"][0]["inconclusive"])
        self.assertEqual(result["por_item"][1]["status"], "inconclusivo")
        self.assertTrue(result["por_item"][1]["inconclusive"])
        self.assertEqual(result["resumo"]["sem_resultado"], 1)
        self.assertEqual(result["resumo"]["inconclusivos"], 1)

    async def test_batch_redacts_inputs_in_items_nested_results_and_ignored(self):
        raw_cpf = "00000000000"
        raw_oab = "12345/PA"
        with patch.object(
            server,
            "_executar_busca",
            AsyncMock(
                return_value={
                    "resultados": [],
                    "sem_resultado_confirmado": True,
                    "valor_busca": raw_cpf,
                }
            ),
        ):
            result = await server.buscar_em_lote(
                f"{raw_cpf};{raw_oab}",
                max_itens=1,
                persona="advogado",
            )

        serialized = str(result)
        self.assertNotIn(raw_cpf, serialized)
        self.assertNotIn(raw_oab, serialized)
        self.assertEqual(
            result["por_item"][0]["valor"],
            result["por_item"][0]["valor_mascarado"],
        )
        self.assertEqual(
            result["por_item"][0]["valor_mascarado"],
            "***.***.***-33",
        )
        self.assertEqual(
            result["por_item"][0]["resultado"]["valor_busca"],
            "***.***.***-33",
        )
        self.assertEqual(
            result["itens_ignorados_mascarados"],
            ["***45/PA"],
        )

    async def test_batch_caps_at_twenty_five_before_dispatching_excess(self):
        values = ";".join(f"nome sintético {index}" for index in range(30))
        with patch.object(
            server,
            "_executar_busca",
            AsyncMock(
                return_value={
                    "resultados": [],
                    "sem_resultado_confirmado": True,
                }
            ),
        ) as execute:
            result = await server.buscar_em_lote(
                values,
                persona="advogado",
            )

        self.assertEqual(execute.await_count, 25)
        self.assertEqual(result["resumo"]["itens_consultados"], 25)
        self.assertEqual(len(result["itens_ignorados_mascarados"]), 5)
        for ignored in result["itens_ignorados_mascarados"]:
            self.assertNotIn("sintético", ignored.casefold())

    async def test_invalid_cnj_is_rejected_without_opening_browser(self):
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(),
        ) as get_client:
            result = await server.buscar_processos_pje(
                acao="consultar_numero",
                valor="0000000-00.2024.8.14.0000",
                persona="advogado",
            )

        get_client.assert_not_awaited()
        self.assertIn("CNJ inválido", result["erro"])
        self.assertIn("numero_corrigido", result)


if __name__ == "__main__":
    unittest.main()
