"""Suíte de testes de segurança E2E (fail-closed, isolamento de sessão e integridade)."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import cliente_singleton
import perfil_contexto
import pje_client
import pje_downloader
import server


class SecurityE2ETests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cliente_singleton._cache_estados_sessao.clear()
        cliente_singleton._cliente = None
        cliente_singleton._chave_ativa = None
        cliente_singleton._sso_falhas_consecutivas = 0
        cliente_singleton._sso_bloqueado_ate = 0.0

    async def test_nao_existe_bloqueio_global_de_consultas_internas(self):
        _, _, bloqueio = server._ativar_perfil_ferramenta(
            persona="servidor",
            grau="1g",
            perfil="Vara de Família / Secretaria / Diretor",
        )
        self.assertIn(
            (bloqueio or {}).get("codigo"),
            {None, "PERFIL_SEM_IDENTIFICADOR_ESTAVEL"},
        )

    async def test_diagnostico_local_permanece_disponivel(self):
        res_schema = await server.painel_e_prazos_pje(
            acao="schema_acervo_tarefas",
            persona="servidor",
            perfil="Vara de Família / Secretaria / Diretor"
        )
        self.assertEqual(res_schema.get("schema_version"), "pje.acervo-tarefas/v2")

    async def test_fail_closed_em_contexto_divergente(self):
        """Valida que o cliente aborta com erro se o perfil do DOM divergir do esperado."""
        cliente = pje_client.PJeClient("123", "senha", "seed", persona="servidor")
        page = MagicMock()
        page.url = "https://pje.tjpa.jus.br/pje/home.seam"
        
        # Simula perfil do DOM divergente
        # Esperado: Vara de Família. DOM: Vara Cível.
        page.evaluate = AsyncMock(return_value="Vara Cível / Secretaria / Servidor")
        cliente._page = page
        cliente._listar_perfis_funcionais_dom = AsyncMock(
            return_value=[
                {
                    "pje_id": "perfil-civel",
                    "texto": "Vara Cível / Secretaria / Servidor",
                    "ativo": True,
                }
            ]
        )
        
        perfil_esperado = {
            "pje_id": "perfil-familia",
            "rotulo": "Vara de Família / Secretaria / Servidor",
            "unidade": "Vara de Família",
            "localizacao": "Secretaria",
            "papel": "Servidor"
        }
        
        with self.assertRaises(RuntimeError) as ctx:
            await cliente.selecionar_e_validar_contexto(perfil_esperado)
        self.assertIn("CONTEXTO_DIVERGENTE", str(ctx.exception))

    async def test_sessao_fixada_bloqueia_divergencia_antes_de_publicar(self):
        cliente = pje_client.PJeClient(
            "123", "senha", "JBSWY3DPEHPK3PXP", persona="servidor"
        )
        page = MagicMock()
        page.url = "https://pje.tjpa.jus.br/pje/home.seam"
        page.wait_for_load_state = AsyncMock()
        page.title = AsyncMock(return_value="PJe - Painel")
        elemento = MagicMock()
        elemento.count = AsyncMock(return_value=1)
        elemento.evaluate = AsyncMock(return_value="a")
        elemento.click = AsyncMock()
        elementos = MagicMock()
        elementos.nth.return_value = elemento
        menu = MagicMock()
        menu.count = AsyncMock(return_value=1)
        menu.click = AsyncMock()
        menu_locator = MagicMock()
        menu_locator.first = menu
        page.locator.side_effect = (
            lambda seletor: menu_locator
            if seletor == "li.menu-usuario a.dropdown-toggle"
            else elementos
        )

        async def avaliar(script, *_args):
            if "const texto = (el)" in script:
                return {
                    "usuario": "Usuário Sintético",
                    "rotulo": "Vara Criminal / Secretaria / Diretor",
                }
            return None

        page.evaluate = AsyncMock(side_effect=avaliar)
        cliente._page = page
        cliente._listar_perfis_funcionais_dom = AsyncMock(
            return_value=[
                {
                    "index": 0,
                    "texto": "Vara de Família / Secretaria / Diretor",
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "CONTEXTO_DIVERGENTE"):
            await cliente.fixar_contexto_por_rotulo(
                "Vara de Família / Secretaria / Diretor"
            )
        self.assertIsNone(cliente.contexto_fixado)

    async def test_sessao_fixada_e_imutavel_e_identidade_nao_vem_do_rotulo(self):
        cliente = pje_client.PJeClient(
            "123", "senha", "JBSWY3DPEHPK3PXP", persona="servidor"
        )
        page = MagicMock()
        page.url = "https://pje.tjpa.jus.br/pje/home.seam"
        page.wait_for_load_state = AsyncMock()
        page.title = AsyncMock(return_value="PJe - Painel")
        elemento = MagicMock()
        elemento.count = AsyncMock(return_value=1)
        elemento.evaluate = AsyncMock(return_value="a")
        elemento.click = AsyncMock()
        elementos = MagicMock()
        elementos.nth.return_value = elemento
        menu = MagicMock()
        menu.count = AsyncMock(return_value=1)
        menu.click = AsyncMock()
        menu_locator = MagicMock()
        menu_locator.first = menu
        page.locator.side_effect = (
            lambda seletor: menu_locator
            if seletor == "li.menu-usuario a.dropdown-toggle"
            else elementos
        )

        async def avaliar(script, *_args):
            if "const texto = (el)" in script:
                return {
                    "usuario": "Usuário Sintético",
                    "rotulo": "Vara de Família / Secretaria / Diretor",
                }
            return None

        page.evaluate = AsyncMock(side_effect=avaliar)
        cliente._page = page
        cliente._listar_perfis_funcionais_dom = AsyncMock(
            return_value=[
                {
                    "index": 0,
                    "texto": "Vara de Família / Secretaria / Diretor",
                }
            ]
        )

        fixado = await cliente.fixar_contexto_por_rotulo(
            "Vara de Família / Secretaria / Diretor"
        )
        self.assertTrue(fixado["contexto_validado_id"])
        self.assertNotEqual(
            fixado["contexto_validado_id"],
            perfil_contexto.criar_contexto(
                "servidor",
                "1g",
                "Vara de Família / Secretaria / Diretor",
            )["perfil_id"],
        )
        with self.assertRaisesRegex(
            RuntimeError, "SESSAO_CONTEXTO_FIXADO_IMUTAVEL"
        ):
            await cliente._selecionar_lotacao({"pje_id": "qualquer"})

    async def test_distribuicao_exige_confirmacao_humana_especifica(self):
        cliente = pje_client.PJeClient(
            "123", "senha", "JBSWY3DPEHPK3PXP", persona="servidor"
        )
        page = MagicMock()
        page.url = "https://pje.tjpa.jus.br/pje/home.seam"
        page.wait_for_load_state = AsyncMock()
        page.title = AsyncMock(return_value="PJe - Painel")
        elemento = MagicMock()
        elemento.count = AsyncMock(return_value=1)
        elemento.evaluate = AsyncMock(return_value="a")
        elemento.click = AsyncMock()
        elementos = MagicMock()
        elementos.nth.return_value = elemento
        menu = MagicMock()
        menu.count = AsyncMock(return_value=1)
        menu.click = AsyncMock()
        menu_locator = MagicMock()
        menu_locator.first = menu
        page.locator.side_effect = (
            lambda seletor: menu_locator
            if seletor == "li.menu-usuario a.dropdown-toggle"
            else elementos
        )
        page.evaluate = AsyncMock(
            return_value={
                "usuario": "Usuário Sintético",
                "rotulo": "Marabá / Servidor Distribuição",
            }
        )
        cliente._page = page
        cliente._listar_perfis_funcionais_dom = AsyncMock(
            return_value=[
                {"index": 0, "texto": "Marabá / Servidor Distribuição"}
            ]
        )

        with self.assertRaisesRegex(
            RuntimeError, "CONFIRMACAO_LOCALIZACAO_OBRIGATORIA"
        ):
            await cliente.fixar_contexto_por_rotulo(
                "Marabá / Servidor Distribuição"
            )
        fixado = await cliente.fixar_contexto_por_rotulo(
            "Marabá / Servidor Distribuição",
            "CONFIRMO_NAO_APLICAVEL",
        )
        self.assertEqual(fixado["localizacao"], "")
        self.assertEqual(fixado["localizacao_status"], "NAO_APLICAVEL")

    async def test_sso_indeterminado_abre_circuit_breaker_sem_retry(self):
        falso = MagicMock()
        falso._op_lock = asyncio.Lock()
        falso._iniciar = AsyncMock()
        falso._login = AsyncMock(
            side_effect=RuntimeError("LOGIN_ESTADO_INDETERMINADO")
        )
        falso._fechar = AsyncMock()
        with (
            patch.object(
                cliente_singleton, "_get_creds", return_value=("1", "2", "3")
            ),
            patch.object(
                cliente_singleton, "PJeClient", return_value=falso
            ) as construtor,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "LOGIN_ESTADO_INDETERMINADO"
            ):
                await cliente_singleton.criar_sessao_contexto_fixado(
                    localizador_nao_confiavel=(
                        "Vara / Secretaria / Diretor"
                    )
                )
            with self.assertRaisesRegex(
                RuntimeError, "SSO_CIRCUIT_BREAKER_ATIVO"
            ):
                await cliente_singleton.criar_sessao_contexto_fixado(
                    localizador_nao_confiavel=(
                        "Vara / Secretaria / Diretor"
                    )
                )
        self.assertEqual(construtor.call_count, 1)

    async def test_gate_vinte_criacoes_isoladas_sem_cookie_handoff(self):
        criados = []
        localizadores = [
            f"Vara {indice} / Secretaria {indice} / Diretor"
            for indice in range(6)
        ]

        class ClienteFalso:
            def __init__(self, *_args, **kwargs):
                self.storage_state = kwargs.get("storage_state")
                self.isolamento_total = kwargs.get("isolamento_total")
                self._op_lock = asyncio.Lock()
                self.contexto_fixado = None
                self._fechar = AsyncMock()
                self._iniciar = AsyncMock()
                self._login = AsyncMock()
                criados.append(self)

            async def fixar_contexto_por_rotulo(
                self, rotulo, _confirmacao=""
            ):
                self.contexto_fixado = {
                    "contexto_validado_id": f"confirmado-{len(criados)}",
                    "localizador_nao_confiavel": rotulo,
                }
                return self.contexto_fixado

        advogado = MagicMock()
        advogado._op_lock = asyncio.Lock()
        advogado._fechar = AsyncMock()
        cliente_singleton._cliente = advogado
        cliente_singleton._chave_ativa = ("usuario", "advogado", "1g", "")
        with (
            patch.object(
                cliente_singleton, "_get_creds", return_value=("1", "2", "3")
            ),
            patch.object(cliente_singleton, "PJeClient", ClienteFalso),
        ):
            for indice in range(20):
                await cliente_singleton.criar_sessao_contexto_fixado(
                    localizador_nao_confiavel=localizadores[indice % 6],
                    persona="servidor",
                    grau="1g",
                )

        self.assertEqual(len(criados), 20)
        advogado._fechar.assert_awaited_once()
        self.assertTrue(all(item.storage_state is None for item in criados))
        self.assertTrue(all(item.isolamento_total for item in criados))
        self.assertTrue(all(item._fechar.await_count == 1 for item in criados[:-1]))

    async def test_isolamento_de_sessao_por_chave_composta(self):
        """Valida que a chave de sessão separa usuários e perfis distintos."""
        # Configura mocks para simular dois logins diferentes
        with (
            patch.object(cliente_singleton, "_get_creds") as mock_creds,
            patch.object(cliente_singleton, "PJeClient") as mock_client_cls
        ):
            # Primeiro login (CPF '111')
            mock_creds.return_value = ("111", "senha1", "seed1")
            client_mock_1 = MagicMock()
            client_mock_1._iniciar = AsyncMock()
            client_mock_1._login = AsyncMock()
            client_mock_1._selecionar_lotacao = AsyncMock()
            client_mock_1._fechar = AsyncMock()
            client_mock_1._op_lock = asyncio.Lock()
            mock_client_cls.return_value = client_mock_1
            
            perfil1 = {"perfil_id": "perfilA", "rotulo": "Vara A"}
            with patch.object(cliente_singleton.perfil_contexto, "contexto_atual", return_value=perfil1):
                c1 = await cliente_singleton.get_cliente(persona="servidor")
                
            # Segundo login (CPF '222')
            mock_creds.return_value = ("222", "senha2", "seed2")
            client_mock_2 = MagicMock()
            client_mock_2._iniciar = AsyncMock()
            client_mock_2._login = AsyncMock()
            client_mock_2._selecionar_lotacao = AsyncMock()
            client_mock_2._fechar = AsyncMock()
            client_mock_2._op_lock = asyncio.Lock()
            mock_client_cls.return_value = client_mock_2
            
            perfil2 = {"perfil_id": "perfilB", "rotulo": "Vara B"}
            with patch.object(cliente_singleton.perfil_contexto, "contexto_atual", return_value=perfil2):
                c2 = await cliente_singleton.get_cliente(persona="servidor")
                
            self.assertIsNot(c1, c2)
            self.assertEqual(cliente_singleton._chave_ativa[0], "222")

    def test_integridade_download_identifica_html_erro_e_redirecionamento(self):
        """Valida que a integridade detecta HTML corrompido, redirecionamentos ou erros de servidor."""
        # 1. HTML válido (tamanho >= 100 bytes)
        corpo_valido = b"<html><body><p>" + b"A" * 100 + b"</p></body></html>"
        integro, motivo = pje_downloader.conferir_conteudo(corpo_valido, "text/html")
        self.assertTrue(integro)
        self.assertIsNone(motivo)
        
        # 2. Página de redirecionamento / login
        corpo_login = b"<html><script>window.location.href = '/login.seam';</script></html>"
        integro, motivo = pje_downloader.conferir_conteudo(corpo_login, "text/html")
        self.assertFalse(integro)
        self.assertIn("login ou redirecionamento", motivo)
        
        # 3. Página de erro javax.faces
        corpo_erro = b"<html><body>ViewExpiredException: ocorreu um erro inesperado no PJe</body></html>"
        integro, motivo = pje_downloader.conferir_conteudo(corpo_erro, "text/html")
        self.assertFalse(integro)
        self.assertIn("erro do servidor", motivo)

    async def test_cache_pdf_corrompido_nao_e_reutilizado(self):
        """Arquivo existente sem EOF nunca pode virar cache hit."""
        with tempfile.TemporaryDirectory() as tmp:
            bases = {
                "1g": Path(tmp) / "1g",
                "2g": Path(tmp) / "2g",
            }
            cliente = MagicMock()
            cliente.grau = "1g"
            cliente.baixar_processo_nativo = AsyncMock(
                return_value={
                    "caminho": str(Path(tmp) / "novo.pdf"),
                    "tamanho_bytes": 2048,
                    "tamanho_mb": 0.01,
                }
            )
            numero = "0800001-61.2024.8.14.0028"
            with patch.object(pje_downloader, "_PASTAS", bases):
                destino = (
                    pje_downloader.pasta_processo(numero, "1g")
                    / f"{pje_downloader.cnj_safe(numero)}.pdf"
                )
                destino.write_bytes(b"%PDF-" + b"A" * 2048)
                resultado = await pje_downloader.baixar_processo_completo(
                    cliente,
                    numero,
                    metodo="nativo",
                )

            cliente.baixar_processo_nativo.assert_awaited_once()
            self.assertEqual(resultado["metodo"], "nativo")

    async def test_integridade_cache_falha_quando_nao_ha_arquivo(self):
        """Zero arquivos verificados não é evidência de integridade."""
        with tempfile.TemporaryDirectory() as tmp:
            numero = "0800001-61.2024.8.14.0028"
            inventario = {"processos": [{"numero_cnj": numero}]}
            bases = {
                "1g": Path(tmp) / "1g",
                "2g": Path(tmp) / "2g",
            }
            with (
                patch.object(pje_downloader, "_PASTAS", bases),
                patch.object(
                    pje_downloader,
                    "inventario_cache",
                    return_value=inventario,
                ),
            ):
                resultado = await server.verificar_integridade_cache(
                    numero_cnj=numero,
                    grau="1",
                )

            self.assertFalse(resultado["tudo_integro"])
            self.assertEqual(resultado["processos_com_problema"], [numero])
            problema = resultado["detalhe"][0]["com_problema"][0]["problema"]
            self.assertIn("nenhum arquivo", problema)

    async def test_integridade_cache_sem_processo_nao_e_sucesso_vazio(self):
        """Inventário vazio declara ausência de prova, não integridade."""
        with tempfile.TemporaryDirectory() as tmp:
            numero = "0800001-61.2024.8.14.0028"
            bases = {
                "1g": Path(tmp) / "1g",
                "2g": Path(tmp) / "2g",
            }
            with patch.object(pje_downloader, "_PASTAS", bases):
                resultado = await server.verificar_integridade_cache(
                    numero_cnj=numero,
                    grau="1",
                )

            self.assertFalse(resultado["tudo_integro"])
            self.assertTrue(resultado["nenhum_arquivo_verificado"])
            self.assertEqual(resultado["processos_com_problema"], [numero])

    def test_documento_html_nao_e_classificado_como_minuta(self):
        """HTML baixado em documentos é peça, não minuta/relatório gerado."""
        with tempfile.TemporaryDirectory() as tmp:
            numero = "0800001-61.2024.8.14.0028"
            bases = {
                "1g": Path(tmp) / "1g",
                "2g": Path(tmp) / "2g",
            }
            with patch.object(pje_downloader, "_PASTAS", bases):
                pasta = pje_downloader.pasta_processo(numero, "1g")
                documentos = pasta / "documentos"
                documentos.mkdir()
                (documentos / "123.html").write_text(
                    "<html><body>peça sintética</body></html>",
                    encoding="utf-8",
                )
                (pasta / "relatorio.md").write_text(
                    "relatório sintético",
                    encoding="utf-8",
                )
                resultado = pje_downloader.inventario_cache(
                    grau="1g",
                    numero_cnj=numero,
                )

            processo = resultado["processos"][0]
            self.assertEqual(processo["pecas_individuais"], 1)
            self.assertEqual(processo["minutas_e_relatorios"], 1)


if __name__ == "__main__":
    unittest.main()
