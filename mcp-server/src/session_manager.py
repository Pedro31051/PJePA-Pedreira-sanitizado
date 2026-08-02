"""SessionManager — Gerenciador de sessões e autenticação resiliente do PJe.

Fornece:
- Single-flight login lock via asyncio.Future task coalescing
- Pool de páginas isolado por sessão (PagePool) com proteção a abas CDP pré-existentes
- ClientLease transacional com contagem de referências para proteção contra despejo
- Auto-recuperação transparente em expirações e desconexões CDP (execute_with_recovery)
- Gerenciamento multi-sessão isolado por SessionKey (usuario, persona, grau, perfil_id)
"""
import asyncio
import hashlib
import inspect
import math
import os
import resource
import sys
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional, Set, Tuple

import keyring

from pje_client import PJeClient

KEYRING_SERVICE = "mcp-pje-tjpa"
KEYRING_SERVICES_FALLBACK = ("mcp-pje-tjma", "mcp-pje-tjpi")


def _env_int(nome: str, padrao: int, minimo: int = 1) -> int:
    try:
        return max(minimo, int(os.environ.get(nome, str(padrao))))
    except (TypeError, ValueError):
        return padrao


TIMEOUT_INATIVIDADE_S = _env_int("PJE_SESSION_IDLE_SECONDS", 1800, 60)
MAX_ACTIVE_SESSIONS = min(8, _env_int("PJE_MAX_ACTIVE_SESSIONS", 4, 1))
MAX_PAGES_PER_SESSION = _env_int("PJE_MAX_PAGES_PER_SESSION", 5, 1)
SSO_COOLDOWN_S = _env_int("PJE_SSO_COOLDOWN_SECONDS", 180, 30)
SSO_LIMITE_FALHAS = _env_int("PJE_SSO_CIRCUIT_FAILURES", 1, 1)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class SessionExpiredError(Exception):
    """Sessão do PJe expirada no servidor ou token SSO invalidador."""
    pass


class CDPDisconnectedError(Exception):
    """Conexão WebSocket CDP com o Chrome foi interrompida."""
    pass


@dataclass(frozen=True)
class SessionKey:
    usuario_cpf: str
    persona: str
    grau: str
    perfil_id: str

    def to_tuple(self) -> Tuple[str, str, str, str]:
        return (self.usuario_cpf, self.persona, self.grau, self.perfil_id)


def _creds_systemd():
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
            continue
        if all([cpf, senha, seed]):
            return cpf, senha, seed
    return None


def _get_creds():
    import cliente_singleton
    if hasattr(cliente_singleton, "_get_creds"):
        return cliente_singleton._get_creds()
    creds = _creds_systemd() or _creds_keyring()
    if creds:
        return creds
    raise RuntimeError(
        "Credenciais nao encontradas: nem via systemd LoadCredential "
        "($CREDENTIALS_DIRECTORY) nem no Keychain (services 'mcp-pje-tjpa', "
        "'mcp-pje-tjma' e 'mcp-pje-tjpi')."
    )


class PagePool:
    """Pool isolado de páginas Playwright por sessão."""

    def __init__(self, client: PJeClient, max_pages: int = MAX_PAGES_PER_SESSION):
        self.client = client
        self.max_pages = max_pages
        self._semaphore = asyncio.Semaphore(max_pages)
        self._active_pages: Set[Any] = set()

    async def acquire_page(self) -> Any:
        await self._semaphore.acquire()
        try:
            context = getattr(self.client, "_context", None)
            if context is None:
                raise RuntimeError("PJeClient context is None (not initialized)")
            page = await context.new_page()
            if hasattr(self.client, "_setup_dialog_handler"):
                self.client._setup_dialog_handler(page)
            self._active_pages.add(page)
            return page
        except BaseException:
            self._semaphore.release()
            raise

    async def release_page(self, page: Any) -> None:
        try:
            if page in self._active_pages:
                self._active_pages.remove(page)
            abas_preexistentes = getattr(self.client, "_abas_preexistentes", [])
            if page not in abas_preexistentes:
                try:
                    if hasattr(page, "is_closed") and not page.is_closed():
                        await page.close()
                except Exception as e:
                    _log(f"[PAGE_POOL] Erro ao fechar página: {e}")
            if hasattr(self.client, "_descartar_handlers_de_abas_fechadas"):
                self.client._descartar_handlers_de_abas_fechadas()
        finally:
            self._semaphore.release()

    async def close_all(self) -> None:
        for page in list(self._active_pages):
            try:
                abas_preexistentes = getattr(self.client, "_abas_preexistentes", [])
                if page not in abas_preexistentes and hasattr(page, "is_closed") and not page.is_closed():
                    await page.close()
            except Exception:
                pass
        self._active_pages.clear()


class SessionEntry:
    """Entrada da sessão com rastreamento de leases ativas e lock por sessão."""

    def __init__(self, key: SessionKey, client: PJeClient, page_pool: PagePool):
        self.key = key
        self.client = client
        self.page_pool = page_pool
        self.created_at: float = time.monotonic()
        self.last_used_at: float = time.monotonic()
        self.active_leases: Dict[str, "ClientLease"] = {}
        self.lock = asyncio.Lock()


_task_page_var: ContextVar[Optional[Any]] = ContextVar("_task_page_var", default=None)


def get_current_task_page() -> Optional[Any]:
    return _task_page_var.get()


class ClientLease:
    """Context manager transacional para leases de cliente e página."""

    def __init__(self, manager: "SessionManager", entry: SessionEntry, page: Any):
        self.lease_id = uuid.uuid4().hex
        self.manager = manager
        self.entry = entry
        self.client = entry.client
        self.page = page
        self.acquired_at = time.monotonic()
        self._released = False
        self._page_token: Optional[Any] = None

    async def __aenter__(self) -> "ClientLease":
        self._page_token = _task_page_var.set(self.page)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self._page_token is not None:
                _task_page_var.reset(self._page_token)
                self._page_token = None
        finally:
            await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._page_token is not None:
            try:
                _task_page_var.reset(self._page_token)
            except Exception:
                pass
            self._page_token = None
        await self.manager.release_lease(self)


class SessionManager:
    """Gerenciador centralizado de sessões PJe com single-flight login lock e LRU eviction."""

    _instance: Optional["SessionManager"] = None

    @classmethod
    def get_instance(cls) -> "SessionManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def __init__(self):
        self._sessions: Dict[SessionKey, SessionEntry] = {}
        self._login_tasks: Dict[SessionKey, Tuple[asyncio.Task, Set[str]]] = {}
        self._global_lock = asyncio.Lock()
        self.max_idle_seconds = TIMEOUT_INATIVIDADE_S
        self.max_active_sessions = MAX_ACTIVE_SESSIONS
        self.max_pages_per_session = MAX_PAGES_PER_SESSION
        self._sso_falhas_consecutivas = 0
        self._sso_bloqueado_ate = 0.0

    def _exigir_circuito_sso_fechado(self) -> None:
        import cliente_singleton
        if hasattr(cliente_singleton, "_exigir_circuito_sso_fechado"):
            return cliente_singleton._exigir_circuito_sso_fechado()
        restante = self._sso_bloqueado_ate - time.monotonic()
        if restante > 0:
            raise RuntimeError(
                "SSO_CIRCUIT_BREAKER_ATIVO: novas autenticações interrompidas; "
                f"aguarde {math.ceil(restante)} segundo(s)"
            )

    def _registrar_resultado_sso(self, *, sucesso: bool, indeterminado: bool = False) -> None:
        import cliente_singleton
        if hasattr(cliente_singleton, "_registrar_resultado_sso"):
            cliente_singleton._registrar_resultado_sso(sucesso=sucesso, indeterminado=indeterminado)
        if sucesso:
            self._sso_falhas_consecutivas = 0
            self._sso_bloqueado_ate = 0.0
            return
        self._sso_falhas_consecutivas += 1
        if indeterminado or self._sso_falhas_consecutivas >= SSO_LIMITE_FALHAS:
            self._sso_bloqueado_ate = time.monotonic() + SSO_COOLDOWN_S

    async def _is_session_healthy(self, entry: SessionEntry) -> bool:
        if not entry.client or getattr(entry.client, "_browser", None) is None:
            return False
        try:
            if not entry.client._browser.is_connected():
                return False
        except Exception:
            return False

        try:
            page = getattr(entry.client, "_page", None)
            if page and hasattr(page, "is_closed") and not page.is_closed():
                url = page.url
                if "login.seam" in url or "sso.cloud.pje.jus.br" in url:
                    return False
        except Exception:
            return False
        return True

    async def _close_session_entry(self, entry: SessionEntry) -> None:
        try:
            await entry.page_pool.close_all()
        except Exception as e:
            _log(f"[SESSION_MANAGER] Erro ao fechar page pool: {e}")

        try:
            async with entry.client._op_lock:
                await entry.client._fechar()
        except Exception as e:
            _log(f"[SESSION_MANAGER] Erro ao fechar client: {e}")

    async def _evict_idle_or_lru_sessions(self) -> None:
        agora = time.monotonic()
        for key in list(self._sessions.keys()):
            entry = self._sessions[key]
            if len(entry.active_leases) == 0:
                idade = agora - entry.last_used_at
                if idade > self.max_idle_seconds:
                    _log(f"[SESSION_MANAGER] Despejando sessão ociosa ({idade:.0f}s) para chave {key.to_tuple()}")
                    stale = self._sessions.pop(key)
                    await self._close_session_entry(stale)

        while len(self._sessions) >= self.max_active_sessions:
            candidatos = [
                (k, e) for k, e in self._sessions.items()
                if len(e.active_leases) == 0
            ]
            if not candidatos:
                _log("[SESSION_MANAGER] Limite max_active_sessions atingido, mas todas possuem leases ativas; mantendo no pool.")
                break
            mais_antigo_key, mais_antigo_entry = min(candidatos, key=lambda item: item[1].last_used_at)
            _log(f"[SESSION_MANAGER] Evicção LRU: removendo sessão chave {mais_antigo_key.to_tuple()}")
            evict = self._sessions.pop(mais_antigo_key)
            await self._close_session_entry(evict)

    async def get_or_create_session(
        self,
        key: SessionKey,
        perfil_dict: Optional[dict] = None,
        permitir_sem_perfil: bool = False,
    ) -> SessionEntry:
        inicio_chamada = time.monotonic()
        inicio_lock = time.monotonic()
        caller_id = uuid.uuid4().hex

        async with self._global_lock:
            await self._evict_idle_or_lru_sessions()
            espera_lock_ms = (time.monotonic() - inicio_lock) * 1000
            entry = self._sessions.get(key)
            if entry and await self._is_session_healthy(entry):
                entry.last_used_at = time.monotonic()
                import cliente_singleton
                if hasattr(cliente_singleton, "_registrar_metrica"):
                    cliente_singleton._registrar_metrica(
                        "reuso_exato",
                        cache_hit=True,
                        espera_lock_ms=espera_lock_ms,
                        total_ms=(time.monotonic() - inicio_chamada) * 1000,
                    )
                return entry

            if entry and not await self._is_session_healthy(entry):
                stale = self._sessions.pop(key)
                await self._close_session_entry(stale)

            for k in list(self._sessions.keys()):
                if k != key and k.usuario_cpf == key.usuario_cpf and k.persona == key.persona and k.grau == key.grau:
                    old_entry = self._sessions[k]
                    if len(old_entry.active_leases) == 0:
                        _log(f"[SESSION_MANAGER] Fechando sessão anterior supercedida ({k.to_tuple()})")
                        stale = self._sessions.pop(k)
                        await self._close_session_entry(stale)

            if key in self._login_tasks:
                task, waiters = self._login_tasks[key]
                waiters.add(caller_id)
            else:
                waiters = {caller_id}
                task = asyncio.create_task(
                    self._execute_single_flight_login(
                        key, perfil_dict, permitir_sem_perfil
                    )
                )
                self._login_tasks[key] = (task, waiters)

        try:
            client = await asyncio.shield(task)
        except asyncio.CancelledError:
            async with self._global_lock:
                if key in self._login_tasks:
                    t, w = self._login_tasks[key]
                    w.discard(caller_id)
                    if not w and not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
            raise
        finally:
            async with self._global_lock:
                if key in self._login_tasks:
                    t, w = self._login_tasks[key]
                    w.discard(caller_id)
                    if not w:
                        self._login_tasks.pop(key, None)

        async with self._global_lock:
            entry = self._sessions.get(key)
            if not entry or entry.client != client:
                page_pool = PagePool(client, max_pages=self.max_pages_per_session)
                entry = SessionEntry(key, client, page_pool)
                self._sessions[key] = entry
            entry.last_used_at = time.monotonic()
            return entry

    async def _execute_single_flight_login(
        self,
        key: SessionKey,
        perfil_dict: Optional[dict],
        permitir_sem_perfil: bool,
    ) -> PJeClient:
        client = None
        inicio_chamada = time.monotonic()
        inicio_browser = time.monotonic()
        try:
            import cliente_singleton
            self._exigir_circuito_sso_fechado()
            cpf, senha, seed = _get_creds()
            headless = os.environ.get("PJE_HEADLESS", "1") == "1"
            client_cls = getattr(cliente_singleton, "PJeClient", PJeClient)
            client = client_cls(
                cpf,
                senha,
                seed,
                persona=key.persona,
                headless=headless,
                grau=key.grau,
                perfil=perfil_dict,
            )
            res = client._iniciar()
            if inspect.isawaitable(res):
                await res
            iniciar_browser_ms = (time.monotonic() - inicio_browser) * 1000

            inicio_login = time.monotonic()
            res = client._login()
            if inspect.isawaitable(res):
                await res
            login_ms = (time.monotonic() - inicio_login) * 1000

            inicio_perfil = time.monotonic()
            if perfil_dict and hasattr(client, "_selecionar_lotacao"):
                res = client._selecionar_lotacao(perfil_dict)
                if inspect.isawaitable(res):
                    await res
            elif permitir_sem_perfil:
                pass
            elif hasattr(client, "selecionar_e_validar_contexto"):
                fn = getattr(client, "selecionar_e_validar_contexto")
                res = fn(None)
                if inspect.isawaitable(res):
                    await res
            perfil_ms = (time.monotonic() - inicio_perfil) * 1000

            self._registrar_resultado_sso(sucesso=True)
            if hasattr(cliente_singleton, "_registrar_metrica"):
                cliente_singleton._registrar_metrica(
                    "sessao_nova",
                    cache_hit=False,
                    iniciar_browser_ms=iniciar_browser_ms,
                    login_ms=login_ms,
                    perfil_ms=perfil_ms,
                    total_ms=(time.monotonic() - inicio_chamada) * 1000,
                )
            return client
        except asyncio.CancelledError:
            _log(f"[SESSION_MANAGER] Task single-flight cancelada para chave {key.to_tuple()}")
            if client is not None:
                try:
                    await asyncio.shield(client._fechar())
                except Exception:
                    pass
            import cliente_singleton
            if hasattr(cliente_singleton, "_registrar_metrica"):
                cliente_singleton._registrar_metrica(
                    "falha_criacao",
                    cache_hit=False,
                    total_ms=(time.monotonic() - inicio_chamada) * 1000,
                )
            raise
        except BaseException as exc:
            self._registrar_resultado_sso(
                sucesso=False,
                indeterminado="LOGIN_ESTADO_INDETERMINADO" in str(exc),
            )
            if client is not None:
                try:
                    await asyncio.shield(client._fechar())
                except Exception:
                    pass
            import cliente_singleton
            if hasattr(cliente_singleton, "_registrar_metrica"):
                cliente_singleton._registrar_metrica(
                    "falha_criacao",
                    cache_hit=False,
                    total_ms=(time.monotonic() - inicio_chamada) * 1000,
                )
            raise

    async def acquire_lease(
        self,
        key: SessionKey,
        perfil_dict: Optional[dict] = None,
        timeout: float = 30.0,
        permitir_sem_perfil: bool = False,
    ) -> ClientLease:
        entry = await self.get_or_create_session(
            key,
            perfil_dict=perfil_dict,
            permitir_sem_perfil=permitir_sem_perfil,
        )
        try:
            if timeout is not None and timeout > 0:
                page = await asyncio.wait_for(entry.page_pool.acquire_page(), timeout=timeout)
            else:
                page = await entry.page_pool.acquire_page()
        except (asyncio.TimeoutError, TimeoutError) as err:
            _log(f"[SESSION_MANAGER] Timeout de lease ({timeout}s) ao adquirir página para chave {key.to_tuple()}")
            raise TimeoutError(f"Timeout de {timeout}s atingido ao aguardar página no pool de sessões") from err

        lease = ClientLease(self, entry, page)
        async with entry.lock:
            entry.active_leases[lease.lease_id] = lease
            entry.last_used_at = time.monotonic()
        return lease

    async def release_lease(self, lease: ClientLease) -> None:
        entry = lease.entry
        async with entry.lock:
            entry.active_leases.pop(lease.lease_id, None)
            entry.last_used_at = time.monotonic()
        await entry.page_pool.release_page(lease.page)

    async def execute_with_recovery(
        self,
        key: SessionKey,
        perfil_dict: Optional[dict],
        operation_coro_func: Callable[[PJeClient, Any], Coroutine[Any, Any, Any]],
    ) -> Any:
        """Executa operação com auto-recuperação transparente em caso de expiração ou desconexão CDP."""
        for attempt in range(2):
            lease = await self.acquire_lease(key, perfil_dict)
            try:
                async with lease:
                    result = await operation_coro_func(lease.client, lease.page)
                    return result
            except Exception as exc:
                msg = str(exc).lower()
                is_typed_exception = (
                    isinstance(exc, (SessionExpiredError, CDPDisconnectedError))
                    or exc.__class__.__name__ in ("SessionExpiredError", "CDPDisconnectedError")
                )
                is_expired_msg = any(
                    marker in msg for marker in [
                        "session_expired",
                        "sessao_expirada",
                        "sessão_expirada",
                        "sessao expirada",
                        "sessão expirada",
                        "sessao do pje expirada",
                        "sessão do pje expirada",
                        "target page, context or browser has been closed",
                        "browser has been closed",
                        "target closed",
                        "connection closed",
                        "cdpdisconnectederror",
                    ]
                )
                is_expired = is_typed_exception or is_expired_msg
                is_disconnected = (
                    getattr(lease.client, "_browser", None) is not None
                    and not lease.client._browser.is_connected()
                )
                if (is_expired or is_disconnected) and attempt == 0:
                    _log(f"[AUTO_RECOVERY] Tentativa {attempt + 1} falhou com erro de sessão ({exc}); recuperando...")
                    async with self._global_lock:
                        if key in self._sessions and self._sessions[key] == lease.entry:
                            stale = self._sessions.pop(key)
                            await self._close_session_entry(stale)
                    continue
                else:
                    raise

    async def fechar_sessoes_ociosas(self) -> int:
        """Itera sobre todas as sessões no pool e fecha as que estão ociosas (sem leases ativas e idade > max_idle_seconds)."""
        fechadas = 0
        async with self._global_lock:
            agora = time.monotonic()
            for key in list(self._sessions.keys()):
                entry = self._sessions[key]
                if len(entry.active_leases) == 0:
                    idade = agora - entry.last_used_at
                    if idade > self.max_idle_seconds:
                        _log(f"[SESSION_MANAGER] Watchdog: fechando sessão ociosa ({idade:.0f}s) para chave {key.to_tuple()}")
                        stale = self._sessions.pop(key)
                        await self._close_session_entry(stale)
                        fechadas += 1
        return fechadas

    async def close_all_sessions(self) -> None:
        async with self._global_lock:
            keys = list(self._sessions.keys())
            for key in keys:
                entry = self._sessions.pop(key)
                await self._close_session_entry(entry)

    def status_sessao(self) -> dict:
        agora = time.monotonic()
        sessions_info = []
        active_leases_total = 0

        for key, entry in self._sessions.items():
            num_leases = len(entry.active_leases)
            active_leases_total += num_leases
            sessions_info.append({
                "usuario_hash": hashlib.sha256(key.usuario_cpf.encode("utf-8")).hexdigest()[:16],
                "persona": key.persona,
                "grau": key.grau,
                "perfil_id": key.perfil_id,
                "active_leases": num_leases,
                "idle_seconds": int(agora - entry.last_used_at),
            })

        tem_creds = False
        service_usado = None
        if _creds_systemd():
            tem_creds = True
            service_usado = "systemd-credentials"
        else:
            try:
                for service in (KEYRING_SERVICE, *KEYRING_SERVICES_FALLBACK):
                    cpf = keyring.get_password(service, "cpf")
                    senha = keyring.get_password(service, "senha")
                    seed = keyring.get_password(service, "totp_seed")
                    if all([cpf, senha, seed]):
                        tem_creds = True
                        service_usado = service
                        break
            except Exception:
                pass

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

        return {
            "sessao_ativa": len(self._sessions) > 0,
            "pool_active_sessions": len(self._sessions),
            "active_leases_total": active_leases_total,
            "sessions": sessions_info,
            "credenciais_keychain": tem_creds,
            "keyring_service": service_usado,
            "recursos_processo": {
                "descritores_abertos": descritores_abertos,
                "limite_descritores_soft": limite_soft,
                "limite_descritores_hard": limite_hard,
                "uso_descritores_percentual": uso_percentual,
                "max_active_sessions": self.max_active_sessions,
                "max_pages_per_session": self.max_pages_per_session,
                "timeout_inatividade_segundos": self.max_idle_seconds,
            },
        }
