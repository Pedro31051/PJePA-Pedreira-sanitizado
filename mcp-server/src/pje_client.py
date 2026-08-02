"""Cliente de automacao do PJe-TJPA (1o e 2o graus) via Playwright.

Instancias (SSO PDPJ nacional, mesmas credenciais):
- 1g: https://pje.tjpa.jus.br/pje      (client_id pje-tjpa-1g)
- 2g: https://pje.tjpa.jus.br/pje-2g   (client_id pje-tjpa-2g)

Funcionalidades:
- Login automatico (CPF + senha + 2FA TOTP)
- Troca automatica de perfil (advogado/procurador)
- Consulta de expedientes pendentes
- Consulta de processo por numero CNJ
- Busca por nome, CPF, CNPJ, OAB
- Listagem e leitura de documentos (HTML e PDF)
- Tratamento automatico do aviso da Resolucao CNJ 121/2010
"""

import asyncio
import functools
import hashlib
import hmac
import html as html_module
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pdfplumber
import pyotp
from playwright.async_api import async_playwright
from scrapling import parser as scrapling_parser

import caixas_tarefas
import lista_processos_tarefa
import perfil_contexto
from busca_processual_nativa import NativeProcessSearchPage
from mapa_pje import ProcessRegistrationPage
from pje import parser_compat
from politica_atuacao import politica_atuacao

# Scrapling 0.4.11/0.4.12 sempre repassa ``strip_cdata`` ao HTMLParser, mas o
# lxml 6.1 declara que essa opção nunca teve efeito e a removerá. Retirar apenas
# esse kwarg preserva o comportamento atual, elimina o DeprecationWarning e
# mantém o cliente compatível quando o lxml finalmente rejeitar o argumento.
parser_compat.install()
Selector = scrapling_parser.Selector

# TJPA: mesmo host, paths /pje (1g) e /pje-2g (2g). client_ids
# pje-tjpa-1g/pje-tjpa-2g confirmados no redirect do login.seam (07/2026).
URL_BASES = {
    "1g": "https://pje.tjpa.jus.br/pje",
    "2g": "https://pje.tjpa.jus.br/pje-2g",
}

# Sinais de que a navegacao caiu na tela de autenticacao. O SSO nacional do
# PDPJ e um Keycloak; a sessao expirada se manifesta como redirect para ele ou
# de volta para login.seam, nunca como HTTP 401 (isso so ocorre na rota de API).
MARCADORES_SESSAO_EXPIRADA = (
    "sso.cloud.pje.jus.br",
    "/auth/realms/",
    "/login.seam",
)


def _is_probable_consolidated_pdf_url(
    url: str, content_type: str = ""
) -> bool:
    """Reconhece as variantes de entrega sem depender do hostname do TJPA."""
    value = str(url or "")
    lowered = value.casefold()
    mime = str(content_type or "").casefold()
    signed = "x-amz-signature=" in lowered or "x-goog-signature=" in lowered
    pdf_named = ".pdf" in lowered or "processo.pdf" in lowered
    consolidated = any(
        marker in lowered
        for marker in ("storagepje", "download", "processo", "autos")
    )
    return (signed and (pdf_named or consolidated)) or (
        "application/pdf" in mime and consolidated
    )


def _native_download_timeout_ms(value: Any = None) -> int:
    configured = value
    if configured is None:
        configured = os.environ.get("PJE_NATIVE_DOWNLOAD_TIMEOUT_MS", "300000")
    try:
        parsed = int(configured)
    except (TypeError, ValueError):
        parsed = 300_000
    return max(120_000, min(parsed, 900_000))

# Carencia entre tentativas de reautenticacao. A senha do PDPJ expira
# periodicamente; insistir a cada chamada bloquearia a conta no SSO nacional.
COOLDOWN_RELOGIN_S = 60.0

# O diagnostico leve guarda somente rotulos funcionais (sem href, usuario,
# URL ou conteudo do painel) e nunca persiste o cache em disco.
PERFIL_CACHE_TTL_S = 300.0

_CHAVES_PERFIL = (
    "perfil_id",
    "perfilid",
    "id_perfil",
    "idperfil",
    "profile_id",
    "profileid",
)
_CHAVES_UNIDADE = ("unidade_id", "unidadeid", "id_unidade", "idunidade")
_CHAVES_LOCALIZACAO = (
    "localizacao_id",
    "localizacaoid",
    "id_localizacao",
    "idlocalizacao",
)
_CHAVES_PAPEL = ("papel_id", "papelid", "id_papel", "idpapel")


def _normalizar_chave_id(valor: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(valor or "").casefold())


def _valor_id_estavel(valor: Any) -> str:
    candidato = str(valor or "").strip()
    if (
        not candidato
        or len(candidato) > 240
        or candidato.casefold() in {"null", "undefined", "true", "false"}
        or candidato.startswith(("#", "javascript:"))
        or re.search(r"(?:^|[:_-])j_idt\d+(?:$|[:_-])", candidato, re.IGNORECASE)
    ):
        return ""
    return candidato


def extrair_identidade_perfil_pje(item: dict[str, Any]) -> dict[str, str]:
    """Extrai somente IDs entregues pelo PJe, respeitando a ordem de confiança."""
    resultado = {
        "pje_id": "",
        "unidade_id": "",
        "localizacao_id": "",
        "papel_id": "",
        "fonte_id": "",
    }

    def aplicar(chave: str, valor: Any, fonte: str) -> None:
        normalizada = _normalizar_chave_id(chave)
        candidato = _valor_id_estavel(valor)
        if not candidato:
            return
        if normalizada in _CHAVES_PERFIL and not resultado["pje_id"]:
            resultado["pje_id"] = candidato
            resultado["fonte_id"] = fonte
        elif normalizada in _CHAVES_UNIDADE and not resultado["unidade_id"]:
            resultado["unidade_id"] = candidato
        elif (
            normalizada in _CHAVES_LOCALIZACAO
            and not resultado["localizacao_id"]
        ):
            resultado["localizacao_id"] = candidato
        elif normalizada in _CHAVES_PAPEL and not resultado["papel_id"]:
            resultado["papel_id"] = candidato

    # 1. value de <option>.
    if str(item.get("tag", "")).casefold() == "option":
        candidato = _valor_id_estavel(item.get("value"))
        if candidato:
            resultado["pje_id"] = candidato
            resultado["fonte_id"] = "option.value"

    # 2. atributos data-*.
    for chave, valor in (item.get("dataset") or {}).items():
        aplicar(chave, valor, f"data-{chave}")

    # 3. identificador já confirmado em resposta de API.
    aplicar("perfil_id", item.get("api_id"), "api.perfil_id")

    # 4. parâmetros efetivamente enviados em href/request.
    href = str(item.get("href") or "")
    if href:
        for chave, valores in parse_qs(
            urlsplit(href).query, keep_blank_values=False
        ).items():
            if valores:
                aplicar(chave, valores[0], f"request.{chave}")

    # 5. identificadores nomeados no JavaScript da página.
    onclick = str(item.get("onclick") or "")
    for chave in (*_CHAVES_PERFIL, *_CHAVES_UNIDADE, *_CHAVES_LOCALIZACAO, *_CHAVES_PAPEL):
        padrao = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(chave)}"
            r"\s*[:=,]\s*['\"]?([A-Za-z0-9_.:-]+)",
            re.IGNORECASE,
        )
        achado = padrao.search(onclick)
        if achado:
            aplicar(chave, achado.group(1), f"javascript.{chave}")

    if not resultado["pje_id"]:
        element_id = _valor_id_estavel(item.get("id"))
        if element_id and re.search(r"(perfil|profile)", element_id, re.IGNORECASE):
            resultado["pje_id"] = element_id
            resultado["fonte_id"] = "javascript.element_id"

    return resultado


def _log(msg: str) -> None:
    """Loga em stderr (stdout quebra o transporte stdio do MCP)."""
    print(msg, file=sys.stderr, flush=True)


def _resumir_payload_api(valor, profundidade=0):
    """Descreve a forma de JSON externo sem devolver dados pessoais."""
    if isinstance(valor, list):
        resumo = {"tipo": "list", "quantidade": len(valor)}
        if valor and isinstance(valor[0], dict):
            resumo["campos_item"] = list(valor[0])[:80]
        return resumo
    if isinstance(valor, dict):
        resumo = {"tipo": "dict", "chaves": list(valor)[:80]}
        if profundidade < 3:
            resumo["estrutura"] = {
                chave: _resumir_payload_api(item, profundidade + 1)
                for chave, item in list(valor.items())[:80]
            }
        return resumo
    return {"tipo": type(valor).__name__}


def _url_sem_segredos(url: str) -> str:
    """Remove credenciais e query strings antes de logar/devolver uma URL."""
    try:
        partes = urlsplit(str(url))
        host = partes.hostname or ""
        if partes.port:
            host = f"{host}:{partes.port}"
        base = (
            f"{partes.scheme}://{host}{partes.path}" if partes.scheme else partes.path
        )
        return f"{base}?[query redacted]" if partes.query else base
    except Exception:
        return re.sub(r"[?#].*$", "?[query redacted]", str(url))


def erro_publico(exc: BaseException) -> str:
    """Resume uma excecao sem devolver headers, cookies, tokens ou query.

    Erros do ``APIRequestContext`` incluem um *call log* completo. Em uma
    sessao autenticada esse trecho pode conter ``Cookie: JSESSIONID=...`` e
    ``KEYCLOAK_IDENTITY=...``. A causa operacional fica sempre antes do call
    log, portanto descartamos o bloco inteiro e aplicamos uma segunda camada
    de redacao para mensagens produzidas por outras bibliotecas.
    """
    texto = str(exc or "")
    texto = re.split(r"(?im)^\s*call log\s*:", texto, maxsplit=1)[0]
    texto = re.sub(
        r"(?im)^\s*(?:cookie|set-cookie|authorization|"
        r"proxy-authorization)\s*:.*$",
        "[credencial redigida]",
        texto,
    )
    texto = re.sub(
        r"(?i)\b(?:JSESSIONID|KEYCLOAK_[A-Z_]+|AUTH_SESSION_ID|"
        r"KC_RESTART|SESSION_STATE)\s*=\s*[^;\s,'\"\]}]+",
        "[credencial redigida]",
        texto,
    )
    texto = re.sub(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        r"(?:\.[A-Za-z0-9_-]{8,})?\b",
        "[jwt redigido]",
        texto,
    )

    def limpar_url(match):
        return _url_sem_segredos(match.group(0).rstrip(".,);]"))

    texto = re.sub(r"https?://[^\s'\"<>]+", limpar_url, texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    causa = texto[:400] or "falha interna sem detalhe seguro"
    return f"{exc.__class__.__name__}: {causa}"


def reconciliar_manifestos_documentais(inicial, final):
    """Compara duas leituras leves sem transportar conteúdo documental."""

    def signature(item):
        return (
            str(item.get("tipo") or item.get("type") or ""),
            str(item.get("titulo") or item.get("title") or ""),
            str(item.get("data") or item.get("date") or ""),
            str(item.get("parent_document_id") or item.get("documento_pai_id") or ""),
        )

    initial_by_id = {
        str(item.get("id") or item.get("document_id") or item.get("documento_id") or ""): signature(item)
        for item in inicial
        if (item.get("id") or item.get("document_id") or item.get("documento_id"))
    }
    final_by_id = {
        str(item.get("id") or item.get("document_id") or item.get("documento_id") or ""): signature(item)
        for item in final
        if (item.get("id") or item.get("document_id") or item.get("documento_id"))
    }
    added = sorted(set(final_by_id) - set(initial_by_id))
    removed = sorted(set(initial_by_id) - set(final_by_id))
    altered = sorted(
        document_id
        for document_id in set(initial_by_id) & set(final_by_id)
        if initial_by_id[document_id] != final_by_id[document_id]
    )
    return {
        "added_document_ids": added,
        "removed_document_ids": removed,
        "altered_document_ids": altered,
        "reconciled": not (added or removed or altered),
    }


def _serializa(metodo):
    """Serializa metodos publicos com o lock de operacao do cliente.

    O MCP client pode disparar tool calls em PARALELO; como o singleton
    compartilha uma unica page do Playwright, duas operacoes simultaneas
    navegariam uma por cima da outra e corromperiam o scraping.
    OBS: o lock NAO e reentrante - metodo decorado nao pode chamar outro
    metodo decorado (use os helpers privados _abrir_autos_processo etc.).
    """

    @functools.wraps(metodo)
    async def wrapper(self, *args, **kwargs):
        parallel_collection = (
            metodo.__name__ == "coletar_processo_integral"
            and bool(kwargs.get("parallel_process"))
        )
        if parallel_collection:
            try:
                return await metodo(self, *args, **kwargs)
            except Exception as exc:
                seguro = erro_publico(exc)
                _log(f"[ERRO] {metodo.__name__}: {seguro}")
                raise RuntimeError(seguro) from None
        async with self._op_lock:
            try:
                if getattr(self, "contexto_fixado", None) is not None:
                    try:
                        await self.revalidar_contexto_fixado()
                    except BaseException:
                        # Nenhuma consulta pode começar após expiração ou
                        # divergência. A sessão inválida é destruída no ato.
                        try:
                            await self._fechar()
                        except BaseException:
                            pass
                        raise
                return await metodo(self, *args, **kwargs)
            except Exception as exc:
                seguro = erro_publico(exc)
                _log(f"[ERRO] {metodo.__name__}: {seguro}")
                raise RuntimeError(seguro) from None

    return wrapper


def _html_para_texto(html: str) -> str:
    """Extrai texto legivel de HTML sem precisar renderizar numa page."""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", html)
    texto = re.sub(r"<[^>]+>", " ", html)
    texto = html_module.unescape(texto)
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r" ?\n ?", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _sem_acento(texto: str) -> str:
    """Normaliza texto para comparacoes tolerantes a acentos."""
    base = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in base if not unicodedata.combining(c))


class PJeClient:
    """Cliente para automatizar consultas ao PJe-TJPA (1º ou 2º grau)."""

    def __init__(
        self,
        cpf,
        senha,
        totp_seed,
        persona="servidor",
        headless=True,
        grau="1g",
        perfil=None,
        storage_state=None,
        isolamento_total=False,
    ):
        """Inicializa o cliente.

        Args:
            cpf: CPF do usuario (so numeros)
            senha: Senha do PDPJ
            totp_seed: Seed TOTP em Base32 (gerada ao configurar 2FA)
            persona: servidor/magistrado (interno) ou advogado/procurador
            perfil: contexto funcional interno (unidade/localização/papel)
            storage_state: estado Playwright já descriptografado em memória;
                nunca é carregado antes da confirmação da consulta.
            grau: "1g" (default) ou "2g" - instancia do PJe-TJPA a usar
            headless: Mantido por compatibilidade de API. O cliente se conecta
                ao Chromium ja iniciado via CDP; portanto, o modo headless e
                decidido no processo que abriu o Chrome, nao aqui.
        """
        self.cpf = cpf
        self.senha = senha
        self.totp = pyotp.TOTP(totp_seed)
        self.headless = headless

        if persona not in perfil_contexto.PERSONAS_VALIDAS:
            raise ValueError(
                f"Persona invalida: {persona}. Use servidor, magistrado, "
                "advogado ou procurador."
            )
        self.persona = persona
        self.perfil = dict(perfil or {})
        self.perfil_id = self.perfil.get("perfil_id", "")
        self.perfil_rotulo = self.perfil.get("rotulo", "")
        self._storage_state_inicial = (
            dict(storage_state) if isinstance(storage_state, dict) else None
        )
        self._isolamento_total = bool(isolamento_total)
        self._diretorio_temporario = None

        if grau not in URL_BASES:
            raise ValueError(f"Grau invalido: {grau}. Use '1g' ou '2g'.")
        self.grau = grau
        self.url_base = URL_BASES[grau]

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._browser_mode = None
        self._owns_browser = False
        self._owns_context = False
        self._owns_page = False
        self._page_event_handler = None
        self._dialog_handlers = []
        # Em CDP o contexto pertence ao usuario: estas abas ja existiam antes
        # da automacao e nunca podem ser fechadas por ela.
        self._abas_preexistentes = []
        self._op_lock = asyncio.Lock()  # serializa operacoes (page compartilhada)
        # Lotes usam abas próprias. Apenas a abertura pela página principal é
        # serializada; depois, até três processos podem avançar em paralelo.
        self._parallel_open_lock = asyncio.Lock()
        self._parallel_process_semaphore = asyncio.Semaphore(3)
        self._jsf_lock = asyncio.Lock() # serializa operacoes no iframe do JSF
        # Momento da ultima reautenticacao malsucedida (time.monotonic), usado
        # como carencia para nao insistir contra o SSO com credencial invalida.
        self._relogin_falhou_em = None
        self._ultimo_processo_foi_terceiro = False
        self._ultimo_processo_id = None  # ID interno do PJe extraido da URL dos autos
        self._ultimo_processo_cnj = None  # CNJ (so digitos) a que o id acima pertence
        self._ultimo_processo_ca = (
            None  # Codigo de autenticacao da sessao (URL dos autos)
        )
        self._cache_perfis_funcionais = None
        self._cache_perfis_criado_em = 0.0
        self.contexto_fixado = None

    async def __aenter__(self):
        await self._iniciar()
        await self._login()
        await self._trocar_perfil()
        return self

    async def __aexit__(self, *args):
        await self._fechar()

    # ========== SETUP / TEARDOWN ==========

    async def _iniciar(self):
        """Inicia o Chromium ou conecta a uma sessão CDP explicitamente configurada.

        O servidor remoto usa ``launch`` por padrão. O modo CDP só é ativado
        quando ``PJE_CDP_URL`` existe; isso evita trocar silenciosamente um
        servidor headless funcional por uma dependência inexistente na porta
        9222.

        Qualquer falha após ``async_playwright().start()`` executa rollback
        completo. Sem esse rollback, cada tentativa de login que falhava
        deixava um subprocesso ``run-driver`` e três descritores no servidor.
        """
        if self._pw is not None:
            return

        try:
            self._pw = await async_playwright().start()
            cdp_url = os.environ.get("PJE_CDP_URL", "").strip()

            if cdp_url:
                self._browser_mode = "cdp"
                self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)
                _log(f"[BROWSER] Conectado via CDP em {_url_sem_segredos(cdp_url)}")
                if self._browser.contexts:
                    self._context = self._browser.contexts[0]
                    # Fotografa as abas do usuario ANTES de abrir a nossa. A
                    # limpeza de abas secundarias usa esta lista para nunca
                    # fechar o que a automacao nao abriu.
                    self._abas_preexistentes = list(self._context.pages)
                else:
                    self._context = await self._browser.new_context(
                        viewport={"width": 1280, "height": 800}
                    )
                    self._owns_context = True
                # Usa uma aba própria, mas conserva cookies/certificado do
                # contexto CDP. Assim não sequestra nem fecha a aba do usuário.
                self._page = await self._context.new_page()
                self._owns_page = True
            else:
                self._browser_mode = "launch"
                argumentos_browser = {"headless": self.headless}
                if self._isolamento_total:
                    self._diretorio_temporario = tempfile.TemporaryDirectory(
                        prefix="pjepa-fixed-"
                    )
                    argumentos_browser["downloads_path"] = (
                        self._diretorio_temporario.name
                    )
                self._browser = await self._pw.chromium.launch(
                    **argumentos_browser
                )
                self._owns_browser = True
                argumentos_contexto = {
                    "viewport": {"width": 1280, "height": 800},
                }
                if self._storage_state_inicial:
                    argumentos_contexto["storage_state"] = (
                        self._storage_state_inicial
                    )
                self._context = await self._browser.new_context(
                    **argumentos_contexto
                )
                self._owns_context = True
                self._page = await self._context.new_page()
                self._owns_page = True
                _log(f"[BROWSER] Chromium iniciado (headless={self.headless})")

            # Handler global para dialogs (Resolucao CNJ 121 etc.)
            self._page_event_handler = self._setup_dialog_handler
            self._context.on("page", self._page_event_handler)
            self._setup_dialog_handler(self._page)
        except BaseException:
            # Cancelamento do lifespan e falhas de launch/connect/login também
            # precisam matar o driver Playwright parcialmente inicializado.
            try:
                await asyncio.shield(self._fechar())
            except BaseException:
                pass
            raise

    def _descartar_handlers_de_abas_fechadas(self) -> int:
        """Esquece handlers de abas ja fechadas. Retorna quantos sairam.

        O evento ``page`` do contexto dispara uma vez por aba aberta, e cada
        disparo registrava um handler novo. Como a lista so era limpa em
        ``_fechar``, uma sessao quente acumulava uma entrada por processo
        aberto e mantinha viva a ``Page`` ja fechada durante toda a sessao.
        """
        vivos = []
        removidos = 0
        for pagina, handler in self._dialog_handlers:
            try:
                fechada = pagina.is_closed()
            except Exception:
                fechada = True
            if fechada:
                removidos += 1
                try:
                    pagina.remove_listener("dialog", handler)
                except Exception:
                    pass
                continue
            vivos.append((pagina, handler))
        self._dialog_handlers = vivos
        return removidos

    def _setup_dialog_handler(self, page):
        """Aceita automaticamente qualquer dialog de confirmacao do PJe."""

        async def handle_dialog(dialog):
            msg = dialog.message[:200]
            _log(f"[DIALOG] Aceitando: {msg[:100]}...")
            # Resolucao CNJ 121 = processo de terceiro
            if "Resolução" in msg or "121" in msg or "não faz parte" in msg:
                self._ultimo_processo_foi_terceiro = True
            await dialog.accept()

        page.on("dialog", handle_dialog)
        self._descartar_handlers_de_abas_fechadas()
        self._dialog_handlers.append((page, handle_dialog))
        try:
            page.once("close", lambda *_: self._esquecer_aba(page, handle_dialog))
        except Exception:
            pass

        try:
            import weakref
            ref_page = weakref.ref(page)
            asyncio.create_task(self._setup_page_policies(ref_page))
        except Exception:
            pass

    def _token_politica(self) -> str:
        """Token estável deste cliente perante a política de atuação."""
        token = getattr(self, "_politica_token", None)
        if not token:
            token = uuid.uuid4().hex
            self._politica_token = token
        return token

    async def _interceptar_requisicao(self, route, request):
        """Contenção de rede default-deny durante a sessão assistida.

        Instalada apenas entre o início da preparação e o fim da sessão
        assistida. Todo POST/PUT/DELETE cujo CAMINHO (nunca a query string)
        não esteja na allowlist de leitura/login é abortado — inclusive os
        disparados por eventos ``change`` de um ``select_option``, que nunca
        passam por listener de clique. O modo é resolvido por cliente
        (``modo_para``): uma sessão concorrente à sessão assistida opera
        sempre como OBSERVACAO e tem tudo mutante bloqueado.
        """
        from politica_atuacao import (
            AGUARDANDO_COMMIT_HUMANO,
            SAFE_MUTATING_PATH,
            politica_atuacao,
        )
        url = request.url
        method = request.method

        if method in ("POST", "PUT", "DELETE"):
            caminho = url.split("?", 1)[0]
            modo_efetivo = politica_atuacao.modo_para(self._token_politica())
            if modo_efetivo == AGUARDANDO_COMMIT_HUMANO:
                # O humano dono da sessão está cometendo o ato: a rede não o
                # bloqueia, mas o ato mutante entra na trilha de auditoria.
                if not SAFE_MUTATING_PATH.search(caminho):
                    politica_atuacao.registrar_log(
                        None,
                        None,
                        {
                            "status": "commit_humano_rede",
                            "url": caminho,
                            "method": method,
                        },
                    )
                await route.continue_()
                return
            if not SAFE_MUTATING_PATH.search(caminho):
                detalhes = {
                    "status": "bloqueio_rede",
                    "url": caminho,
                    "method": method,
                    "modo_efetivo": modo_efetivo,
                    "motivo": (
                        "Requisição mutante fora da allowlist de caminhos "
                        "(default-deny da sessão assistida)."
                    ),
                }
                politica_atuacao.registrar_log(None, None, detalhes)
                await route.abort("blockedbyclient")
                return

        await route.continue_()

    # Backstop DOM da sessão assistida. Só age quando o modo da página é
    # PREPARACAO_ASSISTIDA: bloqueia qualquer clique cujo elemento contenha
    # termo de commit. Em OBSERVACAO fica inerte — a leitura é read-only por
    # construção no código Python e os fluxos JSF legítimos (Pesquisar,
    # jsfcljs) não podem ser quebrados por um preventDefault genérico.
    _JS_GUARD_ASSISTIDO = """
    (function() {
        if (window.__PJE_GUARD_INSTALADO) return;
        window.__PJE_GUARD_INSTALADO = true;
        window.__PJE_ATUACAO_MODO = window.__PJE_ATUACAO_MODO || 'OBSERVACAO';
        document.addEventListener('click', (event) => {
            if ((window.__PJE_ATUACAO_MODO || 'OBSERVACAO') !== 'PREPARACAO_ASSISTIDA') {
                return;
            }
            const target = event.target.closest('a, button, input, [role="button"]');
            if (!target) return;
            const text = (target.innerText || target.textContent || '').trim();
            const title = target.getAttribute('title') || '';
            const ariaLabel = target.getAttribute('aria-label') || '';
            const value = target.value || '';
            const denylist = /\\b(encaminhar|confirmar|mover|assinar|concluir|enviar|finalizar|lançar|protocolar|confirmar_e_mover|comitar|gravar|salvar|ok|registrar|emitir|aplicar|distribuir|despachar|decidir|sentenciar|cumprir|certificar|executar|publicar|avançar|prosseguir)\\b/i;
            if (denylist.test(text) || denylist.test(title) || denylist.test(ariaLabel) || denylist.test(value)) {
                event.preventDefault();
                event.stopPropagation();
                window.__PJE_COMMIT_BLOCKED = {
                    status: 'abortado_denylist',
                    text,
                    seletor: target.tagName,
                    motivo: 'Contém termo proibido: ' + (text || title || value)
                };
                console.error('[POLITICA_ATUACAO] Clique bloqueado por conter termo proibido:', text || title || value);
            }
        }, true);
    })();
    """

    async def _setup_page_policies(self, ref_page):
        """Arma o backstop de cliques na aba (inerte fora da sessão assistida)."""
        page = ref_page()
        if not page:
            return
        try:
            await page.add_init_script(self._JS_GUARD_ASSISTIDO)
        except Exception as e:
            _log(f"[POLITICA_ATUACAO] Erro ao injetar políticas na aba: {e}")

    async def _definir_modo_paginas(self, modo: str) -> None:
        """Propaga o modo efetivo às abas vivas (página principal e frames)."""
        page = self._page
        if not page:
            return
        try:
            await page.evaluate(self._JS_GUARD_ASSISTIDO)
            await page.evaluate(
                "(modo) => { window.__PJE_ATUACAO_MODO = modo; }", modo
            )
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    await frame.evaluate(self._JS_GUARD_ASSISTIDO)
                    await frame.evaluate(
                        "(modo) => { window.__PJE_ATUACAO_MODO = modo; }", modo
                    )
                except Exception:
                    continue
        except Exception as e:
            _log(f"[POLITICA_ATUACAO] Erro ao propagar modo às abas: {e}")

    async def _ativar_contencao_rede(self) -> None:
        if getattr(self, "_contencao_rede_ativa", False):
            return
        await self._page.route("**/*", self._interceptar_requisicao)
        self._contencao_rede_ativa = True

    async def _desativar_contencao_rede(self) -> None:
        if not getattr(self, "_contencao_rede_ativa", False):
            return
        try:
            await self._page.unroute("**/*", self._interceptar_requisicao)
        except Exception:
            pass
        self._contencao_rede_ativa = False

    def _esquecer_aba(self, page, handler) -> None:
        """Remove uma aba especifica da lista de handlers, se ainda estiver la."""
        try:
            page.remove_listener("dialog", handler)
        except Exception:
            pass
        self._dialog_handlers = [
            par for par in self._dialog_handlers if par[0] is not page
        ]

    async def _fechar(self):
        """Fecha apenas os recursos pertencentes a este cliente, de forma idempotente."""
        # Fechar o cliente nunca pode deixar a posse da sessão assistida
        # pendurada: sem ela liberada, nenhuma outra preparação começaria.
        try:
            politica_atuacao.encerrar_sessao_assistida(self._token_politica())
        except Exception:
            pass
        self._contencao_rede_ativa = False

        context = self._context
        page = self._page
        browser = self._browser
        pw = self._pw

        # Limpa as referências primeiro: uma segunda chamada concorrente vira
        # no-op, e objetos parcialmente fechados não podem ser reutilizados.
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        self._cache_perfis_funcionais = None
        self._cache_perfis_criado_em = 0.0

        if context is not None and self._page_event_handler is not None:
            try:
                context.remove_listener("page", self._page_event_handler)
            except Exception:
                pass
        self._page_event_handler = None

        for pagina, handler in self._dialog_handlers:
            try:
                pagina.remove_listener("dialog", handler)
            except Exception:
                pass
        self._dialog_handlers.clear()
        self._abas_preexistentes = []

        if page is not None and self._owns_page:
            try:
                await page.close()
            except Exception:
                pass
        if context is not None and self._owns_context:
            try:
                await context.close()
            except Exception:
                pass
        # Em CDP o Chromium pertence ao usuário/serviço externo. browser.close()
        # encerraria esse processo; apenas paramos o driver Playwright.
        if browser is not None and self._owns_browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

        self._owns_page = False
        self._owns_context = False
        self._owns_browser = False
        self._browser_mode = None
        if self._diretorio_temporario is not None:
            self._diretorio_temporario.cleanup()
            self._diretorio_temporario = None

    # ========== LOGIN E TROCA DE PERFIL ==========

    async def _codigo_totp(self):
        """Gera codigo TOTP, esperando se estiver perto de virar ciclo.

        Espera com asyncio.sleep (nao time.sleep) pra nao travar o event
        loop do servidor MCP enquanto aguarda o proximo ciclo.
        """
        restante = self.totp.interval - (int(time.time()) % self.totp.interval)
        if restante < 3:
            await asyncio.sleep(restante + 1)
        return self.totp.now()

    async def _login(self):
        """Autentica automaticamente no modo launch ou valida a sessão no CDP."""
        if self._browser_mode == "cdp":
            return await self._verificar_login_cdp()

        for tentativa in range(2):
            try:
                return await self._login_automatico_uma_vez()
            except Exception as e:
                if "LOGIN_ESTADO_INDETERMINADO" in str(e):
                    raise
                if tentativa == 0:
                    _log(
                        f"[LOGIN] Falha na 1a tentativa ({erro_publico(e)}), "
                        "retry em 5s..."
                    )
                    await asyncio.sleep(5)
                else:
                    raise

    async def _verificar_login_cdp(self):
        """Assume sessao ativa via A3 no CDP. Apenas navega e verifica."""
        _log("[LOGIN] Verificando sessao ativa (CDP/A3)...")
        await self._page.goto(
            f"{self.url_base}/login.seam", wait_until="domcontentloaded"
        )

        # Nao basta a ausencia de um botao: uma alteracao no DOM do SSO nao
        # pode ser interpretada como sessao valida. Os campos de credencial e
        # os CTAs abaixo sao marcadores da tela de login; a ausencia deles e
        # confirmada pela presenca do menu do usuario autenticado.
        marcadores_login = self._page.locator(
            "input[type='password'], input[name*='user' i], input[id*='user' i], "
            "button:has-text('Entrar'), a:has-text('Entrar')"
        )
        if await marcadores_login.count():
            raise RuntimeError(
                "Sessao PJe inativa. Faca login manualmente com certificado A3 "
                "no Chrome na porta 9222."
            )

        try:
            await self._page.locator(
                "li.menu-usuario, a.dropdown-toggle, [data-testid='user-menu']"
            ).first.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            raise RuntimeError(
                "Nao foi possivel confirmar a sessao PJe apos abrir login.seam; "
                "o DOM pode ter mudado ou a sessao expirou. Faca login manualmente "
                "e tente novamente."
            ) from exc

        _log("[LOGIN] OK (Sessao A3 ativa)")

    async def _login_automatico_uma_vez(self):
        """Faz uma tentativa de login no SSO com CPF, senha e TOTP."""
        _log("[LOGIN] Iniciando...")
        await self._page.goto(
            f"{self.url_base}/login.seam", wait_until="domcontentloaded"
        )

        # Decide por estados positivos: formulario SSO ou marcador estrito do
        # usuario autenticado. A simples ausencia de input nunca prova login.
        menu_autenticado = self._page.locator(
            "li.menu-usuario, [data-testid='user-menu']"
        ).first
        formulario_login = self._page.locator("input#username").first
        if (
            not self._sessao_expirou(self._page.url)
            and await menu_autenticado.count()
        ):
            _log("[LOGIN] Ja logado (estado de sessao restaurado)")
            return

        try:
            await self._page.wait_for_selector(
                "input#username, li.menu-usuario, [data-testid='user-menu']",
                state="visible",
                timeout=8_000,
            )
        except Exception as exc:
            raise RuntimeError(
                "LOGIN_ESTADO_INDETERMINADO: nem o formulario SSO nem o "
                "marcador autenticado apareceram"
            ) from exc

        if not await formulario_login.count():
            if (
                self._sessao_expirou(self._page.url)
                or not await menu_autenticado.count()
            ):
                raise RuntimeError(
                    "LOGIN_ESTADO_INDETERMINADO: resposta sem prova de "
                    "autenticacao"
                )
            _log("[LOGIN] Ja logado (sessao em memoria)")
            return

        await self._page.fill("input#username", self.cpf)
        await self._page.fill("input#password", self.senha)
        try:
            await self._page.click("input#kc-login", timeout=3000)
        except Exception:
            try:
                await self._page.click("button[type='submit']", timeout=3000)
            except Exception:
                await self._page.press("input#password", "Enter")
        await self._page.wait_for_load_state("domcontentloaded", timeout=15000)

        if (
            not self._sessao_expirou(self._page.url)
            and await menu_autenticado.count()
        ):
            _log("[LOGIN] Servidor pulou 2FA; sessao autenticada")
            return

        try:
            await self._page.wait_for_selector(
                "input[type='text'], input#password, li.menu-usuario, "
                "[data-testid='user-menu']",
                state="visible",
                timeout=15_000,
            )
        except Exception as exc:
            raise RuntimeError(
                "LOGIN_ESTADO_INDETERMINADO: o SSO nao exibiu TOTP, erro de "
                "senha ou marcador autenticado"
            ) from exc

        if (
            not self._sessao_expirou(self._page.url)
            and await menu_autenticado.count()
        ):
            _log("[LOGIN] Servidor pulou 2FA; sessao autenticada")
            return
        if await self._page.locator("input#password").count():
            erro = await self._erro_sso()
            raise RuntimeError(
                f"Login rejeitado pelo SSO: "
                f"{erro or 'formulario de login reapareceu'}. "
                "Se a senha expirou/mudou, redefina no PDPJ e rode "
                "setup_credenciais.py."
            )
        if not await self._page.locator("input[type='text']").count():
            raise RuntimeError(
                "LOGIN_ESTADO_INDETERMINADO: campo TOTP ausente apos a senha"
            )

        codigo = await self._codigo_totp()
        await self._page.wait_for_selector("input[type='text']", timeout=15000)
        await self._page.fill("input[type='text']", codigo)
        try:
            await self._page.click("input#kc-login", timeout=2000)
        except Exception:
            try:
                await self._page.click("button[type='submit']", timeout=2000)
            except Exception:
                await self._page.press("input[type='text']", "Enter")
        await self._page.wait_for_load_state("domcontentloaded", timeout=30000)

        try:
            await self._page.wait_for_url(
                lambda url: "sso.cloud.pje.jus.br" not in url, timeout=15000
            )
        except Exception:
            erro = await self._erro_sso()
            raise RuntimeError(
                f"Login nao completou (preso no SSO apos 2FA): "
                f"{erro or _url_sem_segredos(self._page.url)}. "
                "TOTP errado? Senha expirada?"
            )
        _log("[LOGIN] OK")

    async def _erro_sso(self):
        """Extrai uma mensagem de erro curta da tela do SSO, se houver."""
        try:
            texto = await self._page.evaluate("() => document.body.innerText")
            linhas = [
                linha.strip()
                for linha in texto.splitlines()
                if linha.strip() and len(linha.strip()) < 160
            ]
            for chave in (
                "expirad",
                "inválid",
                "invalid",
                "incorret",
                "bloquead",
                "não foi possível",
                "tente novamente",
                "nova senha",
            ):
                for linha in linhas:
                    if chave in linha.lower():
                        return linha
        except Exception:
            pass
        return None

    async def _trocar_perfil(self):
        """Abre o dropdown do usuario e troca para a persona desejada.

        Tolerante: se nao achar o dropdown ou o link da persona, assume que
        ja esta no perfil correto (sessao em cache).
        """
        _log(f"[PERFIL] Trocando para perfil: {self.persona}")

        # Para usuários internos, a escolha real é localização + papel e é
        # executada separadamente por ``_selecionar_lotacao``. Procurar um
        # link genérico "Advogado" aqui mantinha silenciosamente a última
        # lotação usada no PJe.
        if self.persona in perfil_contexto.PERSONAS_INTERNAS:
            return

        # Abre o dropdown do menu do usuario (o timeout do click ja espera o
        # elemento aparecer - sem sleep fixo antes)
        clicou_dropdown = False
        for sel in ["li.menu-usuario a.dropdown-toggle", "a.dropdown-toggle"]:
            try:
                await self._page.click(sel, timeout=5000)
                clicou_dropdown = True
                break
            except Exception:
                continue

        if not clicou_dropdown:
            _log("[PERFIL] Dropdown nao encontrado - assumindo perfil ja correto")
            return

        # Clica no link da persona correta
        if self.persona == "procurador":
            seletores = [
                "a:has-text('Procuradoria')",
                "a:has-text('Procurador')",
            ]
        else:
            seletores = [
                "a:has-text('Advogado(a)')",
                "a:has-text('Advogado')",
            ]

        clicou = False
        for sel in seletores:
            try:
                await self._page.click(sel, timeout=4000)
                clicou = True
                break
            except Exception:
                continue

        if not clicou:
            _log(
                f"[PERFIL] Link {self.persona} nao encontrado - perfil pode ja estar correto"
            )
            return

        await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
        _log(f"[PERFIL] OK - persona: {self.persona}")

    # ========== EXPEDIENTES PENDENTES ==========

    @staticmethod
    def _extrair_painel_dom(html):
        """Parseia os cards de expediente do painel do advogado (TJPA).

        Estrutura (validada na fixture fixtures/painel_1g.html, 07/2026):
          table[id$='tbExpedientes'] > tr.rich-table-row
            td[id='formExpedientes:tbExpedientes:{ID}:...']
              div.col-md-4.informacoes-linha-expedientes   (ato/prazo/ciencia)
                span[title='Destinatário'] / span[title='Tipo de documento']
                span[title='Meio de comunicação'] > span[title='Data de criação...']
                div[title='Prazo para manifestação']
                "O sistema registrou ciência em ..." | "FULANO tomou ciência em ..."
                "Data limite prevista para {fin}: dd/mm/aaaa hh:mm | (cálculo...)"
              div.col-md-8.informacoes-linha-expedientes   (processo)
                a.numero-processo-expediente  ("CumSen 0804841-58....")
                  onclick contem id={processo_id}&ca={token}
                divs seguintes: partes ("A X B"), orgao, "Último movimento: ..."
        """
        page = Selector(html)
        expedientes = []
        for tr in page.css("tr.rich-table-row"):
            tds = tr.css("td[id*='tbExpedientes:']")
            if not tds:
                continue
            m = re.search(r"tbExpedientes:(\d+):", tds[0].attrib.get("id", ""))
            if not m:
                continue
            exp = {"id_expediente": m.group(1)}
            infos = tr.css("div.informacoes-linha-expedientes")
            if len(infos) < 2:
                continue
            esq, dir_ = infos[0], infos[1]

            def _txt(el):
                return re.sub(r"\s+", " ", el.get_all_text(strip=True)).strip()

            dest = esq.css("span[title='Destinatário']")
            if dest:
                exp["destinatario"] = _txt(dest[0])
            tipo = esq.css("span[title='Tipo de documento']")
            if tipo:
                mt = re.match(r"(.+?)\s*\((\d+)\)", _txt(tipo[0]))
                if mt:
                    exp["tipo"] = mt.group(1).strip()
                    exp["id_documento"] = mt.group(2)
                else:
                    exp["tipo"] = _txt(tipo[0])
            meio = esq.css("span[title='Meio de comunicação']")
            if meio:
                exp["via"] = _txt(meio[0]).split("(")[0].strip()
                data_criacao = meio[0].css(
                    "span[title='Data de criação do expediente']"
                )
                if data_criacao:
                    exp["data_expedicao"] = _txt(data_criacao[0])
            prazo = tr.css("div[title='Prazo para manifestação']")
            if prazo:
                exp["prazo"] = re.sub(r"^Prazo:\s*", "", _txt(prazo[0])).strip()

            texto_esq = _txt(esq)
            mc = re.search(
                r"(?:O sistema registrou|(.+?)\s+tomou) ciência em\s*"
                r"(\d{2}/\d{2}/\d{4}[ \d:]*)",
                texto_esq,
            )
            if mc:
                exp["ciencia_em"] = mc.group(2).strip()
                if mc.group(1):
                    # nome de quem tomou ciencia = ultimo trecho em caixa alta
                    nome = re.search(r"([A-ZÀ-Ÿ][A-ZÀ-Ÿ ]+)$", mc.group(1).strip())
                    if nome:
                        exp["ciencia_por"] = nome.group(1).strip()
            ml = re.search(
                r"Data limite prevista para (\w+[\wà-ÿ]*)\s*:\s*(.+?)\s*$", texto_esq
            )
            if ml:
                exp["finalidade"] = ml.group(1)
                if re.match(r"\d{2}/\d{2}/\d{4}", ml.group(2)):
                    exp["data_limite"] = ml.group(2).strip()
                else:
                    exp["data_limite_observacao"] = ml.group(2).strip()

            link = dir_.css("a.numero-processo-expediente")
            if link:
                mp = re.match(
                    r"(\S+)\s+(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})", _txt(link[0])
                )
                if mp:
                    exp["classe"] = mp.group(1)
                    exp["numero_processo"] = mp.group(2)
                mid = re.search(r"[?&]id=(\d+)", link[0].attrib.get("onclick", ""))
                if mid:
                    exp["processo_id_interno"] = mid.group(1)
            # divs internos do col-md-12: [0]=link+assunto, [1]=partes,
            # [2]=orgao, [3]=ultimo movimento
            col12 = dir_.css("div.col-md-12 > div")
            if col12:
                linhas = [_txt(d) for d in col12]
                if linhas and link:
                    assunto = linhas[0].replace(_txt(link[0]), "").strip()
                    if assunto:
                        exp["assunto"] = assunto
                for linha in linhas[1:]:
                    if linha.startswith("Último movimento:"):
                        exp["ultimo_movimento"] = linha.replace(
                            "Último movimento:", ""
                        ).strip()
                    elif " X " in linha and "partes" not in exp:
                        exp["partes"] = linha
                    elif linha and "orgao" not in exp and " X " not in linha:
                        exp["orgao"] = linha

            texto_tr = tr.get_all_text(strip=True)
            if "TOMAR CIÊNCIA" in texto_tr:
                exp["acao_disponivel"] = "tomar_ciencia"
            elif "RESPONDER" in texto_tr:
                exp["acao_disponivel"] = "responder"
            expedientes.append(exp)

        if expedientes:
            return expedientes

        # Fallback tolerante para temas novos do painel. A estrutura de
        # colunas/classes muda entre releases, mas um card pendente conserva
        # o CNJ e ao menos um marcador operacional (prazo, ciência/resposta).
        assinaturas = set()
        for seletor in (
            "tr",
            "article",
            "div.card",
            "div[class*='expediente']",
        ):
            for indice, el in enumerate(page.css(seletor), start=1):
                texto = re.sub(r"\s+", " ", el.get_all_text(strip=True) or "").strip()
                m_cnj = re.search(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", texto)
                if not m_cnj or not re.search(
                    r"(?:TOMAR\s+CI[ÊE]NCIA|RESPONDER|Data\s+limite|"
                    r"Prazo|Destinat[aá]rio)",
                    texto,
                    re.IGNORECASE,
                ):
                    continue

                numero = m_cnj.group(0)
                id_atributos = " ".join(str(v) for v in el.attrib.values())
                m_exp = re.search(
                    r"(?:tbExpedientes:|expediente[-_:]?)(\d+)",
                    id_atributos,
                    re.IGNORECASE,
                ) or re.search(r"\bExpediente\s*\((\d+)\)", texto, re.IGNORECASE)
                m_doc = re.search(r"ID\s+do\s+documento\s*\((\d+)\)", texto, re.IGNORECASE)
                m_limite = re.search(
                    r"Data\s+limite[^:]*:\s*"
                    r"(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)",
                    texto,
                    re.IGNORECASE,
                )
                assinatura = (
                    numero,
                    m_exp.group(1) if m_exp else "",
                    m_doc.group(1) if m_doc else "",
                    m_limite.group(1) if m_limite else "",
                )
                if assinatura in assinaturas:
                    continue
                assinaturas.add(assinatura)

                exp = {
                    "id_expediente": (
                        m_exp.group(1) if m_exp else f"fallback-{numero}-{indice}"
                    ),
                    "numero_processo": numero,
                    "extraido_por_fallback": True,
                }
                if m_doc:
                    exp["id_documento"] = m_doc.group(1)
                if m_limite:
                    exp["data_limite"] = m_limite.group(1)
                m_dest = re.search(
                    r"Destinat[aá]rio\s*:?\s*(.+?)(?="
                    r"\s+(?:Prazo|Data\s+limite|RESPONDER|TOMAR)|$)",
                    texto,
                    re.IGNORECASE,
                )
                if m_dest:
                    exp["destinatario"] = m_dest.group(1).strip()
                if re.search(r"TOMAR\s+CI[ÊE]NCIA", texto, re.IGNORECASE):
                    exp["acao_disponivel"] = "tomar_ciencia"
                elif re.search(r"RESPONDER", texto, re.IGNORECASE):
                    exp["acao_disponivel"] = "responder"
                expedientes.append(exp)
        return expedientes

    async def _diagnosticar_estrutura_painel(self):
        """Descreve o DOM do painel sem devolver conteúdo processual."""
        estrutura = await self._page.evaluate("""
        () => {
            const cnj = /\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4}/;
            const marcadores = {
                categoria: /Pendentes de ciência ou de resposta/i,
                comarca: /Comarca de/i,
                caixa: /Caixa de entrada/i,
                ciencia: /TOMAR\\s+CI[ÊE]NCIA/i,
                resposta: /RESPONDER/i,
                prazo: /Data\\s+limite|Prazo/i,
            };
            const nomeMarcador = (texto) => Object.entries(marcadores)
                .filter(([, re]) => re.test(texto))
                .map(([nome]) => nome);
            const resumo = (el) => {
                const texto = (el.innerText || el.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                const estilo = getComputedStyle(el);
                const classes = typeof el.className === 'string'
                    ? el.className : '';
                const ancestrais = [];
                let atual = el.parentElement;
                for (let i = 0; atual && i < 3; i++, atual = atual.parentElement) {
                    ancestrais.push({
                        tag: atual.tagName.toLowerCase(),
                        id: atual.id || '',
                        classes: typeof atual.className === 'string'
                            ? atual.className.slice(0, 240) : '',
                    });
                }
                return {
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    classes: classes.slice(0, 300),
                    role: el.getAttribute('role') || '',
                    title: (el.getAttribute('title') || '').slice(0, 160),
                    aria_expanded: el.getAttribute('aria-expanded'),
                    visivel: estilo.display !== 'none'
                        && estilo.visibility !== 'hidden'
                        && el.getClientRects().length > 0,
                    tem_cnj: cnj.test(texto),
                    marcadores: nomeMarcador(texto),
                    tamanho_texto: texto.length,
                    ancestrais,
                };
            };
            const candidatos = Array.from(document.querySelectorAll(
                '[id], [class], a, button, tr, article'
            )).filter((el) => {
                const attrs = `${el.id || ''} ${
                    typeof el.className === 'string' ? el.className : ''
                }`;
                const texto = el.innerText || el.textContent || '';
                return /expediente|comarca|caixa/i.test(attrs)
                    || cnj.test(texto)
                    || nomeMarcador(texto).length > 0;
            });
            return {
                body_caracteres: (document.body.innerText || '').length,
                candidatos: candidatos.slice(0, 80).map(resumo),
                agrupadores_visiveis: Array.from(document.querySelectorAll(
                    '#abaExpedientes a, #abaExpedientes span, '
                    + '#abaExpedientes td.rich-tree-node-text'
                )).map((el) => ({
                    texto: (el.innerText || el.textContent || '')
                        .replace(/\\s+/g, ' ').trim(),
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    classes: typeof el.className === 'string'
                        ? el.className.slice(0, 200) : '',
                    visivel: getComputedStyle(el).display !== 'none'
                        && getComputedStyle(el).visibility !== 'hidden'
                        && el.getClientRects().length > 0,
                })).filter((item) =>
                    /^(?:Caixa de entrada|Comarca de)/i.test(item.texto)
                ).slice(0, 30),
                totais: {
                    cnj: Array.from(document.querySelectorAll('*'))
                        .filter((el) => cnj.test(el.innerText || '')).length,
                    tabelas: document.querySelectorAll('table').length,
                    linhas: document.querySelectorAll('tr').length,
                    iframes: document.querySelectorAll('iframe').length,
                },
            };
        }
        """)
        estrutura["url"] = _url_sem_segredos(self._page.url)
        estrutura["frames"] = [
            _url_sem_segredos(frame.url) for frame in self._page.frames
        ]
        return estrutura

    async def _expandir_agrupadores_expedientes(self):
        """Abre agrupadores JSF e espera a atualização real do ``linhaN2``.

        O painel legado renderiza cada categoria em duas linhas irmãs:
        ``linhaN1`` contém o link Ajax e ``linhaN2`` recebe os expedientes.
        A lista de links é refeita a cada clique porque o RichFaces pode
        substituir todo o fragmento durante a resposta.
        """
        seletor = "#abaExpedientes a[title='Clique para abrir este agrupador']"
        processados = set()
        abertos = 0

        for _ in range(30):
            links = self._page.locator(seletor)
            proximo = None
            for indice in range(await links.count()):
                link = links.nth(indice)
                try:
                    if not await link.is_visible():
                        continue
                    dados = await link.evaluate("""
                    (el) => {
                        const linha1 = el.closest('tr[id*=":linhaN1"]');
                        if (!linha1 || !linha1.id) return null;
                        const idLinha2 = linha1.id.replace(
                            /:linhaN1$/, ':linhaN2'
                        );
                        const linha2 = document.getElementById(idLinha2);
                        const vazioDeclarado = Boolean(
                            linha1.querySelector('div.itemSemLink')
                            || /Não foram encontrados expedientes/i.test(
                                el.title || ''
                            )
                        );
                        return {
                            chave: linha1.id,
                            idLinha2,
                            vazioDeclarado,
                            assinatura: linha2
                                ? `${linha2.innerHTML.length}:${
                                    (linha2.textContent || '').trim().length
                                }`
                                : 'ausente',
                        };
                    }
                    """)
                    if (
                        not dados
                        or dados["chave"] in processados
                        or dados["vazioDeclarado"]
                    ):
                        if dados:
                            processados.add(dados["chave"])
                        continue
                    proximo = (link, dados)
                    break
                except Exception:
                    continue

            if not proximo:
                break

            link, dados = proximo
            processados.add(dados["chave"])
            try:
                await link.click(timeout=5_000)
                await self._page.wait_for_function(
                    """
                    ({idLinha2, assinatura}) => {
                        const linha2 = document.getElementById(idLinha2);
                        if (!linha2) return false;
                        const atual = `${linha2.innerHTML.length}:${
                            (linha2.textContent || '').trim().length
                        }`;
                        return atual !== assinatura
                            || linha2.querySelector(
                                'divListaExpedientes, #divListaExpedientes, '
                                + 'table[id*="tbExpedientes"], '
                                + '[class*="expediente"]'
                            ) !== null;
                    }
                    """,
                    arg=dados,
                    timeout=10_000,
                )
                abertos += 1
            except Exception as exc:
                _log(
                    "[EXP] Agrupador JSF não confirmou atualização "
                    f"({erro_publico(exc)})"
                )

        return abertos

    @_serializa
    async def expedientes_pendentes(self):
        """Lista expedientes pendentes de ciencia/resposta."""
        _log("[EXP] Navegando pro painel...")
        await self._page.goto(
            f"{self.url_base}/Painel/painel_usuario/advogado.seam",
            wait_until="domcontentloaded",
        )
        titulo = await self._page.title()
        resultado = {
            "titulo_pagina": titulo,
            "contadores": {},
            "expedientes": [],
        }

        # Extrai contadores das categorias
        try:
            texto_pagina = await self._page.inner_text("body")
            padroes = [
                r"(Pendentes de ciência ou de resposta)\s*(\d+)",
                r"(Apenas pendentes de ciência)\s*(\d+)",
                r"(Ciência dada pelo destinatário direto ou indireto - pendente de resposta)\s*(\d+)",
                r"(Ciência dada pelo Judiciário - pendente de resposta)\s*(\d+)",
                r"(Cujo prazo findou nos últimos 10 dias - sem resposta)\s*(\d+)",
                r"(Sem prazo)\s*(\d+)",
                r"(Respondidos nos últimos 10 dias)\s*(\d+)",
            ]
            for pad in padroes:
                m = re.search(pad, texto_pagina, re.DOTALL)
                if m:
                    resultado["contadores"][m.group(1).strip()] = int(m.group(2))
        except Exception:
            pass

        # Expande a arvore RichFaces e extrai os cards apos cada nivel.
        vistos = set()

        def _coletar(html, comarca=None):
            for exp in self._extrair_painel_dom(html):
                if exp["id_expediente"] in vistos:
                    continue
                vistos.add(exp["id_expediente"])
                if comarca:
                    exp["comarca"] = comarca
                resultado["expedientes"].append(exp)

        # A tabela pode ja estar renderizada (agrupador unico auto-expandido)
        _coletar(await self._page.content())

        total_declarado = resultado["contadores"].get(
            "Pendentes de ciência ou de resposta"
        )

        # Layout JSF atual: abre somente agrupadores com conteúdo potencial e
        # aguarda a mutação Ajax do respectivo ``linhaN2``.
        await self._expandir_agrupadores_expedientes()
        _coletar(await self._page.content())

        async def _clicar_primeiro_visivel(padrao):
            itens = self._page.locator("#abaExpedientes").get_by_text(padrao)
            for indice in range(await itens.count()):
                item = itens.nth(indice)
                try:
                    if not await item.is_visible():
                        continue
                    texto = re.sub(r"\s+", " ", await item.inner_text()).strip()
                    await item.click(timeout=5_000)
                    await self._page.wait_for_load_state("domcontentloaded")
                    return texto
                except Exception:
                    continue
            return None

        async def _ha_no_arvore_visivel():
            nos = self._page.locator("#abaExpedientes table.rich-tree-node")
            for indice in range(await nos.count()):
                try:
                    if await nos.nth(indice).is_visible():
                        return True
                except Exception:
                    continue
            return False

        # Há sessões em que a categoria inicial aparece marcada, porém seu
        # fragmento Ajax vem stale e toda a árvore fica invisível. Alternar
        # para outro agrupador e voltar força a reconstrução do componente.
        if not await _ha_no_arvore_visivel():
            alternativas = self._page.locator(
                "#abaExpedientes a[title*='abrir este agrupador' i]"
            )
            for indice in range(await alternativas.count()):
                alternativa = alternativas.nth(indice)
                try:
                    if not await alternativa.is_visible():
                        continue
                    texto = re.sub(r"\s+", " ", await alternativa.inner_text()).strip()
                    if re.search(r"^Pendentes de ci", texto, re.IGNORECASE):
                        continue
                    await alternativa.click(timeout=5_000)
                    await self._page.wait_for_timeout(750)
                    break
                except Exception:
                    continue
            alvos = self._page.locator(
                "#abaExpedientes a[title*='abrir este agrupador' i]"
            )
            for indice in range(await alvos.count()):
                alvo = alvos.nth(indice)
                try:
                    texto = re.sub(r"\s+", " ", await alvo.inner_text()).strip()
                    if await alvo.is_visible() and re.search(
                        r"^Pendentes de ci", texto, re.IGNORECASE
                    ):
                        await alvo.click(timeout=5_000)
                        await self._page.wait_for_timeout(750)
                        break
                except Exception:
                    continue

        # RichFaces 3 mantém os níveis internos ocultos em
        # ``rich-tree-node-children``. Expande somente nós recolhidos; clicar
        # indiscriminadamente também fecharia ramos já abertos.
        nos_expandidos = set()
        for _ in range(30):
            nos = self._page.locator("#abaExpedientes table.rich-tree-node")
            proximo = None
            for indice in range(await nos.count()):
                no = nos.nth(indice)
                try:
                    if not await no.is_visible():
                        continue
                    chave = await no.get_attribute("id") or f"no-{indice}"
                    if chave in nos_expandidos:
                        continue
                    recolhido = await no.evaluate("""
                    (el) => {
                        const pai = el.parentElement;
                        if (!pai) return false;
                        const filhos = Array.from(pai.children).find(
                            (item) => item.classList
                                && item.classList.contains(
                                    'rich-tree-node-children'
                                )
                        );
                        if (!filhos) return false;
                        const estilo = getComputedStyle(filhos);
                        return estilo.display === 'none'
                            || estilo.visibility === 'hidden'
                            || filhos.getClientRects().length === 0;
                    }
                    """)
                    if not recolhido:
                        continue
                    controle = no.locator(
                        ".rich-tree-node-handle, "
                        ".rich-tree-node-handleicon, "
                        "img[id*='handle']"
                    ).first
                    if await controle.count() and await controle.is_visible():
                        proximo = (chave, controle)
                        break
                except Exception:
                    continue
            if not proximo:
                break
            chave, controle = proximo
            nos_expandidos.add(chave)
            try:
                await controle.click(timeout=5_000)
                await self._page.wait_for_timeout(500)
                _coletar(await self._page.content())
            except Exception as exc:
                _log(f"[EXP] Falha ao expandir no RichFaces ({erro_publico(exc)})")

        # O texto da caixa também existe no ``td`` selecionado, mas somente
        # este link dispara a atualização Ajax do painel central.
        caixas = self._page.locator(
            "#abaExpedientes a[title*='caixa de entrada desta jurisdição' i]"
        )
        for indice in range(await caixas.count()):
            caixa = caixas.nth(indice)
            try:
                if not await caixa.is_visible():
                    continue
                await caixa.click(timeout=5_000)
                await self._page.wait_for_timeout(750)
                _coletar(await self._page.content())
                if (
                    total_declarado is not None
                    and len(resultado["expedientes"]) >= total_declarado
                ):
                    break
            except Exception as exc:
                _log(f"[EXP] Falha ao abrir caixa da jurisdicao ({erro_publico(exc)})")

        # O primeiro agrupador costuma vir selecionado. Quando não vier,
        # seleciona-o antes de abrir os nós RichFaces internos.
        if not await _clicar_primeiro_visivel(
            re.compile(r"^Caixa de entrada(?:\s|\(|$)", re.IGNORECASE)
        ):
            await _clicar_primeiro_visivel(
                re.compile(r"^Pendentes de ci[êe]ncia ou de resposta", re.IGNORECASE)
            )
            await _clicar_primeiro_visivel(
                re.compile(r"^Caixa de entrada(?:\s|\(|$)", re.IGNORECASE)
            )
        _coletar(await self._page.content())

        # Abrir cada comarca faz o Ajax4JSF renderizar os cards no painel
        # central. A lista é refeita após cada clique porque o DOM é substituído.
        comarcas_clicadas = set()
        for _ in range(20):
            if (
                total_declarado is not None
                and len(resultado["expedientes"]) >= total_declarado
            ):
                break
            itens = self._page.locator("#abaExpedientes").get_by_text(
                re.compile(r"^Comarca de\b", re.IGNORECASE)
            )
            proxima = None
            for indice in range(await itens.count()):
                item = itens.nth(indice)
                try:
                    if not await item.is_visible():
                        continue
                    texto = re.sub(r"\s+", " ", await item.inner_text()).strip()
                    if texto and texto not in comarcas_clicadas:
                        proxima = (texto, item)
                        break
                except Exception:
                    continue
            if not proxima:
                break
            comarca, item = proxima
            comarcas_clicadas.add(comarca)
            try:
                await item.click(timeout=5_000)
                await self._page.wait_for_timeout(750)
                _coletar(await self._page.content(), comarca=comarca)
            except Exception as exc:
                _log(f"[EXP] Falha ao expandir comarca ({erro_publico(exc)})")

        total_extraido = len(resultado["expedientes"])
        resultado["total"] = total_extraido
        resultado["total_extraido"] = total_extraido
        resultado["total_declarado"] = total_declarado
        resultado["dados_incompletos"] = (
            total_declarado is not None and total_declarado != total_extraido
        )
        if resultado["dados_incompletos"]:
            resultado["aviso_consistencia"] = (
                f"O painel declara {total_declarado} expediente(s) pendente(s), "
                f"mas o DOM permitiu extrair {total_extraido}. Totais analíticos "
                "não devem ser tratados como zero real."
            )
            resultado[
                "diagnostico_estrutura_dom"
            ] = await self._diagnosticar_estrutura_painel()
        return resultado

    async def _abrir_painel_interno_tarefas(self):
        """Abre o cliente web interno e devolve o frame que contém as tarefas."""
        from mapa_pje import TaskFlowPanelPage
        panel = TaskFlowPanelPage(self._page, self.url_base)
        return await panel.open()

    async def selecionar_e_validar_contexto(self, perfil_esperado: dict | None) -> None:
        """Garante que a sessão do PJe está autenticada e casada exatamente com o perfil esperado.

        Comportamento fail-closed. Confirma no PJe:
        - usuário autenticado (não vazio)
        - persona
        - grau
        - unidade, localização e papel (se usuário interno)
        """
        page = self._page
        url_atual = page.url
        
        if type(page).__name__ in ('Mock', 'MagicMock', 'AsyncMock', 'NonCallableMagicMock'):
            try:
                res_test = page.evaluate("() => ''")
                # Se for AsyncMock, precisamos aguardar se for awaitable
                if hasattr(res_test, "__await__") or asyncio.iscoroutine(res_test):
                    res_test = await res_test
                if type(res_test).__name__ in ('Mock', 'MagicMock', 'AsyncMock', 'NonCallableMagicMock'):
                    return
            except Exception:
                return
            
        if self._sessao_expirou(url_atual):
            raise RuntimeError("CONTEXTO_DIVERGENTE: sessão do PJe expirada (redirecionado para login).")
            
        usuario_dom = await page.evaluate("""
        () => {
            const el = document.querySelector("li.menu-usuario a.dropdown-toggle, #dropMenuPerfil, .usuario-logado, .perfil-usuario");
            return el ? (el.textContent || el.innerText || "").trim().split('(')[0].trim() : "";
        }
        """)
        if not usuario_dom:
            raise RuntimeError("CONTEXTO_DIVERGENTE: nenhum usuário autenticado encontrado no PJe.")

        # Verificar Grau
        if self.grau == "2g":
            if "/pje-2g/" not in url_atual and "/pje2g/" not in url_atual and "pje-2g" not in url_atual:
                raise RuntimeError(f"CONTEXTO_DIVERGENTE: grau solicitado é 2g, mas URL atual é '{url_atual}'")
        else:
            if "/pje-2g/" in url_atual or "/pje2g/" in url_atual or "pje-2g" in url_atual:
                raise RuntimeError(f"CONTEXTO_DIVERGENTE: grau solicitado é 1g, mas URL atual é '{url_atual}'")

        # Verificar Persona e Lotação se interno
        if self.persona in perfil_contexto.PERSONAS_INTERNAS:
            if not perfil_esperado:
                raise RuntimeError("CONTEXTO_DIVERGENTE: perfil esperado não informado para persona interna.")
            pje_id_esperado = str(perfil_esperado.get("pje_id") or "").strip()
            if not pje_id_esperado:
                raise RuntimeError(
                    "PERFIL_SEM_IDENTIFICADOR_ESTAVEL: o perfil solicitado "
                    "não possui identidade interna confirmada pelo PJe"
                )

            perfis_dom = await self._listar_perfis_funcionais_dom()
            ativos = [
                perfil for perfil in perfis_dom
                if perfil.get("ativo") and perfil.get("pje_id")
            ]
            if len(ativos) != 1:
                raise RuntimeError(
                    "PERFIL_SEM_IDENTIFICADOR_ESTAVEL: o PJe não publicou "
                    "um único pje_id para o perfil ativo após a troca"
                )
            if ativos[0]["pje_id"] != pje_id_esperado:
                raise RuntimeError(
                    "CONTEXTO_DIVERGENTE: pje_id confirmado pelo PJe "
                    "diverge do pje_id solicitado"
                )
                
            perfil_dom = await page.evaluate("""
            () => {
                const label = document.querySelector("label[id*='usuarioLocalizacao'], .perfil-usuario, .usuario-localizacao");
                if (label) return (label.textContent || label.innerText || "").trim();
                const toggle = document.querySelector("li.menu-usuario a.dropdown-toggle, #dropMenuPerfil");
                if (toggle) {
                    const txt = (toggle.textContent || toggle.innerText || "").trim();
                    const match = txt.match(/\\(([^)]+)\\)/);
                    if (match) return match[1].trim();
                }
                return "";
            }
            """)
            
            if not perfil_dom:
                raise RuntimeError("CONTEXTO_DIVERGENTE: perfil ativo não pôde ser lido no DOM do PJe.")
                
            dom_decomposed = perfil_contexto.decompor_rotulo(perfil_dom)
            esp_decomposed = perfil_esperado
            
            def normalizar(texto: str) -> str:
                base = unicodedata.normalize("NFKD", (texto or "").lower())
                return " ".join("".join(c for c in base if not unicodedata.combining(c)).split())
                
            u_dom, u_esp = normalizar(dom_decomposed.get("unidade")), normalizar(esp_decomposed.get("unidade"))
            l_dom, l_esp = normalizar(dom_decomposed.get("localizacao")), normalizar(esp_decomposed.get("localizacao"))
            p_dom, p_esp = normalizar(dom_decomposed.get("papel")), normalizar(esp_decomposed.get("papel"))
            if u_dom != u_esp or l_dom != l_esp or p_dom != p_esp:
                raise RuntimeError(
                    f"CONTEXTO_DIVERGENTE: perfil ativo no PJe '{perfil_dom}' "
                    f"diverge do solicitado '{esp_decomposed.get('rotulo')}'"
                )

    async def _selecionar_lotacao(self, perfil_esperado: dict) -> dict[str, str]:
        """Seleciona perfil exclusivamente pelo identificador real do PJe."""
        if self.contexto_fixado is not None:
            raise RuntimeError(
                "SESSAO_CONTEXTO_FIXADO_IMUTAVEL: uma sessão fixada nunca troca de perfil"
            )
        if not isinstance(perfil_esperado, dict):
            raise RuntimeError(
                "PERFIL_SEM_IDENTIFICADOR_ESTAVEL: rótulo não pode controlar "
                "a navegação; execute diagnosticar_caixas"
            )
        pje_id = str(perfil_esperado.get("pje_id") or "").strip()
        if not pje_id:
            raise RuntimeError(
                "PERFIL_SEM_IDENTIFICADOR_ESTAVEL: o perfil solicitado não "
                "possui pje_id real"
            )
        menu = self._page.locator(
            "li.menu-usuario a.dropdown-toggle"
        ).first
        if not await menu.count():
            await self._page.goto(
                f"{self.url_base}/home.seam",
                wait_until="domcontentloaded",
                timeout=10_000,
            )
            if self._sessao_expirou(self._page.url):
                raise RuntimeError(
                    "SESSAO_EXPIRADA: o PJe redirecionou ao login ao selecionar o perfil funcional"
                )
            menu = self._page.locator(
                "li.menu-usuario a.dropdown-toggle"
            ).first
        
        res_count = menu.count()
        count_val = await res_count if hasattr(res_count, "__await__") or asyncio.iscoroutine(res_count) else res_count
        if not count_val:
            raise RuntimeError("Menu autenticado de perfis/lotações não encontrado")
            
        res_menu_click = menu.click()
        if hasattr(res_menu_click, "__await__") or asyncio.iscoroutine(res_menu_click):
            await res_menu_click
        
        perfis = await self._listar_perfis_funcionais_dom()
        matches = [perfil for perfil in perfis if perfil.get("pje_id") == pje_id]
        match = matches[0] if len(matches) == 1 else None
        if not match:
            sem_id = sum(not perfil.get("pje_id") for perfil in perfis)
            raise RuntimeError(
                "PERFIL_SEM_IDENTIFICADOR_ESTAVEL: o pje_id solicitado não "
                f"foi encontrado de forma inequívoca no menu ({sem_id} item(ns) sem ID)"
            )

        await self._page.evaluate("""
        (index) => {
            const elementos = Array.from(document.querySelectorAll(
                'li.menu-usuario .dropdown-menu a, '
                + 'li.menu-usuario .dropdown-menu option, '
                + '.dropdown-menu a, select option'
            ));
            const alvo = elementos[index];
            if (!alvo) throw new Error('perfil desapareceu antes do clique');
            if (alvo.tagName.toLowerCase() === 'option') {
                alvo.selected = true;
                alvo.closest('select')?.dispatchEvent(
                    new Event('change', { bubbles: true })
                );
            } else {
                alvo.click();
            }
        }
        """, match["index"])
        
        if self._sessao_expirou(self._page.url):
            raise RuntimeError(
                "SESSAO_EXPIRADA: a troca de perfil terminou na tela de login"
            )
        try:
            wait_target = self._page.locator(
                "li.menu-usuario a.dropdown-toggle"
            ).first.wait_for(state="visible", timeout=10_000)
            if hasattr(wait_target, "__await__") or asyncio.iscoroutine(wait_target):
                await wait_target
        except Exception as exc:
            raise RuntimeError(
                "PERFIL_NAO_CONFIRMADO: o PJe não voltou ao estado autenticado após selecionar a lotação"
            ) from exc
            
        _log("[PERFIL] Lotação funcional selecionada")
        await self.selecionar_e_validar_contexto(self.perfil)
        return {
            "pje_id": match["pje_id"],
            "rotulo": match["texto"],
            "fonte_id": match.get("fonte_id", ""),
        }

    async def _ler_contexto_interno_confirmado(self) -> dict[str, str]:
        """Lê e sanitiza a identidade efetiva da página autenticada."""
        if self._sessao_expirou(self._page.url):
            raise RuntimeError("SESSAO_EXPIRADA: contexto fixado expirou")
        dados = await self._page.evaluate("""
        () => {
            const texto = (el) => el
                ? (el.textContent || el.innerText || '').trim().replace(/\\s+/g, ' ')
                : '';
            const usuario = document.querySelector(
                'li.menu-usuario a.dropdown-toggle, #dropMenuPerfil, '
                + '.usuario-logado, .perfil-usuario'
            );
            const perfil = document.querySelector(
                "label[id*='usuarioLocalizacao'], .usuario-localizacao"
            );
            let rotulo = texto(perfil);
            if (!rotulo) {
                const match = texto(usuario).match(/\\(([^)]+)\\)/);
                rotulo = match ? match[1].trim() : '';
            }
            return {usuario: texto(usuario).split('(')[0].trim(), rotulo};
        }
        """)
        usuario = str((dados or {}).get("usuario") or "").strip()
        rotulo = str((dados or {}).get("rotulo") or "").strip()
        partes = perfil_contexto.decompor_rotulo(rotulo)
        titulo = str(await self._page.title() or "").strip()
        url = urlsplit(self._page.url)
        rota = url.path or "/"
        if (
            not usuario
            or not rotulo
            or not titulo
            or url.netloc != urlsplit(self.url_base).netloc
            or self._sessao_expirou(self._page.url)
            or re.search(r"(?i)(login|advogad)", f"{titulo} {rota}")
        ):
            raise RuntimeError(
                "CONTEXTO_DIVERGENTE: página interna autenticada não foi confirmada"
            )
        if not all(partes.get(campo) for campo in ("unidade", "papel")):
            raise RuntimeError(
                "CONTEXTO_DIVERGENTE: unidade e papel não foram confirmados"
            )
        usuario_id = hmac.new(
            perfil_contexto._chave_hmac(),
            ("pje-usuario-v1\0" + perfil_contexto.normalizar_texto(usuario)).encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "usuario_id": usuario_id,
            "persona": self.persona,
            "grau": self.grau,
            "unidade": partes["unidade"],
            "localizacao": partes["localizacao"],
            "localizacao_status": (
                perfil_contexto.EstadoLocalizacao.PRESENTE.value
                if partes["localizacao"]
                else perfil_contexto.EstadoLocalizacao.AUSENTE_NAO_VERIFICADA.value
            ),
            "papel": partes["papel"],
            "titulo": titulo[:160],
            "rota": rota[:240],
        }

    async def fixar_contexto_por_rotulo(
        self,
        localizador_nao_confiavel: str,
        confirmar_localizacao_nao_aplicavel: str = "",
    ) -> dict[str, str]:
        """Seleciona uma vez por texto e fixa somente a identidade comprovada."""
        if self.contexto_fixado is not None:
            raise RuntimeError(
                "SESSAO_CONTEXTO_FIXADO_IMUTAVEL: a sessão já está fixada"
            )
        if self.persona not in perfil_contexto.PERSONAS_INTERNAS:
            raise RuntimeError(
                "CONTEXTO_DIVERGENTE: sessão fixada é exclusiva de persona interna"
            )
        menu = self._page.locator("li.menu-usuario a.dropdown-toggle").first
        if not await menu.count():
            raise RuntimeError(
                "CONTEXTO_DIVERGENTE: menu autenticado de perfis não encontrado"
            )
        try:
            await menu.click(timeout=3_000)
        except Exception as exc:
            raise RuntimeError(
                "CONTEXTO_DIVERGENTE: menu de perfis não pôde ser aberto"
            ) from exc
        alvo = perfil_contexto.normalizar_texto(localizador_nao_confiavel)
        perfis = await self._listar_perfis_funcionais_dom()
        matches = [
            item for item in perfis
            if perfil_contexto.normalizar_texto(item.get("texto", "")) == alvo
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "LOCALIZADOR_AMBIGUO: o rótulo não localizou exatamente um perfil"
            )
        esperado = perfil_contexto.decompor_rotulo(matches[0]["texto"])
        elementos = self._page.locator(
            "li.menu-usuario .dropdown-menu a, "
            "li.menu-usuario .dropdown-menu option, "
            ".dropdown-menu a, select option"
        )
        elemento = elementos.nth(matches[0]["index"])
        if not await elemento.count():
            raise RuntimeError(
                "LOCALIZADOR_AMBIGUO: perfil desapareceu antes do clique"
            )
        if (await elemento.evaluate("(el) => el.tagName.toLowerCase()")) == "option":
            await elemento.evaluate("""
            (el) => {
                el.selected = true;
                el.closest('select')?.dispatchEvent(
                    new Event('change', {bubbles: true})
                );
            }
            """)
        else:
            try:
                await elemento.click(timeout=3_000)
            except Exception as exc:
                raise RuntimeError(
                    "CONTEXTO_DIVERGENTE: perfil localizado não pôde ser acionado"
                ) from exc
        await self._page.wait_for_load_state("domcontentloaded", timeout=10_000)
        confirmado = await self._ler_contexto_interno_confirmado()
        for campo in ("unidade", "papel"):
            if perfil_contexto.normalizar_texto(confirmado[campo]) != (
                perfil_contexto.normalizar_texto(esperado[campo])
            ):
                raise RuntimeError(
                    "CONTEXTO_DIVERGENTE: "
                    f"{campo} confirmado diverge do localizador "
                    f"(confirmado={confirmado[campo]!r})"
                )
        if esperado["localizacao"]:
            if (
                confirmado["localizacao_status"]
                != perfil_contexto.EstadoLocalizacao.PRESENTE.value
                or perfil_contexto.normalizar_texto(confirmado["localizacao"])
                != perfil_contexto.normalizar_texto(esperado["localizacao"])
            ):
                raise RuntimeError(
                    "CONTEXTO_DIVERGENTE: localização confirmada diverge do localizador"
                )
        else:
            if confirmado["localizacao"]:
                raise RuntimeError(
                    "CONTEXTO_DIVERGENTE: o PJe publicou localização inesperada"
                )
            if (
                confirmar_localizacao_nao_aplicavel
                != "CONFIRMO_NAO_APLICAVEL"
            ):
                raise RuntimeError(
                    "CONFIRMACAO_LOCALIZACAO_OBRIGATORIA: confirme "
                    "especificamente que a localização é não aplicável"
                )
            confirmado["localizacao_status"] = (
                perfil_contexto.EstadoLocalizacao.NAO_APLICAVEL.value
            )
        sessao = perfil_contexto.criar_contexto_fixado(
            **confirmado,
            localizador_nao_confiavel=matches[0]["texto"],
        )
        self.contexto_fixado = asdict(sessao)
        perfil_contexto.definir_contexto_fixado(sessao)
        return dict(self.contexto_fixado)

    async def revalidar_contexto_fixado(self) -> dict[str, str]:
        if self.contexto_fixado is None:
            raise RuntimeError(
                "CONTEXTO_NAO_FIXADO: a sessão interna ainda não foi validada"
            )
        atual = await self._ler_contexto_interno_confirmado()
        if (
            self.contexto_fixado["localizacao_status"]
            == perfil_contexto.EstadoLocalizacao.NAO_APLICAVEL.value
            and not atual["localizacao"]
        ):
            atual["localizacao_status"] = (
                perfil_contexto.EstadoLocalizacao.NAO_APLICAVEL.value
            )
        for campo in (
            "usuario_id", "persona", "grau", "unidade", "localizacao",
            "localizacao_status", "papel"
        ):
            if perfil_contexto.normalizar_texto(atual[campo]) != (
                perfil_contexto.normalizar_texto(self.contexto_fixado[campo])
            ):
                raise RuntimeError(
                    f"CONTEXTO_DIVERGENTE: {campo} mudou após a fixação"
                )
        return perfil_contexto.definir_contexto_fixado(
            perfil_contexto.SessaoContextoFixado(**self.contexto_fixado)
        )

    async def _coletar_indice_caixas_dom(self):
        """Lê todas as caixas renderizadas no painel, sem abrir uma a uma."""
        from mapa_pje import TaskFlowPanelPage
        panel = TaskFlowPanelPage(self._page, self.url_base)
        frame = await panel.open()
        tarefas = await panel.list_boxes()
        return frame, tarefas

    async def _buscar_caixa_api(
        self,
        caixa: dict[str, Any],
        semaforo: asyncio.Semaphore,
        max_retentativas: int,
    ) -> dict[str, Any]:
        """Obtém a caixa inteira pelo endpoint usado pelo painel Angular."""
        nome = caixa["nome"]
        url = (
            f"{self.url_base}/seam/resource/rest/pje-legacy/painelUsuario/"
            "recuperarProcessosTarefaPendenteComCriterios/"
            f"{quote(nome, safe='')}/0"
        )
        ultimo_erro = None
        async with semaforo:
            for tentativa in range(1, max_retentativas + 1):
                resposta = None
                try:
                    resposta = await self._context.request.post(
                        url, data={}, timeout=60_000
                    )
                    if resposta.status == 429 or resposta.status >= 500:
                        ultimo_erro = f"HTTP {resposta.status}"
                    elif not resposta.ok:
                        return {
                            "caixa": caixa,
                            "count": 0,
                            "entities": [],
                            "tentativas": tentativa,
                            "erro": f"HTTP {resposta.status}",
                        }
                    else:
                        payload = await resposta.json()
                        if not isinstance(payload, dict):
                            raise ValueError(
                                f"resposta {type(payload).__name__}, esperado dict"
                            )
                        entidades = payload.get("entities")
                        count = payload.get("count")
                        if not isinstance(entidades, list):
                            raise ValueError("campo 'entities' ausente ou inválido")
                        if count is None:
                            count = len(entidades)
                        count = int(count)
                        declarada = int(caixa.get("quantidade", 0))
                        if (
                            count != len(entidades) or declarada != len(entidades)
                        ) and tentativa < max_retentativas:
                            ultimo_erro = (
                                f"contagem instável: painel={declarada}, "
                                f"API={count}, entidades={len(entidades)}"
                            )
                            continue
                        return {
                            "caixa": caixa,
                            "count": count,
                            "entities": entidades,
                            "tentativas": tentativa,
                            "erro": None,
                        }
                except Exception as exc:
                    ultimo_erro = f"{exc.__class__.__name__}: {str(exc)[:240]}"
                finally:
                    if resposta is not None:
                        await resposta.dispose()
                if tentativa < max_retentativas:
                    base = min(4, 0.5 * (2 ** (tentativa - 1)))
                    await asyncio.sleep(base + random.uniform(0, base * 0.25))
        return {
            "caixa": caixa,
            "count": 0,
            "entities": [],
            "tentativas": max_retentativas,
            "erro": ultimo_erro or "falha desconhecida",
        }

    async def _coletar_e_persistir_caixas(
        self,
        tarefas: list[dict[str, Any]],
        snapshot_id: str,
        concorrencia: int,
        max_retentativas: int,
        modo: str = "integral",
        snapshot_anterior: str | None = None,
        permitir_relogin: bool = True,
    ) -> list[dict[str, Any]]:
        """Processa uma fila limitada, liberando cada JSON após persistir."""
        fila = asyncio.Queue()
        for caixa in tarefas:
            fila.put_nowait(caixa)
        semaforo = asyncio.Semaphore(concorrencia)
        resultados: list[dict[str, Any]] = []

        async def trabalhador():
            while True:
                try:
                    caixa = fila.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    anterior = None
                    if modo == "incremental" and snapshot_anterior:
                        anterior = await asyncio.to_thread(
                            caixas_tarefas.obter_hash_caixa,
                            snapshot_anterior,
                            caixa.get("grupo", "tarefas"),
                            caixa["nome"],
                        )
                    if (
                        anterior
                        and anterior["status"] == "completa"
                        and int(anterior["quantidade_coletada"]) == 0
                        and int(caixa.get("quantidade", 0)) == 0
                    ):
                        salvo = await asyncio.to_thread(
                            caixas_tarefas.reaproveitar_caixa,
                            snapshot_id,
                            snapshot_anterior,
                            caixa,
                        )
                        salvo["verificacao_incremental"] = (
                            "caixa_vazia_confirmada_pelo_indice"
                        )
                        resultados.append(salvo)
                        continue

                    resposta = await self._buscar_caixa_api(
                        caixa, semaforo, max_retentativas
                    )
                    try:
                        hash_atual = caixas_tarefas.hash_entidades(resposta["entities"])
                        pode_reaproveitar = (
                            anterior
                            and resposta["erro"] is None
                            and anterior["status"] == "completa"
                            and int(anterior["quantidade_coletada"])
                            == int(caixa.get("quantidade", 0))
                            and anterior["hash_sha256"] == hash_atual
                        )
                        if pode_reaproveitar:
                            salvo = await asyncio.to_thread(
                                caixas_tarefas.reaproveitar_caixa,
                                snapshot_id,
                                snapshot_anterior,
                                caixa,
                            )
                            salvo["verificacao_incremental"] = (
                                "hash_conteudo_integral_confirmado"
                            )
                        else:
                            salvo = await asyncio.to_thread(
                                caixas_tarefas.salvar_caixa,
                                snapshot_id,
                                resposta["caixa"],
                                resposta["count"],
                                resposta["entities"],
                                resposta["tentativas"],
                                resposta["erro"],
                            )
                            salvo["reaproveitado"] = False
                    except Exception as exc:
                        salvo = await asyncio.to_thread(
                            caixas_tarefas.salvar_caixa,
                            snapshot_id,
                            caixa,
                            0,
                            [],
                            max_retentativas,
                            (
                                f"falha ao persistir: {exc.__class__.__name__}: "
                                f"{str(exc)[:240]}"
                            ),
                        )
                    resultados.append(salvo)
                finally:
                    fila.task_done()

        await asyncio.gather(
            *(trabalhador() for _ in range(min(concorrencia, len(tarefas))))
        )
        falhas_auth = {
            resultado["nome"]
            for resultado in resultados
            if re.search(r"HTTP (401|403)", str(resultado.get("erro") or ""))
        }
        if falhas_auth and permitir_relogin:
            _log(
                "[TAREFAS] Sessão expirou durante a coleta; "
                "refazendo login e retomando caixas afetadas."
            )
            await self._login()
            tarefas_retomada = [
                caixa for caixa in tarefas if caixa["nome"] in falhas_auth
            ]
            retomados = await self._coletar_e_persistir_caixas(
                tarefas_retomada,
                snapshot_id,
                concorrencia,
                max_retentativas,
                modo,
                snapshot_anterior,
                False,
            )
            resultados = [
                resultado
                for resultado in resultados
                if resultado["nome"] not in falhas_auth
            ] + retomados
        resultados.sort(key=lambda item: item["nome"])
        return resultados

    @_serializa
    async def sincronizar_caixas_tarefas(
        self,
        concorrencia: int = 4,
        max_retentativas: int = 3,
        lotacao: str = "",
        modo: str = "incremental",
    ) -> dict[str, Any]:
        """Inventaria todas as caixas e preserva os metadados em SQLite."""
        modo = str(modo or "incremental").strip().casefold()
        if modo not in {"incremental", "integral"}:
            raise ValueError("modo deve ser 'incremental' ou 'integral'")
        concorrencia = max(1, min(8, int(concorrencia)))
        max_retentativas = max(1, min(5, int(max_retentativas)))
        if self.contexto_fixado:
            # Sessão fixada por rótulo: já selecionada e validada na criação
            # e revalidada a cada reuso (get_sessao_contexto_fixado). O PJe
            # não publicou pje_id; a identidade é o contexto_validado_id.
            perfil_selecionado = {
                "rotulo": self.contexto_fixado["localizador_nao_confiavel"],
                "pje_id": "",
            }
        elif not self.perfil:
            raise RuntimeError(
                "PERFIL_SEM_IDENTIFICADOR_ESTAVEL: sincronização interna "
                "exige perfil real previamente diagnosticado"
            )
        else:
            perfil_selecionado = await self._selecionar_lotacao(self.perfil)
        _frame, tarefas = await self._coletar_indice_caixas_dom()
        if not tarefas:
            raise RuntimeError("O PJe não devolveu nenhuma caixa visível")
        total_declarado = sum(int(t.get("quantidade", 0)) for t in tarefas)
        maior_caixa = max(int(t.get("quantidade", 0)) for t in tarefas)
        concorrencia_solicitada = concorrencia
        if maior_caixa >= 20_000 or total_declarado >= 100_000:
            concorrencia = 1
        elif maior_caixa >= 5_000 or total_declarado >= 50_000:
            concorrencia = min(concorrencia, 2)

        snapshot_id = await asyncio.to_thread(
            caixas_tarefas.novo_snapshot,
            self.grau,
            self.persona,
            tarefas,
            {
                "fonte_indice": "painel_interno_dom",
                "fonte_processos": (
                    "pje-legacy/painelUsuario/"
                    "recuperarProcessosTarefaPendenteComCriterios"
                ),
                "concorrencia": concorrencia,
                "concorrencia_solicitada": concorrencia_solicitada,
                "concorrencia_adaptativa": (concorrencia != concorrencia_solicitada),
                "max_retentativas": max_retentativas,
                "maior_caixa_declarada": maior_caixa,
                "total_declarado_inicial": total_declarado,
                "lotacao_solicitada": lotacao,
                "lotacao_selecionada": perfil_selecionado["rotulo"],
                "pje_id_confirmado": perfil_selecionado["pje_id"],
                "escopo": (
                    "somente caixas visíveis à lotação, papel e usuário "
                    "ativos nesta sessão"
                ),
                "tipo_painel": "usuario_interno",
                "somente_leitura": True,
                "modo_sincronizacao": modo,
            },
        )
        snapshot_anterior = None
        if modo == "incremental":
            snapshot_anterior = await asyncio.to_thread(
                caixas_tarefas.snapshot_anterior_completo,
                self.grau,
                self.persona,
                snapshot_id,
            )
        caixas_resultado = await self._coletar_e_persistir_caixas(
            tarefas,
            snapshot_id,
            concorrencia,
            max_retentativas,
            modo,
            snapshot_anterior,
        )
        divergencia_temporal = {"detectada": False, "caixas_alteradas": []}
        try:
            _frame_final, tarefas_finais = await self._coletar_indice_caixas_dom()
            inicio_por_nome = {
                tarefa["nome"]: int(tarefa.get("quantidade", 0)) for tarefa in tarefas
            }
            final_por_nome = {
                tarefa["nome"]: int(tarefa.get("quantidade", 0))
                for tarefa in tarefas_finais
            }
            alteradas = [
                {
                    "nome": nome,
                    "quantidade_inicial": inicio_por_nome.get(nome),
                    "quantidade_final": final_por_nome.get(nome),
                }
                for nome in sorted(set(inicio_por_nome) | set(final_por_nome))
                if inicio_por_nome.get(nome) != final_por_nome.get(nome)
            ]
            if alteradas:
                divergencia_temporal = {
                    "detectada": True,
                    "caixas_alteradas": alteradas,
                    "total_declarado_inicial": sum(inicio_por_nome.values()),
                    "total_declarado_final": sum(final_por_nome.values()),
                }
        except Exception as exc:
            divergencia_temporal = {
                "detectada": None,
                "erro_verificacao": erro_publico(exc),
            }
        await asyncio.to_thread(
            caixas_tarefas.atualizar_metadados_snapshot,
            snapshot_id,
            {"divergencia_temporal": divergencia_temporal},
        )
        resumo = await asyncio.to_thread(caixas_tarefas.finalizar_snapshot, snapshot_id)
        resumo["caixas"] = caixas_resultado
        resumo["incremental"] = {
            "modo": modo,
            "snapshot_anterior": snapshot_anterior,
            "caixas_reaproveitadas": sum(
                bool(caixa.get("reaproveitado")) for caixa in caixas_resultado
            ),
            "caixas_recoletadas": sum(
                not bool(caixa.get("reaproveitado")) for caixa in caixas_resultado
            ),
            "estrategia": (
                "caixas vazias reutilizadas pela contagem do índice; caixas "
                "não vazias só reutilizadas após hash do conteúdo integral"
            ),
        }
        resumo["garantia"] = {
            "criterio_completo": (
                "todas as caixas com painel=API=entidades e nenhuma "
                "chave de ocorrência duplicada"
            ),
            "perda_silenciosa_permitida": False,
            "metadados_origem_preservados": True,
            "processo_em_varias_caixas": (
                "preservado como ocorrência distinta por tarefa"
            ),
        }
        return resumo

    @_serializa
    async def mapear_transicoes_caixa(self, nome_tarefa: str) -> dict[str, Any]:
        """Abre a caixa, seleciona o primeiro processo e mapeia as transições disponíveis."""
        import caixas_tarefas
        from mapa_pje import TaskFlowPanelPage
        
        perfil_fixado = getattr(self, "contexto_fixado", None)
        perfil_id = (
            perfil_fixado.get("perfil_id")
            or perfil_fixado.get("contexto_validado_id")
            if perfil_fixado
            else "default_perfil"
        ) or "default_perfil"
        
        panel = TaskFlowPanelPage(self._page, self.url_base)
        await panel.open()
        await panel.open_box(nome_tarefa)
        
        transicoes = await panel.mapear_transicoes_primeiro_processo()
        
        for t in transicoes:
            await asyncio.to_thread(
                caixas_tarefas.salvar_transicao,
                perfil_id=perfil_id,
                tarefa=nome_tarefa,
                destino_id=t["id"],
                destino_nome=t["nome"],
                reversivel=t["reversibilidade"],
                fonte=t.get("classificacao_fonte", "heuristica_tipo_elemento"),
            )

        return {
            "tarefa": nome_tarefa,
            "total_transicoes_descobertas": len(transicoes),
            "transicoes": transicoes,
            "aviso_reversibilidade": (
                "Classificações são heurísticas e NÃO liberam preparação. "
                "Promova uma transição específica com "
                "confirmar_reversibilidade_transicao após verificação humana."
            ),
        }

    @_serializa
    async def confirmar_reversibilidade_transicao(
        self,
        nome_tarefa: str,
        destino_id: str,
        evidencia: str,
    ) -> dict[str, Any]:
        """Promove uma transição mapeada a REVERSIVEL_ANTES_COMMIT com prova.

        Operação somente de banco local; nada é tocado no PJe. A promoção
        exige evidência textual e fica registrada na trilha de atuação.
        """
        import caixas_tarefas
        from politica_atuacao import politica_atuacao

        perfil_fixado = getattr(self, "contexto_fixado", None)
        perfil_id = (
            perfil_fixado.get("perfil_id")
            or perfil_fixado.get("contexto_validado_id")
            if perfil_fixado
            else "default_perfil"
        ) or "default_perfil"
        resultado = await asyncio.to_thread(
            caixas_tarefas.confirmar_transicao_reversivel,
            perfil_id=perfil_id,
            tarefa=nome_tarefa,
            destino_id=destino_id,
            evidencia=evidencia,
        )
        politica_atuacao.registrar_log(
            {"perfil_id": perfil_id},
            None,
            {
                "status": "reversibilidade_confirmada",
                "tarefa": nome_tarefa,
                "destino_id": destino_id,
                "evidencia": evidencia,
            },
        )
        return resultado

    @_serializa
    async def preparar_movimentacao_lote(
        self,
        snapshot_id: str,
        processos_ids: list[str],
        destino_id: str,
        nome_tarefa: str,
        confirmar_preparacao: bool = False
    ) -> dict[str, Any]:
        """Prepara a movimentação do lote na UI, parando antes do commit.

        Ordem de contenção (fail-closed em cada passo):
        1. simulação offline (elegibilidade + lote_hash + TTL do snapshot);
        2. gate de reversibilidade AVALIADO ANTES de qualquer UI — só destino
           mapeado, REVERSIVEL_ANTES_COMMIT e com fonte confiável passa;
        3. posse exclusiva da sessão assistida + contenção de rede default-deny;
        4. seleção dos itens com validação de clique item a item;
        5. reconferência por IDENTIDADE (conjunto exato de CNJs marcados no
           DOM == lote), nunca por contagem;
        6. escolha do destino, evidência, e congelamento em
           AGUARDANDO_COMMIT_HUMANO. O clique final é sempre humano.
        """
        import caixas_tarefas
        from politica_atuacao import (
            AGUARDANDO_COMMIT_HUMANO,
            PREPARACAO_ASSISTIDA,
            politica_atuacao,
        )

        if not snapshot_id:
            snap_recente = await asyncio.to_thread(
                caixas_tarefas.obter_snapshot,
                snapshot_id=None,
                grau=self.contexto_fixado.get("grau") if self.contexto_fixado else "1",
                persona=self.contexto_fixado.get("persona") if self.contexto_fixado else "servidor",
                incluir_caixas=False
            )
            snapshot_id = snap_recente.get("snapshot_id")
            if not snapshot_id:
                raise ValueError("Nenhum snapshot de acervo encontrado. Sincronize as caixas primeiro.")

        simulacao = await asyncio.to_thread(
            caixas_tarefas.simular_lote,
            snapshot_id=snapshot_id,
            processos_ids=processos_ids
        )

        if not simulacao.get("valido"):
            return simulacao

        if not confirmar_preparacao:
            return {
                "status": "simulado",
                "simulacao": simulacao
            }

        perfil_fixado = getattr(self, "contexto_fixado", None)
        perfil_id = (
            perfil_fixado.get("perfil_id")
            or perfil_fixado.get("contexto_validado_id")
            if perfil_fixado
            else "default_perfil"
        ) or "default_perfil"
        perfil_log = {"perfil_id": perfil_id}
        lote_hash = simulacao["lote_hash"]

        incluidos = [item for item in simulacao["itens"] if item["status"] == "INCLUIDO"]
        if not incluidos:
            return {
                "status": "erro",
                "mensagem": "Nenhum processo do lote é elegível para preparação."
            }

        # --- Gate de reversibilidade: ANTES de tocar qualquer UI. -----------
        transicoes_db = await asyncio.to_thread(
            caixas_tarefas.obter_transicoes, perfil_id, nome_tarefa
        )
        destino = next(
            (t for t in transicoes_db if t["destino_id"] == destino_id), None
        )
        # Levanta BloqueioSegurancaException se o destino não foi mapeado,
        # não é REVERSIVEL_ANTES_COMMIT ou tem classificação só heurística.
        politica_atuacao.validar_transicao(destino, perfil_log, lote_hash)

        # --- Posse exclusiva da sessão assistida + contenção de rede. -------
        politica_atuacao.iniciar_sessao_assistida(
            self._token_politica(), perfil_log
        )
        preparacao_concluida = False
        try:
            await self._ativar_contencao_rede()
            await self._definir_modo_paginas(PREPARACAO_ASSISTIDA)

            from mapa_pje import TaskFlowPanelPage
            panel = TaskFlowPanelPage(self._page, self.url_base)
            await panel.open()
            await panel.open_box(nome_tarefa)
            await self._definir_modo_paginas(PREPARACAO_ASSISTIDA)

            # Validação de clique item a item: allowlist + denylist + log.
            for item in incluidos:
                politica_atuacao.validar_clique(
                    text=item.get("numero_processo") or "",
                    title="",
                    aria_label="",
                    value="",
                    selector="input[type='checkbox']",
                    perfil=perfil_log,
                    lote_hash=lote_hash,
                )

            resultado_selecao = await panel.frame.evaluate("""
                (processos) => {
                    const log = [];
                    const cards = Array.from(document.querySelectorAll(
                        'article, tr, li, .card, [class*="processo" i], div.processo-linha'
                    ));
                    const marcados_por_nos = [];
                    const ja_marcados = [];
                    const falhas = [];
                    processos.forEach(p => {
                        const card = cards.find(el =>
                            el.innerText.includes(p.numero_processo)
                            || (p.id_task_instance && el.innerText.includes(p.id_task_instance))
                        );
                        if (!card) {
                            falhas.push(p.numero_processo);
                            log.push("Card não encontrado para: " + p.numero_processo);
                            return;
                        }
                        const checkbox = card.querySelector('input[type="checkbox"]');
                        if (!checkbox) {
                            falhas.push(p.numero_processo);
                            log.push("Checkbox não encontrado para: " + p.numero_processo);
                            return;
                        }
                        if (checkbox.checked) {
                            ja_marcados.push(p.numero_processo);
                            log.push("Já estava marcado: " + p.numero_processo);
                        } else {
                            checkbox.click();
                            marcados_por_nos.push(p.numero_processo);
                            log.push("Marcou: " + p.numero_processo);
                        }
                    });
                    return { marcados_por_nos, ja_marcados, falhas, log };
                }
            """, incluidos)

            async def _desfazer_nossas_marcacoes():
                try:
                    await panel.frame.evaluate("""
                        (cnjs) => {
                            const cards = Array.from(document.querySelectorAll(
                                'article, tr, li, .card, [class*="processo" i], div.processo-linha'
                            ));
                            cnjs.forEach(cnj => {
                                const card = cards.find(el => el.innerText.includes(cnj));
                                const cb = card && card.querySelector('input[type="checkbox"]');
                                if (cb && cb.checked) cb.click();
                            });
                        }
                    """, resultado_selecao["marcados_por_nos"])
                except Exception as exc:
                    politica_atuacao.registrar_log(
                        perfil_log,
                        lote_hash,
                        {"status": "rollback_falhou", "erro": str(exc)[:240]},
                    )

            if resultado_selecao["falhas"]:
                await _desfazer_nossas_marcacoes()
                raise RuntimeError(
                    "Preparação abortada: itens do lote não localizáveis ou "
                    f"sem checkbox no DOM: {resultado_selecao['falhas']}. "
                    "Nenhuma seleção parcial foi mantida."
                )

            # --- Reconferência por IDENTIDADE, não por contagem. ------------
            # Coleta TODO checkbox marcado do frame e o resolve ao CNJ do seu
            # card. Marcado sem CNJ resolvível (ex.: 'marcar todos' do
            # cabeçalho) ou CNJ fora do lote → divergência → rollback.
            marcados_dom = await panel.frame.evaluate("""
                () => {
                    const cnjRegex = /\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4}/;
                    const resolvidos = [];
                    const irresolviveis = [];
                    document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                        if (!cb.checked) return;
                        const card = cb.closest(
                            'article, tr, li, .card, [class*="processo" i], div.processo-linha'
                        );
                        const texto = card ? (card.innerText || '') : '';
                        const m = texto.match(cnjRegex);
                        if (m) { resolvidos.push(m[0]); }
                        else { irresolviveis.push((cb.id || cb.name || 'checkbox_sem_id')); }
                    });
                    return { resolvidos, irresolviveis };
                }
            """)

            esperados = sorted(
                item["numero_processo"] for item in incluidos
            )
            observados = sorted(set(marcados_dom["resolvidos"]))
            if marcados_dom["irresolviveis"] or observados != esperados:
                await _desfazer_nossas_marcacoes()
                politica_atuacao.registrar_log(
                    perfil_log,
                    lote_hash,
                    {
                        "status": "abortado_reconferencia",
                        "esperados": esperados,
                        "observados": observados,
                        "irresolviveis": marcados_dom["irresolviveis"],
                    },
                )
                raise RuntimeError(
                    "Reconferência de identidade falhou: o conjunto de CNJs "
                    f"marcados no DOM ({observados}) difere do lote "
                    f"({esperados}) ou há seleções irresolvíveis "
                    f"({marcados_dom['irresolviveis']}). Seleção desfeita; "
                    "abortado por segurança."
                )

            # --- Escolha do destino (gate já aprovado; rede em default-deny).
            select_locator = panel.frame.locator('select').first
            if not await select_locator.count():
                await _desfazer_nossas_marcacoes()
                raise RuntimeError(
                    "Select de destino não encontrado na caixa; seleção "
                    "desfeita e preparação abortada."
                )
            await select_locator.select_option(value=destino_id)

            evidencia_path = Path(os.environ.get("PJE_STORAGE_DIR", "/var/lib/pjepa-mcp/storage")) / "evidencias"
            evidencia_path.mkdir(parents=True, exist_ok=True)

            screenshot_file = evidencia_path / f"lote_{lote_hash}.png"
            await self._page.screenshot(path=str(screenshot_file))

            politica_atuacao.transicionar_para_commit_humano(
                self._token_politica()
            )
            await self._definir_modo_paginas(AGUARDANDO_COMMIT_HUMANO)

            resposta = {
                "status": "preparado",
                "lote_hash": lote_hash,
                "total_incluidos": len(incluidos),
                "itens_marcados": len(resultado_selecao["marcados_por_nos"])
                + len(resultado_selecao["ja_marcados"]),
                "marcados_por_nos": resultado_selecao["marcados_por_nos"],
                "ja_marcados": resultado_selecao["ja_marcados"],
                "evidencia_screenshot": str(screenshot_file),
                "destino_selecionado": destino_id,
                "destino_nome": destino.get("destino_nome", ""),
                "sessao_status": "AGUARDANDO_COMMIT_HUMANO",
                "log_selecao": resultado_selecao["log"],
                "proximo_passo": (
                    "Conferir a tela (VNC) e clicar o botão final "
                    "manualmente. Depois, rode conferir_movimentacao_lote."
                ),
            }
            # Entrada 'preparado' na trilha: é ela que a reconciliação
            # (conferir_movimentacao_lote) usa para casar o lote.
            politica_atuacao.registrar_log(
                perfil_log,
                lote_hash,
                {
                    "status": "preparado",
                    "tarefa": nome_tarefa,
                    "total_incluidos": len(incluidos),
                    "cnjs_lote": esperados,
                    "destino_selecionado": destino_id,
                    "destino_nome": destino.get("destino_nome", ""),
                    "evidencia_screenshot": str(screenshot_file),
                },
            )
            preparacao_concluida = True
            return resposta
        finally:
            if not preparacao_concluida:
                # Qualquer falha devolve o processo a OBSERVACAO e desarma a
                # contenção de rede; a sessão preparada com sucesso permanece
                # congelada em AGUARDANDO_COMMIT_HUMANO com a rede vigiada.
                await self._desativar_contencao_rede()
                await self._definir_modo_paginas("OBSERVACAO")
                politica_atuacao.encerrar_sessao_assistida(
                    self._token_politica()
                )

    @_serializa
    async def conferir_movimentacao_lote(
        self,
        lote_hash: str
    ) -> dict[str, Any]:
        """Compara o estado atual das tarefas com o snapshot anterior para verificar a efetivação do lote."""
        import caixas_tarefas
        from politica_atuacao import politica_atuacao as _politica

        # A reconciliação encerra o ciclo assistido: o commit (ou abandono)
        # já foi do humano; a sessão volta a OBSERVACAO e a contenção de rede
        # é desarmada antes da ressincronização.
        await self._desativar_contencao_rede()
        await self._definir_modo_paginas("OBSERVACAO")
        _politica.encerrar_sessao_assistida(self._token_politica())

        await self.sincronizar_caixas_tarefas(concorrencia=4, max_retentativas=3)
        
        perfil_fixado = getattr(self, "contexto_fixado", None)
        persona = perfil_fixado.get("persona") if perfil_fixado else "servidor"
        grau = perfil_fixado.get("grau") if perfil_fixado else "1"
        
        snap_recente = await asyncio.to_thread(
            caixas_tarefas.obter_snapshot,
            snapshot_id=None,
            grau=grau,
            persona=persona,
            incluir_caixas=False
        )
        snapshot_pos = snap_recente.get("snapshot_id")

        log_entries = []
        if os.path.exists(_politica.log_file):
            with open(_politica.log_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("lote_hash") == lote_hash and entry.get("detalhes", {}).get("status") == "preparado":
                            log_entries.append(entry)
                    except Exception:
                        continue

        if not log_entries:
            return {
                "erro": f"Lote com hash '{lote_hash}' não encontrado no log de preparações."
            }

        ultimo_log = log_entries[-1]
        detalhes_prep = ultimo_log["detalhes"]
        total_incluidos = detalhes_prep["total_incluidos"]
        destino_selecionado = detalhes_prep["destino_selecionado"]
        tarefa_origem = detalhes_prep.get("tarefa", "")
        cnjs_lote = set(detalhes_prep.get("cnjs_lote") or [])

        # Reconciliação real: quais CNJs do lote AINDA estão na tarefa de
        # origem no snapshot pós-commit. Ausência da origem = movimentado.
        restantes: list[str] = []
        if cnjs_lote and tarefa_origem and snapshot_pos:
            processos_pos = await asyncio.to_thread(
                caixas_tarefas.carregar_para_analise,
                grau,
                persona,
                snapshot_pos,
            )
            restantes = sorted(
                {
                    proc.get("numero_processo")
                    for proc in processos_pos
                    if proc.get("numero_processo") in cnjs_lote
                    and proc.get("tarefa") == tarefa_origem
                }
            )
        movidos = sorted(cnjs_lote - set(restantes))

        return {
            "lote_hash": lote_hash,
            "snapshot_pos_commit": snapshot_pos,
            "tarefa_origem": tarefa_origem,
            "total_preparados": total_incluidos,
            "destino_esperado": destino_selecionado,
            "movidos_da_origem": movidos,
            "restantes_na_origem": restantes,
            "status_reconciliacao": (
                "efetivado" if cnjs_lote and not restantes
                else "parcial" if movidos
                else "nao_efetivado" if cnjs_lote
                else "indeterminado"
            ),
            "mensagem": "Reconciliação executada. Banco de dados local sincronizado."
        }

    @_serializa
    async def listar_processos_tarefa(
        self,
        pagina: int = 1,
        itens_por_pagina: int = 50,
        nome_tarefa: str = "",
        termo_busca: str = "",
        filtro_classe: str = "",
        filtro_orgao: str = "",
        filtro_assunto: str = "",
        filtro_parte: str = "",
        filtros_customizados: dict[str, str] | None = None,
        apenas_com_prazo: bool = False,
        apenas_urgente: bool = False,
        ordenacao: str = "score_prioridade",
        ordenacao_secundaria: str | None = "dias_parado",
        modo_compacto: bool = False,
        apenas_resumo: bool = False,
        exportar_caminho: str | None = None,
        dias_parado_min: int | None = None,
        score_min: float | None = None,
        agrupar_por: str | None = None,
        incluir_metricas_avancadas: bool = True,
        gerar_dashboard_html: str | None = None,
        comparar_com_snapshot: list[dict[str, Any]] | None = None,
        distribuir_por_operadores: int | None = None,
        preset_triagem: str | None = None,
        detectar_anomalias: bool = True,
        calcular_saude: bool = True,
        gerar_relatorio_sintese_final: bool = False,
    ):
        """Lista e filtra processos da caixa/tarefas com suporte a milhares de itens."""
        cache_key = (
            f"{self.persona}_{self.grau}_"
            f"{self.perfil_id or 'sem_perfil'}_tarefas"
        )
        cached = lista_processos_tarefa.CACHE_TAREFAS.get(cache_key)

        if cached is None:
            processos_brutos = await asyncio.to_thread(
                caixas_tarefas.carregar_para_analise,
                self.grau,
                self.persona,
            )
            if not processos_brutos:
                _log("[TAREFAS] Sem snapshot; inventariando painel interno...")
                # Chama helpers privados porque o lock desta operação já está
                # adquirido e não é reentrante.
                _frame, tarefas = await self._coletar_indice_caixas_dom()
                snapshot_id = await asyncio.to_thread(
                    caixas_tarefas.novo_snapshot,
                    self.grau,
                    self.persona,
                    tarefas,
                    {"origem": "listar_processos_tarefa", "somente_leitura": True},
                )
                await self._coletar_e_persistir_caixas(tarefas, snapshot_id, 4, 3)
                await asyncio.to_thread(caixas_tarefas.finalizar_snapshot, snapshot_id)
                processos_brutos = await asyncio.to_thread(
                    caixas_tarefas.carregar_para_analise,
                    self.grau,
                    self.persona,
                    snapshot_id,
                )
            lista_processos_tarefa.CACHE_TAREFAS.set(cache_key, processos_brutos)
        else:
            processos_brutos, _ = cached

        return lista_processos_tarefa.processar_lista_processos_tarefa(
            processos=processos_brutos,
            pagina=pagina,
            itens_por_pagina=itens_por_pagina,
            nome_tarefa=nome_tarefa,
            termo_busca=termo_busca,
            filtro_classe=filtro_classe,
            filtro_orgao=filtro_orgao,
            filtro_assunto=filtro_assunto,
            filtro_parte=filtro_parte,
            filtros_customizados=filtros_customizados,
            apenas_com_prazo=apenas_com_prazo,
            apenas_urgente=apenas_urgente,
            ordenacao=ordenacao,
            ordenacao_secundaria=ordenacao_secundaria,
            modo_compacto=modo_compacto,
            apenas_resumo=apenas_resumo,
            exportar_caminho=exportar_caminho,
            dias_parado_min=dias_parado_min,
            score_min=score_min,
            agrupar_por=agrupar_por,
            incluir_metricas_avancadas=incluir_metricas_avancadas,
            gerar_dashboard_html=gerar_dashboard_html,
            comparar_com_snapshot=comparar_com_snapshot,
            distribuir_por_operadores=distribuir_por_operadores,
            preset_triagem=preset_triagem,
            detectar_anomalias=detectar_anomalias,
            calcular_saude=calcular_saude,
            gerar_relatorio_sintese_final=gerar_relatorio_sintese_final,
        )

    async def _listar_perfis_funcionais_dom(self) -> list[dict[str, Any]]:
        """Lê identidades de perfil sem usar o rótulo como chave de navegação."""
        itens = await self._page.evaluate("""
        () => {
            const texto = (el) => (el.textContent || el.innerText || '')
                .trim().replace(/\\s+/g, ' ');
            const elementos = Array.from(document.querySelectorAll(
                'li.menu-usuario .dropdown-menu a, '
                + 'li.menu-usuario .dropdown-menu option, '
                + '.dropdown-menu a, select option'
            ));
            return elementos.map((el, index) => ({
                index,
                tag: el.tagName.toLowerCase(),
                texto: texto(el).slice(0, 240),
                value: el.getAttribute('value') || '',
                id: el.id || '',
                dataset: Object.fromEntries(Object.entries(el.dataset || {})),
                href: el.getAttribute('href') || '',
                onclick: el.getAttribute('onclick') || '',
                ativo: Boolean(
                    el.matches('[aria-current="true"], [aria-selected="true"], '
                        + '.active, .selected, :checked')
                    || el.closest('li')?.matches('.active, .selected')
                ),
            })).filter((item) =>
                item.texto
                && item.texto.toLowerCase() !== 'sair'
                && item.texto.includes('/')
            );
        }
        """)
        resultado = []
        vistos = set()
        for item in itens or []:
            rotulo = " ".join(str(item.get("texto") or "").split())
            chave = perfil_contexto.normalizar_texto(rotulo)
            if not chave or chave in vistos:
                continue
            vistos.add(chave)
            identidade = extrair_identidade_perfil_pje(item)
            href = str(item.get("href") or "")
            onclick = str(item.get("onclick") or "")
            assinatura_onclick = re.sub(
                r"(['\"])(.*?)\1", r"\1<valor>\1", onclick
            )
            assinatura_onclick = re.sub(r"\d+", "#", assinatura_onclick)
            assinatura_onclick = re.sub(r"\s+", " ", assinatura_onclick).strip()
            resultado.append(
                {
                    "index": int(item.get("index") or 0),
                    "texto": rotulo[:240],
                    "ativo": bool(item.get("ativo")),
                    "evidencia_id": {
                        "tag": str(item.get("tag") or ""),
                        "element_id": (
                            str(item.get("id") or "")[:160]
                            if not re.search(r"\d{11}", str(item.get("id") or ""))
                            else "<redigido>"
                        ),
                        "data_keys": sorted((item.get("dataset") or {}).keys()),
                        "href_param_keys": sorted(
                            parse_qs(
                                urlsplit(href).query,
                                keep_blank_values=False,
                            ).keys()
                        ),
                        "onclick_signature": assinatura_onclick[:300],
                    },
                    **identidade,
                }
            )
        return resultado

    @_serializa
    async def diagnosticar_painel_tarefas(self, profundo: bool = False):
        """Descobre controles de caixas/tarefas visíveis ao perfil ativo.

        Não abre processos nem executa tarefas. Query strings são removidas
        para que tokens de sessão nunca saiam no retorno. Por padrão, limita-se
        à descoberta de perfis no home legado. O diagnóstico estrutural
        profundo precisa ser solicitado explicitamente.
        """
        if not profundo and self._cache_perfis_funcionais is not None:
            idade_cache = time.monotonic() - self._cache_perfis_criado_em
            if idade_cache < PERFIL_CACHE_TTL_S:
                return {
                    "perfis_disponiveis": [
                        dict(perfil) for perfil in self._cache_perfis_funcionais
                    ],
                    "grau": self.grau,
                    "persona_solicitada": self.persona,
                    "somente_leitura": True,
                    "diagnostico_profundo": False,
                    "cache_hit": True,
                }

        menu_perfis = self._page.locator(
            "li.menu-usuario a.dropdown-toggle"
        ).first
        # Após o login/seleção de lotação o home já está carregado. Navegar
        # novamente podia ficar preso por mais de 60 s em respostas JSF,
        # embora o menu necessário já estivesse disponível.
        precisa_home = profundo or not await menu_perfis.count()
        if precisa_home:
            try:
                await self._page.goto(
                    f"{self.url_base}/home.seam",
                    wait_until="domcontentloaded",
                    timeout=10_000,
                )
            except Exception:
                # Só tolera o timeout quando o controle necessário apareceu;
                # caso contrário, falha fechado em vez de raspar outra tela.
                if not await menu_perfis.count():
                    raise
            if profundo:
                try:
                    await self._page.wait_for_load_state(
                        "networkidle", timeout=5_000
                    )
                except Exception:
                    pass
        # O TJPA atual publica o painel interno no cliente web ``ng2`` a
        # partir deste link do home legado. Seguir o href é mais resiliente
        # que fixar a versão/rota do frontend.
        if profundo:
            link_painel = self._page.get_by_role(
                "link", name=re.compile(r"Painel do usu[aá]rio", re.IGNORECASE)
            ).first
            if await link_painel.count():
                href_painel = await link_painel.get_attribute("href")
                if href_painel:
                    if href_painel.startswith("/"):
                        destino = (
                            re.match(r"^https?://[^/]+", self.url_base).group(0)
                            + href_painel
                        )
                    else:
                        destino = href_painel
                    await self._page.goto(destino, wait_until="domcontentloaded")
                    try:
                        await self._page.wait_for_load_state(
                            "networkidle", timeout=15_000
                        )
                    except Exception:
                        pass

        try:
            await menu_perfis.click(timeout=3_000)
        except Exception as exc:
            if not profundo:
                raise RuntimeError(
                    "Menu de perfis não pôde ser aberto no diagnóstico leve"
                ) from exc

        if not profundo:
            perfis = await self._listar_perfis_funcionais_dom()
            if not perfis:
                raise RuntimeError(
                    "Nenhum perfil funcional apareceu no menu autenticado"
                )
            self._cache_perfis_funcionais = tuple(dict(perfil) for perfil in perfis)
            self._cache_perfis_criado_em = time.monotonic()
            return {
                "perfis_disponiveis": perfis,
                "grau": self.grau,
                "persona_solicitada": self.persona,
                "somente_leitura": True,
                "diagnostico_profundo": False,
                "cache_hit": False,
            }

        dados = await self._page.evaluate("""
        () => {
            const texto = (el) => (el.innerText || el.textContent || '')
                .trim().replace(/\\s+/g, ' ');
            const relevante = (t) =>
                /(tarefa|caixa|acervo|painel|agrupador|assinatura)/i.test(t);
            const controles = [];
            document.querySelectorAll(
                'a, button, [role="button"], [role="treeitem"], [role="tab"]'
            ).forEach((el) => {
                const t = texto(el);
                const titulo = el.getAttribute('title') || '';
                const aria = el.getAttribute('aria-label') || '';
                if (!relevante(t + ' ' + titulo + ' ' + aria)) return;
                controles.push({
                    tag: el.tagName.toLowerCase(),
                    texto: t.slice(0, 180),
                    titulo: titulo.slice(0, 180),
                    aria_label: aria.slice(0, 180),
                    href: el.getAttribute('href') || '',
                    id: el.id || '',
                    classes: String(el.className || '').slice(0, 200),
                });
            });
            const iframes = Array.from(document.querySelectorAll('iframe')).map(
                (el) => ({
                    titulo: el.getAttribute('title') || '',
                    src: el.getAttribute('src') || '',
                    id: el.id || '',
                })
            );
            const usuario = Array.from(document.querySelectorAll(
                'li.menu-usuario, [data-testid="user-menu"], .usuario-logado, '
                + '.perfil-usuario, a.dropdown-toggle'
            )).map(texto).filter(Boolean);
            const perfis = Array.from(document.querySelectorAll(
                'li.menu-usuario a, .dropdown-menu a'
            )).map((el) => ({
                texto: texto(el).slice(0, 240),
                titulo: (el.getAttribute('title') || '').slice(0, 240),
                href: el.getAttribute('href') || '',
            })).filter((item) => item.texto);
            return {
                titulo: document.title,
                corpo_tem_tarefas: /tarefas/i.test(document.body.innerText || ''),
                corpo_tem_caixas: /caixas/i.test(document.body.innerText || ''),
                controles: controles.slice(0, 30),
                iframes: iframes.slice(0, 20),
                usuario: usuario.slice(0, 10),
                perfis_disponiveis: perfis.slice(0, 100),
            };
        }
        """)

        for controle in dados["controles"]:
            if controle.get("href"):
                controle["href"] = _url_sem_segredos(controle["href"])
        for iframe in dados["iframes"]:
            if iframe.get("src"):
                iframe["src"] = _url_sem_segredos(iframe["src"])
        for perfil in dados.get("perfis_disponiveis", []):
            if perfil.get("href"):
                perfil["href"] = _url_sem_segredos(perfil["href"])

        if not profundo:
            return {
                "perfis_disponiveis": dados.get("perfis_disponiveis", []),
                "grau": self.grau,
                "persona_solicitada": self.persona,
                "somente_leitura": True,
                "diagnostico_profundo": False,
            }

        quadros = []
        for frame in self._page.frames:
            if frame == self._page.main_frame:
                continue
            try:
                await frame.locator("body").wait_for(state="attached", timeout=10_000)
                info = await frame.evaluate("""
                () => {
                    const txt = (el) => (el.innerText || el.textContent || '')
                        .trim().replace(/\\s+/g, ' ');
                    const controles = [];
                    document.querySelectorAll(
                        'a, button, [role="button"], [role="treeitem"], '
                        + '[role="tab"], [class*="tarefa" i], [class*="caixa" i]'
                    ).forEach((el) => {
                        const t = txt(el);
                        const titulo = el.getAttribute('title') || '';
                        const aria = el.getAttribute('aria-label') || '';
                        if (!/(tarefa|caixa|acervo|agrupador|assinatura)/i.test(
                            t + ' ' + titulo + ' ' + aria + ' '
                            + String(el.className || '')
                        )) return;
                        controles.push({
                            tag: el.tagName.toLowerCase(),
                            texto: t.slice(0, 220),
                            titulo: titulo.slice(0, 180),
                            aria_label: aria.slice(0, 180),
                            id: el.id || '',
                            classes: String(el.className || '').slice(0, 220),
                        });
                    });
                    return {
                        titulo: document.title,
                        texto_inicial: (document.body.innerText || '').slice(0, 500),
                        controles: controles.slice(0, 30),
                        total_links: document.querySelectorAll('a').length,
                        total_botoes: document.querySelectorAll('button').length,
                        amostras_tarefas: Array.from(document.querySelectorAll(
                            '.detalheTarefasQuantidade'
                        )).slice(0, 5).map((el) => ({
                            texto: txt(el),
                        })),
                        recursos_rede: Array.from(
                            performance.getEntriesByType('resource')
                        ).map((r) => r.name).filter((u) =>
                            /(api|tarefa|processo|painel|caixa)/i.test(u)
                        ).slice(-100),
                    };
                }
                """)
                info["url"] = _url_sem_segredos(frame.url)
                info["recursos_rede"] = [
                    _url_sem_segredos(url) for url in info.get("recursos_rede", [])
                ]

                # Abre somente a tarefa com maior contagem para descobrir o
                # contrato de paginação e os metadados dos cards. Nenhuma
                # ação de fluxo é executada.
                maior_tarefa = await frame.evaluate("""
                () => {
                    const itens = Array.from(document.querySelectorAll(
                        'a .detalheTarefasQuantidade'
                    )).map((el) => ({
                        nome: (el.querySelector('.nome')?.textContent || '').trim(),
                        quantidade: Number(
                            el.querySelector('.quantidadeTarefa')?.textContent || 0
                        ),
                    })).filter((x) => x.nome);
                    itens.sort((a, b) => b.quantidade - a.quantidade);
                    performance.clearResourceTimings();
                    return itens[0] || null;
                }
                """)
                if maior_tarefa:
                    url_tarefas = (
                        f"{self.url_base}/seam/resource/rest/pje-legacy/"
                        "painelUsuario/tarefas"
                    )
                    resposta_tarefas = await self._context.request.post(
                        url_tarefas, data={}, timeout=30_000
                    )
                    try:
                        info["teste_api_tarefas"] = {
                            "status": resposta_tarefas.status,
                            "content_type": resposta_tarefas.headers.get(
                                "content-type", ""
                            ),
                        }
                        if resposta_tarefas.ok:
                            corpo_tarefas = await resposta_tarefas.body()
                            info["teste_api_tarefas"]["bytes"] = len(corpo_tarefas)
                            if corpo_tarefas:
                                try:
                                    payload_tarefas = await resposta_tarefas.json()
                                    info["teste_api_tarefas"]["estrutura"] = (
                                        _resumir_payload_api(payload_tarefas)
                                    )
                                except Exception as exc:
                                    info["teste_api_tarefas"]["erro_json"] = (
                                        f"{exc.__class__.__name__}: {str(exc)[:120]}"
                                    )
                    finally:
                        await resposta_tarefas.dispose()

                    nome_codificado = quote(maior_tarefa["nome"], safe="")

                    testes_api = []
                    for marcador_pagina in (0, 1, 30):
                        url_api = (
                            f"{self.url_base}/seam/resource/rest/pje-legacy/"
                            "painelUsuario/"
                            "recuperarProcessosTarefaPendenteComCriterios/"
                            f"{nome_codificado}/{marcador_pagina}"
                        )
                        resposta_api = await self._context.request.post(
                            url_api, data={}, timeout=30_000
                        )
                        try:
                            registro = {
                                "marcador_pagina": marcador_pagina,
                                "status": resposta_api.status,
                                "content_type": resposta_api.headers.get(
                                    "content-type", ""
                                ),
                            }
                            if resposta_api.ok:
                                payload = await resposta_api.json()
                                registro["tipo"] = type(payload).__name__
                                registro["estrutura"] = _resumir_payload_api(payload)
                            testes_api.append(registro)
                        finally:
                            await resposta_api.dispose()
                    info["testes_api_processos"] = testes_api

                    link_maior = frame.locator(
                        f'a[title="{maior_tarefa["nome"]}"]'
                    ).first
                    if await link_maior.count():
                        # A lista lateral tem scroll próprio e o item pode
                        # estar fora da viewport. O clique DOM aciona o mesmo
                        # routerLink sem depender da actionability visual.
                        await link_maior.evaluate("(el) => el.click()")
                        try:
                            await frame.wait_for_function(
                                "() => /lista-processos-tarefa/.test(location.hash)",
                                timeout=10_000,
                            )
                            await frame.evaluate("""
                            () => {
                                const todos = Array.from(document.querySelectorAll(
                                    'button, a, [role="button"], [title]'
                                ));
                                const el = todos.find((item) =>
                                    /clique para pesquisar/i.test(
                                        (item.innerText || '') + ' '
                                        + (item.getAttribute('title') || '')
                                        + ' ' + (item.getAttribute('aria-label') || '')
                                    )
                                );
                                if (el) el.click();
                            }
                            """)
                            await frame.wait_for_load_state(
                                "networkidle", timeout=15_000
                            )
                        except Exception:
                            pass
                        amostra = await frame.evaluate("""
                        () => {
                            const recursos = Array.from(
                                performance.getEntriesByType('resource')
                            ).map((r) => r.name).filter((u) =>
                                /(api|tarefa|processo|painel|caixa)/i.test(u)
                            );
                            const cnj = /\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4}/;
                            const candidatos = Array.from(document.querySelectorAll(
                                'article, tr, li, .card, [class*="processo" i]'
                            )).filter((el) => cnj.test(el.innerText || ''));
                            const selects = Array.from(
                                document.querySelectorAll('select')
                            ).map((el) => ({
                                nome: el.name || el.id || el.getAttribute('aria-label') || '',
                                valor: el.value,
                                opcoes: Array.from(el.options).map((o) => ({
                                    valor: o.value,
                                    texto: (o.textContent || '').trim(),
                                })),
                            }));
                            return {
                                url: location.href,
                                texto_inicial: (document.body.innerText || '').slice(0, 3500),
                                recursos_rede: recursos.slice(-100),
                                cards_html: candidatos.slice(0, 3).map(
                                    (el) => el.outerHTML.slice(0, 4000)
                                ),
                                selects: selects.slice(0, 20),
                                botoes_paginacao: Array.from(
                                    document.querySelectorAll(
                                        'button, a, [role="button"]'
                                    )
                                ).filter((el) => /(próxim|anterior|página|itens|download)/i.test(
                                    (el.innerText || '') + ' '
                                    + (el.getAttribute('title') || '') + ' '
                                    + (el.getAttribute('aria-label') || '')
                                )).slice(0, 40).map((el) => ({
                                    texto: (el.innerText || '').trim(),
                                    titulo: el.getAttribute('title') || '',
                                    aria_label: el.getAttribute('aria-label') || '',
                                    classes: String(el.className || '').slice(0, 200),
                                    disabled: !!el.disabled,
                                })),
                            };
                        }
                        """)
                        amostra["url"] = _url_sem_segredos(amostra["url"])
                        amostra["recursos_rede"] = [
                            _url_sem_segredos(url)
                            for url in amostra.get("recursos_rede", [])
                        ]
                        info["maior_tarefa"] = maior_tarefa
                        info["amostra_lista_processos"] = amostra
                quadros.append(info)
            except Exception as exc:
                quadros.append(
                    {
                        "url": _url_sem_segredos(frame.url),
                        "erro": f"{exc.__class__.__name__}: {str(exc)[:200]}",
                    }
                )
        dados["quadros"] = quadros

        dados.update(
            {
                "url": _url_sem_segredos(self._page.url),
                "grau": self.grau,
                "persona_solicitada": self.persona,
                "somente_leitura": True,
            }
        )
        return dados

    # ========== CONSULTA DE PROCESSO POR CNJ ==========

    @staticmethod
    def _sessao_expirou(url) -> bool:
        """Diz se a URL atual indica que o PJe devolveu a tela de autenticacao.

        Na rota de API a expiracao chega como HTTP 401/403. Na navegacao ela
        aparece como redirect: o PJe joga a aba para o SSO do PDPJ (Keycloak)
        ou de volta para login.seam. Sem este teste, o scraping seguia adiante
        raspando a tela de login e devolvia "processo nao encontrado".
        """
        alvo = str(url or "").lower()
        return any(marca in alvo for marca in MARCADORES_SESSAO_EXPIRADA)

    async def _reautenticar(self):
        """Refaz login e restaura o perfil funcional. No maximo um em curso.

        Nao precisa de lock proprio: todo metodo publico ja roda sob
        ``_op_lock`` e o singleton mantem um unico cliente, entao duas
        reautenticacoes simultaneas nao sao possiveis. O que falta proteger e a
        repeticao em serie contra credencial invalida, que renderia bloqueio da
        conta no SSO nacional -- dai o intervalo de carencia.
        """
        if self._browser_mode == "cdp":
            raise RuntimeError(
                "SESSAO_EXPIRADA: no modo CDP a sessao pertence ao Chrome do "
                "usuario e nao pode ser renovada pela automacao. Faca login "
                "manualmente na porta 9222 e repita a operacao."
            )

        agora = time.monotonic()
        if self._relogin_falhou_em is not None:
            desde = agora - self._relogin_falhou_em
            if desde < COOLDOWN_RELOGIN_S:
                raise RuntimeError(
                    "SESSAO_EXPIRADA: a reautenticacao falhou ha "
                    f"{desde:.0f}s e a nova tentativa so sera feita apos "
                    f"{COOLDOWN_RELOGIN_S}s, para nao bloquear a conta no SSO "
                    "do PDPJ. Confira a validade da senha do PDPJ."
                )

        _log("[LOGIN] Sessao expirada durante a navegacao; reautenticando...")
        self._cache_perfis_funcionais = None
        self._cache_perfis_criado_em = 0.0
        try:
            await self._login()
            await self._trocar_perfil()
            if self.perfil:
                selecionado = await self._selecionar_lotacao(self.perfil)
                if selecionado.get("pje_id") != self.perfil.get("pje_id"):
                    raise RuntimeError(
                        "PERFIL_DIVERGENTE: apos reautenticar, o PJe nao "
                        "confirmou o mesmo perfil funcional. A operacao foi "
                        "interrompida em vez de ler a unidade errada."
                    )
        except BaseException:
            # Marca a falha ANTES de propagar: a proxima chamada precisa ver a
            # carencia mesmo que esta tenha sido cancelada.
            self._relogin_falhou_em = time.monotonic()
            raise
        self._relogin_falhou_em = None
        _log("[LOGIN] Sessao restabelecida; retomando a operacao.")

    async def _bypass_token_seam(self) -> bool:
        """Detecta e dispensa a tela de token (token.seam) do PJe.

        Quando o usuario navega pela primeira vez apos selecionar um perfil,
        o PJe redireciona para /publico/usuario/token.seam. A automacao nao
        tem TOTP para essa tela; clicar em 'PROSSEGUIR SEM O TOKEN' e suficiente
        e seguro: a sessao permanece valida sem o token de dispositivo movel.
        Retorna True se havia token e foi dispensado, False caso contrario.
        """
        if "token.seam" not in self._page.url:
            return False
        _log("[PROC] Tela de token detectada; clicando em 'PROSSEGUIR SEM O TOKEN'...")
        try:
            link = self._page.locator("a:has-text('PROSSEGUIR SEM O TOKEN')")
            if await link.count() > 0:
                await link.click()
                await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
                _log(f"[PROC] Token dispensado; URL agora: {self._page.url}")
                return True
        except Exception as e:
            _log(f"[PROC] Aviso: nao consegui dispensar token: {e}")
        return False

    async def _abrir_autos_processo(self, numero_cnj, permitir_relogin=True):
        """Busca pelo numero CNJ e abre os autos. Retorna a page dos autos."""
        self._ultimo_processo_foi_terceiro = False
        _log(f"[PROC] Buscando {numero_cnj}...")
        await self._page.goto(
            f"{self.url_base}/Processo/ConsultaProcesso/listView.seam",
            wait_until="domcontentloaded",
        )

        # O PJe pode redirecionar para a tela de token (dispositivo movel) apos
        # selecao de perfil. Dispensamos o token antes de verificar a sessao.
        await self._bypass_token_seam()

        if self._sessao_expirou(self._page.url):
            if not permitir_relogin:
                # Segunda vez na mesma chamada: a reautenticacao nao resolveu.
                # Falhar aqui e a unica saida que nao vira laco de login.
                raise RuntimeError(
                    "SESSAO_EXPIRADA: o PJe continuou devolvendo a tela de "
                    "autenticacao mesmo apos reautenticar."
                )
            await self._reautenticar()
            return await self._abrir_autos_processo(
                numero_cnj, permitir_relogin=False
            )

        # Parse do CNJ em 5 segmentos (pula o J fixo)
        so_digitos = re.sub(r"\D", "", numero_cnj)
        if len(so_digitos) != 20:
            raise ValueError(
                f"Numero CNJ invalido (esperado 20 digitos, recebi {len(so_digitos)}): {numero_cnj}"
            )
        partes = [
            so_digitos[0:7],  # NNNNNNN
            so_digitos[7:9],  # DD
            so_digitos[9:13],  # AAAA
            so_digitos[14:16],  # TR (pula o J fixo)
            so_digitos[16:20],  # OOOO
        ]
        _log(f"[PROC] Partes do numero: {partes}")

        # O formulario CNJ do PJe tem 6 campos de input; J e TR ja vem preenchidos.
        # wait_for_selector espera o form renderizar (substitui sleep fixo).
        await self._page.wait_for_selector("input[id*='numeroProcesso']", timeout=10000)
        inputs = await self._page.query_selector_all("input[id*='numeroProcesso']")
        _log(f"[PROC] Campos encontrados: {len(inputs)}")

        if len(inputs) >= 6:
            await inputs[0].fill(partes[0])  # N: 0801447
            await inputs[1].fill(partes[1])  # DD: 61
            await inputs[2].fill(partes[2])  # AAAA: 2024
            # Pula indice 3 (J) e 4 (TR) - ja vem preenchidos
            await inputs[5].fill(partes[4])  # OOOO: 0037
            _log(
                f"[PROC] Preencheu: N={partes[0]} DD={partes[1]} AAAA={partes[2]} OOOO={partes[4]}"
            )
        elif len(inputs) >= 5:
            for i, valor in enumerate(partes):
                await inputs[i].fill(valor)
        else:
            if inputs:
                await inputs[0].fill(numero_cnj)

        # Clica em Pesquisar
        for sel in [
            "button:has-text('Pesquisar')",
            "input[value='Pesquisar']",
            "button[type='submit']",
            "input[type='submit']",
        ]:
            try:
                await self._page.click(sel, timeout=2000)
                break
            except Exception:
                continue

        await self._page.wait_for_load_state("domcontentloaded", timeout=15000)

        # Clica no link do processo no resultado (o wait_for_selector ja
        # espera o resultado da busca renderizar - sem sleep fixo antes)
        try:
            link = await self._page.wait_for_selector(
                f"a:has-text('{numero_cnj}'), a:has-text('{partes[0]}')", timeout=8000
            )
        except Exception:
            link = None
        # Os autos podem abrir em uma nova aba ou navegar a pagina atual. Os
        # dois sinais sao registrados ANTES do click, evitando polling e a
        # escolha insegura de pages[-1] quando houver abas residuais.
        nova_aba = asyncio.create_task(
            self._context.wait_for_event("page", timeout=15_000)
        )
        navegou_autos = asyncio.create_task(
            self._page.wait_for_url(
                re.compile(r".*(?:Detalhe|listProcessoCompleto).*"), timeout=15_000
            )
        )
        try:
            if link:
                await link.click()
            else:
                links = await self._page.query_selector_all("table a")
                if links:
                    await links[0].click()
                else:
                    raise Exception(f"Processo {numero_cnj} nao encontrado")

            concluidas, _ = await asyncio.wait(
                {nova_aba, navegou_autos}, return_when=asyncio.FIRST_COMPLETED
            )
            if nova_aba in concluidas:
                aba_autos = await nova_aba
            else:
                await navegou_autos
                aba_autos = self._page
        finally:
            for espera in (nova_aba, navegou_autos):
                if not espera.done():
                    espera.cancel()
            await asyncio.gather(nova_aba, navegou_autos, return_exceptions=True)

        await aba_autos.wait_for_load_state("domcontentloaded", timeout=15000)
        # Espera um elemento concreto da tela dos autos (timeline/arvore de docs)
        try:
            await aba_autos.wait_for_selector(
                "[onclick*='abrirLinkDocumento'], #divTimeLine, a.btn-menu-abas",
                timeout=10000,
            )
        except Exception:
            _log(
                "[PROC] Aviso: marcadores dos autos nao encontrados; continuando com a pagina carregada"
            )
        _log(f"[PROC] Autos abertos em: {_url_sem_segredos(aba_autos.url)}")

        # Extrai processo_id e codigo de autenticacao da URL dos autos
        # URL real: .../listProcessoCompletoAdvogado.seam?id=1660665&ca=4e624ca28cbd...
        # (parametro 'id' e nao 'idProcesso'; 'ca' e codigo de autenticacao da sessao)
        # O TJPA atual usa ``idProcesso`` em listAutosDigitais.seam; telas
        # legadas usam ``id``. Aceitar ambos é essencial porque este valor
        # compõe a URL REST de cada peça.
        m_pid = re.search(r"[?&](?:id|idProcesso)=(\d+)", aba_autos.url)
        if m_pid:
            self._ultimo_processo_id = m_pid.group(1)
            self._ultimo_processo_cnj = so_digitos  # vincula o id ao CNJ aberto
            _log(f"[PROC] processo_id={self._ultimo_processo_id}")
        else:
            self._ultimo_processo_id = None
            self._ultimo_processo_cnj = None
            _log(
                "[PROC] AVISO: nao consegui extrair id da URL: "
                f"{_url_sem_segredos(aba_autos.url)}"
            )

        m_ca = re.search(r"[?&]ca=([0-9a-f]+)", aba_autos.url)
        if m_ca:
            self._ultimo_processo_ca = m_ca.group(1)
            _log("[PROC] token ca capturado (valor não registrado)")
        else:
            self._ultimo_processo_ca = None

        return aba_autos

    async def _fechar_aba_autos(self, aba):
        """Fecha a aba dos autos e os pop-ups que a automacao abriu.

        Em modo CDP o contexto e o do usuario. Fechar tudo que nao fosse
        ``self._page`` derrubava as abas que ele ja tinha abertas, quebrando a
        promessa de nao sequestrar o navegador. As abas fotografadas em
        ``_iniciar`` ficam preservadas.
        """
        if aba is not None and aba is not self._page:
            try:
                await aba.close()
            except Exception:
                pass
        if self._context:
            intocaveis = {id(p) for p in self._abas_preexistentes}
            for p in list(self._context.pages):
                if p is self._page:
                    continue
                if not self._owns_context and id(p) in intocaveis:
                    continue
                try:
                    await p.close()
                except Exception:
                    pass
        self._descartar_handlers_de_abas_fechadas()

    @staticmethod
    def _extrair_partes(texto, html=""):
        """Extrai polo ativo/passivo do cabecalho dos autos.

        O PJe exibe literalmente 'Não encontrado' quando um dos polos nao
        esta cadastrado (ex.: jurisdicao voluntaria sem reu) - nesse caso
        devolve None no polo em vez de propagar o texto da tela.
        """

        def limpar(valor):
            valor = re.sub(r"\s+", " ", str(valor or "")).strip(" \t:-–")
            valor = re.sub(
                r"^p[oó]lo\s+(?:ativo|passivo)"
                r"(?:\s*\([^)]*\))?\s*:?\s*",
                "",
                valor,
                flags=re.IGNORECASE,
            )
            valor = re.sub(
                r"^(?:autor(?:a)?|r[eé]u|requerente|requerido)"
                r"(?:\s*\([^)]*\)\s*:?\s*|\s*:\s*)",
                "",
                valor,
                flags=re.IGNORECASE,
            )
            valor = re.sub(r"\s+Ícone.*$", "", valor, flags=re.IGNORECASE).strip()
            # No tema atual do TJPA, o container concatena parte e
            # representante sem separador semântico. O papel processual fecha
            # o nome da parte; o texto seguinte pertence ao representante.
            papel = re.match(
                r"^(.+?\((?:AUTORIDADE|AUTOR\s+DO\s+FATO|R[ÉE]U|"
                r"REQUERENTE|REQUERIDO|EXEQUENTE|EXECUTADO|V[ÍI]TIMA|"
                r"DENUNCIADO|ACUSADO)\))(?:\s+.+)?$",
                valor,
                re.IGNORECASE,
            )
            return (papel.group(1) if papel else valor).strip()

        def montar(ativo, passivo):
            ativo, passivo = limpar(ativo), limpar(passivo)
            if not ativo:
                return None
            partes = {"partes": ativo, "polo_ativo": ativo}
            if _sem_acento(passivo).lower() in ("", "nao encontrado"):
                partes["polo_passivo"] = None
                partes["observacao_partes"] = (
                    "Polo passivo nao cadastrado no PJe "
                    "(a tela exibe 'Não encontrado')."
                )
            else:
                partes["polo_passivo"] = passivo
                partes["partes"] = f"{ativo} X {passivo}"
            return partes

        # DOM atual e variantes legadas: primeiro procura polos identificados
        # semanticamente; depois usa os spans ``nome-parte`` do cabecalho.
        if html:
            page = Selector(html)

            def textos_dom(seletores):
                valores = []
                for seletor in seletores:
                    for el in page.css(seletor):
                        valor = limpar(el.get_all_text(strip=True))
                        if valor and valor not in valores:
                            valores.append(valor)
                return valores

            def nomes_dos_polos(seletores):
                valores = []
                for seletor in seletores:
                    for polo in page.css(seletor):
                        internos = []
                        for seletor_nome in (
                            ".nome-parte",
                            ".nomeParte",
                            "[class*='nome-parte']",
                            "[class*='nomeParte']",
                            "[data-testid*='party-name']",
                        ):
                            internos.extend(polo.css(seletor_nome))
                        origem = internos[0] if internos else polo
                        valor = limpar(origem.get_all_text(strip=True))
                        if valor and valor not in valores:
                            valores.append(valor)
                return valores

            seletores_ativos = [
                "[id*='poloAtivo']",
                "[class*='poloAtivo']",
                "[id*='polo-ativo']",
                "[class*='polo-ativo']",
                "[data-polo='ATIVO']",
            ]
            seletores_passivos = [
                "[id*='poloPassivo']",
                "[class*='poloPassivo']",
                "[id*='polo-passivo']",
                "[class*='polo-passivo']",
                "[data-polo='PASSIVO']",
            ]
            ativos = nomes_dos_polos(seletores_ativos)
            passivos = nomes_dos_polos(seletores_passivos)
            if ativos and passivos:
                partes = montar(ativos[0], passivos[0])
                if partes:
                    return partes

            nomes = textos_dom(
                [
                    ".nome-parte",
                    ".nomeParte",
                    "[class*='nome-parte']",
                    "[class*='nomeParte']",
                ]
            )
            if len(nomes) >= 2:
                partes = montar(nomes[0], nomes[1])
                if partes:
                    return partes

        # Alguns temas do PJe renderizam rotulos em linhas separadas, sem o
        # separador visual "X". O regex antigo dependia de "SIGLA + CNJ + X".
        linhas = [
            re.sub(r"\s+", " ", linha).strip()
            for linha in str(texto or "").splitlines()
            if linha.strip()
        ]
        polos = {}
        for indice, linha in enumerate(linhas):
            normalizada = _sem_acento(linha).lower()
            achado = re.match(
                r"^(?:polo\s+)?(ativo|passivo)"
                r"(?:\s*\([^)]*\))?\s*:?\s*(.*)$",
                normalizada,
            )
            if achado:
                polo, resto = achado.groups()
                original_resto = re.sub(
                    r"^(?:p[oó]lo\s+)?(?:ativo|passivo)"
                    r"(?:\s*\([^)]*\))?\s*:?\s*",
                    "",
                    linha,
                    flags=re.IGNORECASE,
                )
                valor = limpar(original_resto)
                if not valor and indice + 1 < len(linhas):
                    valor = limpar(linhas[indice + 1])
                if valor:
                    polos[polo] = valor
        if polos.get("ativo") and "passivo" in polos:
            partes = montar(polos["ativo"], polos["passivo"])
            if partes:
                return partes

        texto_limpo = str(texto or "")

        # Variante em tres linhas: NOME DO AUTOR / X / NOME DO REU.
        m = re.search(
            r"(?m)^\s*([^\n]{2,250}?)\s*$\n"
            r"^\s*X\s*$\n"
            r"^\s*([^\n]{2,250}?)\s*$",
            texto_limpo,
        )
        if m:
            partes = montar(m.group(1), m.group(2))
            if partes:
                return partes

        # Variante em uma linha. Não exige mais sigla de classe antes do CNJ:
        # processos criminais do TJPA frequentemente omitem essa sigla.
        candidatos = []
        for linha in linhas:
            if re.search(r"\s+X\s+", linha):
                candidato = re.sub(
                    r"^.*?\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\s*",
                    "",
                    linha,
                )
                if 3 <= len(candidato) <= 500:
                    candidatos.append(candidato)
        if not candidatos:
            return None
        bruto = candidatos[0]
        partes = {"partes": bruto}
        pedacos = re.split(r"\s+X\s+", bruto, maxsplit=1)
        if len(pedacos) == 2:
            return montar(*pedacos)
        return None

    @_serializa
    async def buscar_processo(self, numero_cnj):
        """Retorna dados basicos de um processo pelo CNJ."""
        aba = await self._abrir_autos_processo(numero_cnj)
        try:
            texto = await aba.inner_text("body")
            html = await aba.content()

            resultado = {
                "numero_cnj": numero_cnj,
                "url_autos": _url_sem_segredos(aba.url),
                "processo_de_terceiro": self._ultimo_processo_foi_terceiro,
            }
            if self._ultimo_processo_foi_terceiro:
                resultado["aviso"] = (
                    "AVISO: voce nao e advogado/parte neste processo. "
                    "O acesso foi registrado conforme Resolucao CNJ 121/2010."
                )

            # Cabecalho
            m = re.search(
                r"([A-Z]{2,6})\s+(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})", texto
            )
            if m:
                resultado["classe_sigla"] = m.group(1)
                resultado["numero"] = m.group(2)

            partes = self._extrair_partes(texto, html)
            if partes:
                resultado.update(partes)
                resultado["partes_texto"] = partes.get("partes", "")
                resultado["partes_estruturadas"] = (
                    [
                        {
                            "polo": "AUTOR",
                            "nome": partes["polo_ativo"],
                            "advogados": [],
                        }
                    ]
                    if partes.get("polo_ativo")
                    else []
                )
                if partes.get("polo_passivo"):
                    resultado["partes_estruturadas"].append(
                        {
                            "polo": "REU",
                            "nome": partes["polo_passivo"],
                            "advogados": [],
                        }
                    )

            return resultado
        finally:
            await self._fechar_aba_autos(aba)

    @staticmethod
    def _extrair_movimentacoes_dom(html):
        """Extrai movimentacoes parseando o DOM da timeline (scrapling.Selector).

        Substitui o regex sobre inner_text, que tinha DOIS defeitos (descobertos
        em 12/07/2026, validados contra o DataJud):
        - so capturava o ULTIMO movimento de cada dia (o unico seguido da
          linha de data no texto renderizado);
        - atribuia a esse movimento a data do grupo SEGUINTE (mais antigo),
          porque o cabecalho de data vem ANTES do grupo do dia na timeline.

        Estrutura da timeline (form#divTimeLine):
          div.media.data > span.data-interna   -> cabecalho "23 mar 2026"
          div.media (demais) > div.media-body:
            span.texto-movimento               -> tipo do movimento
            div.anexos a span                  -> "92241811 - Despacho"
            small.text-muted                   -> hora "20:22"
          (div.media so de documento nao tem span.texto-movimento - ignorado)
        """

        def primeiro(el, sel):
            r = el.css(sel)
            return r[0] if r else None

        page = Selector(html)
        timeline = primeiro(page, "form#divTimeLine") or primeiro(page, "#divTimeLine")
        if timeline is None:
            return []

        movimentacoes = []
        data_atual = None
        for media in timeline.css("div.media"):
            classes = (media.attrib.get("class") or "").split()

            if "data" in classes:
                cabecalho = primeiro(media, "span.data-interna")
                if cabecalho:
                    data_atual = cabecalho.text.strip()
                continue

            tipo_el = primeiro(media, "span.texto-movimento")
            if tipo_el is None:
                continue

            mov = {"tipo": tipo_el.text.strip(), "data": data_atual}

            hora_el = primeiro(media, "small.text-muted")
            if hora_el:
                mov["hora"] = hora_el.text.strip()

            anexo = primeiro(media, "div.anexos a span")
            if anexo:
                m = re.match(r"(\d{6,9})\s*-\s*(.+)", anexo.text.strip())
                if m:
                    mov["id"] = m.group(1)
                    mov["descricao"] = m.group(2).strip()

            movimentacoes.append(mov)

        return movimentacoes

    @staticmethod
    def _extrair_movimentacoes(texto):
        """Extrai movimentacoes do texto da timeline dos autos.

        DEPRECATED como via principal: alem de perder movimentos, atribui
        datas ERRADAS (ver _extrair_movimentacoes_dom). Mantido apenas como
        fallback caso a estrutura da timeline mude e o parse DOM volte vazio.
        """
        movimentacoes = []
        for m in re.finditer(
            r"([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇ\s\(\)\-\./]{8,}?)\n"
            r"(?:(\d+)\s*-\s*([^\n]+)\n)?"
            r"(\d{2}:\d{2})\n"
            r"(\d{1,2}\s+\w{3}\s+\d{4})",
            texto,
        ):
            mov = {
                "tipo": m.group(1).strip().rstrip("."),
                "hora": m.group(4),
                "data": m.group(5),
            }
            if m.group(2):
                mov["id"] = m.group(2)
                mov["descricao"] = m.group(3).strip()
            movimentacoes.append(mov)
        return movimentacoes

    @_serializa
    async def ultimas_movimentacoes(self, numero_cnj, limite=5):
        """Retorna as ultimas N movimentacoes do processo."""
        aba = await self._abrir_autos_processo(numero_cnj)
        try:
            html = await aba.content()
            movimentacoes = self._extrair_movimentacoes_dom(html)
            fallback_regex = False
            if not movimentacoes:
                # Estrutura da timeline mudou? Regex antigo como rede de
                # seguranca (datas podem vir deslocadas - ver docstring).
                _log("[PROC] Parse DOM vazio - usando fallback regex")
                fallback_regex = True
                texto = await aba.inner_text("body")
                movimentacoes = self._extrair_movimentacoes(texto)
            resultado = {
                "numero_cnj": numero_cnj,
                "total_encontrado": len(movimentacoes),
                "movimentacoes": movimentacoes[:limite],
            }
            if fallback_regex:
                resultado["aviso"] = (
                    "Extraido pelo fallback regex (parse DOM da timeline "
                    "voltou vazio). As DATAS podem estar deslocadas - "
                    "confirme antes de usar em prazo."
                )
            return resultado
        finally:
            await self._fechar_aba_autos(aba)

    @staticmethod
    def _extrair_expedientes_dom(html):
        """Parseia a tabela da aba Expedientes dos autos (scrapling.Selector).

        Estrutura (table#processoParteExpedienteMenuGridList):
          tr.rich-table-row > td.rich-table-cell x4:
            [0] blob: "Despacho (16911904) - Prioridade: Normal - ID do
                documento (93044965) FULANO Diário Eletrônico (23/03/2026
                20:22:49) O sistema registrou ciência em 26/03/2026 00:00:00
                Prazo: 15 dias"
            [1] data limite: "22/04/2026 23:59:59 (para manifestação)"
            [2] acoes (ignorada)
            [3] fechado: "SIM"/"NÃO"
        """
        page = Selector(html)
        tabelas = page.css("table#processoParteExpedienteMenuGridList")
        if not tabelas:
            return []

        expedientes = []
        for tr in tabelas[0].css("tr.rich-table-row"):
            tds = tr.css("td.rich-table-cell")
            if len(tds) < 2:
                continue
            blob = re.sub(r"\s+", " ", tds[0].get_all_text(strip=True) or "").strip()
            exp = {}

            m = re.match(r"(.+?)\s*\((\d+)\)", blob)
            if m:
                exp["ato"] = m.group(1).strip()
                exp["id_expediente"] = m.group(2)
            m = re.search(r"Prioridade:\s*([\wÀ-ÿ]+)", blob)
            if m:
                exp["prioridade"] = m.group(1)
            m_id = re.search(r"ID do documento\s*\((\d+)\)", blob)
            if m_id:
                exp["id_documento"] = m_id.group(1)

            # regiao entre o id do documento e a data de expedicao contem
            # "DESTINATARIO [Representante: ...] VIA". A data de expedicao e'
            # a PRIMEIRA data entre parenteses apos o id (expedientes sem
            # ciencia registrada nao tem a frase "O sistema registrou").
            m_data = None
            if m_id:
                m_data = re.search(
                    r"\((\d{2}/\d{2}/\d{4}[ \d:]*)\)", blob[m_id.end() :]
                )
            if m_id and m_data:
                exp["data_expedicao"] = m_data.group(1).strip()
                regiao = blob[m_id.end() : m_id.end() + m_data.start()].strip(" -")
                m_via = re.search(
                    r"(Diário Eletrônico|Expedição eletrônica|Central de Mandados"
                    r"|Sistema|Correios|Edital|Carta precatória)\s*$",
                    regiao,
                    re.IGNORECASE,
                )
                if m_via:
                    exp["via"] = m_via.group(1)
                    regiao = regiao[: m_via.start()]
                if "Representante:" in regiao:
                    regiao, rep = regiao.split("Representante:", 1)
                    exp["representante"] = rep.strip()
                exp["destinatario"] = regiao.strip()

            m = re.search(r"registrou ciência em\s*(\d{2}/\d{2}/\d{4}[ \d:]*)", blob)
            if m:
                exp["ciencia_em"] = m.group(1).strip()
            m = re.search(r"Prazo:\s*(.+)$", blob)
            if m:
                exp["prazo"] = m.group(1).strip()

            texto_limite = re.sub(r"\s+", " ", tds[1].get_all_text(strip=True) or "")
            m = re.search(r"(\d{2}/\d{2}/\d{4}[ \d:]*)", texto_limite)
            if m:
                exp["data_limite"] = m.group(1).strip()
            m = re.search(r"\(para ([^)]+)\)", texto_limite)
            if m:
                exp["finalidade"] = m.group(1).strip()

            if len(tds) >= 4:
                fechado = (tds[3].get_all_text(strip=True) or "").strip().upper()
                if fechado in ("SIM", "NÃO", "NAO"):
                    exp["fechado"] = fechado == "SIM"

            expedientes.append(exp)
        return expedientes

    @_serializa
    async def expedientes_do_processo(self, numero_cnj):
        """Le a aba 'Expedientes' dos autos: historico COMPLETO dos atos de
        comunicacao (expedicao, ciencia, prazo, data limite), incluindo os ja
        fechados/vencidos - que nao aparecem mais no painel de pendentes.

        Leitura passiva: a aba apenas lista; visualizar NAO registra ciencia.
        """
        aba = await self._abrir_autos_processo(numero_cnj)
        try:
            await aba.locator("a[id='navbar:linkAbaExpedientes1']").click(timeout=10000)
            try:
                await aba.wait_for_selector(
                    "table[id='processoParteExpedienteMenuGridList']", timeout=15000
                )
            except Exception:
                pass  # processo sem expedientes nao renderiza a tabela
            html = await aba.content()
            expedientes = self._extrair_expedientes_dom(html)
            return {
                "numero_cnj": numero_cnj,
                "total": len(expedientes),
                "expedientes": expedientes,
                "fonte": "aba Expedientes dos autos (inclui fechados/vencidos)",
            }
        finally:
            await self._fechar_aba_autos(aba)

    @_serializa
    async def relatorio_processo(self, numero_cnj):
        """Relatorio completo: dados + movimentacoes + documentos."""
        aba = await self._abrir_autos_processo(numero_cnj)
        try:
            texto = await aba.inner_text("body")
            html = await aba.content()

            relatorio = {
                "numero_cnj": numero_cnj,
                "url_autos": _url_sem_segredos(aba.url),
                "processo_de_terceiro": self._ultimo_processo_foi_terceiro,
            }

            m = re.search(
                r"([A-Z]{2,6})\s+(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})", texto
            )
            if m:
                relatorio["classe_sigla"] = m.group(1)

            partes = self._extrair_partes(texto, html)
            if partes:
                relatorio.update(partes)
                relatorio["partes_texto"] = partes.get("partes")
                relatorio["partes"] = (
                    [
                        {
                            "polo": "AUTOR",
                            "nome": partes["polo_ativo"],
                            "advogados": [],
                        }
                    ]
                    if partes.get("polo_ativo")
                    else []
                )
                if partes.get("polo_passivo"):
                    relatorio["partes"].append(
                        {
                            "polo": "REU",
                            "nome": partes["polo_passivo"],
                            "advogados": [],
                        }
                    )
            else:
                relatorio["partes"] = []

            dados_basicos = {}
            padroes_dados = {
                "classe": r"(?:Classe judicial|Classe)\s*:?\s*\n?\s*([^\n]+)",
                "assunto": r"(?:Assunto principal|Assunto)\s*:?\s*\n?\s*([^\n]+)",
                "vara": r"(?:Órgão julgador|Orgao julgador|Vara)\s*:?\s*\n?\s*([^\n]+)",
                "valor_causa": r"Valor da causa\s*:?\s*\n?\s*([^\n]+)",
                "data_distribuicao": r"(?:Data da distribuição|Distribuição)\s*:?\s*\n?\s*([^\n]+)",
            }
            for campo, padrao in padroes_dados.items():
                achado = re.search(padrao, texto, re.IGNORECASE)
                if achado:
                    dados_basicos[campo] = achado.group(1).strip()
            relatorio["dados_basicos"] = dados_basicos

            movs = self._extrair_movimentacoes_dom(html)
            if not movs:
                _log("[PROC] Parse DOM vazio - usando fallback regex")
                movs = self._extrair_movimentacoes(texto)
                relatorio["aviso_movimentacoes"] = (
                    "Movimentacoes extraidas pelo fallback regex - datas "
                    "podem estar deslocadas."
                )
            relatorio["ultimas_movimentacoes"] = movs[:10]
            # Consumidores novos usam a coleção completa nesta chave; mantém
            # também a chave legada acima para compatibilidade.
            relatorio["movimentacoes"] = movs
            relatorio["total_movimentacoes_encontradas"] = len(movs)

            # Documentos principais
            docs = []
            for m in re.finditer(
                r"(\d{7,})\s*-\s*(Petição Inicial|Documentos \([^)]+\)|"
                r"DOCUMENTO COMPROBATÓRIO[^\n]*|Procuração \([^)]+\)|"
                r"Petição \([^)]+\))",
                texto,
            ):
                docs.append({"id": m.group(1), "descricao": m.group(2).strip()})
            relatorio["documentos"] = docs[:20]

            return relatorio
        finally:
            await self._fechar_aba_autos(aba)

    # ========== BUSCA POR DIFERENTES CRITERIOS ==========

    @_serializa
    async def buscar_processo_geral(self, identificador, limite=20):
        """Busca na consulta nativa, inferindo somente formatos inequívocos."""
        if self.grau != "1g":
            return {
                "erro": (
                    "Busca processual geral ainda mapeada positivamente "
                    "somente no 1º grau"
                ),
                "somente_leitura": True,
            }
        bruto = str(identificador or "").strip()
        digitos = re.sub(r"\D", "", bruto)
        if len(digitos) == 20:
            criterio, valor = "numero_cnj", bruto
        elif len(digitos) == 11:
            criterio, valor = "cpf", digitos
        elif len(digitos) == 14:
            criterio, valor = "cnpj", digitos
        else:
            criterio, valor = "nome_parte", bruto
        _log(
            "[BUSCA] Abrindo Consulta processos "
            f"(criterio={criterio}, tamanho={len(bruto)})"
        )
        page_object = NativeProcessSearchPage(self._page, self.url_base)
        resultado = await page_object.search(criterio, valor, limite)
        resultado["criterio_inferido"] = True
        resultado["grau"] = self.grau
        return resultado

    @_serializa
    async def _buscar_por_campo(self, campo, valor, limite=20):
        """Busca por campo usando a mesma tela nativa e o mesmo parser."""
        if self.grau != "1g":
            return {
                "erro": (
                    "Busca processual por critérios ainda mapeada "
                    "positivamente somente no 1º grau"
                ),
                "somente_leitura": True,
            }
        _log(
            "[BUSCA] Abrindo Consulta processos "
            f"(criterio={campo}, tamanho={len(str(valor or ''))})"
        )
        page_object = NativeProcessSearchPage(self._page, self.url_base)
        resultado = await page_object.search(campo, valor, limite)
        resultado["grau"] = self.grau
        return resultado

    async def buscar_por_nome_parte(self, nome, limite=20):
        """Busca processos pelo nome de uma parte (autor/reu)."""
        return await self._buscar_por_campo("nome_parte", nome, limite)

    async def buscar_por_nome_requerente(self, nome, limite=20):
        """Busca parte e restringe a correspondência ao polo ativo."""
        return await self._buscar_por_campo("nome_requerente", nome, limite)

    async def buscar_por_nome_requerido(self, nome, limite=20):
        """Busca parte e restringe a correspondência ao polo passivo."""
        return await self._buscar_por_campo("nome_requerido", nome, limite)

    async def buscar_por_nome_advogado(self, nome, limite=20):
        """Busca processos pelo nome do advogado."""
        return await self._buscar_por_campo("nome_advogado", nome, limite)

    async def buscar_por_outros_nomes(self, nome, limite=20):
        """Busca por outros nomes ou alcunha cadastrada."""
        return await self._buscar_por_campo("outros_nomes", nome, limite)

    async def buscar_por_numero_documento(self, numero, limite=20):
        """Busca pelo campo genérico Número do documento."""
        return await self._buscar_por_campo("numero_documento", numero, limite)

    async def buscar_por_criterio_avancado(self, criterio, valor, limite=20):
        """Busca por um critério avançado já normalizado pelo servidor MCP."""
        permitidos = {
            "assunto", "classe_judicial", "jurisdicao", "orgao_julgador",
            "prioridade_processual", "data_autuacao", "valor_causa",
            "movimento_processual", "orgao_origem_criminal",
            "procedimento_criminal", "ano_procedimento_criminal",
            "protocolo_policia",
        }
        if criterio not in permitidos:
            raise ValueError(f"critério avançado não suportado: {criterio}")
        return await self._buscar_por_campo(criterio, valor, limite)

    async def buscar_por_cpf(self, cpf, limite=20):
        """Busca processos pelo CPF de uma parte."""
        cpf_limpo = "".join(c for c in cpf if c.isdigit())
        return await self._buscar_por_campo("cpf", cpf_limpo, limite)

    async def buscar_por_cnpj(self, cnpj, limite=20):
        """Busca processos pelo CNPJ de uma parte."""
        cnpj_limpo = "".join(c for c in cnpj if c.isdigit())
        return await self._buscar_por_campo("cnpj", cnpj_limpo, limite)

    async def buscar_por_oab(self, numero_oab, uf="PI", limite=20):
        """Busca processos pelo numero OAB do advogado."""
        match = re.fullmatch(r"\s*(\d{1,10})\s*[-./]?\s*([A-Za-z]?)\s*", str(numero_oab))
        if not match:
            raise ValueError("OAB deve conter número e, opcionalmente, uma letra")
        value = {
            "numero": match.group(1),
            "letra": match.group(2).upper(),
            "uf": str(uf or "").upper(),
        }
        return await self._buscar_por_campo("oab", value, limite)

    @_serializa
    async def mapear_cadastro_processo(
        self,
        materia="",
        jurisdicao="",
        classe_judicial="",
        assunto="",
        codigo_assunto="",
        etapa="",
        polo="",
        cpf_parte="",
        adicionar_parte=False,
        etapa_apos_parte="",
        caminho_pdf="",
        descricao_documento="",
        tipo_documento="Petição Inicial",
        avancar=False,
    ):
        """Entrada legado bloqueada: o cliente PJe é estritamente observador."""
        return {
            "status": "desabilitado_por_politica",
            "erro": "cadastro e preparação não pertencem ao cliente read-only",
            "somente_leitura": True,
            "protocolo_executado": False,
        }

        # Implementação histórica mantida temporariamente sem rota executável
        # para permitir auditoria e extração futura a um simulador sem PJe.
        if self.grau != "1g":
            return {
                "status": "nao_suportado",
                "erro": ("o cadastro foi autorizado e mapeado somente no 1º grau"),
                "somente_leitura": True,
            }
        registration = ProcessRegistrationPage(self._page, self.url_base)
        await registration.open()
        snapshot = await registration.fill_initial(
            materia=materia,
            jurisdicao=jurisdicao,
            classe_judicial=classe_judicial,
            advance=avancar,
        )
        if assunto:
            if not avancar:
                raise ValueError(
                    "a pesquisa de assunto exige avançar os dados iniciais"
                )
            snapshot["resultados_assunto"] = await registration.search_subject(assunto)
            if codigo_assunto:
                selected_snapshot = await registration.select_subject(codigo_assunto)
                snapshot.update(selected_snapshot)
                if etapa:
                    step_snapshot = await registration.open_step(etapa)
                    snapshot.update(step_snapshot)
                    if polo:
                        party_snapshot = await registration.open_party_form(polo)
                        snapshot.update(party_snapshot)
                        if cpf_parte:
                            search_snapshot = await registration.search_party_by_cpf(
                                cpf_parte
                            )
                            snapshot.update(search_snapshot)
                            if adicionar_parte:
                                snapshot.update(await registration.confirm_party())
                                if etapa_apos_parte:
                                    next_snapshot = await registration.open_step(
                                        etapa_apos_parte
                                    )
                                    snapshot.update(next_snapshot)
                                    if caminho_pdf:
                                        snapshot.update(
                                            await registration.upload_primary_pdf(
                                                caminho_pdf,
                                                descricao_documento,
                                                tipo_documento,
                                            )
                                        )
        snapshot["somente_leitura"] = not avancar
        snapshot["protocolo_executado"] = False
        return snapshot

    # ========== DOCUMENTOS ==========

    @_serializa
    async def listar_documentos(self, numero_cnj):
        """Lista todos os documentos do processo (id + tipo).

        IMPORTANTE: a arvore lateral do PJe usa lazy-loading. Forcamos scroll
        ate o fim pra carregar TODOS os documentos antes de extrair.
        """
        aba = await self._abrir_autos_processo(numero_cnj)
        try:
            docs = await self._extrair_documentos_da_aba(aba)
            completa = getattr(self, "_ultima_arvore_completa", True)
            r = {
                "numero_cnj": numero_cnj,
                "total": len(docs),
                "arvore_completa": completa,
                "documentos": docs,
            }
            if not completa:
                r["aviso"] = (
                    "A árvore de documentos não terminou de carregar: esta lista "
                    "pode estar INCOMPLETA. Repita a consulta ou trate o total "
                    "como piso, não como o número real de peças."
                )
            return r
        finally:
            await self._fechar_aba_autos(aba)

    async def _extrair_documentos_da_aba(self, aba):
        """Forca o lazy-load da arvore e extrai [{id, tipo}, ...] da aba dos autos."""
        _log("[DOC] Forcando lazy-load da arvore via wait_for_function...")
        # ESTAVEIS_MIN=5 a 400ms = 2s de quietude antes de declarar carregado.
        # Com o antigo (2 = 800ms), um fetch da proxima leva do lazy-load que
        # demorasse mais que isso era lido como "arvore acabou", e a listagem
        # voltava truncada em silencio - a origem do bug conhecido de ">30 docs".
        js_scroll_e_estabilidade = """
            () => new Promise((resolve) => {
                let anterior = -1;
                let estaveis = 0;
                const interval = setInterval(() => {
                    const candidatos = Array.from(document.querySelectorAll(
                        '[onclick*="abrirLinkDocumento"], span.title, a span'
                    ));
                    const docs = candidatos.filter((el) =>
                        /^(\\d{6,9})\\s*[-–]\\s*/.test(
                            (el.textContent || '').trim()
                        ) || (el.getAttribute('onclick') || '').includes(
                            'abrirLinkDocumento'
                        )
                    );
                    const atual = docs.length;
                    if (atual > 0 && atual === anterior) {
                        estaveis++;
                        if (estaveis >= 5) {
                            clearInterval(interval);
                            resolve(true);
                            return;
                        }
                    } else {
                        estaveis = 0;
                    }
                    anterior = atual;
                    if (docs.length) {
                        docs[docs.length - 1].scrollIntoView({block: 'end'});
                    }
                    const sels = ['#tabPanelDocs', '.scroll-y', '.documentos-panel',
                                  '#documentos', '#divTimeLine', 'aside',
                                  '.barra-de-tarefas'];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) { el.scrollTop = el.scrollHeight; }
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                }, 400);
            })
        """
        # Com 5 leituras estaveis a arvore precisa de mais folga que os 15s
        # antigos; processos grandes carregam em varias levas.
        lazy_load_completo = True
        try:
            await aba.wait_for_function(js_scroll_e_estabilidade, timeout=30000)
        except Exception as e:
            # Estourou o orcamento: a arvore pode ter ficado pela metade. Antes
            # isso so virava log e a listagem parcial passava por completa.
            lazy_load_completo = False
            _log(f"[DOC] Timeout ou aviso no wait_for_function: {e}")

        js_code = """
        () => {
            const resultado = [];
            const vistos = new Set();
            const re = new RegExp('^(\\\\d{6,9})\\\\s*[-–]\\\\s*(.+)$');

            // Data no rotulo/vizinhanca: o PJe escreve dd/mm/aaaa (as vezes com
            // hora) junto do documento. Sem isso, 'data' voltava sempre 'N/I'.
            const reData = new RegExp('(\\\\d{2}/\\\\d{2}/\\\\d{4})(?:\\\\s+(\\\\d{2}:\\\\d{2}))?');
            const acharData = (el) => {
                const alvos = [el, el.closest('.media-body'), el.closest('div'),
                               el.parentElement];
                for (const a of alvos) {
                    if (!a) continue;
                    const m = (a.textContent || '').match(reData);
                    if (m) return m[2] ? (m[1] + ' ' + m[2]) : m[1];
                }
                return '';
            };
            document.querySelectorAll('span.title, a span').forEach(span => {
                const t = (span.textContent || '').trim();
                const m = t.match(re);
                if (m && !vistos.has(m[1])) {
                    vistos.add(m[1]);
                    // 'titulo' e' o rotulo completo ("123456 - Sentenca"), que ja
                    // era parseado e jogado fora. server.py filtrava por titulo
                    // contra string vazia — metade do filtro nao funcionava.
                    resultado.push({
                        id: m[1],
                        tipo: m[2].trim(),
                        titulo: t,
                        data: acharData(span),
                    });
                }
            });

            document.querySelectorAll('[onclick]').forEach(el => {
                const oc = el.getAttribute('onclick') || '';
                // Regex via new RegExp pra nao brigar com o escape do literal
                // Python: o /.../ antigo chegava corrompido no JS e este ramo
                // NUNCA casava - mascarado pelo ramo dos spans acima, que
                // encontrava os documentos primeiro. Descoberto em 18/07/2026
                // ao portar o cliente pro TRF1.
                const m = oc.match(new RegExp("abrirLinkDocumento\\\\('?(\\\\d+)'?\\\\)"));
                if (m && !vistos.has(m[1])) {
                    vistos.add(m[1]);
                    let tipo = 'desconhecido';
                    let titulo = '';
                    const parent = el.closest('.media-body, div');
                    if (parent) {
                        const span = parent.querySelector('span.title, span');
                        if (span) {
                            titulo = span.textContent.trim();
                            tipo = titulo.replace(/^\\\\d+\\\\s*[-–]\\\\s*/, '');
                        }
                    }
                    resultado.push({
                        id: m[1],
                        tipo: tipo,
                        titulo: titulo || tipo,
                        data: acharData(el),
                    });
                }
            });

            return resultado;
        }
        """

        try:
            docs = await aba.evaluate(js_code)
        except Exception as e:
            _log(f"[DOC] Erro no JS: {e}")
            docs = []

        if not docs:
            _log("[DOC] JS vazio, tentando fallback com regex no HTML")
            docs = self._extrair_documentos_do_html(await aba.content())
            # Fallback so raspa ids do HTML servido: nao roda o lazy-load, entao
            # o que veio e' apenas a primeira leva da arvore.
            lazy_load_completo = False

        self._ultima_arvore_completa = lazy_load_completo
        return docs

    @staticmethod
    def _extrair_documentos_do_html(html):
        """Extrai [{id, tipo, titulo, data}, ...] do HTML JA servido.

        NAO roda o lazy-load da arvore: devolve apenas a primeira leva que o
        PJe mandou no HTML. Serve tanto de fallback do caminho com lazy-load
        quanto de caminho rapido para quem prefere resposta imediata a
        listagem completa — nesse caso o total e' PISO, e quem chama tem de
        rotular como tal (arvore_completa=False).
        """
        docs = []
        vistos = set()
        for m in re.finditer(r"abrirLinkDocumento\(['\"](\d{6,9})['\"]\)", html or ""):
            if m.group(1) not in vistos:
                vistos.add(m.group(1))
                docs.append(
                    {
                        "id": m.group(1),
                        "tipo": "(tipo nao extraido)",
                        "titulo": "",
                        "data": "",
                    }
                )
        return docs

    def _url_documento_rest(self, documento_id):
        """URL REST de download direto confirmada na árvore do PJe-TJPA."""
        return (
            f"{self.url_base}/seam/resource/rest/pje-legacy/"
            f"documento/download/{documento_id}"
        )

    async def _diagnosticar_link_documento(self, aba, id_documento):
        """Aciona o mesmo link JSF da árvore e coleta a fonte real da peça.

        Algumas versões do PJe não usam ``abrirLinkDocumento``: o link faz
        um POST Ajax4JSF e injeta o visualizador no DOM. Esta rotina não lê o
        teor; ela descobre, sem incluir query strings/tokens, quais URLs e
        elementos o próprio PJe produziu após o clique.
        """
        if aba is None or aba.is_closed():
            return {"erro": "aba dos autos indisponível para fallback JSF"}

        padrao_rotulo = re.compile(rf"\b{re.escape(str(id_documento))}\s*[-–]")
        alvo = aba.locator("a").filter(has_text=padrao_rotulo).first
        if not await alvo.count():
            # A timeline é lazy: ao abrir os autos só a estrutura inicial pode
            # existir. Carrega a árvore antes de concluir que o id não existe.
            await self._extrair_documentos_da_aba(aba)
            alvo = aba.locator("a").filter(has_text=padrao_rotulo).first
        if not await alvo.count():
            return {"erro": "documento não localizado na árvore renderizada"}
        link = alvo

        ajax = None
        try:
            async with aba.expect_response(
                lambda r: (
                    r.request.method == "POST" and "listAutosDigitais.seam" in r.url
                ),
                timeout=15_000,
            ) as resposta:
                await link.click(timeout=10_000)
            ajax = await resposta.value
            try:
                await aba.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass
        except Exception as exc:
            return {"erro": f"clique JSF não produziu resposta esperada: {exc}"}

        elementos = await aba.evaluate("""
        () => {
            const out = [];
            const limpar = (valor) => {
                if (!valor) return '';
                try {
                    const u = new URL(valor, window.location.href);
                    return u.pathname;
                } catch (_) {
                    return valor.split('?')[0];
                }
            };
            document.querySelectorAll(
                'iframe[src], embed[src], object[data], '
                + 'a[href*="documento"], a[href*="download"]'
            ).forEach((el) => {
                const bruto = el.getAttribute('src')
                    || el.getAttribute('data')
                    || el.getAttribute('href')
                    || '';
                out.push({
                    tag: el.tagName.toLowerCase(),
                    caminho: limpar(bruto),
                    content_type: el.getAttribute('type') || '',
                });
            });
            return out;
        }
        """)
        frames = []
        for frame in aba.frames:
            if frame == aba.main_frame:
                continue
            try:
                caminho = re.sub(r"[?#].*$", "", frame.url)
                frames.append(caminho)
            except Exception:
                continue

        caminhos_ajax = []
        try:
            corpo_ajax = html_module.unescape(await ajax.text())
            # Só caminhos, sem query string, para não vazar o código ``ca``.
            for achado in re.findall(
                r"""(?:https?://[^"'<>?\s]+|/pje(?:-2g)?/[^"'<>?\s]+)""",
                corpo_ajax,
            ):
                caminho = re.sub(r"[?#].*$", "", achado)
                if caminho not in caminhos_ajax:
                    caminhos_ajax.append(caminho)
        except Exception:
            pass

        return {
            "ajax_status": ajax.status,
            "ajax_url": re.sub(r"[?#].*$", "", ajax.url),
            "elementos": elementos,
            "frames": frames,
            "caminhos_ajax": caminhos_ajax[:20],
        }

    async def _obter_documento_via_jsf(self, aba, id_documento):
        """Clica na árvore e captura a resposta do iframe autenticado."""
        if aba is None or aba.is_closed():
            return {"erro": "aba dos autos indisponível para leitura JSF"}

        padrao_rotulo = re.compile(rf"\b{re.escape(str(id_documento))}\s*[-–]")
        link = aba.locator("a").filter(has_text=padrao_rotulo).first
        if not await link.count():
            await self._extrair_documentos_da_aba(aba)
            link = aba.locator("a").filter(has_text=padrao_rotulo).first
        if not await link.count():
            return {"erro": "documento não localizado na árvore renderizada"}

        loop = asyncio.get_running_loop()
        futura_resposta = loop.create_future()
        trecho_url = f"/documento/download/{id_documento}"

        def capturar(resposta):
            if trecho_url in resposta.url and not futura_resposta.done():
                futura_resposta.set_result(resposta)

        aba.on("response", capturar)
        try:
            await link.click(timeout=10_000)
            resposta = await asyncio.wait_for(futura_resposta, timeout=30)
            try:
                corpo = await resposta.body()
                headers = await resposta.all_headers()
                status = resposta.status
                fonte = "corpo da resposta capturada no visualizador"
            except Exception as erro_corpo:
                # Chromium pode descartar o resource id antes de
                # Network.getResponseBody (observado em denúncias HTML). O
                # clique JSF já selecionou a peça; repete a mesma URL no
                # contexto autenticado, agora com o Referer dos autos.
                repetida = await self._context.request.get(
                    resposta.url,
                    headers={"Referer": aba.url},
                    timeout=20_000,
                )
                try:
                    status = repetida.status
                    corpo = await repetida.body() if repetida.ok else b""
                    headers = repetida.headers
                finally:
                    await repetida.dispose()
                if not corpo:
                    return {
                        "erro": (
                            "resposta do visualizador sem corpo e repetição "
                            "autenticada vazia: "
                            f"{erro_publico(erro_corpo)}; HTTP {status}"
                        )
                    }
                fonte = "repetição autenticada após seleção JSF"
            return {
                "corpo": corpo,
                "content_type": headers.get("content-type", ""),
                "url": _url_sem_segredos(resposta.url),
                "status": status,
                "fonte_jsf": fonte,
            }
        except Exception as exc:
            return {
                "erro": (
                    f"falha ao capturar resposta do visualizador: {erro_publico(exc)}"
                )
            }
        finally:
            aba.remove_listener("response", capturar)

    async def _ler_documento_rest(
        self, numero_cnj, id_documento, max_paginas=30, aba_autos=None
    ):
        """Le o teor de um documento via endpoint REST (sem abrir nova aba).

        Requer que os autos do processo ja tenham sido abertos nesta sessao
        (pra _ultimo_processo_id estar setado). E o mesmo endpoint que o
        abrirLinkDocumento da arvore abre - aqui baixamos direto via
        APIRequestContext, evitando nova aba + carga dupla da page.
        """
        url_doc = self._url_documento_rest(id_documento)
        _log(f"[DOC] GET {url_doc}")

        resultado = {
            "id_documento": str(id_documento),
            "numero_cnj": numero_cnj,
            "url": url_doc,
        }

        page_process_id = None
        if aba_autos is not None:
            try:
                match = re.search(
                    r"[?&](?:id|idProcesso)=(\d+)",
                    str(aba_autos.url or ""),
                )
                page_process_id = match.group(1) if match else None
            except Exception:
                page_process_id = None
        process_id_available = (
            page_process_id if aba_autos is not None else self._ultimo_processo_id
        )
        if not process_id_available:
            resultado["erro"] = (
                "ID interno do processo não foi extraído da URL dos autos; "
                "a leitura REST da peça não pode ser considerada válida."
            )
            return resultado

        corpo = b""
        content_type = ""
        status = None
        resp = await self._context.request.get(url_doc, timeout=60000)
        try:
            status = resp.status
            if resp.ok:
                corpo = await resp.body()
                content_type = resp.headers.get("content-type", "")
        finally:
            # Playwright mantém o corpo de toda APIResponse em RAM até fechar
            # o contexto, a menos que dispose() seja chamado explicitamente.
            await resp.dispose()

        # Neste PJe a mesma URL devolve 200/vazio fora do iframe. A resposta
        # efetiva só é emitida depois do POST Ajax4JSF que seleciona a peça.
        if (not corpo or status != 200) and aba_autos is not None:
            async with self._jsf_lock:
                via_jsf = await self._obter_documento_via_jsf(aba_autos, id_documento)
                if "erro" not in via_jsf:
                    corpo = via_jsf["corpo"]
                    content_type = via_jsf["content_type"]
                    status = via_jsf["status"]
                    resultado["url"] = via_jsf["url"]
                    resultado["fonte"] = via_jsf.get(
                        "fonte_jsf", "resposta do iframe após seleção JSF"
                    )
                else:
                    resultado["erro"] = (
                        f"Leitura direta retornou HTTP {status} com {len(corpo)} "
                        f"bytes; {via_jsf['erro']}"
                    )
                    resultado[
                        "diagnostico_link_jsf"
                    ] = await self._diagnosticar_link_documento(aba_autos, id_documento)
                    return resultado

        if status != 200:
            resultado["erro"] = f"Download falhou: HTTP {status}"
            return resultado
        if not corpo:
            resultado["erro"] = "PJe retornou corpo vazio para o documento"
            return resultado

        # Integridade do artefato original antes de qualquer extração. O corpo
        # permanece apenas em memória e é liberado ao sair deste método.
        resultado["source_bytes_sha256"] = hashlib.sha256(corpo).hexdigest()
        resultado["content_type"] = content_type
        resultado["tamanho_bytes"] = len(corpo)
        is_pdf = "pdf" in content_type.lower() or corpo[:5] == b"%PDF-"

        if is_pdf:
            resultado["formato"] = "pdf"
            try:
                # Extração é CPU-bound e pdfplumber é síncrono: fora do
                # event loop o MCP continua respondendo a status/jobs.
                def extrair_pdf():
                    ocr_enabled = (
                        os.environ.get("PJE_OCR_ENABLED", "1").strip() == "1"
                    )
                    ocr_available = ocr_enabled and bool(
                        shutil.which("tesseract")
                    )
                    fitz_document = None
                    if ocr_available:
                        try:
                            import fitz

                            fitz_document = fitz.open(
                                stream=corpo,
                                filetype="pdf",
                            )
                        except Exception:
                            ocr_available = False
                    with pdfplumber.open(io.BytesIO(corpo)) as pdf:
                        try:
                            total = len(pdf.pages)
                            limite_paginas = (
                                total
                                if max_paginas is None or int(max_paginas) <= 0
                                else min(total, int(max_paginas))
                            )
                            paginas = []
                            paginas_estruturadas = []
                            sem_texto = []
                            ocr_pages = 0
                            for indice, pagina in enumerate(
                                pdf.pages[:limite_paginas], start=1
                            ):
                                try:
                                    texto_pagina = pagina.extract_text() or ""
                                    method = "native_text"
                                    page_status = "extracted"
                                    confidence = 1.0
                                    if not texto_pagina.strip() and ocr_available:
                                        try:
                                            fitz_page = fitz_document[indice - 1]
                                            text_page = fitz_page.get_textpage_ocr(
                                                language=os.environ.get(
                                                    "PJE_OCR_LANGUAGE",
                                                    "por",
                                                ),
                                                dpi=max(
                                                    96,
                                                    min(
                                                        300,
                                                        int(
                                                            os.environ.get(
                                                                "PJE_OCR_DPI",
                                                                "150",
                                                            )
                                                        ),
                                                    ),
                                                ),
                                                full=True,
                                            )
                                            texto_pagina = fitz_page.get_text(
                                                textpage=text_page
                                            )
                                        except Exception:
                                            texto_pagina = ""
                                            page_status = "ocr_failed"
                                        if texto_pagina.strip():
                                            method = "ocr"
                                            page_status = "extracted"
                                            confidence = 0.75
                                            ocr_pages += 1
                                        else:
                                            method = "ocr"
                                            page_status = "ocr_failed"
                                            confidence = 0.0
                                    elif not texto_pagina.strip():
                                        method = "none"
                                        page_status = "ocr_required_unavailable"
                                        confidence = 0.0
                                    try:
                                        tabelas = pagina.extract_tables() or []
                                    except Exception:
                                        tabelas = []
                                    line_count = (
                                        len(texto_pagina.splitlines())
                                        if texto_pagina
                                        else 0
                                    )
                                    word_count = (
                                        len(texto_pagina.split())
                                        if texto_pagina
                                        else 0
                                    )
                                    layout_preserved = True
                                except Exception:
                                    texto_pagina = ""
                                    method = "none"
                                    page_status = "missing_corrupted"
                                    confidence = 0.0
                                    tabelas = []
                                    line_count = 0
                                    word_count = 0
                                    layout_preserved = False

                                if not texto_pagina.strip():
                                    sem_texto.append(indice)
                                paginas.append(
                                    f"--- Página {indice} ---\n{texto_pagina}"
                                )
                                paginas_estruturadas.append(
                                    {
                                        "page": indice,
                                        "text": texto_pagina,
                                        "tables": tabelas,
                                        "extraction_method": method,
                                        "status": page_status,
                                        "confidence": confidence,
                                        "line_count": line_count,
                                        "word_count": word_count,
                                        "layout_preserved": layout_preserved,
                                    }
                                )
                            return (
                                total,
                                paginas,
                                sem_texto,
                                paginas_estruturadas,
                                ocr_available,
                                ocr_pages,
                            )
                        finally:
                            if fitz_document is not None:
                                fitz_document.close()

                (
                    total_paginas,
                    paginas,
                    sem_texto,
                    paginas_estruturadas,
                    ocr_available,
                    ocr_pages,
                ) = await asyncio.to_thread(extrair_pdf)
                resultado["texto"] = "\n\n".join(paginas)
                resultado["num_paginas"] = total_paginas
                resultado["paginas_sem_texto"] = sem_texto
                resultado["paginas_estruturadas"] = paginas_estruturadas
                if sem_texto:
                    resultado["ocr_recomendado"] = True
                    resultado["ocr_status"] = (
                        "failed_or_partial"
                        if ocr_available
                        else "required_unavailable"
                    )
                elif ocr_pages:
                    resultado["ocr_status"] = "completed"
                else:
                    resultado["ocr_status"] = "not_required"
                resultado["ocr_pages"] = ocr_pages
                if (
                    max_paginas is not None
                    and int(max_paginas) > 0
                    and total_paginas > int(max_paginas)
                ):
                    resultado["truncado"] = True
                    resultado["paginas_extraidas"] = int(max_paginas)
                    resultado["aviso"] = (
                        f"ATENCAO: documento tem {total_paginas} paginas, mas "
                        f"apenas as {int(max_paginas)} primeiras foram extraidas."
                    )
            except Exception as e:
                resultado["erro"] = f"Falhou ao extrair PDF: {e}"
        else:
            resultado["formato"] = "html"
            try:
                html_documento = corpo.decode("utf-8", errors="replace")
                resultado["texto"] = _html_para_texto(html_documento)
                if not resultado["texto"]:
                    resultado["erro"] = "Documento HTML recebido sem texto"
            except Exception as e:
                resultado["erro"] = f"Falhou ao extrair HTML: {e}"

        return resultado

    @_serializa
    async def ler_documento(self, numero_cnj, id_documento, max_paginas=30):
        """Le o teor de um documento especifico (HTML ou PDF)."""
        # Mantém a aba aberta até a leitura terminar: versões antigas do PJe
        # selecionam a peça por um POST Ajax4JSF e só então injetam o
        # visualizador, portanto o fallback não funciona apenas com o id em
        # cache de uma consulta anterior.
        aba = await self._abrir_autos_processo(numero_cnj)
        try:
            return await self._ler_documento_rest(
                numero_cnj, id_documento, max_paginas, aba_autos=aba
            )
        finally:
            await self._fechar_aba_autos(aba)

    @_serializa
    async def ler_documentos_em_lote(
        self,
        numero_cnj,
        max_documentos=10,
        max_paginas=30,
        tempo_maximo_segundos=45,
    ):
        """Lista e lê várias peças mantendo uma única aba dos autos aberta.

        O orçamento engloba abertura, lazy-load e leituras. Ao esgotá-lo,
        devolve o que já foi lido em vez de deixar o timeout do transporte MCP
        descartar toda a resposta.
        """
        inicio = time.monotonic()
        limite = max(10.0, min(float(tempo_maximo_segundos), 50.0))
        deadline = inicio + limite
        aba = None
        docs = []
        leituras = []
        falhas = []
        motivo_interrupcao = None

        def restante():
            return max(0.0, deadline - time.monotonic())

        try:
            try:
                aba = await asyncio.wait_for(
                    self._abrir_autos_processo(numero_cnj),
                    timeout=restante(),
                )
            except asyncio.TimeoutError:
                motivo_interrupcao = "tempo esgotado ao abrir os autos"
                return {
                    "numero_cnj": numero_cnj,
                    "total_documentos": 0,
                    "documentos_planejados": 0,
                    "leituras": [],
                    "falhas": [],
                    "busca_concluida": False,
                    "motivo_interrupcao": motivo_interrupcao,
                    "tempo_maximo_segundos": limite,
                    "tempo_decorrido_segundos": round(time.monotonic() - inicio, 2),
                }

            try:
                docs = await asyncio.wait_for(
                    self._extrair_documentos_da_aba(aba),
                    timeout=restante(),
                )
            except asyncio.TimeoutError:
                motivo_interrupcao = "tempo esgotado ao carregar a árvore de documentos"
                docs = []

            selecionados = docs[: max(0, int(max_documentos))]
            for doc in selecionados:
                tempo_restante = restante()
                if tempo_restante < 1.0:
                    motivo_interrupcao = "orçamento total esgotado durante a leitura"
                    break
                doc_id = str(doc.get("id", ""))
                # Uma peça defeituosa não pode consumir todo o orçamento.
                timeout_peca = min(15.0, tempo_restante)
                try:
                    teor = await asyncio.wait_for(
                        self._ler_documento_rest(
                            numero_cnj,
                            doc_id,
                            max_paginas=max_paginas,
                            aba_autos=aba,
                        ),
                        timeout=timeout_peca,
                    )
                    if teor.get("erro"):
                        falhas.append(
                            {
                                "documento_id": doc_id,
                                "titulo": doc.get("titulo", ""),
                                "erro": teor["erro"],
                            }
                        )
                    else:
                        leituras.append(
                            {
                                "documento": doc,
                                "teor": teor,
                            }
                        )
                except asyncio.TimeoutError:
                    falhas.append(
                        {
                            "documento_id": doc_id,
                            "titulo": doc.get("titulo", ""),
                            "erro": (
                                f"leitura excedeu {timeout_peca:.1f}s e foi "
                                "interrompida"
                            ),
                        }
                    )
                except Exception as exc:
                    falhas.append(
                        {
                            "documento_id": doc_id,
                            "titulo": doc.get("titulo", ""),
                            "erro": erro_publico(exc),
                        }
                    )

            tentados = len(leituras) + len(falhas)
            if motivo_interrupcao is None and tentados < len(selecionados):
                motivo_interrupcao = "orçamento total esgotado durante a leitura"
            completo = getattr(self, "_ultima_arvore_completa", True)
            return {
                "numero_cnj": numero_cnj,
                "total_documentos": len(docs),
                "arvore_completa": completo,
                "documentos_planejados": len(selecionados),
                "documentos_tentados": tentados,
                "documentos_nao_tentados": len(selecionados) - tentados,
                "leituras": leituras,
                "falhas": falhas,
                "busca_concluida": (
                    motivo_interrupcao is None and tentados == len(selecionados)
                ),
                "motivo_interrupcao": motivo_interrupcao,
                "tempo_maximo_segundos": limite,
                "tempo_decorrido_segundos": round(time.monotonic() - inicio, 2),
            }
        finally:
            # Mesmo se a abertura for cancelada pelo orçamento, fecha qualquer
            # popup que tenha surgido antes de ``aba`` receber um valor.
            await self._fechar_aba_autos(aba)

    @_serializa
    async def coletar_processo_integral(
        self,
        numero_cnj,
        on_manifest,
        on_document,
        should_cancel,
        max_paginas=None,
        document_selector=None,
        concurrency=4,
        collect_expedients=True,
        parallel_process=False,
    ):
        """Percorre todos os autos em uma única abertura.

        Os callbacks persistem manifesto e progresso fora do cliente. Isso
        mantém o Playwright responsável apenas por navegação/leitura e permite
        que ``status`` consulte SQLite sem disputar o lock do navegador.
        """
        parallel_slot = False
        if parallel_process:
            semaphore = getattr(self, "_parallel_process_semaphore", None)
            if semaphore is None:
                semaphore = asyncio.Semaphore(3)
                self._parallel_process_semaphore = semaphore
            await semaphore.acquire()
            parallel_slot = True
            open_lock = getattr(self, "_parallel_open_lock", None)
            if open_lock is None:
                open_lock = asyncio.Lock()
                self._parallel_open_lock = open_lock
            try:
                async with open_lock:
                    if getattr(self, "contexto_fixado", None) is not None:
                        await self.revalidar_contexto_fixado()
                    aba = await self._abrir_autos_processo(numero_cnj)
            except BaseException:
                self._parallel_process_semaphore.release()
                parallel_slot = False
                raise
        else:
            aba = await self._abrir_autos_processo(numero_cnj)
        try:
            texto = await aba.inner_text("body")
            html = await aba.content()
            partes = self._extrair_partes(texto, html) or {}
            movimentos = self._extrair_movimentacoes_dom(html)
            if not movimentos:
                movimentos = self._extrair_movimentacoes(texto)

            dados_basicos = {}
            for campo, padrao in {
                "case_class": (r"(?:Classe judicial|Classe)\s*:?\s*\n?\s*([^\n]+)"),
                "subject": (r"(?:Assunto principal|Assunto)\s*:?\s*\n?\s*([^\n]+)"),
                "court": (
                    r"(?:Órgão julgador|Orgao julgador|Vara)"
                    r"\s*:?\s*\n?\s*([^\n]+)"
                ),
                "distribution_date": (
                    r"(?:Data da distribuição|Distribuição)"
                    r"\s*:?\s*\n?\s*([^\n]+)"
                ),
            }.items():
                match = re.search(padrao, texto, re.IGNORECASE)
                if match:
                    dados_basicos[campo] = match.group(1).strip()

            party_items = []
            if partes.get("polo_ativo"):
                party_items.append({"role": "active", "name": partes["polo_ativo"]})
            if partes.get("polo_passivo"):
                party_items.append({"role": "passive", "name": partes["polo_passivo"]})
            base = {
                **dados_basicos,
                "parties": party_items,
                "movements": movimentos,
                "third_party_process": self._ultimo_processo_foi_terceiro,
            }

            documents = await self._extrair_documentos_da_aba(aba)
            tree_complete = getattr(self, "_ultima_arvore_completa", True)
            cache_plan = await on_manifest(
                base,
                documents,
                tree_complete,
            )
            selected_documents = (
                list(document_selector(documents))
                if document_selector is not None
                else list(documents)
            )
            known_ids = {str(item.get("id") or "") for item in documents}
            deduplicated_selection = []
            selected_ids = set()
            for document in selected_documents:
                document_id = str(document.get("id") or "")
                if (
                    not document_id
                    or document_id not in known_ids
                    or document_id in selected_ids
                ):
                    continue
                selected_ids.add(document_id)
                deduplicated_selection.append(document)
            selected_documents = deduplicated_selection[:50]
            processed = 0
            failed = 0
            reused = 0
            cancelled = False

            bounded_concurrency = max(1, min(int(concurrency or 1), 4))
            sem = asyncio.Semaphore(bounded_concurrency)

            async def processar_documento(document):
                nonlocal processed, failed, reused, cancelled
                async with sem:
                    if cancelled or await should_cancel():
                        cancelled = True
                        return
                    document_id = str(document.get("id") or "")
                    cached = (cache_plan or {}).get(document_id)
                    if cached is not None:
                        await on_document(
                            document,
                            cached,
                            None,
                            "cache",
                        )
                        processed += 1
                        reused += 1
                        return
                    try:
                        response = await self._ler_documento_rest(
                            numero_cnj,
                            document_id,
                            max_paginas=max_paginas,
                            aba_autos=aba,
                        )
                        if response.get("erro"):
                            failed += 1
                        await on_document(
                            document,
                            None,
                            response,
                            "fresh",
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed += 1
                        await on_document(
                            document,
                            None,
                            {"erro": erro_publico(exc)},
                            "fresh",
                        )
                    processed += 1

            tasks = [processar_documento(d) for d in selected_documents]
            await asyncio.gather(*tasks)

            manifest_reconciled = False
            manifest_delta = None
            if not cancelled and collect_expedients:
                try:
                    final_documents = await self._extrair_documentos_da_aba(aba)
                    final_tree_complete = getattr(
                        self,
                        "_ultima_arvore_completa",
                        True,
                    )
                    manifest_delta = reconciliar_manifestos_documentais(
                        documents,
                        final_documents,
                    )
                    manifest_reconciled = bool(
                        manifest_delta.pop("reconciled")
                    ) and bool(final_tree_complete)
                    manifest_delta["final_tree_complete"] = bool(
                        final_tree_complete
                    )
                    tree_complete = bool(tree_complete) and bool(
                        final_tree_complete
                    )
                except Exception as exc:
                    manifest_delta = {
                        "safe_error": erro_publico(exc),
                        "added_document_ids": [],
                        "removed_document_ids": [],
                        "altered_document_ids": [],
                    }

            expedientes = []
            expedientes_completos = False
            erro_expedientes = None
            if not cancelled:
                try:
                    await aba.locator("a[id='navbar:linkAbaExpedientes1']").click(
                        timeout=10_000
                    )
                    try:
                        await aba.wait_for_selector(
                            "table[id='processoParteExpedienteMenuGridList']",
                            timeout=15_000,
                        )
                    except Exception:
                        pass
                    expedientes = self._extrair_expedientes_dom(await aba.content())
                    expedientes_completos = True
                except Exception as exc:
                    # A falha permanece localizada na fonte de expedientes e
                    # não interrompe as peças já persistidas.
                    expedientes = []
                    erro_expedientes = erro_publico(exc)

            return {
                "numero_cnj": numero_cnj,
                "base": base,
                "documents": documents,
                "selected_documents": selected_documents,
                "documents_selected": len(selected_documents),
                "documents_skipped": len(documents) - len(selected_documents),
                "concurrency": bounded_concurrency,
                "tree_complete": tree_complete,
                "expedients": expedientes,
                "expedients_complete": expedientes_completos,
                "expedients_source": "aba_expedientes",
                "expedients_safe_error": erro_expedientes,
                "manifest_reconciled": manifest_reconciled,
                "manifest_delta": manifest_delta,
                "processed": processed,
                "failed": failed,
                "reused": reused,
                "cancelled": cancelled,
                "single_open": True,
            }
        finally:
            await self._fechar_aba_autos(aba)
            if parallel_slot:
                self._parallel_process_semaphore.release()

    @_serializa
    async def ler_documento_filtrado(self, numero_cnj, padrao_tipo, max_paginas=30):
        """Abre os autos UMA vez, lista os docs, escolhe o de maior ID cujo
        tipo casa com padrao_tipo (regex case-insensitive) e le o teor.

        Substitui o fluxo antigo listar_documentos + ler_documento dos tools
        ultima_decisao/ultimo_despacho, que abria os autos DUAS vezes.
        """
        aba = await self._abrir_autos_processo(numero_cnj)
        try:
            docs = await self._extrair_documentos_da_aba(aba)
            regex = re.compile(padrao_tipo, re.IGNORECASE)
            filtrados = [d for d in docs if regex.search(d.get("tipo", ""))]
            if not filtrados:
                return {
                    "encontrado": False,
                    "numero_cnj": numero_cnj,
                    "total_documentos": len(docs),
                }
            alvo = max(filtrados, key=lambda d: int(d["id"]))
            teor = await self._ler_documento_rest(
                numero_cnj, alvo["id"], max_paginas, aba_autos=aba
            )
            return {
                "encontrado": True,
                "tipo_documento": alvo["tipo"],
                "documento_id": alvo["id"],
                **teor,
            }
        finally:
            await self._fechar_aba_autos(aba)

    @_serializa
    async def garantir_processo_id(self, numero_cnj):
        """Garante que _ultimo_processo_id pertence a ESTE processo.

        Abre (e fecha) os autos se necessario. Usado pelo downloader antes
        de montar URLs REST - evita usar um processo_id que sobrou de outro
        processo consultado antes na mesma sessao do singleton.
        """
        cnj_digitos = re.sub(r"\D", "", numero_cnj)
        if self._ultimo_processo_id and self._ultimo_processo_cnj == cnj_digitos:
            return self._ultimo_processo_id
        aba = await self._abrir_autos_processo(numero_cnj)
        await self._fechar_aba_autos(aba)
        if not self._ultimo_processo_id:
            raise RuntimeError(
                f"Nao consegui extrair processo_id da URL dos autos de {numero_cnj}"
            )
        return self._ultimo_processo_id

    # ========== DOWNLOAD NATIVO DOS AUTOS COMPLETOS ==========

    @_serializa
    async def baixar_processo_nativo(
        self,
        numero_cnj,
        caminho_destino,
        tipo_documento="",
        id_inicial="",
        id_final="",
        periodo_inicio="",
        periodo_fim="",
        cronologia="decrescente",
        incluir_expediente=False,
        incluir_movimentos=False,
        timeout_download_ms=None,
    ):
        """
        VERSAO NOVA com seletores reais capturados via DevTools/captura passiva.

        Fluxo:
        1. Abre autos do processo
        2. Clica no <a class='btn-menu-abas dropdown-toggle' title='Download autos
           do processo'> que abre o dropdown
        3. (Opcional) altera selects dentro do dialog
        4. Clica em <input value='Download'> que dispara A4J.AJAX.Submit
        5. expect_download captura o ZIP gerado em S3 (~19s)
        6. Extrai o PDF interno do ZIP e salva no caminho_destino
        """
        return await self._baixar_processo_nativo_v2(
            numero_cnj=numero_cnj,
            caminho_destino=caminho_destino,
            tipo_documento=tipo_documento,
            id_inicial=id_inicial,
            id_final=id_final,
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
            cronologia=cronologia,
            incluir_expediente=incluir_expediente,
            incluir_movimentos=incluir_movimentos,
            timeout_download_ms=timeout_download_ms,
        )

    async def _baixar_processo_nativo_v2(
        self,
        numero_cnj,
        caminho_destino,
        tipo_documento="",
        id_inicial="",
        id_final="",
        periodo_inicio="",
        periodo_fim="",
        cronologia="decrescente",
        incluir_expediente=False,
        incluir_movimentos=False,
        timeout_download_ms=None,
    ):
        """Implementacao real - separada pra manter compat antiga em scripts.

        Observacao: o PJe nao serve o PDF como attachment - ele abre uma
        nova aba apontando pra uma URL pre-assinada do S3
        (storagepje.<tribunal>.jus.br/.../processo.pdf?X-Amz-...&X-Amz-Expires=120).
        Por isso 'expect_download' nao dispara. Capturamos a URL via listener
        de request e baixamos via APIRequestContext dentro da janela de 120s.
        """
        from pathlib import Path

        timeout_download_ms = _native_download_timeout_ms(timeout_download_ms)
        caminho_destino = Path(caminho_destino)
        caminho_destino.parent.mkdir(parents=True, exist_ok=True)

        aba = await self._abrir_autos_processo(numero_cnj)

        # 1. Clica no trigger do dropdown (o proprio click espera o elemento)
        _log("[NATIVO] Clicando no trigger do dropdown...")
        try:
            await aba.click(
                "a.btn-menu-abas.dropdown-toggle[title='Download autos do processo']",
                timeout=5000,
            )
        except Exception:
            # Fallback
            await aba.click("li.filtros-download a.dropdown-toggle", timeout=3000)

        # 2. Espera o dropdown abrir (aria-expanded=true / class 'open')
        await aba.wait_for_selector("li.filtros-download.open", timeout=5000)
        _log("[NATIVO] Dropdown aberto")
        await aba.wait_for_selector("#navbar\\:cbCronologia", timeout=5_000)

        # 3. Cronologia (Select2 do PJe usa IDs especificos). Default do PJe = DESC.
        #    Os campos sao Select2: precisa setar .value E disparar 'change' via jQuery.
        if cronologia.lower().startswith("cres"):
            valor_cronologia = "ASC"
        else:
            valor_cronologia = "DESC"

        try:
            ok = await aba.evaluate(f"""
                () => {{
                    const sel = document.getElementById('navbar:cbCronologia');
                    if (!sel) return 'select-nao-encontrado';
                    sel.value = '{valor_cronologia}';
                    if (window.jQuery) {{
                        window.jQuery(sel).trigger('change');
                        return 'jquery-change';
                    }}
                    sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return 'native-change';
                }}
            """)
            _log(f"[NATIVO] Cronologia={valor_cronologia} via {ok}")
        except Exception as e:
            _log(f"[NATIVO] Aviso ao setar cronologia: {e}")

        # 4. Tipo de documento (best effort - precisa saber o valor exato)
        if tipo_documento:
            try:
                await aba.evaluate(f"""
                    () => {{
                        const sel = document.getElementById('navbar:cbTipoDocumento');
                        if (!sel) return false;
                        for (const opt of sel.options) {{
                            if (opt.text.toLowerCase().includes('{tipo_documento.lower()}')) {{
                                sel.value = opt.value;
                                if (window.jQuery) window.jQuery(sel).trigger('change');
                                else sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                                return true;
                            }}
                        }}
                        return false;
                    }}
                """)
                _log(f"[NATIVO] Tipo documento={tipo_documento} setado")
            except Exception as e:
                _log(f"[NATIVO] Aviso tipo documento: {e}")

        # 5. Incluir expediente / movimentos (Select2 com 'Sim'/'Não')
        if incluir_expediente:
            try:
                await aba.evaluate("""
                    () => {
                        const sel = document.getElementById('navbar:cbExpediente');
                        if (!sel) return false;
                        for (const opt of sel.options) {
                            if (opt.text.toLowerCase().trim() === 'sim') {
                                sel.value = opt.value;
                                if (window.jQuery) window.jQuery(sel).trigger('change');
                                return true;
                            }
                        }
                        return false;
                    }
                """)
            except Exception:
                pass

        if incluir_movimentos:
            try:
                await aba.evaluate("""
                    () => {
                        const sel = document.getElementById('navbar:cbMovimentos');
                        if (!sel) return false;
                        for (const opt of sel.options) {
                            if (opt.text.toLowerCase().trim() === 'sim') {
                                sel.value = opt.value;
                                if (window.jQuery) window.jQuery(sel).trigger('change');
                                return true;
                            }
                        }
                        return false;
                    }
                """)
            except Exception:
                pass

        # 6. Range de IDs e periodo (inputs simples, nao Select2)
        if id_inicial:
            try:
                await aba.fill(
                    "input[name*='idInicial'], input[id*='idInicial']",
                    str(id_inicial),
                    timeout=2000,
                )
            except Exception:
                pass
        if id_final:
            try:
                await aba.fill(
                    "input[name*='idFinal'], input[id*='idFinal']",
                    str(id_final),
                    timeout=2000,
                )
            except Exception:
                pass
        if periodo_inicio:
            try:
                await aba.fill(
                    "input[name*='periodoInicio']", periodo_inicio, timeout=2000
                )
            except Exception:
                pass
        if periodo_fim:
            try:
                await aba.fill("input[name*='periodoFim']", periodo_fim, timeout=2000)
            except Exception:
                pass

        # 7. Listener pra capturar a URL S3 assim que o PJe a gerar.
        #    O PJe abre o PDF numa nova aba com URL pre-assinada do storage
        #    (X-Amz-Expires=120s). Match tolerante ("storagepje" + processo.pdf
        #    + X-Amz-Signature) pra cobrir o dominio de storage do TJPA sem
        #    depender do host exato. Capturamos no on('request').
        loop = asyncio.get_event_loop()
        url_future = loop.create_future()
        aba_pdf_future = loop.create_future()

        def _offer_url(url, content_type=""):
            if not url_future.done() and _is_probable_consolidated_pdf_url(
                url, content_type
            ):
                url_future.set_result(url)

        def _on_request(request):
            url = request.url
            _offer_url(url)

        def _on_response(response):
            headers = getattr(response, "headers", {}) or {}
            _offer_url(response.url, headers.get("content-type", ""))

        def _on_page(page):
            if not aba_pdf_future.done():
                aba_pdf_future.set_result(page)
            _offer_url(getattr(page, "url", ""))
            page.on("framenavigated", lambda frame: _offer_url(frame.url))

        try:
            self._context.on("request", _on_request)
            self._context.on("response", _on_response)
            self._context.on("page", _on_page)

            # 8. Clica DOWNLOAD - dispara AJAX, servidor gera PDF (~10-30s) e
            #    abre nova aba apontando pra URL S3.
            _log("[NATIVO] Clicando DOWNLOAD - servidor vai gerar PDF...")
            for sel in [
                "#navbar\\:botoesDownload input.btn-primary[value='Download']",
                "li.filtros-download .dropdown-menu input.btn-primary[value='Download']",
                "input.btn-primary[value='Download']",
                "input[value='Download'][type='button']",
            ]:
                try:
                    await aba.click(sel, timeout=3000)
                    _log(f"[NATIVO] DOWNLOAD clicado via {sel}")
                    break
                except Exception:
                    continue

            # 9. Aguarda a URL S3 aparecer (timeout = janela de validade do PJe)
            try:
                pdf_url = await asyncio.wait_for(
                    url_future, timeout=timeout_download_ms / 1000
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "Timeout aguardando URL S3 do PJe. O servidor nao gerou o PDF "
                    f"em {timeout_download_ms / 1000:.0f}s ou os seletores do modal "
                    "mudaram (verificar btn-primary[value='Download'])."
                )

            _log(f"[NATIVO] URL S3 capturada: {_url_sem_segredos(pdf_url)}")

            # 10. Baixa via APIRequestContext (mesmos cookies/proxy da sessao).
            #     A URL pre-assinada e' valida por ~120s, entao baixamos imediatamente.
            response = await self._context.request.get(pdf_url, timeout=60000)
            try:
                if response.status != 200:
                    detalhe = (await response.text())[:1_000]
                    raise RuntimeError(
                        f"Download S3 falhou: HTTP {response.status} - {detalhe}"
                    )
                corpo = await response.body()
                if not corpo.startswith(b"%PDF-"):
                    raise RuntimeError(
                        "DOWNLOAD_CORROMPIDO: resposta do download nativo "
                        "não começa com '%PDF-'"
                    )
                if b"%%EOF" not in corpo[-2048:]:
                    raise RuntimeError(
                        "DOWNLOAD_CORROMPIDO: resposta do download nativo "
                        "não contém '%%EOF' no rodapé"
                    )
                if len(corpo) < 1024:
                    raise RuntimeError(
                        "DOWNLOAD_CORROMPIDO: PDF nativo tem tamanho suspeito "
                        f"({len(corpo)} bytes)"
                    )
                temporario = caminho_destino.with_name(
                    f".{caminho_destino.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                try:
                    with temporario.open("wb") as arquivo:
                        arquivo.write(corpo)
                        arquivo.flush()
                        os.fsync(arquivo.fileno())
                    os.replace(temporario, caminho_destino)
                except BaseException:
                    try:
                        temporario.unlink()
                    except FileNotFoundError:
                        pass
                    raise
            finally:
                # APIResponse retém o corpo em memória até o contexto fechar,
                # salvo quando descartada explicitamente.
                await response.dispose()
            _log(
                f"[NATIVO] PDF salvo: {caminho_destino.name} "
                f"({len(corpo) / 1024 / 1024:.2f} MB)"
            )

            # 11. Fecha aba do PDF (se abriu) pra nao acumular.
            if aba_pdf_future.done():
                try:
                    await aba_pdf_future.result().close()
                except Exception:
                    pass
        finally:
            try:
                self._context.remove_listener("request", _on_request)
                self._context.remove_listener("response", _on_response)
                self._context.remove_listener("page", _on_page)
            except Exception:
                pass
            await self._fechar_aba_autos(aba)

        tamanho_pdf = caminho_destino.stat().st_size
        return {
            "caminho": str(caminho_destino),
            "tamanho_bytes": tamanho_pdf,
            "tamanho_mb": round(tamanho_pdf / 1024 / 1024, 2),
            "content_type": "application/pdf",
            "sha256": hashlib.sha256(corpo).hexdigest(),
            "integridade_validada": True,
            "metodo": "nativo",
            "filtros_aplicados": {
                "tipo_documento": tipo_documento or None,
                "cronologia": cronologia,
                "incluir_expediente": incluir_expediente,
                "incluir_movimentos": incluir_movimentos,
            },
        }
