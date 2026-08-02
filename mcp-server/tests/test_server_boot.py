"""Testes de inicialização (boot) do servidor MCP e endpoint /analise_extensao."""

import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from starlette.testclient import TestClient

import server


class ServerBootAndExtensionTests(unittest.IsolatedAsyncioTestCase):
    extension_origin = {"Origin": "chrome-extension://synthetic-test-id"}

    def test_server_imports_batch_engine(self):
        """Valida que batch_engine foi importado no topo de server.py."""
        self.assertTrue(hasattr(server, "batch_engine"))
        self.assertIsNotNone(server.batch_engine)

    def test_criar_app_http(self):
        """Valida que _criar_app_http() instancia a aplicação ASGI sem exceções."""
        app = server._criar_app_http()
        self.assertIsNotNone(app)

    async def test_runtime_pje_lifespan_boot(self):
        """Valida que o lifespan _runtime_pje() executa sem NameError ou falha de inicialização."""
        async with server._runtime_pje():
            self.assertTrue(hasattr(server, "batch_engine"))

    def test_analise_extensao_cors_and_options(self):
        """Valida pré-flight OPTIONS e cabeçalhos CORS no endpoint /analise_extensao."""
        app = server._criar_app_http()
        client = TestClient(app)

        response = client.options(
            "/analise_extensao",
            headers={"Origin": "chrome-extension://abcdef"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "chrome-extension://abcdef")
        self.assertIn("Authorization", response.headers.get("access-control-allow-headers", ""))
        self.assertIn("X-Extension-Token", response.headers.get("access-control-allow-headers", ""))

    def test_analise_extensao_token_auth(self):
        """Valida endurecimento por PJE_EXTENSION_TOKEN no endpoint /analise_extensao."""
        app = server._criar_app_http()
        client = TestClient(app)

        # Sem token na env -> deve aceitar/prosseguir (400 ou 500 se payload for inválido, mas não 401 por auth)
        if "PJE_EXTENSION_TOKEN" in os.environ:
            del os.environ["PJE_EXTENSION_TOKEN"]

        resp_no_env = client.post(
            "/analise_extensao", json={}, headers=self.extension_origin
        )
        self.assertNotEqual(resp_no_env.status_code, 401)

        # Com token na env
        try:
            os.environ["PJE_EXTENSION_TOKEN"] = "token_secreto_teste"

            # Sem header de token -> 401
            resp_no_token = client.post(
                "/analise_extensao", json={}, headers=self.extension_origin
            )
            self.assertEqual(resp_no_token.status_code, 401)
            self.assertEqual(resp_no_token.json(), {"erro": "Não autorizado: token inválido ou ausente"})

            # Token inválido -> 401
            resp_bad_token = client.post(
                "/analise_extensao",
                json={},
                headers={**self.extension_origin, "Authorization": "Bearer token_errado"}
            )
            self.assertEqual(resp_bad_token.status_code, 401)

            # Token válido via Authorization Bearer -> passa da auth (não é 401)
            resp_bearer = client.post(
                "/analise_extensao",
                json={},
                headers={
                    **self.extension_origin,
                    "Authorization": "Bearer token_secreto_teste",
                }
            )
            self.assertNotEqual(resp_bearer.status_code, 401)

            # Token válido via X-Extension-Token -> passa da auth (não é 401)
            resp_header = client.post(
                "/analise_extensao",
                json={},
                headers={
                    **self.extension_origin,
                    "X-Extension-Token": "token_secreto_teste",
                }
            )
            self.assertNotEqual(resp_header.status_code, 401)

        finally:
            os.environ.pop("PJE_EXTENSION_TOKEN", None)


if __name__ == "__main__":
    unittest.main()
