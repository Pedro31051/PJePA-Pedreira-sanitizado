"""Regressões do vazamento de subprocessos/FDs do Playwright."""

import asyncio
import gc
import hashlib
import os
import sys
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import cliente_singleton
import pje_client


class _ChromiumQueFalha:
    async def connect_over_cdp(self, _url):
        raise RuntimeError("CDP indisponivel")


class _PlaywrightParcial:
    def __init__(self):
        self.chromium = _ChromiumQueFalha()
        self.stop = AsyncMock()


class _Starter:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


class ResourceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cliente_singleton._cliente = None
        cliente_singleton._chave_ativa = None
        cliente_singleton._ultimo_uso = 0

    async def asyncTearDown(self):
        await cliente_singleton.fechar_cliente()

    async def test_inicializacao_parcial_para_driver(self):
        parcial = _PlaywrightParcial()
        cliente = pje_client.PJeClient("1", "2", "JBSWY3DPEHPK3PXP")

        with (
            patch.dict(os.environ, {"PJE_CDP_URL": "http://127.0.0.1:9222"}),
            patch.object(
                pje_client,
                "async_playwright",
                return_value=_Starter(parcial),
            ),
            self.assertRaisesRegex(RuntimeError, "CDP indisponivel"),
        ):
            await cliente._iniciar()

        parcial.stop.assert_awaited_once()
        self.assertIsNone(cliente._pw)
        self.assertIsNone(cliente._browser)

    async def test_status_sessao_expoe_apenas_hash_do_usuario(self):
        usuario = "00000000000"
        cliente = MagicMock()
        cliente._fechar = AsyncMock()
        cliente._browser.is_connected.return_value = True
        cliente_singleton._cliente = cliente
        cliente_singleton._chave_ativa = (usuario, "advogado", "1g", "")

        with (
            patch.object(cliente_singleton, "_creds_systemd", return_value=None),
            patch.object(
                cliente_singleton.keyring,
                "get_password",
                return_value=None,
            ),
        ):
            status = cliente_singleton.status_sessao()

        chave = status["chave_ativa"]
        self.assertNotIn("usuario", chave)
        self.assertEqual(
            chave["usuario_hash"],
            hashlib.sha256(usuario.encode("utf-8")).hexdigest()[:16],
        )
        self.assertNotIn(usuario, str(status))

    async def test_falha_de_login_fecha_cliente_antes_de_publicar(self):
        falso = AsyncMock()
        falso._iniciar.return_value = None
        falso._login.side_effect = RuntimeError("login falhou")

        with (
            patch.object(cliente_singleton, "_get_creds", return_value=("1", "2", "3")),
            patch.object(cliente_singleton, "PJeClient", return_value=falso),
            self.assertRaisesRegex(RuntimeError, "login falhou"),
        ):
            await cliente_singleton.get_cliente(persona="advogado")

        falso._fechar.assert_awaited_once()
        self.assertIsNone(cliente_singleton._cliente)

    async def test_cancelamento_tambem_fecha_cliente_parcial(self):
        iniciou = asyncio.Event()
        continuar = asyncio.Event()
        falso = AsyncMock()

        async def login_lento():
            iniciou.set()
            await continuar.wait()

        falso._login.side_effect = login_lento

        with (
            patch.object(cliente_singleton, "_get_creds", return_value=("1", "2", "3")),
            patch.object(cliente_singleton, "PJeClient", return_value=falso),
        ):
            tarefa = asyncio.create_task(
                cliente_singleton.get_cliente(persona="advogado")
            )
            await iniciou.wait()
            tarefa.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await tarefa

        falso._fechar.assert_awaited_once()
        self.assertIsNone(cliente_singleton._cliente)

    async def test_url_documento_tjpa_nao_inventa_processo_ou_grau(self):
        cliente = pje_client.PJeClient("1", "2", "JBSWY3DPEHPK3PXP")
        self.assertEqual(
            cliente._url_documento_rest("165465042"),
            (
                "https://pje.tjpa.jus.br/pje/seam/resource/rest/"
                "pje-legacy/documento/download/165465042"
            ),
        )

    async def test_url_de_log_remove_credencial_e_query(self):
        limpa = pje_client._url_sem_segredos(
            "https://usuario:senha@exemplo.test/autos?id=1&ca=segredo"
        )
        self.assertEqual(
            limpa,
            "https://exemplo.test/autos?[query redacted]",
        )
        self.assertNotIn("segredo", limpa)
        self.assertNotIn("senha", limpa)

    async def test_erro_playwright_remove_call_log_cookies_e_jwt(self):
        jwt = (
            "eyJhbGciOiJSUzI1NiJ9."
            "eyJjcGYiOiIxMjM0NTY3ODkwMCIsIm5vbWUiOiJUZXN0ZSJ9."
            "assinaturaficticia"
        )
        exc = RuntimeError(
            "APIRequestContext.get: Request context disposed\n"
            "Call log:\n"
            "  - GET https://pje.test/doc?id=1&ca=segredo\n"
            "    cookie: JSESSIONID=sessao-secreta; "
            f"KEYCLOAK_IDENTITY={jwt}"
        )

        seguro = pje_client.erro_publico(exc)

        self.assertIn("Request context disposed", seguro)
        self.assertNotIn("sessao-secreta", seguro)
        self.assertNotIn("segredo", seguro)
        self.assertNotIn("KEYCLOAK", seguro)
        self.assertNotIn("12345678900", seguro)
        self.assertNotIn("Call log", seguro)

    async def test_metodo_serializado_nunca_propaga_credencial(self):
        cliente = pje_client.PJeClient("1", "2", "JBSWY3DPEHPK3PXP")

        @pje_client._serializa
        async def falhar(_self):
            raise RuntimeError(
                "APIRequestContext.get: Request context disposed\n"
                "Call log:\n"
                "cookie: JSESSIONID=sessao-secreta"
            )

        with self.assertRaises(RuntimeError) as capturada:
            await falhar(cliente)

        mensagem = str(capturada.exception)
        self.assertIn("Request context disposed", mensagem)
        self.assertNotIn("sessao-secreta", mensagem)
        self.assertNotIn("JSESSIONID", mensagem)


class _AbaFalsa:
    """Superfície mínima da Page do Playwright usada pelos handlers."""

    def __init__(self, rotulo=""):
        self.rotulo = rotulo
        self._listeners = {}
        self._once = {}
        self._closed = False

    def on(self, evento, handler):
        self._listeners.setdefault(evento, []).append(handler)

    def once(self, evento, handler):
        self._once.setdefault(evento, []).append(handler)

    def remove_listener(self, evento, handler):
        if handler in self._listeners.get(evento, []):
            self._listeners[evento].remove(handler)

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True
        for handler in self._once.pop("close", []):
            handler(self)


class _ContextoFalso:
    def __init__(self, paginas=()):
        self.pages = list(paginas)
        self._page_handlers = []

    def on(self, evento, handler):
        if evento == "page":
            self._page_handlers.append(handler)

    def remove_listener(self, evento, handler):
        if handler in self._page_handlers:
            self._page_handlers.remove(handler)

    def abrir(self, aba):
        """Reproduz o evento 'page' que o Playwright dispara por aba nova."""
        self.pages.append(aba)
        for handler in self._page_handlers:
            handler(aba)


def _cliente_com_contexto(contexto, principal):
    cliente = pje_client.PJeClient("1", "2", "JBSWY3DPEHPK3PXP")
    cliente._context = contexto
    cliente._page = principal
    cliente._page_event_handler = cliente._setup_dialog_handler
    contexto.on("page", cliente._page_event_handler)
    cliente._setup_dialog_handler(principal)
    return cliente


class DialogHandlerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_abas_fechadas_nao_acumulam_handlers(self):
        principal = _AbaFalsa("principal")
        contexto = _ContextoFalso()
        cliente = _cliente_com_contexto(contexto, principal)

        for i in range(50):
            aba = _AbaFalsa(f"autos-{i}")
            contexto.abrir(aba)
            await cliente._fechar_aba_autos(aba)
            contexto.pages.remove(aba)

        # Só a aba principal continua registrada; sem a poda a lista teria 51.
        self.assertEqual(len(cliente._dialog_handlers), 1)
        self.assertIs(cliente._dialog_handlers[0][0], principal)

    async def test_aba_fechada_solta_a_referencia_da_page(self):
        principal = _AbaFalsa("principal")
        contexto = _ContextoFalso()
        cliente = _cliente_com_contexto(contexto, principal)

        aba = _AbaFalsa("autos")
        contexto.abrir(aba)
        referencia = weakref.ref(aba)
        await cliente._fechar_aba_autos(aba)
        contexto.pages.remove(aba)
        del aba
        gc.collect()

        self.assertIsNone(referencia(), "Page fechada continuou retida pelo cliente")

    async def test_modo_cdp_nao_fecha_abas_do_usuario(self):
        do_usuario = _AbaFalsa("gmail-do-usuario")
        contexto = _ContextoFalso([do_usuario])
        principal = _AbaFalsa("automacao")
        cliente = _cliente_com_contexto(contexto, principal)
        # Em CDP o contexto é do usuário: o cliente não o possui.
        cliente._owns_context = False
        cliente._abas_preexistentes = [do_usuario]

        autos = _AbaFalsa("autos")
        contexto.abrir(autos)
        await cliente._fechar_aba_autos(autos)

        self.assertTrue(autos.is_closed(), "aba dos autos deveria ter sido fechada")
        self.assertFalse(
            do_usuario.is_closed(),
            "a automação fechou uma aba preexistente do usuário",
        )

    async def test_contexto_proprio_ainda_limpa_abas_residuais(self):
        residual = _AbaFalsa("popup-residual")
        contexto = _ContextoFalso([residual])
        principal = _AbaFalsa("automacao")
        cliente = _cliente_com_contexto(contexto, principal)
        # Em launch o contexto é nosso: nada ali pertence ao usuário.
        cliente._owns_context = True
        cliente._abas_preexistentes = []

        await cliente._fechar_aba_autos(None)

        self.assertTrue(residual.is_closed())


class _PaginaNavegavel:
    """Page que devolve uma sequência roteirizada de URLs a cada goto."""

    def __init__(self, urls):
        self._urls = list(urls)
        self.url = ""
        self.gotos = 0

    async def goto(self, _url, **_kwargs):
        self.url = self._urls[min(self.gotos, len(self._urls) - 1)]
        self.gotos += 1


class ReautenticacaoNavegacaoTests(unittest.IsolatedAsyncioTestCase):
    def _cliente(self, urls):
        cliente = pje_client.PJeClient("1", "2", "JBSWY3DPEHPK3PXP")
        cliente._browser_mode = "launch"
        cliente._page = _PaginaNavegavel(urls)
        return cliente

    def test_detecta_tela_de_autenticacao(self):
        expirada = [
            "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid",
            "https://pje.tjpa.jus.br/pje/login.seam",
        ]
        viva = "https://pje.tjpa.jus.br/pje/Processo/ConsultaProcesso/listView.seam"
        for url in expirada:
            self.assertTrue(pje_client.PJeClient._sessao_expirou(url), url)
        self.assertFalse(pje_client.PJeClient._sessao_expirou(viva))
        self.assertFalse(pje_client.PJeClient._sessao_expirou(None))

    async def test_redirect_para_sso_dispara_um_relogin_e_retoma(self):
        cliente = self._cliente(
            [
                "https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid",
                "https://pje.tjpa.jus.br/pje/Processo/ConsultaProcesso/listView.seam",
            ]
        )
        chamadas = []

        async def reautenticar():
            chamadas.append("relogin")

        cliente._reautenticar = reautenticar
        # Após o retry a sessão está viva; interrompemos no ponto seguinte do
        # fluxo, que exige DOM real e não faz parte deste contrato.
        with self.assertRaises(Exception):
            await cliente._abrir_autos_processo("0801447612024814_0037")

        self.assertEqual(chamadas, ["relogin"], "deveria reautenticar exatamente 1x")
        self.assertEqual(cliente._page.gotos, 2, "deveria repetir a navegação 1x")

    async def test_sessao_ainda_expirada_apos_relogin_falha_sem_lacar(self):
        cliente = self._cliente(
            ["https://sso.cloud.pje.jus.br/auth/realms/pje/protocol/openid"]
        )
        chamadas = []

        async def reautenticar():
            chamadas.append("relogin")

        cliente._reautenticar = reautenticar

        with self.assertRaisesRegex(RuntimeError, "SESSAO_EXPIRADA"):
            await cliente._abrir_autos_processo("0801447612024814_0037")

        self.assertEqual(chamadas, ["relogin"], "não pode virar laço de login")
        self.assertEqual(cliente._page.gotos, 2)

    async def test_modo_cdp_nao_tenta_relogin_automatico(self):
        cliente = self._cliente(["https://sso.cloud.pje.jus.br/auth/realms/pje"])
        cliente._browser_mode = "cdp"
        logou = []
        cliente._login = lambda: logou.append(1)

        with self.assertRaisesRegex(RuntimeError, "SESSAO_EXPIRADA"):
            await cliente._abrir_autos_processo("0801447612024814_0037")

        self.assertEqual(logou, [], "em CDP a sessão é do usuário; não relogar")

    async def test_carencia_impede_martelar_o_sso_apos_falha(self):
        cliente = self._cliente(["x"])
        tentativas = []

        async def login_que_falha():
            tentativas.append(1)
            raise RuntimeError("credencial inválida")

        cliente._login = login_que_falha

        with self.assertRaisesRegex(RuntimeError, "credencial inválida"):
            await cliente._reautenticar()
        self.assertIsNotNone(cliente._relogin_falhou_em)

        # Segunda tentativa imediata: barrada pela carência, sem tocar no SSO.
        with self.assertRaisesRegex(RuntimeError, "SESSAO_EXPIRADA"):
            await cliente._reautenticar()

        self.assertEqual(len(tentativas), 1, "não pode repetir login na carência")

    async def test_relogin_bem_sucedido_limpa_a_carencia(self):
        cliente = self._cliente(["x"])
        cliente._relogin_falhou_em = None
        cliente._login = AsyncMock()
        cliente._trocar_perfil = AsyncMock()

        await cliente._reautenticar()

        self.assertIsNone(cliente._relogin_falhou_em)
        cliente._login.assert_awaited_once()
        cliente._trocar_perfil.assert_awaited_once()

    async def test_perfil_divergente_apos_relogin_interrompe(self):
        cliente = self._cliente(["x"])
        cliente.perfil = {
            "rotulo": "Vara de Família de Marabá",
            "pje_id": "perfil-familia",
        }
        cliente._login = AsyncMock()
        cliente._trocar_perfil = AsyncMock()
        cliente._selecionar_lotacao = AsyncMock(
            return_value={"rotulo": "Vara Cível de Belém", "pje_id": "perfil-civel"}
        )

        with self.assertRaisesRegex(RuntimeError, "PERFIL_DIVERGENTE"):
            await cliente._reautenticar()

        # Falha de perfil também entra na carência: repetir não corrigiria.
        self.assertIsNotNone(cliente._relogin_falhou_em)


class _BrowserSessaoFalso:
    def __init__(self):
        self.conectado = True

    def is_connected(self):
        return self.conectado


class _ContextoStorageFalso:
    def __init__(self, estado, eventos, nome):
        async def capturar():
            eventos.append(f"capturar:{nome}")
            return estado

        self.storage_state = AsyncMock(side_effect=capturar)


class _ClienteSessaoFalso:
    def __init__(self, nome, estado, eventos, rotulo_selecionado="Perfil Alvo"):
        self.nome = nome
        self._browser = _BrowserSessaoFalso()
        self._browser_mode = "launch"
        self._context = _ContextoStorageFalso(estado, eventos, nome)
        self._op_lock = asyncio.Lock()
        self._storage_state_inicial = None

        async def iniciar():
            eventos.append(f"iniciar:{nome}")

        async def fechar():
            eventos.append(f"fechar:{nome}")
            self._browser.conectado = False

        self._iniciar = AsyncMock(side_effect=iniciar)
        self._login = AsyncMock()
        self._trocar_perfil = AsyncMock()
        self._selecionar_lotacao = AsyncMock(return_value=rotulo_selecionado)
        self._fechar = AsyncMock(side_effect=fechar)


class SessionStateHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cliente_singleton._cliente = None
        cliente_singleton._chave_ativa = None
        cliente_singleton._ultimo_uso = 0
        cliente_singleton._ultima_metrica_sessao = {}
        cliente_singleton._limpar_cache_estados()

    async def asyncTearDown(self):
        await cliente_singleton.fechar_cliente()

    async def test_descoberta_para_perfil_interno_nao_transfere_estado(self):
        eventos = []
        estado = {
            "cookies": [
                {
                    "name": "JSESSIONID",
                    "value": "token-sintetico",
                    "domain": "pje.test",
                    "path": "/",
                }
            ],
            "origins": [],
        }
        primeiro = _ClienteSessaoFalso("descoberta", estado, eventos)
        segundo = _ClienteSessaoFalso("perfil", {"cookies": [], "origins": []}, eventos)
        criados = iter((primeiro, segundo))
        estados_recebidos = []

        def construir(*_args, **kwargs):
            recebido = kwargs.get("storage_state")
            estados_recebidos.append(
                recebido["cookies"][0]["value"] if recebido else None
            )
            cliente = next(criados)
            cliente._storage_state_inicial = recebido
            return cliente

        perfil = {
            "perfil_id": "id-opaco",
            "pje_id": "perfil-real",
            "rotulo": "Perfil Alvo",
        }
        with (
            patch.object(
                cliente_singleton,
                "_get_creds",
                return_value=("1", "2", "3"),
            ),
            patch.object(cliente_singleton, "PJeClient", side_effect=construir),
            patch.object(
                cliente_singleton.perfil_contexto,
                "contexto_atual",
                side_effect=(None, perfil),
            ),
        ):
            descoberto = await cliente_singleton.get_cliente(
                persona="servidor",
                permitir_sem_perfil=True,
            )
            selecionado = await cliente_singleton.get_cliente(persona="servidor")

        self.assertIs(descoberto, primeiro)
        self.assertIs(selecionado, segundo)
        self.assertEqual(estados_recebidos, [None, None])
        primeiro._selecionar_lotacao.assert_not_awaited()
        primeiro._fechar.assert_awaited_once()
        segundo._selecionar_lotacao.assert_awaited_once_with(perfil)
        self.assertNotIn("capturar:descoberta", eventos)
        self.assertLess(eventos.index("fechar:descoberta"), eventos.index("iniciar:perfil"))
        self.assertEqual(
            cliente_singleton._ultima_metrica_sessao["modo"],
            "sessao_nova",
        )
        self.assertFalse(cliente_singleton._ultima_metrica_sessao["cache_hit"])
        self.assertEqual(cliente_singleton._cache_estados_sessao, {})

    async def test_cdp_nunca_captura_ou_restaura_storage_state(self):
        eventos = []
        primeiro = _ClienteSessaoFalso(
            "cdp",
            {"cookies": [{"name": "x", "value": "y"}], "origins": []},
            eventos,
        )
        primeiro._browser_mode = "cdp"
        primeiro._chave = ("servidor", "1g", "descoberta")
        cliente_singleton._cliente = primeiro
        cliente_singleton._chave_ativa = primeiro._chave
        cliente_singleton._ultimo_uso = time.time()
        segundo = _ClienteSessaoFalso("novo-cdp", {"cookies": [], "origins": []}, eventos)
        recebido = []

        def construir(*_args, **kwargs):
            recebido.append(kwargs.get("storage_state"))
            return segundo

        perfil = {"perfil_id": "id-opaco", "rotulo": "Perfil Alvo"}
        with (
            patch.dict(os.environ, {"PJE_CDP_URL": "http://127.0.0.1:9222"}),
            patch.object(cliente_singleton, "_get_creds", return_value=("1", "2", "3")),
            patch.object(cliente_singleton, "PJeClient", side_effect=construir),
            patch.object(cliente_singleton.perfil_contexto, "contexto_atual", return_value=perfil),
        ):
            await cliente_singleton.get_cliente(persona="servidor")

        primeiro._context.storage_state.assert_not_awaited()
        self.assertEqual(recebido, [None])
        self.assertFalse(cliente_singleton._ultima_metrica_sessao["cache_hit"])

    def test_snapshot_expira_e_e_descartado(self):
        estado = {
            "cookies": [{"name": "JSESSIONID", "value": "segredo-sintetico"}],
            "origins": [],
        }
        chave = ("servidor", "1g", "descoberta")
        self.assertTrue(
            cliente_singleton._guardar_estado_sessao(
                chave,
                estado,
                validade_s=10,
                agora_monotonic=100,
            )
        )

        restaurado = cliente_singleton._retirar_estado_sessao(
            chave,
            agora_monotonic=111,
        )

        self.assertIsNone(restaurado)
        self.assertEqual(cliente_singleton._cache_estados_sessao, {})
        # A entrada original não é apropriada/mutada pelo cache.
        self.assertEqual(estado["cookies"][0]["value"], "segredo-sintetico")

    def test_metrica_tem_schema_fixo_e_rejeita_campo_livre(self):
        with patch.object(cliente_singleton, "_log"):
            cliente_singleton._registrar_metrica(
                "modo-controlado-pelo-usuario",
                cache_hit=True,
                login_ms=float("nan"),
                total_ms=-1,
            )

        metrica = cliente_singleton._ultima_metrica_sessao
        self.assertEqual(metrica["modo"], "invalido")
        self.assertEqual(metrica["schema_version"], "pje.session-performance/v1")
        self.assertEqual(metrica["login_ms"], 0.0)
        self.assertEqual(metrica["total_ms"], 0.0)
        self.assertNotIn("modo-controlado-pelo-usuario", repr(metrica))
        self.assertTrue(metrica["cache_hit"])
        with self.assertRaises(TypeError):
            cliente_singleton._registrar_metrica(
                "sessao_nova",
                perfil="dado-proibido",
            )


class CredenciaisFallbackTests(unittest.TestCase):
    def test_keyring_tenta_service_seguinte_apos_erro_de_backend(self):
        chamadas = []

        def get_password(service, chave):
            chamadas.append(service)
            if service == cliente_singleton.KEYRING_SERVICE:
                raise RuntimeError("NoKeyringError")
            return {"cpf": "1", "senha": "2", "totp_seed": "3"}[chave]

        with patch.object(cliente_singleton.keyring, "get_password", get_password):
            creds = cliente_singleton._creds_keyring()

        self.assertEqual(creds, ("1", "2", "3"))
        self.assertIn(cliente_singleton.KEYRING_SERVICES_FALLBACK[0], chamadas)


if __name__ == "__main__":
    unittest.main()
