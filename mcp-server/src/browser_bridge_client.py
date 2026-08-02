"""Cliente assíncrono do bridge Playwright local do PJe.

O servidor MCP não controla o Chrome diretamente nesta rota. Ele envia um
comando JSON delimitado por nova linha ao processo Node ``pje_browser_bridge``
e recebe uma resposta igualmente delimitada. O socket é local, pertence ao
mesmo usuário e tem limite rígido de tamanho.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any


MAX_MESSAGE_BYTES = 256 * 1024
MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 30_000
COMMANDS = frozenset(
    {
        "get_state",
        "search_process",
        "open_process",
        "list_documents",
        "navigate_document",
        "download_current_document",
        "open_expedients",
        "list_expedients",
        "inspect_document_join",
        "inspect_communications",
        "analyze_communication_plan",
        "inspect_document_issue",
        "analyze_document_issue_plan",
        "inspect_process_metadata",
        "preview_process_label_change",
        "apply_process_label_change",
        "inspect_retification",
        "list_retification_candidates",
        "preview_retification",
        "simulate_retification",
        "apply_retification",
        "extract_document",
        "consume_signal",
        "ack_signal",
        "dispatch_event",
    }
)


class BrowserBridgeError(RuntimeError):
    """Erro público, estruturado e saneado devolvido pela ponte."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code or "BRIDGE_ERROR"
        self.retryable = bool(retryable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }


def bridge_socket_path() -> Path:
    configured = os.environ.get("PJE_BROWSER_BRIDGE_SOCKET", "").strip()
    if configured:
        return Path(configured)
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"pje-tjpa-browser-{uid}.sock"


def _validate_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise BrowserBridgeError(
            "BRIDGE_UNAVAILABLE",
            "A automação Playwright local não está em execução.",
            retryable=True,
        ) from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise BrowserBridgeError(
            "BRIDGE_UNSAFE_PATH",
            "O caminho configurado para a automação não é um socket UNIX.",
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise BrowserBridgeError(
            "BRIDGE_UNSAFE_OWNER",
            "O socket da automação pertence a outro usuário.",
        )


async def call_browser_bridge(
    command: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_ms: int = 10_000,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Executa um comando no bridge e devolve somente o campo ``data``."""
    if command not in COMMANDS:
        raise BrowserBridgeError("INVALID_REQUEST", "Comando da automação inválido.")
    if not isinstance(timeout_ms, int) or not MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS:
        raise BrowserBridgeError(
            "INVALID_REQUEST", "timeout_ms deve estar entre 1000 e 30000."
        )
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise BrowserBridgeError("INVALID_REQUEST", "payload deve ser um objeto.")

    path = Path(socket_path) if socket_path is not None else bridge_socket_path()
    _validate_socket(path)
    request_id = uuid.uuid4().hex
    request = {
        "request_id": request_id,
        "command": command,
        "payload": payload,
        "timeout_ms": timeout_ms,
    }
    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise BrowserBridgeError("INVALID_REQUEST", "Requisição excede 256 KiB.")

    writer = None

    async def _exchange():
        nonlocal writer
        reader, writer = await asyncio.open_unix_connection(
            str(path), limit=MAX_MESSAGE_BYTES + 1
        )
        writer.write(encoded)
        await writer.drain()
        return await reader.readline()

    try:
        response_line = await asyncio.wait_for(_exchange(), timeout_ms / 1000)
    except asyncio.TimeoutError as exc:
        raise BrowserBridgeError(
            "BRIDGE_TIMEOUT", "A automação excedeu o tempo limite.", retryable=True
        ) from exc
    except (ConnectionError, OSError) as exc:
        raise BrowserBridgeError(
            "BRIDGE_UNAVAILABLE",
            "Não foi possível comunicar com a automação Playwright local.",
            retryable=True,
        ) from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    if not response_line:
        raise BrowserBridgeError(
            "INVALID_RESPONSE", "A automação encerrou a conexão sem resposta.", retryable=True
        )
    if len(response_line) > MAX_MESSAGE_BYTES or not response_line.endswith(b"\n"):
        raise BrowserBridgeError("INVALID_RESPONSE", "Resposta da automação inválida ou excessiva.")
    try:
        response = json.loads(response_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserBridgeError("INVALID_RESPONSE", "Resposta da automação não é JSON válido.") from exc
    if not isinstance(response, dict) or response.get("request_id") != request_id:
        raise BrowserBridgeError("INVALID_RESPONSE", "Resposta da automação não corresponde à requisição.")
    if response.get("ok") is not True:
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        raise BrowserBridgeError(
            str(error.get("code") or "BRIDGE_ERROR"),
            str(error.get("message") or "Falha na automação Playwright."),
            retryable=bool(error.get("retryable")),
        )
    data = response.get("data")
    if not isinstance(data, dict):
        raise BrowserBridgeError("INVALID_RESPONSE", "A automação devolveu dados fora do contrato.")
    return data
