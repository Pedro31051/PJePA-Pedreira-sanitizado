"""Testes para as três novas tools implementadas:
- pje_consultar_processo
- pje_analisar_autos_lote
- pje_capturar_evidencia
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server

CNJ_VALIDO = "0800001-61.2024.8.14.0028"
CNJ_INVALIDO = "0000000-00.2024.8.14.0000"


class NovasToolsUnitTests(unittest.IsolatedAsyncioTestCase):

    @patch("server._exigir_confirmacao_consulta", return_value=None)
    @patch("server._ativar_perfil_ferramenta", return_value=("servidor", "1", None))
    @patch("server.consultar_processo")
    @patch("server.ultimas_movimentacoes")
    async def test_pje_consultar_processo_sucesso(
        self, mock_ultimas, mock_consultar, mock_ativar, mock_confirmacao
    ):
        mock_consultar.return_value = {"numero": CNJ_VALIDO, "classe": "Inventário"}
        mock_ultimas.return_value = {"movimentacoes": [{"tipo": "Juntada"}], "destaques_familia": []}

        res = await server.pje_consultar_processo(
            numero_processo=CNJ_VALIDO,
            incluir_movimentacoes=True,
            limite_movimentacoes=5,
        )

        self.assertEqual(res["numero"], CNJ_VALIDO)
        self.assertEqual(res["ultimas_movimentacoes"], [{"tipo": "Juntada"}])
        mock_consultar.assert_called_once_with(server._normaliza_cnj(CNJ_VALIDO), "servidor", "1")
        mock_ultimas.assert_called_once_with(server._normaliza_cnj(CNJ_VALIDO), 5, "servidor", "1")

    @patch("server._exigir_confirmacao_consulta", return_value=None)
    @patch("server._ativar_perfil_ferramenta", return_value=("servidor", "1", None))
    async def test_pje_consultar_processo_cnj_invalido(self, mock_ativar, mock_confirmacao):
        res = await server.pje_consultar_processo(numero_processo=CNJ_INVALIDO)
        self.assertIn("erro", res)
        self.assertIn("CNJ inválido", res["erro"])

    @patch("server._exigir_confirmacao_consulta", return_value=None)
    @patch("server._ativar_perfil_ferramenta", return_value=("servidor", "1", None))
    @patch("server.consultar_processo")
    @patch("server.ultimas_movimentacoes")
    async def test_pje_consultar_processo_sem_movimentacoes(
        self, mock_ultimas, mock_consultar, mock_ativar, mock_confirmacao
    ):
        mock_consultar.return_value = {"numero": CNJ_VALIDO, "classe": "Inventário"}

        res = await server.pje_consultar_processo(
            numero_processo=CNJ_VALIDO,
            incluir_movimentacoes=False,
        )

        self.assertEqual(res["numero"], CNJ_VALIDO)
        self.assertNotIn("ultimas_movimentacoes", res)
        mock_consultar.assert_called_once()
        mock_ultimas.assert_not_called()

    @patch("server._exigir_confirmacao_consulta", return_value=None)
    @patch("server._ativar_perfil_ferramenta", return_value=("servidor", "1", None))
    async def test_pje_analisar_autos_lote_bloqueia_scans(self, mock_ativar, mock_confirmacao):
        res = await server.pje_analisar_autos_lote(
            numero_processo=CNJ_VALIDO,
            incluir_scans=True,
        )
        self.assertIn("erro", res)
        self.assertEqual(res["status"], "bloqueado")

    @patch("server._exigir_confirmacao_consulta", return_value=None)
    @patch("server._ativar_perfil_ferramenta", return_value=("servidor", "1", None))
    @patch("server.pje_ler_autos_digitais")
    @patch("server.ultimas_movimentacoes")
    async def test_pje_analisar_autos_lote_repassa(
        self, mock_ultimas, mock_ler, mock_ativar, mock_confirmacao
    ):
        mock_ler.return_value = {
            "status": "concluido",
            "total_documentos": 2,
            "leituras": [
                {
                    "documento": {"id": "101", "titulo": "Petição Inicial de Inventário"},
                    "teor": {"paginas": ["p1", "p2"]},
                },
                {
                    "documento": {"id": "102", "titulo": "Despacho do Juiz"},
                    "teor": {"paginas": ["p1"]},
                }
            ],
            "leituras_com_erro": 0,
            "leituras_incompletas": 0,
        }
        mock_ultimas.return_value = {
            "movimentacoes": [{"tipo": "Decisao"}],
            "destaques_familia": []
        }

        res = await server.pje_analisar_autos_lote(
            numero_processo=CNJ_VALIDO,
            incluir_scans=False,
            max_paginas=50,
            max_documentos=10,
        )

        self.assertEqual(res["status"], "concluido")
        # Confere classificação
        self.assertEqual(res["leituras"][0]["documento"]["classe_documento"], "Petição Inicial")
        self.assertEqual(res["leituras"][1]["documento"]["classe_documento"], "Despacho")

        # Confere cobertura
        cobertura = res["resumo_cobertura"]
        self.assertEqual(cobertura["total_paginas_lidas"], 3)
        self.assertEqual(cobertura["cobertura_percentual"], 100.0)

        # Confere classificação por tipo agregada
        self.assertEqual(res["classificacao_por_tipo"]["Petição Inicial"], 1)
        self.assertEqual(res["classificacao_por_tipo"]["Despacho"], 1)

        # Confere timeline
        self.assertEqual(res["movimentacoes"], [{"tipo": "Decisao"}])

        mock_ler.assert_called_once_with(
            numero_cnj=server._normaliza_cnj(CNJ_VALIDO),
            acao="ler_lote",
            max_documentos=10,
            max_paginas=50,
            tempo_maximo_segundos=120,
            persona="servidor",
            grau="1",
            perfil="",
            confirmar_consulta="",
            confirmation_token="",
        )
        mock_ultimas.assert_called_once_with(
            server._normaliza_cnj(CNJ_VALIDO),
            15,
            "servidor",
            "1"
        )

    @patch("server._exigir_confirmacao_consulta", return_value=None)
    @patch("server._ativar_perfil_ferramenta", return_value=("servidor", "1", None))
    @patch("cliente_singleton.get_cliente")
    async def test_pje_capturar_evidencia_sem_processo(
        self, mock_get_cliente, mock_ativar, mock_confirmacao
    ):
        mock_page = AsyncMock()
        mock_pje = AsyncMock()
        mock_pje._page = mock_page
        mock_get_cliente.return_value = mock_pje

        with patch("evidencia.tirar_evidencia_efemera") as mock_tirar:
            mock_tirar.return_value = {"mime": "image/png", "b64": "dummy"}

            res = await server.pje_capturar_evidencia(
                numero_processo="",
                descricao_acao="teste sem processo",
                full_page=False,
            )

            self.assertEqual(res["status"], "concluido")
            self.assertEqual(res["evidencia_screenshot"]["b64"], "dummy")
            mock_tirar.assert_called_once_with(
                mock_page,
                full_page=False,
                processo=None,
                descricao="teste sem processo",
            )
            mock_pje._abrir_autos_processo.assert_not_called()

    @patch("server._exigir_confirmacao_consulta", return_value=None)
    @patch("server._ativar_perfil_ferramenta", return_value=("servidor", "1", None))
    @patch("cliente_singleton.get_cliente")
    async def test_pje_capturar_evidencia_com_processo(
        self, mock_get_cliente, mock_ativar, mock_confirmacao
    ):
        mock_page = AsyncMock()
        mock_aba = AsyncMock()
        mock_pje = AsyncMock()
        mock_pje._page = mock_page
        mock_pje._abrir_autos_processo.return_value = mock_aba
        mock_get_cliente.return_value = mock_pje

        with patch("evidencia.tirar_evidencia_efemera") as mock_tirar:
            mock_tirar.return_value = {"mime": "image/png", "b64": "dummy"}

            res = await server.pje_capturar_evidencia(
                numero_processo=CNJ_VALIDO,
                descricao_acao="teste com processo",
                full_page=True,
            )

            self.assertEqual(res["status"], "concluido")
            mock_pje._abrir_autos_processo.assert_called_once_with(server._normaliza_cnj(CNJ_VALIDO))
            mock_tirar.assert_called_once_with(
                mock_aba,
                full_page=True,
                processo=server._normaliza_cnj(CNJ_VALIDO),
                descricao="teste com processo",
            )
            mock_pje._fechar_aba_autos.assert_called_once_with(mock_aba)


if __name__ == "__main__":
    unittest.main()
