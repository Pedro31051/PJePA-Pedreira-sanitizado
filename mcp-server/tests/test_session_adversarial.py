"""Suíte de testes de estresse adversarial para SessionManager (Milestone 1).

Testa:
1. Concorrência Single-Flight com 10 a 50 chamadas simultâneas (zero deadlocks, exatamente 1 login).
2. Concorrência Single-Flight com 50 chamadas de acquire_lease (controle de pool de páginas por semáforo).
3. Concorrência multi-identidade (50 chamadas divididas em 5 chaves distintas -> exatamente 5 logins).
4. Reutilização de sessão em 20 e 100 chamadas sequenciais sob a mesma identidade.
5. Chegadas defasadas de requisições durante o login (staggered arrivals).
6. Cancelamento parcial de garçons (partial cancellation) e cancelamento total.
7. Resiliência a falhas de login (exceções propagadas e limpeza de _login_tasks).
8. Auto-recuperação de sessão em caso de expiração (execute_with_recovery).
"""

import asyncio
import random
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import cliente_singleton
from session_manager import (
    SessionExpiredError,
    SessionKey,
    SessionManager,
)


class AdversarialSessionTests(unittest.IsolatedAsyncioTestCase):

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

    def _create_mock_client(self, cpf: str = "00000000000", persona: str = "advogado"):
        mock_client = MagicMock()
        mock_client.usuario_cpf = cpf
        mock_client.persona = persona
        mock_client._browser = MagicMock()
        mock_client._browser.is_connected.return_value = True

        created_pages = []

        async def fake_new_page():
            p = MagicMock()
            p.is_closed.return_value = False
            p.url = "https://pje.tjpa.jus.br/pje/Painel/painel_usuario/advogado.seam"
            p.close = AsyncMock()
            created_pages.append(p)
            return p

        mock_context = MagicMock()
        mock_context.new_page = AsyncMock(side_effect=fake_new_page)
        mock_client._context = mock_context
        mock_client._page = MagicMock()
        mock_client._page.is_closed.return_value = False
        mock_client._page.url = "https://pje.tjpa.jus.br/pje/Painel/painel_usuario/advogado.seam"
        mock_client._abas_preexistentes = []
        mock_client._op_lock = asyncio.Lock()
        mock_client._fechar = AsyncMock()
        mock_client.created_pages = created_pages

        return mock_client

    async def test_single_flight_10_concurrent_stress(self):
        """10 chamadas concorrentes sob a mesma identidade resultam em exatamente 1 login."""
        login_count = 0
        mock_client = self._create_mock_client()

        async def slow_login():
            nonlocal login_count
            login_count += 1
            await asyncio.sleep(0.05)

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=slow_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            tasks = [manager.get_or_create_session(key) for _ in range(10)]
            entries = await asyncio.gather(*tasks)

        self.assertEqual(len(entries), 10)
        self.assertTrue(all(e.client is mock_client for e in entries))
        self.assertEqual(login_count, 1, "Exatamente 1 login deve ser executado para 10 chamadas simultâneas")

    async def test_single_flight_50_concurrent_stress(self):
        """50 chamadas concorrentes sob a mesma identidade resultam em exatamente 1 login e zero deadlocks."""
        login_count = 0
        iniciar_count = 0
        mock_client = self._create_mock_client()

        async def slow_iniciar():
            nonlocal iniciar_count
            iniciar_count += 1

        async def slow_login():
            nonlocal login_count
            login_count += 1
            await asyncio.sleep(0.08)

        mock_client._iniciar = AsyncMock(side_effect=slow_iniciar)
        mock_client._login = AsyncMock(side_effect=slow_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            tasks = [manager.get_or_create_session(key) for _ in range(50)]
            entries = await asyncio.gather(*tasks)

        self.assertEqual(len(entries), 50)
        self.assertTrue(all(e.client is mock_client for e in entries))
        self.assertEqual(iniciar_count, 1, "Exatamente 1 inicialização de browser para 50 chamadas")
        self.assertEqual(login_count, 1, "Exatamente 1 login SSO para 50 chamadas simultâneas")

    async def test_single_flight_50_concurrent_acquire_lease(self):
        """50 chamadas concorrentes de acquire_lease: exatamente 1 login, semafóro de páginas respeitado (máx 5), 0 vazamentos."""
        login_count = 0
        mock_client = self._create_mock_client()

        async def slow_login():
            nonlocal login_count
            login_count += 1
            await asyncio.sleep(0.05)

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=slow_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        max_concurrent_active_pages = 0
        current_active_pages = 0
        active_lock = asyncio.Lock()

        async def worker():
            nonlocal current_active_pages, max_concurrent_active_pages
            async with await manager.acquire_lease(key) as lease:
                async with active_lock:
                    current_active_pages += 1
                    if current_active_pages > max_concurrent_active_pages:
                        max_concurrent_active_pages = current_active_pages
                await asyncio.sleep(0.01)
                async with active_lock:
                    current_active_pages -= 1

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            tasks = [worker() for _ in range(50)]
            await asyncio.gather(*tasks)

        self.assertEqual(login_count, 1, "Exatamente 1 login executado para 50 solicitações de lease")
        self.assertLessEqual(max_concurrent_active_pages, 5, "Pool de páginas deve limitar a no máximo 5 abas ativas simultâneas")
        entry = manager._sessions.get(key)
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry.page_pool._active_pages), 0, "Todas as páginas devem ser liberadas ao final")

    async def test_multi_identity_50_concurrent_stress(self):
        """50 chamadas simultâneas divididas em 5 identidades distintas -> exatamente 5 logins (1 por identidade)."""
        login_counts = {}

        def make_client(cpf):
            client = self._create_mock_client(cpf=cpf)

            async def slow_login():
                login_counts[cpf] = login_counts.get(cpf, 0) + 1
                await asyncio.sleep(0.05)

            client._iniciar = AsyncMock()
            client._login = AsyncMock(side_effect=slow_login)
            client._selecionar_lotacao = AsyncMock()
            return client

        clients_by_cpf = {f"user_{i}": make_client(f"user_{i}") for i in range(5)}
        manager = SessionManager.get_instance()

        tasks = []
        for i in range(50):
            cpf = f"user_{i % 5}"
            key = SessionKey(cpf, "advogado", "1g", f"perfil_{i % 5}")
            tasks.append((key, cpf))

        def pje_client_factory(cpf, senha, seed, persona, headless, grau, perfil):
            if perfil and "pje_id" in perfil:
                pid = perfil["pje_id"]
                user_key = f"user_{pid.split('_')[-1]}"
                if user_key in clients_by_cpf:
                    return clients_by_cpf[user_key]
            return clients_by_cpf.get(cpf, list(clients_by_cpf.values())[0])

        with patch.object(cliente_singleton, "_get_creds", return_value=("user_static", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", side_effect=pje_client_factory):

            results = await asyncio.gather(*[manager.get_or_create_session(k, perfil_dict={"pje_id": k.perfil_id}) for k, _ in tasks])

        self.assertEqual(len(results), 50)
        self.assertEqual(len(login_counts), 5)
        for cpf, count in login_counts.items():
            self.assertEqual(count, 1, f"Identidade {cpf} deve ter exatamente 1 login")

    async def test_20_sequential_calls_session_reuse(self):
        """20 chamadas sequenciais sob a mesma identidade reutilizam exatamente a mesma instância de sessão sem novos logins."""
        login_count = 0
        iniciar_count = 0
        perfil_count = 0
        mock_client = self._create_mock_client()

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

            first_entry = await manager.get_or_create_session(key, perfil_dict=perfil_dict)

            for i in range(19):
                entry = await manager.get_or_create_session(key, perfil_dict=perfil_dict)
                self.assertIs(entry, first_entry, f"Chamada sequencial #{i+2} retornou instância de SessionEntry diferente!")

        self.assertEqual(iniciar_count, 1, "Browser não deve ser reiniciado")
        self.assertEqual(login_count, 1, "Login SSO não deve ser repetido")
        self.assertEqual(perfil_count, 1, "Seleção de perfil não deve ser repetida")

    async def test_100_sequential_acquire_release_reuse(self):
        """100 chamadas sequenciais de acquire_lease/release reusam a mesma sessão e mantém 0 vazamentos de páginas."""
        login_count = 0
        mock_client = self._create_mock_client()

        async def fake_login():
            nonlocal login_count
            login_count += 1

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=fake_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            for _ in range(100):
                async with await manager.acquire_lease(key) as lease:
                    self.assertIs(lease.client, mock_client)

        self.assertEqual(login_count, 1, "Exatamente 1 login para 100 chamadas sequenciais")
        entry = manager._sessions.get(key)
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry.page_pool._active_pages), 0, "Zero vazamentos de abas após 100 execuções")

    async def test_single_flight_staggered_arrivals(self):
        """Requisições que chegam com atrasos aleatórios durante a autenticação acoplam-se ao single-flight."""
        login_count = 0
        mock_client = self._create_mock_client()

        async def slow_login():
            nonlocal login_count
            login_count += 1
            await asyncio.sleep(0.15)

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=slow_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        async def delayed_caller(delay_s):
            await asyncio.sleep(delay_s)
            return await manager.get_or_create_session(key)

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            delays = [random.uniform(0.0, 0.05) for _ in range(30)]
            tasks = [delayed_caller(d) for d in delays]
            entries = await asyncio.gather(*tasks)

        self.assertEqual(len(entries), 30)
        self.assertTrue(all(e.client is mock_client for e in entries))
        self.assertEqual(login_count, 1, "Todas as requisições defasadas durante a autenticação devem compartilhar o único login")

    async def test_single_flight_partial_cancellation(self):
        """Cancelamento de 5 de 10 garçons não interrompe nem corrompe a sessão para os 5 garçons restantes."""
        login_count = 0
        mock_client = self._create_mock_client()

        async def slow_login():
            nonlocal login_count
            login_count += 1
            await asyncio.sleep(0.1)

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=slow_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            tasks = [asyncio.create_task(manager.get_or_create_session(key)) for _ in range(10)]
            await asyncio.sleep(0.02)

            for t in tasks[:5]:
                t.cancel()

            results = []
            for t in tasks[5:]:
                try:
                    res = await t
                    results.append(res)
                except asyncio.CancelledError:
                    pass

        self.assertEqual(
            len(results),
            5,
            "VULNERABILIDADE SINGLE-FLIGHT: Cancelamento de um garçom cancelou o login para todos os outros 5 garçons aguardando!",
        )
        self.assertTrue(all(r.client is mock_client for r in results))
        self.assertEqual(login_count, 1)

    async def test_single_flight_total_cancellation(self):
        """Se TODOS os garçons forem cancelados, a task de login é cancelada e limpa de _login_tasks."""
        mock_client = self._create_mock_client()
        login_started = asyncio.Event()

        async def slow_login():
            login_started.set()
            await asyncio.sleep(0.2)

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=slow_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            tasks = [asyncio.create_task(manager.get_or_create_session(key)) for _ in range(5)]
            await login_started.wait()

            for t in tasks:
                t.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

        self.assertNotIn(key, manager._login_tasks, "_login_tasks deve ser limpo após cancelamento total")

    async def test_single_flight_exception_recovery(self):
        """Se o login falhar com exceção, a falha se propaga a todos os garçons e permite nova tentativa limpa."""
        mock_client = self._create_mock_client()

        async def failing_login():
            await asyncio.sleep(0.02)
            raise RuntimeError("Falha de autenticação Keycloak SSO")

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=failing_login)
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            tasks = [manager.get_or_create_session(key) for _ in range(10)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            self.assertEqual(len(results), 10)
            self.assertTrue(all(isinstance(r, RuntimeError) for r in results))
            self.assertNotIn(key, manager._login_tasks, "_login_tasks deve ser limpo após exceção")

            # Reset do circuit breaker para permitir a nova tentativa limpa pós-falha
            manager._sso_bloqueado_ate = 0.0
            cliente_singleton._sso_bloqueado_ate = 0.0

            mock_client_2 = self._create_mock_client()
            mock_client_2._iniciar = AsyncMock()
            mock_client_2._login = AsyncMock()
            mock_client_2._selecionar_lotacao = AsyncMock()

            with patch.object(cliente_singleton, "PJeClient", return_value=mock_client_2):
                entry = await manager.get_or_create_session(key)
                self.assertIs(entry.client, mock_client_2, "Próxima chamada após falha deve iniciar novo login limpo")

    async def test_auto_recovery_on_session_expiration(self):
        """execute_with_recovery recupera transparentemente após expiração de sessão evictando a sessão anterior."""
        mock_client_1 = self._create_mock_client()
        mock_client_1._iniciar = AsyncMock()
        mock_client_1._login = AsyncMock()
        mock_client_1._selecionar_lotacao = AsyncMock()

        mock_client_2 = self._create_mock_client()
        mock_client_2._iniciar = AsyncMock()
        mock_client_2._login = AsyncMock()
        mock_client_2._selecionar_lotacao = AsyncMock()

        clients = [mock_client_1, mock_client_2]
        client_idx = 0

        def client_factory(*args, **kwargs):
            nonlocal client_idx
            c = clients[client_idx]
            client_idx = min(client_idx + 1, len(clients) - 1)
            return c

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        call_count = 0

        async def sample_op(client, page):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SessionExpiredError("Sessão do PJe expirada no servidor")
            return "SUCESSO_REOPERACAO"

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", side_effect=client_factory):

            res = await manager.execute_with_recovery(key, None, sample_op)

        self.assertEqual(res, "SUCESSO_REOPERACAO")
        self.assertEqual(call_count, 2, "Operação deve ser tentada 2 vezes (1 falha com expiração + 1 sucesso na nova sessão)")
        self.assertIs(manager._sessions[key].client, mock_client_2, "Sessão expirada foi substituída pela nova")

    async def test_acquire_lease_timeout_enforcement(self):
        """acquire_lease com timeout deve lançar TimeoutError quando a pool de páginas está esgotada."""
        mock_client = self._create_mock_client()
        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock()
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            leases = []
            for _ in range(5):
                lease = await manager.acquire_lease(key, timeout=1.0)
                leases.append(lease)

            with self.assertRaises(TimeoutError):
                await manager.acquire_lease(key, timeout=0.1)

            for l in leases:
                await l.release()

    async def test_watchdog_idle_eviction_pool_sessions(self):
        """fechar_se_ocioso limpa sessões ociosas em SessionManager cuja inatividade excede TIMEOUT_INATIVIDADE_S."""
        mock_client = self._create_mock_client()
        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock()
        mock_client._selecionar_lotacao = AsyncMock()

        key = SessionKey("00000000000", "advogado", "1g", "12345")
        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("00000000000", "senha", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            entry = await manager.get_or_create_session(key)
            self.assertIn(key, manager._sessions)

            import time
            entry.last_used_at = time.monotonic() - (manager.max_idle_seconds + 10)

            closed = await cliente_singleton.fechar_se_ocioso()
            self.assertTrue(closed, "fechar_se_ocioso deve retornar True ao fechar sessão ociosa do pool")
            self.assertNotIn(key, manager._sessions, "Sessão ociosa deve ser removida do SessionManager")
