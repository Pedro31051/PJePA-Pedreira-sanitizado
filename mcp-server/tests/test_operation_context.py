"""Suíte de testes unitários para OperationContext e @operation_boundary."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import cliente_singleton
from operation_context import (
    OperationContext,
    OperationTimeoutError,
    get_current_operation_context,
    operation_boundary,
)
from session_manager import (
    SessionKey,
    SessionManager,
)


class OperationContextTests(unittest.IsolatedAsyncioTestCase):

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

    async def test_operation_context_lifecycle_and_contextvar(self):
        """M1: Contexto de operação rastreia correlation_id, tempo decorrido, status e ContextVar."""
        self.assertIsNone(get_current_operation_context())

        ctx = OperationContext(
            operation_name="test_op",
            correlation_id="corr-12345",
            timeout=10.0,
            persona="advogado",
            grau="1g",
        )
        self.assertEqual(ctx.status, "created")
        self.assertEqual(ctx.correlation_id, "corr-12345")

        async with ctx:
            self.assertEqual(ctx.status, "running")
            self.assertIs(get_current_operation_context(), ctx)
            await asyncio.sleep(0.01)
            self.assertGreater(ctx.elapsed_ms, 0.0)

        self.assertEqual(ctx.status, "completed")
        self.assertIsNone(get_current_operation_context())

        info = ctx.status_info()
        self.assertEqual(info["correlation_id"], "corr-12345")
        self.assertEqual(info["operation_name"], "test_op")
        self.assertEqual(info["status"], "completed")
        self.assertIsNone(info["error"])

    async def test_operation_boundary_decorator_success(self):
        """M1: Decorador @operation_boundary intercepta parâmetros, injeta ctx e executa normalmente."""
        @operation_boundary(name="minha_tool", timeout=5.0)
        async def minha_tool(persona: str = "advogado", grau: str = "1g", ctx: OperationContext = None):
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.operation_name, "minha_tool")
            self.assertEqual(ctx.persona, "advogado")
            self.assertEqual(ctx.grau, "1g")
            return {"sucesso": True}

        res = await minha_tool(persona="advogado", grau="1")
        self.assertEqual(res, {"sucesso": True})

    async def test_operation_boundary_timeout_handling(self):
        """M1: Decorador @operation_boundary interrompe operações lentas lançando OperationTimeoutError."""
        @operation_boundary(name="tool_lenta", timeout=0.05)
        async def tool_lenta(persona: str = "advogado", grau: str = "1g"):
            await asyncio.sleep(0.5)
            return "ok"

        with self.assertRaises(OperationTimeoutError):
            await tool_lenta()

    async def test_transactional_lease_acquisition_and_auto_release(self):
        """M1: OperationContext adquire ClientLease e garante liberação transacional ao sair do bloco."""
        mock_client = MagicMock()
        mock_client._browser = MagicMock()
        mock_client._browser.is_connected.return_value = True
        mock_client._context = MagicMock()

        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.close = AsyncMock()
        mock_client._context.new_page = AsyncMock(return_value=mock_page)
        mock_client._abas_preexistentes = []
        mock_client._op_lock = asyncio.Lock()
        mock_client._fechar = AsyncMock()
        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock()

        manager = SessionManager.get_instance()

        with patch.object(cliente_singleton, "_get_creds", return_value=("11122233344", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            ctx = OperationContext("test_lease_op", persona="advogado", grau="1g")
            async with ctx:
                lease = await ctx.acquire_lease(manager)
                self.assertIsNotNone(lease)
                self.assertIsNotNone(lease.page)
                entry = manager._sessions.get(ctx.build_session_key())
                self.assertIsNotNone(entry)
                self.assertEqual(len(entry.active_leases), 1)

            # Após sair do bloco async with ctx, a lease deve ser liberada automaticamente
            self.assertIsNone(ctx.lease)
            self.assertEqual(len(entry.active_leases), 0)
            mock_page.close.assert_called_once()

    async def test_single_flight_auth_under_concurrency_with_op_context(self):
        """M1: Múltiplas chamadas concorrentes via OperationContext compartilham o mesmo single-flight login."""
        login_count = 0

        mock_client = MagicMock()
        mock_client._browser = MagicMock()
        mock_client._browser.is_connected.return_value = True
        mock_client._context = MagicMock()

        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.close = AsyncMock()
        mock_client._context.new_page = AsyncMock(return_value=mock_page)
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

        with patch.object(cliente_singleton, "_get_creds", return_value=("55566677788", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            async def op_worker(i: int):
                ctx = OperationContext(f"op_{i}", persona="advogado", grau="1g")
                async with ctx:
                    return await ctx.execute_with_recovery(
                        lambda client, page: asyncio.sleep(0.01, result="done")
                    )

            tasks = [op_worker(i) for i in range(10)]
            results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), 10)
        self.assertTrue(all(r == "done" for r in results))
        self.assertEqual(login_count, 1, "Single-flight falhou no OperationContext")

    async def test_execute_with_recovery_auto_reconnect(self):
        """M1: execute_with_recovery detecta desconexão CDP/expiração e reconecta na 2a tentativa."""
        mock_client_old = MagicMock()
        mock_client_old._browser = MagicMock()
        mock_client_old._browser.is_connected.return_value = False
        mock_client_old._context = MagicMock()
        mock_page_old = MagicMock()
        mock_page_old.is_closed.return_value = False
        mock_page_old.close = AsyncMock()
        mock_client_old._context.new_page = AsyncMock(return_value=mock_page_old)
        mock_client_old._abas_preexistentes = []
        mock_client_old._op_lock = asyncio.Lock()
        mock_client_old._fechar = AsyncMock()
        mock_client_old._iniciar = AsyncMock()
        mock_client_old._login = AsyncMock()

        mock_client_new = MagicMock()
        mock_client_new._browser = MagicMock()
        mock_client_new._browser.is_connected.return_value = True
        mock_client_new._context = MagicMock()
        mock_page_new = MagicMock()
        mock_page_new.is_closed.return_value = False
        mock_page_new.close = AsyncMock()
        mock_client_new._context.new_page = AsyncMock(return_value=mock_page_new)
        mock_client_new._abas_preexistentes = []
        mock_client_new._op_lock = asyncio.Lock()
        mock_client_new._fechar = AsyncMock()
        mock_client_new._iniciar = AsyncMock()
        mock_client_new._login = AsyncMock()

        attempts = 0

        def factory_client(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return mock_client_old
            return mock_client_new

        async def failing_op(client, page):
            if client is mock_client_old:
                raise RuntimeError("Target page, context or browser has been closed")
            return "recovered_result"

        with patch.object(cliente_singleton, "_get_creds", return_value=("99988877766", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", side_effect=factory_client):

            ctx = OperationContext("auto_recovery_op", persona="advogado", grau="1g")
            res = await ctx.execute_with_recovery(failing_op)

        self.assertEqual(res, "recovered_result")
        self.assertEqual(attempts, 2)


class AdversarialOperationContextTests(unittest.IsolatedAsyncioTestCase):

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

    def _create_mock_client(self, cpf: str = "11122233344"):
        mock_client = MagicMock()
        mock_client.usuario_cpf = cpf
        mock_client.persona = "advogado"
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

    async def test_high_concurrency_50_op_context_lease_acquisition(self):
        """M1 Stress: 60 requisições concorrentes usando OperationContext + acquire_lease:
        - Exatamente 1 login
        - Semáforo do PagePool respeitado (máx 5 abas ativas simultâneas)
        - 0 vazamentos de abas ou leases após conclusão
        """
        login_count = 0
        mock_client = self._create_mock_client()

        async def slow_login():
            nonlocal login_count
            login_count += 1
            await asyncio.sleep(0.05)

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=slow_login)
        mock_client._selecionar_lotacao = AsyncMock()

        manager = SessionManager.get_instance()
        max_concurrent_pages = 0
        current_active_pages = 0
        lock = asyncio.Lock()
        sample_key = None

        async def worker(i: int):
            nonlocal max_concurrent_pages, current_active_pages, sample_key
            ctx = OperationContext(f"op_{i}", persona="advogado", grau="1g")
            async with ctx:
                lease = await ctx.acquire_lease(manager)
                if sample_key is None:
                    sample_key = ctx.build_session_key()
                async with lease:
                    async with lock:
                        current_active_pages += 1
                        if current_active_pages > max_concurrent_pages:
                            max_concurrent_pages = current_active_pages
                    await asyncio.sleep(0.01)
                    async with lock:
                        current_active_pages -= 1

        with patch.object(cliente_singleton, "_get_creds", return_value=("11122233344", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            tasks = [worker(i) for i in range(60)]
            await asyncio.gather(*tasks)

        self.assertEqual(login_count, 1, "Exatamente 1 login executado para 60 operações concorrentes")
        self.assertLessEqual(max_concurrent_pages, 5, "PagePool limitou páginas concorrentes a no máximo 5")
        entry = manager._sessions.get(sample_key)
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry.active_leases), 0, "Todas as leases devem ser liberadas")
        self.assertEqual(len(entry.page_pool._active_pages), 0, "Nenhuma página ativa residual no pool")

    async def test_single_flight_auth_lock_simulated_login_failure(self):
        """M1 Stress: Falha simulada no login SSO sob 50 requisições concorrentes:
        - Todas as 50 corrotinas recebem a mesma exceção
        - _login_tasks é limpo
        - Próxima tentativa pós-reset realiza nova tentativa limpa
        """
        mock_client = self._create_mock_client()

        async def failing_login():
            await asyncio.sleep(0.03)
            raise RuntimeError("SSO Authentication Server Error 500")

        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock(side_effect=failing_login)
        mock_client._selecionar_lotacao = AsyncMock()

        manager = SessionManager.get_instance()
        key = SessionKey("11122233344", "advogado", "1g", "")

        with patch.object(cliente_singleton, "_get_creds", return_value=("11122233344", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            async def op_caller(i: int):
                ctx = OperationContext(f"fail_op_{i}", persona="advogado", grau="1g")
                async with ctx:
                    await ctx.acquire_lease(manager)

            results = await asyncio.gather(*[op_caller(i) for i in range(50)], return_exceptions=True)

        self.assertEqual(len(results), 50)
        self.assertTrue(all(isinstance(r, RuntimeError) and "SSO Authentication Server Error 500" in str(r) for r in results))
        self.assertNotIn(key, manager._login_tasks, "_login_tasks deve ser limpo após falha de login")

        # Limpar estado do circuit breaker para permitir nova tentativa
        manager._sso_bloqueado_ate = 0.0
        cliente_singleton._sso_bloqueado_ate = 0.0

        mock_client_ok = self._create_mock_client()
        mock_client_ok._iniciar = AsyncMock()
        mock_client_ok._login = AsyncMock()
        mock_client_ok._selecionar_lotacao = AsyncMock()

        with patch.object(cliente_singleton, "_get_creds", return_value=("11122233344", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client_ok):
            ctx_new = OperationContext("ok_op", persona="advogado", grau="1g")
            async with ctx_new:
                lease = await ctx_new.acquire_lease(manager)
                self.assertIs(lease.client, mock_client_ok)

    async def test_page_pool_leak_isolation_under_unhandled_exceptions(self):
        """M1 Stress: Exceções não tratadas no código do usuário liberam leasing e abas sem vazamentos:
        - Exceção no meio do bloco async with lease
        - Exceção dentro de @operation_boundary
        - _task_page_var é resetado para None
        - Semáforo do PagePool é restaurado
        """
        mock_client = self._create_mock_client()
        mock_client._iniciar = AsyncMock()
        mock_client._login = AsyncMock()
        mock_client._selecionar_lotacao = AsyncMock()

        manager = SessionManager.get_instance()
        key = SessionKey("11122233344", "advogado", "1g", "")

        with patch.object(cliente_singleton, "_get_creds", return_value=("11122233344", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=mock_client):

            # Teste 1: Exceção dentro de async with lease
            ctx = OperationContext("error_op_1", persona="advogado", grau="1g")
            try:
                async with ctx:
                    lease = await ctx.acquire_lease(manager)
                    async with lease:
                        raise ValueError("Simulated unexpected user exception inside tool")
            except ValueError:
                pass

            self.assertIsNone(ctx.lease, "Lease deve ser liberada e resetada no ctx")
            entry = manager._sessions.get(key)
            self.assertEqual(len(entry.active_leases), 0, "active_leases deve estar zerado")
            self.assertEqual(len(entry.page_pool._active_pages), 0, "page_pool._active_pages deve estar zerado")
            from session_manager import get_current_task_page
            self.assertIsNone(get_current_task_page())

            # Teste 2: Exceção dentro de @operation_boundary
            @operation_boundary(name="broken_tool")
            async def broken_tool(persona: str = "advogado", grau: str = "1g", ctx: OperationContext = None):
                l = await ctx.acquire_lease()
                async with l:
                    raise KeyError("Simulated key error inside decorated tool")

            try:
                await broken_tool()
            except KeyError:
                pass

            self.assertEqual(len(entry.active_leases), 0, "active_leases zerado após erro no @operation_boundary")
            self.assertEqual(len(entry.page_pool._active_pages), 0, "page_pool._active_pages zerado após erro no @operation_boundary")

            # Teste 3: Adquirir 5 leases adicionais consecutivas para verificar que o semáforo não ficou travado
            for i in range(5):
                ctx_check = OperationContext(f"check_{i}", persona="advogado", grau="1g")
                async with ctx_check:
                    lease_check = await ctx_check.acquire_lease(manager)
                    self.assertIsNotNone(lease_check.page)

            self.assertEqual(len(entry.active_leases), 0)
            self.assertEqual(len(entry.page_pool._active_pages), 0)

