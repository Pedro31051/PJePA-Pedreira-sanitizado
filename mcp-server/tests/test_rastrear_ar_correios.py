"""Regressões dos modos e do gate da ferramenta de rastreamento AR."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server

CNJ_VALIDO = "0800001-61.2024.8.14.0028"


class _AbaExpedientesFalsa:
    def __init__(self):
        self.link_expedientes = MagicMock()
        self.link_expedientes.click = AsyncMock()

    def locator(self, _seletor):
        return self.link_expedientes

    async def wait_for_load_state(self, _estado, timeout):
        self.timeout = timeout

    async def content(self):
        return "abrirLinkDocumento('123456')"


class _RespostaHttpFalsa:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload


class _SessaoHttpFalsa:
    async def get(self, url, **_kwargs):
        if "AA123456789BR" in url:
            return _RespostaHttpFalsa(
                200,
                {
                    "objetos": [{
                        "descricao": "Carta",
                        "eventos": [{
                            "descricao": "OBJETO EM TRÂNSITO",
                            "dtHrCriado": "2026-07-29T10:00:00",
                            "unidade": None,
                        }],
                    }],
                },
            )
        return _RespostaHttpFalsa(503)


class _ContextoHttpFalso:
    async def __aenter__(self):
        return _SessaoHttpFalsa()

    async def __aexit__(self, *_args):
        return False


class GateRastreamentoArTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        credenciais = patch.object(
            server.cliente_singleton,
            "_get_creds",
            return_value=("00000000000", "senha-sintetica", "seed-sintetica"),
        )
        credenciais.start()
        self.addCleanup(credenciais.stop)

    async def test_codigo_avulso_passa_pelo_gate_antes_da_rede(self):
        recusa = {"codigo": "CONFIRMACAO_NECESSARIA"}
        with (
            patch.object(
                server,
                "_exigir_confirmacao_consulta",
                return_value=recusa,
            ) as gate,
            patch.object(
                server.cliente_singleton,
                "get_cliente",
                AsyncMock(),
            ) as get_cliente,
        ):
            resultado = await server.pje_rastrear_ar_correios(
                codigo_ar="AA123456789BR",
            )

        self.assertEqual(resultado, recusa)
        self.assertEqual(gate.call_args.args[0], "pje_rastrear_ar_correios")
        self.assertEqual(gate.call_args.args[1], "codigo_ar")
        self.assertEqual(
            gate.call_args.args[2]["codigo_ar"],
            "AA123456789BR",
        )
        get_cliente.assert_not_awaited()

    async def test_documento_e_processo_informam_modos_distintos(self):
        recusa = {"codigo": "CONFIRMACAO_NECESSARIA"}
        for id_documento, modo in (("123456", "documento"), ("", "processo")):
            with self.subTest(modo=modo):
                with patch.object(
                    server,
                    "_exigir_confirmacao_consulta",
                    return_value=recusa,
                ) as gate:
                    resultado = await server.pje_rastrear_ar_correios(
                        numero_cnj=CNJ_VALIDO,
                        id_documento=id_documento,
                    )

                self.assertEqual(resultado, recusa)
                self.assertEqual(gate.call_args.args[1], modo)

    async def test_entradas_invalidas_falham_antes_do_gate(self):
        with patch.object(
            server,
            "_exigir_confirmacao_consulta",
        ) as gate:
            sem_entrada = await server.pje_rastrear_ar_correios()
            ar_invalido = await server.pje_rastrear_ar_correios(
                codigo_ar="invalido",
            )
            cnj_invalido = await server.pje_rastrear_ar_correios(
                numero_cnj="invalido",
            )

        self.assertIn("numero_cnj ou codigo_ar", sem_entrada["erro"])
        self.assertIn("Código AR inválido", ar_invalido["erro"])
        self.assertIn("CNJ inválido", cnj_invalido["erro"])
        gate.assert_not_called()

    async def test_erro_http_nao_e_classificado_como_objeto_pendente(self):
        aba = _AbaExpedientesFalsa()
        pje = AsyncMock()
        pje._abrir_autos_processo.return_value = aba
        pje.ler_documento.return_value = {
            "texto": "AA123456789BR, BB123456789BR",
        }

        with (
            patch.object(
                server,
                "_exigir_confirmacao_consulta",
                return_value=None,
            ),
            patch.object(
                server.cliente_singleton,
                "get_cliente",
                AsyncMock(return_value=pje),
            ),
            patch(
                "httpx.AsyncClient",
                return_value=_ContextoHttpFalso(),
            ),
        ):
            resultado = await server.pje_rastrear_ar_correios(
                numero_cnj=CNJ_VALIDO,
            )

        self.assertEqual(resultado["total_codigos_ar"], 2)
        self.assertEqual(resultado["entregues"], 0)
        self.assertEqual(resultado["pendentes_ou_em_transito"], 1)
        self.assertEqual(resultado["com_erro"], 1)
        pendente = resultado["rastreamentos"][0]
        self.assertEqual(pendente["ultimo_status"], "OBJETO EM TRÂNSITO")
        self.assertEqual(pendente["ultima_data"], "2026-07-29T10:00:00")
        self.assertEqual(pendente["eventos"][0]["local"], "")
        pje._fechar_aba_autos.assert_awaited_once_with(aba)


if __name__ == "__main__":
    unittest.main()
