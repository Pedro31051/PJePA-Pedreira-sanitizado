"""Contrato de execução direta das consultas externas ao PJe."""

import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server
from pje_client import PJeClient

CNJ_VALIDO = "0800001-61.2024.8.14.0028"


class ConfirmacaoConsultaTests(unittest.IsolatedAsyncioTestCase):
    async def test_consulta_executa_diretamente_sem_token(self):
        esperado = {"movimentacoes": []}
        with patch.object(
            server,
            "ultimas_movimentacoes",
            AsyncMock(return_value=esperado),
        ) as consulta:
            resposta = await server.analisar_processo_pje(
                acao="movimentacoes",
                numero_cnj=CNJ_VALIDO,
                persona="advogado",
                limite=3,
            )

        consulta.assert_awaited_once()
        self.assertEqual(resposta, esperado)

    async def test_campos_legados_sao_ignorados(self):
        with patch.object(
            server,
            "ultimas_movimentacoes",
            AsyncMock(return_value={"movimentacoes": []}),
        ):
            resposta = await server.analisar_processo_pje(
                acao="movimentacoes",
                numero_cnj=CNJ_VALIDO,
                persona="advogado",
                confirmar_consulta="valor-legado",
                confirmation_token="token-legado",
            )

        self.assertEqual(resposta, {"movimentacoes": []})

    async def test_operacoes_locais_executam_diretamente(self):
        corrigido = await server.buscar_processos_pje(
            acao="corrigir_cnj",
            valor=CNJ_VALIDO,
            persona="advogado",
        )
        schema = await server.painel_e_prazos_pje(
            acao="schema_acervo_tarefas",
        )

        self.assertNotIn("confirmation_token", corrigido)
        self.assertNotIn("confirmation_token", schema)

    async def test_diagnostico_leve_remove_usuario_e_retorna_so_perfis(self):
        perfil = (
            "Vara de Família de Marabá / Secretaria Vara Cível / "
            "Diretor de Secretaria"
        )
        cliente = AsyncMock()
        cliente.diagnosticar_painel_tarefas.return_value = {
            "usuario": ["Nome Pessoal"],
            "url": "https://pje.example/rota-interna",
            "perfis_disponiveis": [
                {"texto": "Nome Pessoal", "href": ""},
                {"texto": perfil, "href": "/perfil?id=segredo"},
                {"texto": "Sair", "href": "/logout"},
            ],
            "quadros": [{"texto_inicial": "dados processuais"}],
        }
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(return_value=cliente),
        ):
            resposta = await server.diagnosticar_caixas_tarefas(
                persona="servidor",
                grau="1",
            )

        cliente.diagnosticar_painel_tarefas.assert_awaited_once_with(
            profundo=False
        )
        self.assertEqual(resposta["status"], "bloqueado")
        self.assertEqual(
            resposta["codigo"], "PERFIL_SEM_IDENTIFICADOR_ESTAVEL"
        )
        self.assertEqual(resposta["modo"], "leve")
        self.assertEqual(resposta["quantidade_perfis"], 1)
        self.assertEqual(resposta["perfis_funcionais"][0]["rotulo"], perfil)
        serializado = str(resposta)
        self.assertNotIn("Nome Pessoal", serializado)
        self.assertNotIn("https://", serializado)
        self.assertNotIn("dados processuais", serializado)

    async def test_diagnostico_profundo_expoe_apenas_resumo_estrutural(self):
        cliente = AsyncMock()
        cliente.diagnosticar_painel_tarefas.return_value = {
            "usuario": ["Nome Pessoal"],
            "perfis_disponiveis": [],
            "corpo_tem_tarefas": True,
            "controles": [{}, {}],
            "iframes": [{"src": "https://interno"}],
            "quadros": [
                {
                    "texto_inicial": "conteúdo sigiloso",
                    "total_links": 10,
                    "total_botoes": 3,
                    "testes_api_processos": [
                        {"status": 200, "payload": "não expor"}
                    ],
                }
            ],
        }
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(return_value=cliente),
        ):
            resposta = await server.diagnosticar_caixas_tarefas(
                diagnostico_profundo=True,
            )

        resumo = resposta["diagnostico_estrutural"]
        self.assertEqual(resumo["total_controles"], 2)
        self.assertEqual(
            resumo["quadros"][0]["status_testes_api_processos"],
            [200],
        )
        self.assertNotIn("Nome Pessoal", str(resposta))
        self.assertNotIn("conteúdo sigiloso", str(resposta))
        self.assertNotIn("https://", str(resposta))

    async def test_diagnostico_leve_reusa_home_sem_navegacao_redundante(self):
        cliente = object.__new__(PJeClient)
        cliente.url_base = "https://pje.example"
        cliente.grau = "1g"
        cliente.persona = "servidor"
        cliente._cache_perfis_funcionais = None
        cliente._cache_perfis_criado_em = 0.0

        pagina = MagicMock()
        menu = MagicMock()
        menu.first = menu
        menu.count = AsyncMock(return_value=1)
        menu.click = AsyncMock()
        links = MagicMock()
        links.count = AsyncMock(return_value=1)
        links.all_inner_texts = AsyncMock(
            return_value=["Vara de Família / Secretaria / Diretor"]
        )
        pagina.locator.side_effect = lambda seletor: (
            links if "dropdown-menu" in seletor else menu
        )
        pagina.goto = AsyncMock()
        pagina.evaluate = AsyncMock()
        cliente._page = pagina
        cliente._listar_perfis_funcionais_dom = AsyncMock(
            return_value=[
                {
                    "index": 0,
                    "texto": "Vara de Família / Secretaria / Diretor",
                    "pje_id": "perfil-real-1",
                    "fonte_id": "data-perfilId",
                }
            ]
        )

        primeira = await PJeClient.diagnosticar_painel_tarefas.__wrapped__(
            cliente,
            profundo=False,
        )
        segunda = await PJeClient.diagnosticar_painel_tarefas.__wrapped__(
            cliente,
            profundo=False,
        )

        pagina.goto.assert_not_awaited()
        menu.click.assert_awaited_once()
        cliente._listar_perfis_funcionais_dom.assert_awaited_once()
        self.assertFalse(primeira["cache_hit"])
        self.assertTrue(segunda["cache_hit"])
        self.assertEqual(primeira["perfis_disponiveis"], segunda["perfis_disponiveis"])

    async def test_login_restaurado_exige_marcador_autenticado_estrito(self):
        cliente = PJeClient("1", "2", "JBSWY3DPEHPK3PXP")
        pagina = MagicMock()
        pagina.url = "https://pje.tjpa.jus.br/pje/home.seam"
        pagina.goto = AsyncMock()
        pagina.wait_for_selector = AsyncMock()
        menu = MagicMock()
        menu.first = menu
        menu.count = AsyncMock(return_value=1)
        formulario = MagicMock()
        formulario.first = formulario
        formulario.count = AsyncMock(return_value=0)
        pagina.locator.side_effect = lambda seletor: (
            formulario if seletor == "input#username" else menu
        )
        cliente._page = pagina

        await cliente._login_automatico_uma_vez()

        pagina.wait_for_selector.assert_not_awaited()
        pagina.fill.assert_not_called()

    async def test_login_sem_formulario_e_sem_menu_falha_fechado(self):
        cliente = PJeClient("1", "2", "JBSWY3DPEHPK3PXP")
        pagina = MagicMock()
        pagina.url = "https://sso.cloud.pje.jus.br/auth/realms/pje"
        pagina.goto = AsyncMock()
        pagina.wait_for_selector = AsyncMock(side_effect=TimeoutError())
        ausente = MagicMock()
        ausente.first = ausente
        ausente.count = AsyncMock(return_value=0)
        pagina.locator.return_value = ausente
        cliente._page = pagina

        with self.assertRaisesRegex(RuntimeError, "LOGIN_ESTADO_INDETERMINADO"):
            await cliente._login_automatico_uma_vez()

        pagina.fill.assert_not_called()

    async def test_selecao_lotacao_reusa_home_e_exige_estado_autenticado(self):
        cliente = PJeClient("1", "2", "JBSWY3DPEHPK3PXP")
        pagina = MagicMock()
        pagina.url = "https://pje.tjpa.jus.br/pje/home.seam"
        pagina.goto = AsyncMock()
        menu = MagicMock()
        menu.first = menu
        menu.count = AsyncMock(return_value=1)
        menu.click = AsyncMock()
        menu.wait_for = AsyncMock()
        pagina.locator.return_value = menu
        pagina.evaluate = AsyncMock()
        cliente._page = pagina
        cliente._listar_perfis_funcionais_dom = AsyncMock(
            return_value=[
                {
                    "index": 0,
                    "texto": "Vara de Família / Secretaria / Diretor",
                    "pje_id": "perfil-real-1",
                    "fonte_id": "data-perfilId",
                }
            ]
        )
        cliente.selecionar_e_validar_contexto = AsyncMock()
        cliente.perfil = {
            "pje_id": "perfil-real-1",
            "rotulo": "Vara de Família / Secretaria / Diretor",
        }

        escolhido = await cliente._selecionar_lotacao(
            cliente.perfil
        )

        pagina.goto.assert_not_awaited()
        menu.wait_for.assert_awaited_once()
        self.assertEqual(escolhido["pje_id"], "perfil-real-1")
        pagina.evaluate.assert_awaited_once()

    async def test_nove_ferramentas_publicam_os_dois_campos(self):
        ferramentas = [
            server.analisar_processo_completo_pje,
            server.status_e_auditoria_pje,
            server.painel_e_prazos_pje,
            server.buscar_processos_pje,
            server.analisar_processo_pje,
            server.gerir_documentos_pje,
            server.download_e_cache_pje,
            server.producao_minutas_e_relatorios,
            server.auditar_fluxo_processual_pje,
        ]
        for ferramenta in ferramentas:
            parametros = inspect.signature(ferramenta).parameters
            self.assertIn("confirmar_consulta", parametros)
            self.assertIn("confirmation_token", parametros)


if __name__ == "__main__":
    unittest.main()
