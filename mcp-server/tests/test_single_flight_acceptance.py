"""Acceptance Test Suite for Single-Flight Authentication & Session Manager Isolation.

Verifies:
1. 20 sequential calls under single identity execute exactly 1 login, 1 browser start, and 1 profile selection.
2. 10 concurrent calls without active session trigger exactly 1 single-flight login lock without deadlocks.
3. 30 rapid lease acquisitions/releases leave zero page leaks in `page_pool._active_pages`.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cliente_singleton
from session_manager import SessionKey, SessionManager


class SingleFlightAcceptanceTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        SessionManager.reset_instance()
        cliente_singleton._cliente = None
        cliente_singleton._chave_ativa = None
        cliente_singleton._ultimo_uso = 0
        cliente_singleton._sso_falhas_consecutivas = 0
        cliente_singleton._sso_bloqueado_ate = 0.0

    async def asyncTearDown(self):
        await SessionManager.get_instance().close_all_sessions()
        SessionManager.reset_instance()

    async def test_20_sequential_calls_single_identity(self):
        """20 sequential calls under the same identity result in exactly 1 login, 1 browser start, 1 profile selection."""
        iniciar_count = 0
        login_count = 0
        perfil_count = 0

        mock_client = MagicMock()
        mock_client._browser = MagicMock()
        mock_client._browser.is_connected.return_value = True
        mock_client._context = MagicMock()

        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_client._context.new_page = AsyncMock(return_value=mock_page)
        mock_client._page = mock_page
        mock_client._abas_preexistentes = []
        mock_client._op_lock = asyncio.Lock()
        mock_client._fechar = AsyncMock()

        async def fake_iniciar():
            nonlocal iniciar_count
            iniciar_count += 1

        async def fake_login():
            nonlocal login_count
            login_count += 1

        async def fake_selecionar(perfil):
            nonlocal perfil_count
            perfil_count += 1

        mock_client._iniciar = AsyncMock(side_effect=fake_iniciar)
        mock_client._login = AsyncMock(side_effect=fake_login)
        mock_client._selecionar_lotacao = AsyncMock(side_effect=fake_selecionar)

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()
        perfil_dict = {"pje_id": "12345", "rotulo": "Vara 1"}

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            clients = []
            for _ in range(20):
                entry = await manager.get_or_create_session(key, perfil_dict=perfil_dict)
                clients.append(entry.client)

        self.assertEqual(len(clients), 20)
        self.assertTrue(all(c is mock_client for c in clients))
        self.assertEqual(iniciar_count, 1, "Browser foi iniciado mais de 1 vez")
        self.assertEqual(login_count, 1, "Login SSO executou mais de 1 vez")
        self.assertEqual(perfil_count, 1, "Seleção de perfil executou mais de 1 vez")

    async def test_10_concurrent_calls_single_flight_lock(self):
        """10 concurrent calls without active session trigger exactly 1 single-flight login lock with 0 deadlocks."""
        login_count = 0

        mock_client = MagicMock()
        mock_client._browser = MagicMock()
        mock_client._browser.is_connected.return_value = True
        mock_client._context = MagicMock()

        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_client._context.new_page = AsyncMock(return_value=mock_page)
        mock_client._page = mock_page
        mock_client._abas_preexistentes = []
        mock_client._op_lock = asyncio.Lock()
        mock_client._fechar = AsyncMock()

        async def slow_login():
            nonlocal login_count
            login_count += 1
            await asyncio.sleep(0.05)

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=slow_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("88888888888", "servidor", "1g", "999")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("88888888888", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            tasks = [manager.get_or_create_session(key) for _ in range(10)]
            entries = await asyncio.gather(*tasks)

        self.assertEqual(len(entries), 10)
        self.assertTrue(all(e.client is mock_client for e in entries))
        self.assertEqual(login_count, 1, "Single-flight lock failed: executed multiple concurrent logins!")

    async def test_zero_page_leak_and_fd_exhaustion(self):
        """30 rapid lease acquisitions/releases leave zero page leaks in page_pool._active_pages."""
        mock_client = MagicMock()
        mock_client._browser = MagicMock()
        mock_client._browser.is_connected.return_value = True

        created_pages = []

        async def fake_new_page():
            p = MagicMock()
            p.is_closed.return_value = False
            p.close = AsyncMock()
            created_pages.append(p)
            return p

        mock_context = MagicMock()
        mock_context.new_page = AsyncMock(side_effect=fake_new_page)
        mock_client._context = mock_context

        user_tab = MagicMock()
        user_tab.is_closed.return_value = False
        user_tab.close = AsyncMock()
        mock_client._abas_preexistentes = [user_tab]

        mock_client._op_lock = asyncio.Lock()
        mock_client._fechar = AsyncMock()
        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock()

        key = SessionKey("99999999999", "advogado", "1g", "555")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("99999999999", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            async def run_lease_operation():
                async with await manager.acquire_lease(key) as lease:
                    await asyncio.sleep(0.005)

            # Perform 3 batches of 10 concurrent acquisitions (total 30)
            for _ in range(3):
                await asyncio.gather(*[run_lease_operation() for _ in range(10)])

        entry = manager._sessions.get(key)
        self.assertIsNotNone(entry)
        self.assertEqual(
            len(entry.page_pool._active_pages),
            0,
            "Active page leak detected in PagePool after lease release!",
        )
        user_tab.close.assert_not_called()
        self.assertTrue(
            all(p.close.awaited for p in created_pages),
            "Allocated pages were not properly closed upon release!",
        )


if __name__ == "__main__":
    unittest.main()
