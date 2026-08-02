"""Regressão: o caminho de lease deve propagar permitir_sem_perfil.

Bug de 31/07/2026: get_cliente(permitir_sem_perfil=True) delegava para
OperationContext.acquire_lease -> SessionManager.acquire_lease sem repassar a
flag; a criação da sessão então chamava selecionar_e_validar_contexto(None) e
persona interna sem perfil morria com CONTEXTO_DIVERGENTE mesmo em ações de
descoberta (diagnosticar_caixas / listar_perfis).
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import cliente_singleton
from operation_context import OperationContext
from session_manager import SessionManager


def _mock_client():
    client = MagicMock()
    client._browser = MagicMock()
    client._browser.is_connected.return_value = True
    client._context = MagicMock()
    page = MagicMock()
    page.is_closed.return_value = False
    page.close = AsyncMock()
    client._context.new_page = AsyncMock(return_value=page)
    client._abas_preexistentes = []
    client._op_lock = asyncio.Lock()
    client._fechar = AsyncMock()
    client._iniciar = AsyncMock()
    client._login = AsyncMock()
    client.selecionar_e_validar_contexto = AsyncMock()
    client.contexto_fixado = None
    return client


class LeasePermitirSemPerfilTests(unittest.IsolatedAsyncioTestCase):

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

    async def test_lease_com_permitir_sem_perfil_nao_valida_contexto_nulo(self):
        client = _mock_client()
        manager = SessionManager.get_instance()
        with patch.object(cliente_singleton, "_get_creds", return_value=("11122233344", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=client):
            ctx = OperationContext(
                "descoberta_perfis",
                persona="servidor",
                grau="1g",
                perfil_dict=None,
                permitir_sem_perfil=True,
            )
            async with ctx:
                lease = await ctx.acquire_lease(manager)
                self.assertIsNotNone(lease)
        client.selecionar_e_validar_contexto.assert_not_called()

    async def test_lease_sem_flag_mantem_validacao_fail_closed(self):
        client = _mock_client()
        client.selecionar_e_validar_contexto.side_effect = RuntimeError(
            "CONTEXTO_DIVERGENTE: perfil esperado não informado para persona interna."
        )
        manager = SessionManager.get_instance()
        with patch.object(cliente_singleton, "_get_creds", return_value=("11122233344", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=client):
            ctx = OperationContext(
                "consulta_interna",
                persona="servidor",
                grau="1g",
                perfil_dict=None,
                permitir_sem_perfil=False,
            )
            with self.assertRaises(RuntimeError):
                async with ctx:
                    await ctx.acquire_lease(manager)
        client.selecionar_e_validar_contexto.assert_awaited_once_with(None)

    async def test_get_cliente_repassa_flag_ao_op_context(self):
        client = _mock_client()
        manager = SessionManager.get_instance()
        with patch.object(cliente_singleton, "_get_creds", return_value=("11122233344", "pwd", "seed")), \
             patch.object(cliente_singleton, "PJeClient", return_value=client), \
             patch.object(cliente_singleton.perfil_contexto, "contexto_atual", return_value=None):
            ctx = OperationContext(
                "diagnosticar_caixas",
                persona="servidor",
                grau="1g",
                perfil_dict=None,
                permitir_sem_perfil=False,  # decorator default: a flag vem do get_cliente
            )
            async with ctx:
                resultado = await cliente_singleton.get_cliente(
                    "servidor", "1g", permitir_sem_perfil=True
                )
                self.assertIs(resultado, client)
        client.selecionar_e_validar_contexto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
