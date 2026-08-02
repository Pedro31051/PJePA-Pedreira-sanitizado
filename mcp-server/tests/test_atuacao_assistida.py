"""Testes da política de atuação assistida no fluxo de tarefas.

O contrato central verificado aqui é adversarial: NENHUM caminho de código
pode levar uma transição não provada reversível até a UI, e nenhuma falha
(log indisponível, sessão concorrente, DOM divergente) pode degradar para
"segue em frente". Tudo falha fechado.
"""

import asyncio
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import caixas_tarefas
import pje_client
import server
from politica_atuacao import (
    AGUARDANDO_COMMIT_HUMANO,
    COMMIT_NA_SELECAO,
    INDETERMINADA,
    OBSERVACAO,
    PREPARACAO_ASSISTIDA,
    REVERSIVEL_ANTES_COMMIT,
    BloqueioSegurancaException,
    politica_atuacao,
)


class _BasePolitica(unittest.TestCase):
    """Storage temporário + política zerada em cada teste."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "PJE_STORAGE_DIR": self.tmp.name,
                "PJE_AUDIT_MASTER_KEY": base64.urlsafe_b64encode(
                    b"a" * 32
                ).decode("ascii"),
            },
        )
        self.env.start()
        politica_atuacao.configurar_storage(self.tmp.name)
        politica_atuacao.modo_ativo = OBSERVACAO
        politica_atuacao._sessao_dona = None

    def tearDown(self):
        politica_atuacao.modo_ativo = OBSERVACAO
        politica_atuacao._sessao_dona = None
        os.environ.pop("PJE_ATUACAO_ASSISTIDA", None)
        self.env.stop()
        self.tmp.cleanup()
        politica_atuacao.configurar_storage("/var/lib/pjepa-mcp/storage")


class PoliticaAtuacaoUnitTests(_BasePolitica):
    def test_kill_switch_double_check(self):
        # Sem PJE_ATUACAO_ASSISTIDA=1, o modo ativo é forçado para OBSERVACAO
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "0"
        politica_atuacao.set_modo(PREPARACAO_ASSISTIDA)
        self.assertEqual(politica_atuacao.modo_ativo, OBSERVACAO)

        # Com PJE_ATUACAO_ASSISTIDA=1, transição é permitida
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        politica_atuacao.set_modo(PREPARACAO_ASSISTIDA)
        self.assertEqual(politica_atuacao.modo_ativo, PREPARACAO_ASSISTIDA)

    def test_validar_clique_somente_leitura(self):
        politica_atuacao.modo_ativo = OBSERVACAO
        with self.assertRaises(BloqueioSegurancaException):
            politica_atuacao.validar_clique(
                text="Avançar", title="", aria_label="", value="",
                selector="button",
            )

    def test_validar_clique_denylist_em_preparacao(self):
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        politica_atuacao.modo_ativo = PREPARACAO_ASSISTIDA

        # Permitido: seletor na allowlist, sem termo de commit
        politica_atuacao.validar_clique(
            text="0000001-01.2026.8.14.0301", title="", aria_label="",
            value="", selector="input[type='checkbox']",
        )

        for termo in ("Confirmar", "Assinar", "Encaminhar", "Salvar"):
            with self.assertRaises(BloqueioSegurancaException):
                politica_atuacao.validar_clique(
                    text=termo, title="", aria_label="", value="",
                    selector="input[type='checkbox']",
                )

    def test_validar_clique_allowlist_default_deny(self):
        """Seletor fora da allowlist é bloqueado MESMO sem termo proibido."""
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        politica_atuacao.modo_ativo = PREPARACAO_ASSISTIDA
        for seletor in ("button", "a#qualquer", "div.icone-sem-texto", ""):
            with self.assertRaises(BloqueioSegurancaException):
                politica_atuacao.validar_clique(
                    text="", title="", aria_label="", value="",
                    selector=seletor,
                )

    def test_log_indisponivel_bloqueia_atuacao(self):
        """Sem trilha de auditoria gravável não existe sessão assistida."""
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        politica_atuacao.configurar_storage(
            Path(self.tmp.name) / "inexistente_sem_permissao"
        )
        # Torna o diretório-pai somente leitura para o mkdir falhar
        bloqueado = Path(self.tmp.name) / "ro"
        bloqueado.mkdir()
        os.chmod(bloqueado, 0o500)
        politica_atuacao.configurar_storage(bloqueado / "sub")
        try:
            with self.assertRaises(BloqueioSegurancaException):
                politica_atuacao.iniciar_sessao_assistida("tok1")
            self.assertEqual(politica_atuacao.modo_ativo, OBSERVACAO)
            # set_modo degrada silenciosamente para OBSERVACAO (fail-closed)
            politica_atuacao.set_modo(PREPARACAO_ASSISTIDA)
            self.assertEqual(politica_atuacao.modo_ativo, OBSERVACAO)
        finally:
            os.chmod(bloqueado, 0o700)


class SessaoAssistidaTests(_BasePolitica):
    def test_posse_exclusiva_da_sessao(self):
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        politica_atuacao.iniciar_sessao_assistida("dono")
        with self.assertRaises(BloqueioSegurancaException):
            politica_atuacao.iniciar_sessao_assistida("intruso")
        # Reentrada do próprio dono é permitida
        politica_atuacao.iniciar_sessao_assistida("dono")
        politica_atuacao.encerrar_sessao_assistida("dono")
        politica_atuacao.iniciar_sessao_assistida("outro")

    def test_modo_para_nao_dono_e_observacao(self):
        """Sessão concorrente NUNCA herda o modo assistido global."""
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        politica_atuacao.iniciar_sessao_assistida("dono")
        politica_atuacao.transicionar_para_commit_humano("dono")
        self.assertEqual(politica_atuacao.modo_ativo, AGUARDANDO_COMMIT_HUMANO)
        self.assertEqual(
            politica_atuacao.modo_para("dono"), AGUARDANDO_COMMIT_HUMANO
        )
        self.assertEqual(politica_atuacao.modo_para("intruso"), OBSERVACAO)
        self.assertEqual(politica_atuacao.modo_para(None), OBSERVACAO)

    def test_commit_humano_so_pelo_dono(self):
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        politica_atuacao.iniciar_sessao_assistida("dono")
        with self.assertRaises(BloqueioSegurancaException):
            politica_atuacao.transicionar_para_commit_humano("intruso")

    def test_encerrar_por_nao_dono_e_noop(self):
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        politica_atuacao.iniciar_sessao_assistida("dono")
        politica_atuacao.encerrar_sessao_assistida("intruso")
        self.assertEqual(politica_atuacao.modo_ativo, PREPARACAO_ASSISTIDA)


class GateTransicaoTests(_BasePolitica):
    """O gate de reversibilidade é o portão do projeto: nada presumido passa."""

    def test_destino_nao_mapeado_bloqueia(self):
        with self.assertRaises(BloqueioSegurancaException):
            politica_atuacao.validar_transicao(None)

    def test_commit_na_selecao_bloqueia(self):
        destino = {
            "destino_id": "5",
            "reversivel": COMMIT_NA_SELECAO,
            "fonte": "confirmacao_humana",
        }
        with self.assertRaises(BloqueioSegurancaException):
            politica_atuacao.validar_transicao(destino)

    def test_indeterminada_bloqueia(self):
        destino = {
            "destino_id": "5",
            "reversivel": INDETERMINADA,
            "fonte": "heuristica_tipo_elemento",
        }
        with self.assertRaises(BloqueioSegurancaException):
            politica_atuacao.validar_transicao(destino)

    def test_reversivel_por_heuristica_bloqueia(self):
        """Rótulo REVERSIVEL sem prova (fonte heurística) NÃO passa."""
        destino = {
            "destino_id": "5",
            "reversivel": REVERSIVEL_ANTES_COMMIT,
            "fonte": "heuristica_tipo_elemento",
        }
        with self.assertRaises(BloqueioSegurancaException):
            politica_atuacao.validar_transicao(destino)

    def test_reversivel_confirmado_passa(self):
        destino = {
            "destino_id": "5",
            "reversivel": REVERSIVEL_ANTES_COMMIT,
            "fonte": "confirmacao_humana",
        }
        politica_atuacao.validar_transicao(destino)  # não levanta


class TransicoesBancoTests(_BasePolitica):
    def test_descoberta_salva_fonte_heuristica(self):
        caixas_tarefas.salvar_transicao("p1", "Triagem", "7", "Concluir", INDETERMINADA)
        (t,) = caixas_tarefas.obter_transicoes("p1", "Triagem")
        self.assertEqual(t["fonte"], "heuristica_tipo_elemento")
        self.assertEqual(t["reversivel"], INDETERMINADA)

    def test_confirmacao_exige_mapeamento_e_evidencia(self):
        with self.assertRaises(ValueError):
            caixas_tarefas.confirmar_transicao_reversivel(
                "p1", "Triagem", "7", evidencia="teste"
            )
        caixas_tarefas.salvar_transicao("p1", "Triagem", "7", "Minutar", INDETERMINADA)
        with self.assertRaises(ValueError):
            caixas_tarefas.confirmar_transicao_reversivel(
                "p1", "Triagem", "7", evidencia="   "
            )

    def test_commit_na_selecao_nao_e_promovivel(self):
        caixas_tarefas.salvar_transicao(
            "p1", "Triagem", "btnEnc", "Encaminhar", COMMIT_NA_SELECAO
        )
        with self.assertRaises(ValueError):
            caixas_tarefas.confirmar_transicao_reversivel(
                "p1", "Triagem", "btnEnc", evidencia="verificado em tela"
            )

    def test_promocao_e_protecao_contra_rebaixamento(self):
        caixas_tarefas.salvar_transicao("p1", "Triagem", "7", "Minutar", INDETERMINADA)
        r = caixas_tarefas.confirmar_transicao_reversivel(
            "p1", "Triagem", "7",
            evidencia="Selecionei o destino num item de teste; nada moveu até clicar o botão.",
        )
        self.assertEqual(r["reversivel"], REVERSIVEL_ANTES_COMMIT)
        self.assertEqual(r["fonte"], "confirmacao_humana")
        # Redescoberta heurística NÃO rebaixa a classificação provada
        caixas_tarefas.salvar_transicao(
            "p1", "Triagem", "7", "Minutar (novo rótulo)", INDETERMINADA
        )
        (t,) = caixas_tarefas.obter_transicoes("p1", "Triagem")
        self.assertEqual(t["reversivel"], REVERSIVEL_ANTES_COMMIT)
        self.assertEqual(t["fonte"], "confirmacao_humana")
        self.assertEqual(t["destino_nome"], "Minutar (novo rótulo)")


def _cliente_fake():
    """Instância mínima de PJeClient sem navegador para exercitar o método real."""
    cli = pje_client.PJeClient.__new__(pje_client.PJeClient)
    cli._op_lock = asyncio.Lock()
    cli.contexto_fixado = None
    cli._page = None
    cli.url_base = "https://pje.tjpa.jus.br/pje"
    cli._contencao_rede_ativa = False
    return cli


class PreparacaoAdversarialTests(_BasePolitica):
    """Prova que uma transição não provada NUNCA chega ao select_option."""

    def _rodar_preparacao(self, transicoes):
        cli = _cliente_fake()
        simulacao = {
            "valido": True,
            "lote_hash": "abc123",
            "itens": [
                {
                    "numero_processo": "0000001-01.2026.8.14.0301",
                    "id_task_instance": "111",
                    "status": "INCLUIDO",
                }
            ],
        }
        with patch.object(
            caixas_tarefas, "obter_snapshot", return_value={"snapshot_id": "s1"}
        ), patch.object(
            caixas_tarefas, "simular_lote", return_value=simulacao
        ), patch.object(
            caixas_tarefas, "obter_transicoes", return_value=transicoes
        ), patch(
            "mapa_pje.TaskFlowPanelPage"
        ) as PanelMock:
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    cli.preparar_movimentacao_lote(
                        snapshot_id="s1",
                        processos_ids=["0000001-01.2026.8.14.0301"],
                        destino_id="5",
                        nome_tarefa="Triagem",
                        confirmar_preparacao=True,
                    )
                )
            # O gate falhou ANTES de qualquer UI: o painel nunca foi aberto.
            PanelMock.assert_not_called()

    def test_destino_nao_mapeado_nunca_abre_ui(self):
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        self._rodar_preparacao([])

    def test_commit_na_selecao_nunca_abre_ui(self):
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        self._rodar_preparacao([
            {
                "destino_id": "5",
                "destino_nome": "Encaminhar",
                "reversivel": COMMIT_NA_SELECAO,
                "fonte": "confirmacao_humana",
            }
        ])

    def test_indeterminada_nunca_abre_ui(self):
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        self._rodar_preparacao([
            {
                "destino_id": "5",
                "destino_nome": "Minutar",
                "reversivel": INDETERMINADA,
                "fonte": "heuristica_tipo_elemento",
            }
        ])

    def test_reversivel_heuristico_nunca_abre_ui(self):
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "1"
        self._rodar_preparacao([
            {
                "destino_id": "5",
                "destino_nome": "Minutar",
                "reversivel": REVERSIVEL_ANTES_COMMIT,
                "fonte": "heuristica_tipo_elemento",
            }
        ])

    def test_kill_switch_desligado_nunca_abre_ui(self):
        """Mesmo com transição provada, sem kill-switch nada acontece."""
        os.environ["PJE_ATUACAO_ASSISTIDA"] = "0"
        self._rodar_preparacao([
            {
                "destino_id": "5",
                "destino_nome": "Minutar",
                "reversivel": REVERSIVEL_ANTES_COMMIT,
                "fonte": "confirmacao_humana",
            }
        ])

    def test_simulacao_nao_exige_gate_nem_ui(self):
        """modo simular continua acessível sem transição confirmada."""
        cli = _cliente_fake()
        simulacao = {"valido": True, "lote_hash": "abc", "itens": []}
        with patch.object(
            caixas_tarefas, "simular_lote", return_value=simulacao
        ), patch("mapa_pje.TaskFlowPanelPage") as PanelMock:
            res = asyncio.run(
                cli.preparar_movimentacao_lote(
                    snapshot_id="s1",
                    processos_ids=["123"],
                    destino_id="5",
                    nome_tarefa="Triagem",
                    confirmar_preparacao=False,
                )
            )
            self.assertEqual(res["status"], "simulado")
            PanelMock.assert_not_called()


class ServerAtuacoesToolTests(unittest.IsolatedAsyncioTestCase):
    @patch("server.cliente_singleton")
    @patch("caixas_tarefas.simular_lote")
    async def test_mapear_transicoes_server(self, mock_simular, mock_singleton):
        mock_pje = AsyncMock()
        mock_pje.mapear_transicoes_caixa.return_value = {
            "tarefa": "Triagem",
            "total_transicoes_descobertas": 2,
            "transicoes": []
        }
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)

        res = await server.atuar_fluxo_tarefas_pje(
            acao="mapear_transicoes_tarefa",
            nome_tarefa="Triagem",
            persona="servidor",
            perfil="Secretaria"
        )
        self.assertEqual(res["tarefa"], "Triagem")
        mock_pje.mapear_transicoes_caixa.assert_called_once_with("Triagem")

    @patch("server.cliente_singleton")
    @patch("caixas_tarefas.simular_lote")
    async def test_preparar_movimentacao_simular(self, mock_simular, mock_singleton):
        mock_pje = AsyncMock()
        mock_pje.preparar_movimentacao_lote.return_value = {
            "status": "simulado",
            "simulacao": {"valido": True, "lote_hash": "abc"}
        }
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)

        res = await server.atuar_fluxo_tarefas_pje(
            acao="preparar_movimentacao_lote",
            nome_tarefa="Triagem",
            processos_ids="123,456",
            destino_id="10",
            modo="simular",
            persona="servidor",
            perfil="Secretaria"
        )
        self.assertEqual(res["status"], "simulado")

    @patch("server.cliente_singleton")
    async def test_preparar_exige_dupla_intencao(self, mock_singleton):
        """confirmar_preparacao=True com modo='simular' é rejeitado sem tocar o cliente."""
        mock_pje = AsyncMock()
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)
        res = await server.atuar_fluxo_tarefas_pje(
            acao="preparar_movimentacao_lote",
            nome_tarefa="Triagem",
            processos_ids="123",
            destino_id="10",
            modo="simular",
            confirmar_preparacao=True,
            persona="servidor",
            perfil="Secretaria",
        )
        self.assertIn("erro", res)
        mock_pje.preparar_movimentacao_lote.assert_not_called()

    @patch("server.cliente_singleton")
    async def test_preparar_na_ui_exige_destino(self, mock_singleton):
        mock_pje = AsyncMock()
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)
        res = await server.atuar_fluxo_tarefas_pje(
            acao="preparar_movimentacao_lote",
            nome_tarefa="Triagem",
            processos_ids="123",
            modo="preparar",
            confirmar_preparacao=True,
            persona="servidor",
            perfil="Secretaria",
        )
        self.assertIn("erro", res)
        mock_pje.preparar_movimentacao_lote.assert_not_called()

    @patch("server.cliente_singleton")
    async def test_confirmar_reversibilidade_exige_token_e_evidencia(
        self, mock_singleton
    ):
        mock_pje = AsyncMock()
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)

        res = await server.atuar_fluxo_tarefas_pje(
            acao="confirmar_reversibilidade_transicao",
            nome_tarefa="Triagem",
            destino_id="5",
            persona="servidor",
            perfil="Secretaria",
        )
        self.assertIn("erro", res)
        self.assertFalse(res["promovido"])

        res = await server.atuar_fluxo_tarefas_pje(
            acao="confirmar_reversibilidade_transicao",
            nome_tarefa="Triagem",
            destino_id="5",
            confirmation_token="CONFIRMO_REVERSIVEL_ANTES_COMMIT",
            persona="servidor",
            perfil="Secretaria",
        )
        self.assertIn("erro", res)
        self.assertFalse(res["promovido"])
        mock_pje.confirmar_reversibilidade_transicao.assert_not_called()

        mock_pje.confirmar_reversibilidade_transicao.return_value = {
            "reversivel": "REVERSIVEL_ANTES_COMMIT",
            "fonte": "confirmacao_humana",
        }
        res = await server.atuar_fluxo_tarefas_pje(
            acao="confirmar_reversibilidade_transicao",
            nome_tarefa="Triagem",
            destino_id="5",
            confirmation_token="CONFIRMO_REVERSIVEL_ANTES_COMMIT",
            evidencia="Destino selecionado em item de teste; nada moveu sem o botão.",
            persona="servidor",
            perfil="Secretaria",
        )
        self.assertTrue(res["promovido"])

    @patch("server.cliente_singleton")
    @patch("caixas_tarefas.obter_snapshot")
    @patch("caixas_tarefas.simular_lote")
    @patch("minutas.salvar_peca")
    async def test_elaborar_minutas_lote(self, mock_salvar, mock_simular, mock_obter_snap, mock_singleton):
        mock_obter_snap.return_value = {"snapshot_id": "snap-123"}
        mock_simular.return_value = {
            "valido": True,
            "itens": [
                {"numero_processo": "0000001-01.2026.8.14.0000", "status": "INCLUIDO"}
            ]
        }
        mock_salvar.return_value = {"numero_cnj": "0000001-01.2026.8.14.0000", "status": "salvo"}
        mock_pje = AsyncMock()
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)

        res = await server.atuar_fluxo_tarefas_pje(
            acao="elaborar_minutas_lote",
            processos_ids="0000001-01.2026.8.14.0000",
            minuta_texto="Texto da Decisão",
            persona="servidor",
            perfil="Secretaria"
        )
        self.assertEqual(res["status"], "concluido")
        self.assertEqual(res["total_minutados"], 1)


from unittest.mock import MagicMock

from evidencia import tirar_evidencia_efemera


class EvidenciaEfemeraTests(unittest.IsolatedAsyncioTestCase):
    async def test_tirar_evidencia_efemera_sucesso(self):
        mock_page = AsyncMock()
        
        async def fake_screenshot(*args, **kwargs):
            path = kwargs.get("path")
            with open(path, "wb") as f:
                f.write(b"fake png data")
        
        mock_page.screenshot = AsyncMock(side_effect=fake_screenshot)
        mock_page.is_closed = MagicMock(return_value=False)
        
        res = await tirar_evidencia_efemera(mock_page)
        
        self.assertIsNotNone(res)
        self.assertEqual(res["mime"], "image/png")
        self.assertEqual(res["bytes"], 13)
        self.assertEqual(res["b64"], base64.b64encode(b"fake png data").decode("ascii"))
        
    async def test_tirar_evidencia_efemera_erro_page_fechada(self):
        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=True)
        res = await tirar_evidencia_efemera(mock_page)
        self.assertIsNone(res)
        
    async def test_tirar_evidencia_efemera_excecao_garante_remocao(self):
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.screenshot.side_effect = RuntimeError("Falha no browser")
        
        res = await tirar_evidencia_efemera(mock_page)
        self.assertIsNone(res)

    @patch("server.cliente_singleton")
    @patch("server.pje_downloader")
    async def test_tool_injects_screenshot_when_requested(self, mock_downloader, mock_singleton):
        mock_pje = AsyncMock()
        mock_pje._page = AsyncMock()
        mock_pje._page.is_closed = MagicMock(return_value=False)
        
        async def fake_screenshot(*args, **kwargs):
            path = kwargs.get("path")
            with open(path, "wb") as f:
                f.write(b"screenshot data")
        mock_pje._page.screenshot = AsyncMock(side_effect=fake_screenshot)
        
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)
        mock_singleton._cliente = mock_pje
        
        res = await server.painel_e_prazos_pje(
            acao="schema_acervo_tarefas",
            persona="servidor",
            grau="1",
            capturar_screenshot=True
        )
        
        self.assertIsNotNone(res)
        self.assertIn("evidencia_screenshot", res)
        self.assertEqual(res["evidencia_screenshot"]["mime"], "image/png")
        self.assertEqual(res["evidencia_screenshot"]["b64"], base64.b64encode(b"screenshot data").decode("ascii"))


class TestConsultarDestinoTarefa(_BasePolitica, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.draft1_path = str(SRC.parent / "policies" / "familia_maraba_draft_1.json")

    @patch("server.cliente_singleton")
    async def test_consultar_destino_com_playbook(self, mock_singleton):
        mock_pje = AsyncMock()
        mock_pje.contexto_fixado = {"perfil_id": "perfil_123"}
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)

        with patch.dict(os.environ, {"PJE_TASK_POLICY_PATH": self.draft1_path}):
            with patch(
                "caixas_tarefas.obter_transicoes_para_perfil",
                return_value=[
                    {
                        "perfil_id": "perfil_123",
                        "tarefa": "Verificar providência a adotar",
                        "destino_id": "dest_outras",
                        "destino_nome": "Outras caixas",
                        "reversivel": "INDETERMINADA",
                        "fonte": "mapeamento_ui",
                        "evidencia": None,
                        "atualizado_em": "2026-07-31T18:00:00Z",
                    }
                ],
            ) as mock_obter:
                res = await server.atuar_fluxo_tarefas_pje(
                    acao="consultar_destino_tarefa",
                    nome_tarefa="Verificar providência a adotar",
                    perfil="perfil_123",
                )

        self.assertEqual(res.get("destino_recomendado"), "Outras caixas")
        self.assertEqual(res.get("tarefa_canonica"), "Verificar providência a adotar")
        self.assertTrue(res.get("commit_humano_obrigatorio"))
        self.assertEqual(res.get("fundamento", {}).get("playbook"), "familia-maraba-draft-1")
        self.assertEqual(len(res.get("fundamento", {}).get("transicoes_mapeadas")), 1)
        mock_obter.assert_called_once_with(
            rotulo="perfil_123",
            persona="servidor",
            grau="1g",
            tarefa="Verificar providência a adotar",
        )

    @patch("server.cliente_singleton")
    async def test_consultar_destino_alias_plural(self, mock_singleton):
        mock_pje = AsyncMock()
        mock_pje.contexto_fixado = {"perfil_id": "perfil_123"}
        mock_singleton.get_cliente = AsyncMock(return_value=mock_pje)

        with patch.dict(os.environ, {"PJE_TASK_POLICY_PATH": self.draft1_path}):
            with patch("caixas_tarefas.obter_transicoes_para_perfil", return_value=[]):
                res = await server.atuar_fluxo_tarefas_pje(
                    acao="qual_destino",
                    nome_tarefa="Verificar providências a adotar",
                    perfil="perfil_123",
                )

        self.assertEqual(res.get("destino_recomendado"), "Outras caixas")
        self.assertEqual(res.get("tarefa_canonica"), "Verificar providência a adotar")
        self.assertTrue(res.get("commit_humano_obrigatorio"))

    @patch("server.cliente_singleton")
    async def test_consultar_destino_sem_nome_tarefa(self, mock_singleton):
        res = await server.atuar_fluxo_tarefas_pje(
            acao="consultar_destino_tarefa",
            nome_tarefa="",
        )
        self.assertIn("erro", res)


