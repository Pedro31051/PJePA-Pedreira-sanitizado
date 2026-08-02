"""Singleton de PJeClient com sessao persistente.

Mantem 1 unica sessao do PJe ativa entre tool calls do MCP server,
evitando logins repetidos (que disparam rate-limiting do SSO do PDPJ).

Comportamento:
- Startup do server: warmup() faz login antecipado em background (evita
  -32001 na 1a tool call a frio)
- Chamadas seguintes em <5min, mesma persona+grau: reusa (~5-10s, sem login)
- Troca de persona/grau OU >5min sem uso: fecha o anterior e cria novo
- Watchdog do server chama fechar_se_ocioso() periodicamente: fecha o
  Chromium DE FATO apos o timeout (nao so na proxima chamada)
- Shutdown (lifespan do server) + atexit (backstop): cleanup

Trade-offs aceitos:
- Cookie do PJe vivo em RAM por ate 5min (nao em disco - usa context normal,
  nao launch_persistent_context). Aceitavel.
- Se o servidor PJe expirar a sessao no meio (timeout proprio), proxima tool
  call retorna erro - nao implementamos retry automatico no meio de scraping.
"""
import asyncio
import atexit
import hashlib
import json
import math
import os
import resource
import sys
import time
from pathlib import Path

import keyring

import operation_context
import perfil_contexto
from pje_client import PJeClient
from session_manager import SessionKey, SessionManager

# O login e' no SSO nacional do PDPJ (sso.cloud.pje.jus.br) - a MESMA conta
# CPF+senha+TOTP vale pra qualquer tribunal. Por isso, se nao houver
# credenciais especificas do TJPA, reusa as ja salvas pros MCPs irmaos
# (TJMA/TJPI, mesma familia de servidores).
KEYRING_SERVICE = "mcp-pje-tjpa"
KEYRING_SERVICES_FALLBACK = ("mcp-pje-tjma", "mcp-pje-tjpi")


def _env_int(nome: str, padrao: int, minimo: int = 1) -> int:
    try:
        return max(minimo, int(os.environ.get(nome, str(padrao))))
    except (TypeError, ValueError):
        return padrao


TIMEOUT_INATIVIDADE_S = _env_int("PJE_SESSION_IDLE_SECONDS", 1800, 60)
TTL_CACHE_SESSAO_S = _env_int("PJE_SESSION_CACHE_SECONDS", 1800, 60)
MAX_ESTADOS_SESSAO = min(
    8,
    _env_int("PJE_SESSION_CACHE_MAX_ENTRIES", 4, 1),
)
MAX_ESTADO_SESSAO_BYTES = 2 * 1024 * 1024
SSO_COOLDOWN_S = _env_int("PJE_SSO_COOLDOWN_SECONDS", 180, 30)
SSO_LIMITE_FALHAS = _env_int("PJE_SSO_CIRCUIT_FAILURES", 1, 1)

# Estado global do singleton
_cliente: PJeClient | None = None
_chave_ativa: tuple | None = None  # (usuario, persona, grau, perfil_id)
_ultimo_uso: float = 0
_lock = asyncio.Lock()
_ultima_metrica_sessao: dict = {}
# Cache de handoff: nunca guarda Browser/Context/Page, apenas storage_state
# Playwright em memória. Cada valor pertence a uma sessão já fechada.
_cache_estados_sessao: dict[tuple, tuple[float, dict]] = {}
_sso_falhas_consecutivas = 0
_sso_bloqueado_ate = 0.0

_MODOS_METRICA = frozenset(
    {
        "reuso_exato",
        "sessao_nova",
        "sessao_restaurada",
        "falha_criacao",
        "invalido",
    }
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _exigir_circuito_sso_fechado() -> None:
    restante = _sso_bloqueado_ate - time.monotonic()
    if restante > 0:
        raise RuntimeError(
            "SSO_CIRCUIT_BREAKER_ATIVO: novas autenticações interrompidas; "
            f"aguarde {math.ceil(restante)} segundo(s)"
        )


def _registrar_resultado_sso(*, sucesso: bool, indeterminado: bool = False) -> None:
    global _sso_falhas_consecutivas, _sso_bloqueado_ate
    if sucesso:
        _sso_falhas_consecutivas = 0
        _sso_bloqueado_ate = 0.0
        return
    _sso_falhas_consecutivas += 1
    if indeterminado or _sso_falhas_consecutivas >= SSO_LIMITE_FALHAS:
        _sso_bloqueado_ate = time.monotonic() + SSO_COOLDOWN_S


def _numero_metrica(valor) -> float:
    """Converte duração para número finito e não negativo."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numero) or numero < 0:
        return 0.0
    return round(numero, 2)


def _registrar_metrica(
    modo: str,
    *,
    cache_hit: bool = False,
    estado_capturado: bool = False,
    espera_lock_ms: float = 0.0,
    captura_estado_ms: float = 0.0,
    fechamento_ms: float = 0.0,
    iniciar_browser_ms: float = 0.0,
    login_ms: float = 0.0,
    perfil_ms: float = 0.0,
    total_ms: float = 0.0,
) -> None:
    """Telemetria por whitelist; não aceita texto livre nem identificadores."""
    global _ultima_metrica_sessao
    modo_seguro = modo if modo in _MODOS_METRICA else "invalido"
    _ultima_metrica_sessao = {
        "schema_version": "pje.session-performance/v1",
        "modo": modo_seguro,
        "cache_hit": bool(cache_hit),
        "estado_capturado": bool(estado_capturado),
        "espera_lock_ms": _numero_metrica(espera_lock_ms),
        "captura_estado_ms": _numero_metrica(captura_estado_ms),
        "fechamento_ms": _numero_metrica(fechamento_ms),
        "iniciar_browser_ms": _numero_metrica(iniciar_browser_ms),
        "login_ms": _numero_metrica(login_ms),
        "perfil_ms": _numero_metrica(perfil_ms),
        "total_ms": _numero_metrica(total_ms),
    }
    _log(
        "[PERF] "
        + json.dumps(
            _ultima_metrica_sessao,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


def _descartar_estado_sensivel(valor) -> None:
    """Solta referências aninhadas ao expirar/invalidar um estado em RAM."""
    if isinstance(valor, dict):
        for item in list(valor.values()):
            _descartar_estado_sensivel(item)
        valor.clear()
    elif isinstance(valor, list):
        for item in valor:
            _descartar_estado_sensivel(item)
        valor.clear()


def _normalizar_estado_sessao(estado) -> dict | None:
    """Aceita somente o contrato JSON cookies/origins e limita uso de RAM."""
    if not isinstance(estado, dict):
        return None
    cookies = estado.get("cookies", [])
    origins = estado.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        return None
    if any(not isinstance(item, dict) for item in (*cookies, *origins)):
        return None
    try:
        serializado = json.dumps(
            {"cookies": cookies, "origins": origins},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None
    if len(serializado.encode("utf-8")) > MAX_ESTADO_SESSAO_BYTES:
        return None
    return json.loads(serializado)


def _expurgar_estados_expirados(agora_monotonic: float | None = None) -> int:
    agora = time.monotonic() if agora_monotonic is None else agora_monotonic
    expirados = [
        chave
        for chave, (expira_em, _estado) in _cache_estados_sessao.items()
        if expira_em <= agora
    ]
    for chave in expirados:
        _expira_em, estado = _cache_estados_sessao.pop(chave)
        _descartar_estado_sensivel(estado)
    return len(expirados)


def _limpar_cache_estados() -> None:
    for _expira_em, estado in list(_cache_estados_sessao.values()):
        _descartar_estado_sensivel(estado)
    _cache_estados_sessao.clear()


def _guardar_estado_sessao(
    chave: tuple,
    estado,
    *,
    validade_s: float | None = None,
    agora_monotonic: float | None = None,
) -> bool:
    """Guarda o estado mais recente por persona+grau, nunca em disco."""
    normalizado = _normalizar_estado_sessao(estado)
    if normalizado is None or len(chave) not in (3, 4):
        return False
    agora = time.monotonic() if agora_monotonic is None else agora_monotonic
    validade = TTL_CACHE_SESSAO_S if validade_s is None else validade_s
    validade = min(TTL_CACHE_SESSAO_S, max(0.0, float(validade)))
    if validade <= 0:
        _descartar_estado_sensivel(normalizado)
        return False
    _expurgar_estados_expirados(agora)

    # Estados do mesmo usuario+persona+grau representam a mesma sessão de backend.
    # Só o snapshot mais novo pode permanecer disponível para handoff.
    prefix_len = 2 if len(chave) == 3 else 3
    for anterior in [
        item for item in _cache_estados_sessao if len(item) == len(chave) and item[:prefix_len] == chave[:prefix_len]
    ]:
        _expira_em, obsoleto = _cache_estados_sessao.pop(anterior)
        _descartar_estado_sensivel(obsoleto)

    while len(_cache_estados_sessao) >= MAX_ESTADOS_SESSAO:
        mais_antiga = min(
            _cache_estados_sessao,
            key=lambda item: _cache_estados_sessao[item][0],
        )
        _expira_em, obsoleto = _cache_estados_sessao.pop(mais_antiga)
        _descartar_estado_sensivel(obsoleto)

    _cache_estados_sessao[chave] = (agora + validade, normalizado)
    return True


def _retirar_estado_sessao(
    chave: tuple,
    *,
    agora_monotonic: float | None = None,
) -> dict | None:
    """Transfere um snapshot compatível ao novo cliente, em uso único."""
    agora = time.monotonic() if agora_monotonic is None else agora_monotonic
    _expurgar_estados_expirados(agora)
    escolhida = chave if chave in _cache_estados_sessao else None
    if escolhida is None:
        prefix_len = 2 if len(chave) == 3 else 3
        compativeis = [
            item for item in _cache_estados_sessao if len(item) == len(chave) and item[:prefix_len] == chave[:prefix_len]
        ]
        if compativeis:
            escolhida = max(
                compativeis,
                key=lambda item: _cache_estados_sessao[item][0],
            )
    if escolhida is None:
        return None
    _expira_em, estado = _cache_estados_sessao.pop(escolhida)
    return estado


async def _capturar_estado_sessao(
    cliente: PJeClient,
    chave: tuple,
    *,
    validade_s: float,
) -> bool:
    """Captura somente contexto próprio; CDP e contexto alheio são excluídos."""
    if getattr(cliente, "_browser_mode", None) != "launch":
        return False
    contexto = getattr(cliente, "_context", None)
    storage_state = getattr(contexto, "storage_state", None)
    if not callable(storage_state):
        return False
    try:
        estado = await storage_state()
    except Exception:
        return False
    return _guardar_estado_sessao(chave, estado, validade_s=validade_s)


def _creds_systemd():
    """Le credenciais entregues pelo systemd via LoadCredential=.

    No Linux (apps-prod) nao ha Keychain: a unit pjepa-mcp.service monta
    /etc/pjepa-mcp/{cpf,senha,totp_seed} em tmpfs e aponta
    $CREDENTIALS_DIRECTORY pra la. Fora do systemd a variavel nao existe
    e retornamos None (cai no keyring, caminho original do macOS).
    """
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not cred_dir:
        return None
    try:
        vals = []
        for nome in ("cpf", "senha", "totp_seed"):
            with open(os.path.join(cred_dir, nome)) as f:
                vals.append(f.read().strip())
    except OSError:
        return None
    return tuple(vals) if all(vals) else None


def _creds_keyring():
    for service in (KEYRING_SERVICE, *KEYRING_SERVICES_FALLBACK):
        try:
            cpf = keyring.get_password(service, "cpf")
            senha = keyring.get_password(service, "senha")
            seed = keyring.get_password(service, "totp_seed")
        except Exception:
            # Falha de backend (Linux sem dbus/gnome-keyring, NoKeyringError) ou
            # de um service especifico. Tenta o proximo em vez de desistir do
            # fallback inteiro por causa do primeiro erro.
            continue
        if all([cpf, senha, seed]):
            if service != KEYRING_SERVICE:
                _log(f"[SINGLETON] Usando credenciais PDPJ do service '{service}'")
            return cpf, senha, seed
    return None


def _get_creds():
    creds = _creds_systemd() or _creds_keyring()
    if creds:
        return creds
    raise RuntimeError(
        "Credenciais nao encontradas: nem via systemd LoadCredential "
        "($CREDENTIALS_DIRECTORY) nem no Keychain (services 'mcp-pje-tjpa', "
        "'mcp-pje-tjma' e 'mcp-pje-tjpi'). No Linux, confira /etc/pjepa-mcp/ "
        "e a unit pjepa-mcp.service; no macOS, execute setup_credenciais.py."
    )


async def get_cliente(
    persona: str = "advogado",
    grau: str = "1g",
    permitir_sem_perfil: bool = False,
) -> PJeClient:
    """Retorna um PJeClient pronto pra uso delegando ao SessionManager.

    Reusa a chave exata ou transfere, somente em RAM, o storage_state de uma
    sessão fechada compatível. Nunca mantém dois clientes Playwright vivos.
    """
    global _cliente, _chave_ativa, _ultimo_uso
    perfil = perfil_contexto.contexto_atual()
    if (
        persona in perfil_contexto.PERSONAS_INTERNAS
        and perfil
        and _cliente is not None
        and getattr(_cliente, "contexto_fixado", None)
        and perfil_contexto.normalizar_texto(perfil.get("rotulo", ""))
        == perfil_contexto.normalizar_texto(
            _cliente.contexto_fixado.get("localizador_nao_confiavel", "")
        )
    ):
        return await get_sessao_contexto_fixado(
            localizador_nao_confiavel=perfil["rotulo"],
            persona=persona,
            grau=grau,
        )
    if persona in perfil_contexto.PERSONAS_INTERNAS and not perfil:
        if not permitir_sem_perfil:
            perfil_contexto.exigir_contexto(persona)
        perfil_id = "descoberta"
    else:
        perfil_id = (perfil or {}).get("pje_id", "")
    try:
        cpf, _, _ = _get_creds()
    except Exception:
        cpf = "mock_user"

    session_key = SessionKey(usuario_cpf=cpf, persona=persona, grau=grau, perfil_id=perfil_id)
    session_mgr = SessionManager.get_instance()

    op_ctx = operation_context.get_current_operation_context()
    if op_ctx is not None and op_ctx.lease is None:
        lease = await op_ctx.acquire_lease(
            session_mgr, permitir_sem_perfil=permitir_sem_perfil
        )
        _cliente = lease.client
        _chave_ativa = session_key.to_tuple()
        _ultimo_uso = time.time()
        return lease.client
    elif op_ctx is not None and op_ctx.lease is not None:
        _cliente = op_ctx.lease.client
        _chave_ativa = session_key.to_tuple()
        _ultimo_uso = time.time()
        return op_ctx.lease.client

    entry = await session_mgr.get_or_create_session(
        session_key, perfil_dict=perfil, permitir_sem_perfil=permitir_sem_perfil
    )

    _cliente = entry.client
    _chave_ativa = session_key.to_tuple()
    _ultimo_uso = time.time()
    return entry.client


async def criar_sessao_contexto_fixado(
    *,
    localizador_nao_confiavel: str,
    persona: str = "servidor",
    grau: str = "1g",
    confirmar_localizacao_nao_aplicavel: str = "",
) -> PJeClient:
    """Cria um navegador novo e só publica a sessão após validar o contexto."""
    global _cliente, _chave_ativa, _ultimo_uso
    p = perfil_contexto.normalizar_persona(persona)
    if p not in perfil_contexto.PERSONAS_INTERNAS:
        raise RuntimeError(
            "CONTEXTO_DIVERGENTE: sessão fixada exige persona interna"
        )
    if not str(localizador_nao_confiavel or "").strip():
        raise RuntimeError("PERFIL_OBRIGATORIO: informe o rótulo funcional completo")

    async with _lock:
        _exigir_circuito_sso_fechado()
        # A criação é deliberadamente não reutilizável: outro navegador e
        # nenhum cookie/storage_state entram no novo contexto.
        if _cliente is not None:
            async with _cliente._op_lock:
                try:
                    await _cliente._fechar()
                finally:
                    _cliente = None
                    _chave_ativa = None

        cpf, senha, seed = _get_creds()
        novo = PJeClient(
            cpf,
            senha,
            seed,
            persona=p,
            headless=os.environ.get("PJE_HEADLESS", "1") == "1",
            grau=grau,
            perfil=None,
            storage_state=None,
            isolamento_total=True,
        )
        login_concluido = False
        try:
            await novo._iniciar()
            await novo._login()
            login_concluido = True
            _registrar_resultado_sso(sucesso=True)
            confirmado = await novo.fixar_contexto_por_rotulo(
                localizador_nao_confiavel,
                confirmar_localizacao_nao_aplicavel,
            )
        except BaseException as exc:
            if not login_concluido:
                _registrar_resultado_sso(
                    sucesso=False,
                    indeterminado="LOGIN_ESTADO_INDETERMINADO" in str(exc),
                )
            try:
                await asyncio.shield(novo._fechar())
            except BaseException:
                pass
            _cliente = None
            _chave_ativa = None
            _ultimo_uso = 0
            raise

        _cliente = novo
        _chave_ativa = ("contexto_fixado", confirmado["contexto_validado_id"])
        _ultimo_uso = time.time()
        return novo


async def get_sessao_contexto_fixado(
    *, localizador_nao_confiavel: str, persona: str, grau: str
) -> PJeClient:
    """Reusa apenas a sessão já fixada exata; divergência a destrói."""
    global _cliente, _chave_ativa, _ultimo_uso
    async with _lock:
        cliente = _cliente
        if (
            cliente is None
            or cliente.contexto_fixado is None
            or cliente.persona != perfil_contexto.normalizar_persona(persona)
            or cliente.grau != grau
            or perfil_contexto.normalizar_texto(
                cliente.contexto_fixado["localizador_nao_confiavel"]
            )
            != perfil_contexto.normalizar_texto(localizador_nao_confiavel)
        ):
            raise RuntimeError(
                "CONTEXTO_NAO_FIXADO: crie uma sessão isolada para este contexto"
            )
        try:
            await cliente.revalidar_contexto_fixado()
        except BaseException:
            async with cliente._op_lock:
                try:
                    await cliente._fechar()
                finally:
                    _cliente = None
                    _chave_ativa = None
                    _ultimo_uso = 0
            raise
        _ultimo_uso = time.time()
        return cliente


def sessao_fixada_compativel(persona: str, grau: str, perfil: str) -> bool:
    """Diz se a sessão fixada viva cobre exatamente (persona, grau, rótulo).

    Só leitura de estado, sem lock: serve para gates decidirem se o fluxo
    pode seguir até get_cliente(), que roteia para a sessão fixada e a
    revalida contra o PJe. Divergência posterior continua fail-closed lá.
    """
    cliente = _cliente
    if cliente is None or not getattr(cliente, "contexto_fixado", None):
        return False
    return (
        cliente.persona == perfil_contexto.normalizar_persona(persona)
        and cliente.grau == str(grau)
        and perfil_contexto.normalizar_texto(
            cliente.contexto_fixado.get("localizador_nao_confiavel", "")
        )
        == perfil_contexto.normalizar_texto(perfil)
    )


def promover_contexto_fixado(persona: str, grau: str, perfil: str) -> bool:
    """Promove o ContextVar da chamada para o contexto fixado compatível.

    Sem isso, escrita (snapshot com perfil_id = contexto_validado_id) e
    leitura (contexto provisório com HMAC do rótulo) nunca se encontram.
    A promoção só copia identidade já comprovada contra o PJe na fixação;
    ações que abrem navegador ainda revalidam em get_sessao_contexto_fixado.
    """
    if not sessao_fixada_compativel(persona, grau, perfil):
        return False
    perfil_contexto.definir_contexto_fixado(
        perfil_contexto.SessaoContextoFixado(**_cliente.contexto_fixado)
    )
    return True


async def warmup(persona: str = "servidor") -> None:
    """Faz o login ANTECIPADO, em background, no startup do servidor MCP.

    Motivo: a 1a tool call a frio (login ~25s + acao ~15s) estoura o timeout
    do protocolo MCP (-32001). Com warm-up, quando a 1a chamada chegar a
    sessao ja esta quente. Se a chamada chegar DURANTE o warm-up, ela espera
    no _lock do get_cliente e reusa o login em andamento (nao duplica).
    Falha de warm-up nao e' fatal: o login acontece na 1a chamada como antes.
    """
    try:
        _log("[SINGLETON] Warm-up: login antecipado em background...")
        # Usuário interno só pode aquecer uma sessão após escolher perfil.
        # O warm-up genérico limita-se a perfis externos.
        if persona in perfil_contexto.PERSONAS_INTERNAS:
            return
        await get_cliente(persona)
        _log("[SINGLETON] Warm-up concluido - sessao pronta")
    except Exception as e:
        _log(f"[SINGLETON] Warm-up falhou ({e.__class__.__name__}: {e}); "
             "login ocorrera na 1a tool call")


async def fechar_se_ocioso() -> bool:
    """Fecha a sessao se passou do timeout de inatividade. Retorna True se fechou.

    Chamado periodicamente pelo watchdog do server. Itera sobre todas as sessoes no
    SessionManager e fecha qualquer uma sem leases ativas e com inatividade > PJE_SESSION_IDLE_SECONDS.
    """
    global _cliente, _chave_ativa
    fechou_alguma = False
    session_mgr = SessionManager.get_instance()
    fechadas_sm = await session_mgr.fechar_sessoes_ociosas()
    if fechadas_sm > 0:
        fechou_alguma = True

    async with _lock:
        _expurgar_estados_expirados()
        if _cliente is not None:
            idade = time.time() - _ultimo_uso
            if idade >= TIMEOUT_INATIVIDADE_S:
                async with _cliente._op_lock:
                    _log(f"[SINGLETON] Watchdog: fechando sessao ociosa ({idade:.0f}s)")
                    try:
                        await _cliente._fechar()
                    except Exception as e:
                        _log(f"[SINGLETON] Erro ao fechar (ignorado): {e}")
                _cliente = None
                _chave_ativa = None
                _limpar_cache_estados()
                fechou_alguma = True
    return fechou_alguma


async def fechar_cliente():
    """Fecha a sessao atual (chamado no shutdown do server/atexit)."""
    global _cliente, _chave_ativa, _ultimo_uso, _sso_falhas_consecutivas, _sso_bloqueado_ate
    _sso_falhas_consecutivas = 0
    _sso_bloqueado_ate = 0.0
    session_mgr = SessionManager.get_instance()
    session_mgr._sso_falhas_consecutivas = 0
    session_mgr._sso_bloqueado_ate = 0.0
    await session_mgr.close_all_sessions()
    async with _lock:
        if _cliente is not None:
            _log("[SINGLETON] Cleanup atexit - fechando sessao")
            async with _cliente._op_lock:
                try:
                    await _cliente._fechar()
                except Exception as e:
                    _log(f"[SINGLETON] Erro no cleanup: {e}")
            _cliente = None
            _chave_ativa = None
            _ultimo_uso = 0
        _limpar_cache_estados()


def _atexit_handler():
    """Wrapper sync pro atexit (que nao aceita coroutine)."""
    if _cliente is None and not _cache_estados_sessao:
        return
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            loop.create_task(fechar_cliente())
        else:
            new_loop = asyncio.new_event_loop()
            try:
                new_loop.run_until_complete(fechar_cliente())
            finally:
                new_loop.close()
    except Exception as e:
        # atexit nao deve falhar
        print(f"[SINGLETON] Erro no atexit handler: {e}", file=sys.stderr)


# Registra cleanup automatico ao desligar o processo
atexit.register(_atexit_handler)


def status_sessao() -> dict:
    """Retorna estado atual da sessao sem modificar nem abrir conexao."""
    global _cliente, _chave_ativa, _ultimo_uso
    session_mgr = SessionManager.get_instance()
    sm_status = session_mgr.status_sessao()

    tem_creds = sm_status.get("credenciais_keychain", False)
    service_usado = sm_status.get("keyring_service")

    browser_vivo = False
    if _cliente is not None:
        try:
            browser_vivo = (
                _cliente._browser is not None
                and _cliente._browser.is_connected()
            )
        except Exception:
            browser_vivo = False

    agora = time.time()
    idade_s = int(agora - _ultimo_uso) if _ultimo_uso > 0 else None

    try:
        descritores_abertos = len(list(Path("/proc/self/fd").iterdir()))
    except OSError:
        descritores_abertos = None
    try:
        limite_soft, limite_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        limite_soft, limite_hard = None, None
    uso_percentual = None
    if descritores_abertos is not None and limite_soft not in (None, 0, resource.RLIM_INFINITY):
        uso_percentual = round(100 * descritores_abertos / limite_soft, 2)
    try:
        filhos = (
            Path(f"/proc/self/task/{os.getpid()}/children")
            .read_text(encoding="ascii")
            .split()
        )
        subprocessos_filhos = len(filhos)
    except OSError:
        subprocessos_filhos = None
    agora_monotonic = time.monotonic()
    estados_ram_validos = sum(
        expira_em > agora_monotonic
        for expira_em, _estado in _cache_estados_sessao.values()
    )

    chave_dict = None
    if _chave_ativa and len(_chave_ativa) == 4 and browser_vivo:
        chave_dict = {
            "usuario_hash": hashlib.sha256(
                str(_chave_ativa[0]).encode("utf-8")
            ).hexdigest()[:16],
            "persona": _chave_ativa[1],
            "grau": _chave_ativa[2],
            "perfil_id": _chave_ativa[3],
        }
    elif sm_status.get("sessions"):
        s0 = sm_status["sessions"][0]
        chave_dict = {
            "usuario_hash": s0.get("usuario_hash", ""),
            "persona": s0.get("persona", ""),
            "grau": s0.get("grau", ""),
            "perfil_id": s0.get("perfil_id", ""),
        }

    return {
        "sessao_ativa": browser_vivo or sm_status.get("sessao_ativa", False),
        "chave_ativa": chave_dict,
        "idade_sessao_segundos": idade_s if browser_vivo else None,
        "credenciais_keychain": tem_creds,
        "keyring_service": service_usado,
        "recursos_processo": {
            "descritores_abertos": descritores_abertos,
            "limite_descritores_soft": limite_soft,
            "limite_descritores_hard": limite_hard,
            "uso_descritores_percentual": uso_percentual,
            "subprocessos_filhos": subprocessos_filhos,
            "timeout_inatividade_segundos": TIMEOUT_INATIVIDADE_S,
        },
        "cache_sessoes_ram": {
            "entradas_validas": estados_ram_validos,
            "ttl_segundos": TTL_CACHE_SESSAO_S,
            "max_entradas": MAX_ESTADOS_SESSAO,
        },
        "ultima_metrica_performance": (
            dict(_ultima_metrica_sessao) if _ultima_metrica_sessao else None
        ),
        "pool_active_sessions": sm_status.get("pool_active_sessions", 0),
        "active_leases_total": sm_status.get("active_leases_total", 0),
        "sessions": sm_status.get("sessions", []),
    }
