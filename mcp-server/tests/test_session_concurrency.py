"""Suíte de testes de concorrência, resiliência de sessão, single-flight lock e client leases."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import cliente_singleton
from session_manager import (
    SessionKey,
    SessionManager,
)


class SessionConcurrencyTests(unittest.IsolatedAsyncioTestCase):

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

    async def test_sequential_20_calls(self):
        """20 chamadas sequenciais sob a mesma identidade executam com exatamente 1 login, 1 browser e 1 seleção de perfil."""
        login_count = 0
        iniciar_count = 0
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

    async def test_single_flight_10_concurrent(self):
        """10 chamadas simultâneas sem sessão ativa resultam em exatamente 1 login (single-flight lock)."""
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
        self.assertEqual(login_count, 1, "Single-flight falhou: executou múltiplos logins concorrentes!")

    async def test_page_pool_leak_isolation(self):
        """Zero vazamento de abas/páginas e zero vazamento de FDs sob chamadas intensivas."""
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

            async def run_operation():
                async with await manager.acquire_lease(key) as lease:
                    await asyncio.sleep(0.01)

            for _ in range(3):
                await asyncio.gather(*[run_operation() for _ in range(10)])

        entry = manager._sessions.get(key)
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry.page_pool._active_pages), 0, "Abas ativas restaram no PagePool após liberação")
        user_tab.close.assert_not_called()
        self.assertTrue(all(p.close.awaited for p in created_pages), "Páginas alocadas não foram fechadas na liberação")

    async def test_client_lease_protection(self):
        """Operações ativas sob a Identidade A são protegidas contra evicção quando a Identidade B solicita lease."""
        manager = SessionManager.get_instance()
        manager.max_active_sessions = 1

        mock_client_a = MagicMock()
        mock_client_a._browser = MagicMock()
        mock_client_a._browser.is_connected.return_value = True
        mock_client_a._context = MagicMock()
        mock_page_a = MagicMock()
        mock_page_a.is_closed.return_value = False
        mock_page_a.close = AsyncMock()
        mock_client_a._context.new_page = AsyncMock(return_value=mock_page_a)
        mock_client_a._abas_preexistentes = []
        mock_client_a._op_lock = asyncio.Lock()
        mock_client_a._fechar = AsyncMock()
        mock_client_a._iniciar = AsyncMock()
        mock_client_a._login = AsyncMock()

        mock_client_b = MagicMock()
        mock_client_b._browser = MagicMock()
        mock_client_b._browser.is_connected.return_value = True
        mock_client_b._context = MagicMock()
        mock_page_b = MagicMock()
        mock_page_b.is_closed.return_value = False
        mock_page_b.close = AsyncMock()
        mock_client_b._context.new_page = AsyncMock(return_value=mock_page_b)
        mock_client_b._abas_preexistentes = []
        mock_client_b._op_lock = asyncio.Lock()
        mock_client_b._fechar = AsyncMock()
        mock_client_b._iniciar = AsyncMock()
        mock_client_b._login = AsyncMock()

        key_a = SessionKey("11111111111", "advogado", "1g", "perfilA")
        key_b = SessionKey("22222222222", "servidor", "1g", "perfilB")

        def factory_client(cpf, *args, **kwargs):
            if cpf == "11111111111":
                return mock_client_a
            return mock_client_b

        with patch.object(cliente_singleton, "_get_creds", side_effect=lambda: ("11111111111", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", side_effect=factory_client):

            lease_a = await manager.acquire_lease(key_a)
            self.assertIn(key_a, manager._sessions)
            self.assertEqual(len(manager._sessions[key_a].active_leases), 1)

            with patch.object(cliente_singleton, "_get_creds", side_effect=lambda: ("22222222222", "pwd", "seed")):
                lease_b = await manager.acquire_lease(key_b)

            mock_client_a._fechar.assert_not_called()
            self.assertIn(key_a, manager._sessions)
            self.assertIn(key_b, manager._sessions)

            await lease_a.release()
            await lease_b.release()

            await manager._evict_idle_or_lru_sessions()
            mock_client_a._fechar.assert_called_once()
