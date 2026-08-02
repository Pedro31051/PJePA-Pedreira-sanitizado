import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import browser_bridge_client
import pdf_scan
import server


class BrowserBridgeClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp_dir.name) / "bridge.sock"
        self.server = None

    async def asyncTearDown(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        self.temp_dir.cleanup()

    async def _start_server(self, responder):
        async def handler(reader, writer):
            request = json.loads(await reader.readline())
            response = responder(request)
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.server = await asyncio.start_unix_server(handler, path=str(self.socket_path))

    async def test_round_trip_success(self):
        await self._start_server(
            lambda request: {
                "request_id": request["request_id"],
                "ok": True,
                "data": {"authenticated": True},
                "error": None,
            }
        )
        result = await browser_bridge_client.call_browser_bridge(
            "get_state", socket_path=self.socket_path
        )
        self.assertEqual(result, {"authenticated": True})

    async def test_structured_bridge_failure(self):
        await self._start_server(
            lambda request: {
                "request_id": request["request_id"],
                "ok": False,
                "data": None,
                "error": {
                    "code": "SESSION_EXPIRED",
                    "message": "Sessão expirada.",
                    "retryable": False,
                },
            }
        )
        with self.assertRaises(browser_bridge_client.BrowserBridgeError) as raised:
            await browser_bridge_client.call_browser_bridge(
                "get_state", socket_path=self.socket_path
            )
        self.assertEqual(raised.exception.code, "SESSION_EXPIRED")

    async def test_missing_socket_fails_closed(self):
        with self.assertRaises(browser_bridge_client.BrowserBridgeError) as raised:
            await browser_bridge_client.call_browser_bridge(
                "get_state", socket_path=self.socket_path
            )
        self.assertEqual(raised.exception.code, "BRIDGE_UNAVAILABLE")
        self.assertTrue(raised.exception.retryable)


class BrowserAutomationToolTests(unittest.IsolatedAsyncioTestCase):
    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_estado_routes_to_bridge(self, call):
        call.return_value = {"authenticated": True, "url": "https://pje.tjpa.jus.br/pje/"}
        result = await server.automacao_navegador_pje(acao="estado")
        self.assertEqual(result["origem"], "playwright_bridge")
        self.assertTrue(result["resultado"]["authenticated"])
        call.assert_awaited_once_with("get_state", timeout_ms=15_000)

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_busca_routes_payload_to_bridge(self, call):
        call.return_value = {"status": "not_found", "resultados": []}
        await server.automacao_navegador_pje(
            acao="buscar", criterio="nome_parte", valor="Pessoa Sintética", limite=5
        )
        call.assert_awaited_once_with(
            "search_process",
            {"criterion": "nome_parte", "value": "Pessoa Sintética", "limit": 5},
            timeout_ms=15_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_busca_reu_alias_routes_to_passive_party_criterion(self, call):
        call.return_value = {"status": "not_found", "resultados": []}
        await server.automacao_navegador_pje(
            acao="buscar", criterio="réu", valor="Pessoa Sintética", limite=7
        )
        call.assert_awaited_once_with(
            "search_process",
            {
                "criterion": "nome_requerido",
                "value": "Pessoa Sintética",
                "limit": 7,
            },
            timeout_ms=15_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_busca_oab_routes_number_letter_and_state(self, call):
        call.return_value = {"status": "found", "resultados": []}
        await server.automacao_navegador_pje(
            acao="buscar",
            criterio="oab",
            valor="12345",
            letra_oab="A",
            uf_oab="PA",
        )
        call.assert_awaited_once_with(
            "search_process",
            {
                "criterion": "oab",
                "value": "12345",
                "limit": 20,
                "uf_oab": "PA",
                "letra_oab": "A",
            },
            timeout_ms=15_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_abrir_processo_routes_to_bridge(self, call):
        call.return_value = {"status": "opened"}
        await server.automacao_navegador_pje(
            acao="abrir_processo", valor="0000000-00.0000.8.14.0000"
        )
        call.assert_awaited_once_with(
            "open_process",
            {"process_number": "0000000-00.0000.8.14.0000"},
            timeout_ms=15_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_navegar_documento_routes_direction(self, call):
        call.return_value = {"status": "navigated"}
        await server.automacao_navegador_pje(
            acao="navegar_documento", direcao="previous"
        )
        call.assert_awaited_once_with(
            "navigate_document", {"direction": "previous"}, timeout_ms=15_000
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_listar_expedientes_routes_to_bridge(self, call):
        call.return_value = {"status": "listed", "expedients": []}
        await server.automacao_navegador_pje(acao="listar_expedientes", limite=25)
        call.assert_awaited_once_with(
            "list_expedients", {"limit": 25}, timeout_ms=15_000
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_mapear_juntada_documentos_routes_to_bridge(self, call):
        call.return_value = {"status": "mapped", "certificate_types": ["Certidão"]}
        await server.automacao_navegador_pje(
            acao="mapear_juntada_documentos",
            valor="0000000-00.0000.0.00.0000",
            tipo_documento="Certidão",
        )
        call.assert_awaited_once_with(
            "inspect_document_join",
            {
                "process_number": "0000000-00.0000.0.00.0000",
                "document_type": "Certidão",
                "task_box": "Verificar providência a adotar",
            },
            timeout_ms=15_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_mapear_comunicacoes_routes_read_only_inspection(self, call):
        call.return_value = {"status": "mapped", "final_action_executed": False}
        await server.automacao_navegador_pje(
            acao="mapear_comunicacoes",
            valor="0000000-00.0000.0.00.0000",
        )
        call.assert_awaited_once_with(
            "inspect_communications",
            {"process_number": "0000000-00.0000.0.00.0000"},
            timeout_ms=15_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_analisar_plano_comunicacoes_routes_without_final_action(self, call):
        call.return_value = {"status": "blocked", "final_action_available": False}
        plan = {
            "process_analysis": {"minor_interest": True},
            "communications": [{
                "recipient_role": "Requerente",
                "type": "Intimação",
                "medium": "Sistema",
                "deadline_days": 15,
            }],
        }
        await server.automacao_navegador_pje(
            acao="analisar_plano_comunicacoes",
            plano_comunicacoes=plan,
        )
        call.assert_awaited_once_with(
            "analyze_communication_plan", plan, timeout_ms=15_000
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_mapear_expedicao_documento_routes_without_signing(self, call):
        call.return_value = {"status": "mapped", "signing_available": False}
        await server.automacao_navegador_pje(
            acao="mapear_expedicao_documento",
            valor="0000001-00.0000.0.00.0000",
        )
        call.assert_awaited_once_with(
            "inspect_document_issue",
            {"process_number": "0000001-00.0000.0.00.0000"},
            timeout_ms=15_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_analisar_plano_expedicao_documento_routes_read_only(self, call):
        call.return_value = {"status": "ready_for_draft_review", "final_action_available": False}
        plan = {
            "document_origin": "interno",
            "document_type": "Ofício",
            "draft_text": "conteúdo",
            "movement_code": 60,
            "movement_complement": "Ofício",
        }
        await server.automacao_navegador_pje(
            acao="analisar_plano_expedicao_documento",
            plano_expedicao_documento=plan,
        )
        call.assert_awaited_once_with(
            "analyze_document_issue_plan", plan, timeout_ms=15_000
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_inspecionar_metadados_routes_to_bridge(self, call):
        call.return_value = {"labels": [], "situations": [], "reminders": {"items": []}}
        await server.automacao_navegador_pje(
            acao="inspecionar_metadados_processo",
            valor="0000000-00.0000.0.00.0000",
        )
        call.assert_awaited_once_with(
            "inspect_process_metadata",
            {"process_number": "0000000-00.0000.0.00.0000"},
            timeout_ms=15_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_etiqueta_requires_confirmation_before_apply(self, call):
        result = await server.gerenciar_etiqueta_processo_pje(
            acao="aplicar",
            numero_cnj="0000000-00.0000.0.00.0000",
            operacao="remover",
            etiqueta="AGUARDAR PRAZO",
        )
        self.assertEqual(result["codigo"], "CONFIRMATION_REQUIRED")
        call.assert_not_awaited()

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_previsualizar_retificacao_routes_to_bridge(self, call):
        call.return_value = {"status": "preview_ready", "confirmation_token": "token"}
        changes = {"caracteristicas": {"justica_gratuita": True}}
        await server.retificar_autuacao_pje(
            acao="previsualizar",
            numero_cnj="0000000-00.0000.8.14.0000",
            alteracoes=changes,
        )
        call.assert_awaited_once_with(
            "preview_retification",
            {
                "process_number": "0000000-00.0000.8.14.0000",
                "changes": changes,
            },
            timeout_ms=30_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_aplicar_retificacao_requires_confirmation_token(self, call):
        result = await server.retificar_autuacao_pje(
            acao="aplicar",
            numero_cnj="0000000-00.0000.8.14.0000",
            alteracoes={"caracteristicas": {"justica_gratuita": True}},
        )
        self.assertEqual(result["codigo"], "CONFIRMATION_REQUIRED")
        call.assert_not_awaited()

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_simular_retificacao_routes_without_confirmation_token(self, call):
        call.return_value = {"status": "simulated_and_verified", "mutating": False}
        changes = {
            "partes": [{
                "polo": "ativo",
                "indice": 0,
                "contato": {
                    "acao": "adicionar",
                    "tipo": "Email",
                    "valor": "teste@example.invalid",
                },
            }]
        }
        await server.retificar_autuacao_pje(
            acao="simular",
            numero_cnj="0000000-00.0000.8.14.0000",
            alteracoes=changes,
        )
        call.assert_awaited_once_with(
            "simulate_retification",
            {"process_number": "0000000-00.0000.8.14.0000", "changes": changes},
            timeout_ms=30_000,
        )

    @patch("server.browser_bridge_client.call_browser_bridge", new_callable=AsyncMock)
    async def test_bridge_error_is_not_silently_fallbacked(self, call):
        call.side_effect = browser_bridge_client.BrowserBridgeError(
            "CDP_UNAVAILABLE", "Chrome indisponível.", retryable=True
        )
        result = await server.automacao_navegador_pje(acao="estado")
        self.assertEqual(result["status"], "erro")
        self.assertEqual(result["codigo"], "CDP_UNAVAILABLE")
        self.assertTrue(result["repetivel"])


class PdfScanTests(unittest.TestCase):
    def test_extracts_native_text_from_controlled_artifact(self):
        import fitz

        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_dir = Path(temp_dir) / "browser-bridge"
            artifact_dir.mkdir()
            target = artifact_dir / "sample.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "Conteudo sintetico para validar a leitura local do PDF.")
            document.save(target)
            document.close()
            with patch.dict(os.environ, {"PJE_STORAGE_DIR": temp_dir}, clear=False):
                result = pdf_scan.inspect_pdf(target, apply_ocr=False)
        self.assertEqual(result["status"], "analyzed")
        self.assertEqual(result["pages_total"], 1)
        self.assertIn("Conteudo sintetico", result["text"])
        self.assertEqual(result["ocr_attempted_pages"], 0)

    def test_rejects_pdf_outside_controlled_artifact_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            outside = Path(temp_dir) / "outside.pdf"
            outside.write_bytes(b"%PDF-test")
            storage = Path(temp_dir) / "storage"
            (storage / "browser-bridge").mkdir(parents=True)
            with patch.dict(os.environ, {"PJE_STORAGE_DIR": str(storage)}, clear=False):
                with self.assertRaises(pdf_scan.PdfScanError):
                    pdf_scan.inspect_pdf(outside, apply_ocr=False)


if __name__ == "__main__":
    unittest.main()
