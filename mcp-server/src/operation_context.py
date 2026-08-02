"""OperationContext — Gerenciamento de contexto de operação, rastreamento de requisições,
correlation_id, timeouts e aquisição de lease transacional para ferramentas MCP.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional, TypeVar

import perfil_contexto
from session_manager import (
    ClientLease,
    SessionKey,
    SessionManager,
)

_log_logger = logging.getLogger("operation_context")

_current_op_context: ContextVar[Optional[OperationContext]] = ContextVar(
    "_current_op_context", default=None
)


def get_current_operation_context() -> Optional[OperationContext]:
    """Retorna o OperationContext ativo na corrotina atual, se houver."""
    return _current_op_context.get()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class OperationTimeoutError(asyncio.TimeoutError):
    """Exceção lançada quando a operação excede o tempo limite configurado."""
    pass


@dataclass
class OperationContext:
    operation_name: str
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timeout: float = field(
        default_factory=lambda: float(os.environ.get("PJE_OPERATION_TIMEOUT_SECONDS", "120.0"))
    )
    persona: str = "advogado"
    grau: str = "1g"
    perfil_dict: Optional[dict] = None
    permitir_sem_perfil: bool = False

    start_time: float = field(default_factory=time.monotonic)
    status: str = "created"  # created, running, completed, failed, timeout
    error: Optional[Exception] = None
    lease: Optional[ClientLease] = None
    _token: Optional[Token] = field(default=None, repr=False)

    def __post_init__(self):
        if hasattr(perfil_contexto, "normalizar_persona"):
            self.persona = perfil_contexto.normalizar_persona(self.persona)
        grau_str = str(self.grau or "1g").strip().lower()
        if grau_str in ("1", "1g"):
            self.grau = "1g"
        elif grau_str in ("2", "2g"):
            self.grau = "2g"
        else:
            self.grau = grau_str

        if self.perfil_dict is None:
            self.perfil_dict = perfil_contexto.contexto_atual()

    @property
    def elapsed_ms(self) -> float:
        if self.start_time > 0:
            return round((time.monotonic() - self.start_time) * 1000, 2)
        return 0.0

    def build_session_key(self, usuario_cpf: Optional[str] = None) -> SessionKey:
        if not usuario_cpf:
            try:
                import cliente_singleton
                usuario_cpf, _, _ = cliente_singleton._get_creds()
            except Exception:
                usuario_cpf = "mock_user"

        personas_internas = getattr(perfil_contexto, "PERSONAS_INTERNAS", ("servidor", "magistrado"))
        if self.persona in personas_internas and not self.perfil_dict:
            perfil_id = "descoberta"
        else:
            perfil_id = (self.perfil_dict or {}).get("pje_id", "")

        return SessionKey(
            usuario_cpf=usuario_cpf,
            persona=self.persona,
            grau=self.grau,
            perfil_id=perfil_id,
        )

    async def acquire_lease(
        self,
        session_manager: Optional[SessionManager] = None,
        permitir_sem_perfil: Optional[bool] = None,
    ) -> ClientLease:
        """Adquire ClientLease transacional usando a SessionKey do contexto."""
        if self.lease is not None:
            return self.lease
        sm = session_manager or SessionManager.get_instance()
        key = self.build_session_key()
        efetivo = (
            self.permitir_sem_perfil
            if permitir_sem_perfil is None
            else permitir_sem_perfil
        )
        self.lease = await sm.acquire_lease(
            key,
            perfil_dict=self.perfil_dict,
            timeout=self.timeout,
            permitir_sem_perfil=efetivo,
        )
        return self.lease

    async def release_lease(self) -> None:
        """Libera a lease transacional se estiver ativa."""
        if self.lease is not None:
            try:
                await self.lease.release()
            finally:
                self.lease = None

    async def execute_with_recovery(
        self,
        operation_coro_func: Callable[[Any, Any], Coroutine[Any, Any, Any]],
        session_manager: Optional[SessionManager] = None,
    ) -> Any:
        """Executa a operação via SessionManager.execute_with_recovery com auto-recuperação."""
        sm = session_manager or SessionManager.get_instance()
        key = self.build_session_key()
        return await sm.execute_with_recovery(
            key, self.perfil_dict, operation_coro_func
        )

    async def __aenter__(self) -> OperationContext:
        self.start_time = time.monotonic()
        self.status = "running"
        self._token = _current_op_context.set(self)
        _log(
            f"[OP_CONTEXT] [{self.correlation_id}] Iniciando operação '{self.operation_name}' "
            f"(persona={self.persona}, grau={self.grau})"
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self.lease is not None:
                await self.release_lease()
        except Exception as e:
            _log(f"[OP_CONTEXT] [{self.correlation_id}] Erro ao liberar lease: {e}")

        if exc_type is not None:
            if issubclass(
                exc_type, (asyncio.TimeoutError, TimeoutError, OperationTimeoutError)
            ):
                self.status = "timeout"
                self.error = (
                    exc_val
                    if isinstance(exc_val, Exception)
                    else OperationTimeoutError(str(exc_val))
                )
                _log(
                    f"[OP_CONTEXT] [{self.correlation_id}] Operação '{self.operation_name}' "
                    f"estourou timeout de {self.timeout}s ({self.elapsed_ms}ms)"
                )
            else:
                self.status = "failed"
                self.error = (
                    exc_val
                    if isinstance(exc_val, Exception)
                    else Exception(str(exc_val))
                )
                _log(
                    f"[OP_CONTEXT] [{self.correlation_id}] Operação '{self.operation_name}' "
                    f"falhou com {exc_type.__name__}: {exc_val} ({self.elapsed_ms}ms)"
                )
        else:
            self.status = "completed"
            _log(
                f"[OP_CONTEXT] [{self.correlation_id}] Operação '{self.operation_name}' "
                f"concluída com sucesso ({self.elapsed_ms}ms)"
            )

        if self._token is not None:
            _current_op_context.reset(self._token)
            self._token = None

    def status_info(self) -> dict:
        return {
            "correlation_id": self.correlation_id,
            "operation_name": self.operation_name,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "persona": self.persona,
            "grau": self.grau,
            "perfil_id": (self.perfil_dict or {}).get("pje_id", ""),
            "error": str(self.error) if self.error else None,
        }


T = TypeVar("T")


def operation_boundary(
    name: Optional[str] = None,
    timeout: float = 120.0,
    persona_param: str = "persona",
    grau_param: str = "grau",
    permitir_sem_perfil: bool = False,
):
    """Decorador para delimitar fronteiras de operação com OperationContext,
    request tracking, correlation_id, timeout e tratamento de exceções.
    """
    def decorator(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
        op_name = name or func.__name__

        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            sig = inspect.signature(func)
            bound_args = sig.bind_partial(*args, **kwargs)
            bound_args.apply_defaults()

            persona = bound_args.arguments.get(
                persona_param, kwargs.get(persona_param, "advogado")
            )
            grau = bound_args.arguments.get(
                grau_param, kwargs.get(grau_param, "1g")
            )
            perfil = bound_args.arguments.get("perfil", kwargs.get("perfil", None))

            perfil_dict = None
            if isinstance(perfil, dict):
                perfil_dict = perfil
            else:
                perfil_dict = perfil_contexto.contexto_atual()

            ctx = OperationContext(
                operation_name=op_name,
                timeout=timeout,
                persona=str(persona),
                grau=str(grau),
                perfil_dict=perfil_dict,
                permitir_sem_perfil=permitir_sem_perfil,
            )

            async with ctx:
                try:
                    async with asyncio.timeout(ctx.timeout):
                        if "ctx" in sig.parameters:
                            kwargs["ctx"] = ctx
                        return await func(*args, **kwargs)
                except TimeoutError as te:
                    ctx.status = "timeout"
                    ctx.error = te
                    raise OperationTimeoutError(
                        f"Operação '{op_name}' excedeu o tempo limite de {ctx.timeout}s"
                    ) from te

        return wrapper

    return decorator
