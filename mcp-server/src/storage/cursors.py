"""Cursores HMAC opacos para paginação de snapshots."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any


def load_or_create_key(path: Path) -> bytes:
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        new_key = os.urandom(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = path.read_bytes()
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(new_key)
            key = new_key
    if len(key) < 32:
        raise RuntimeError("chave local de cursor inválida")
    return key


def encode(payload: dict[str, Any], key: bytes) -> str:
    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.new(key, body, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")
        + "."
        + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    )


def decode(token: str, key: bytes) -> dict[str, Any]:
    try:
        body_b64, signature_b64 = str(token).split(".", 1)
        body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        signature = base64.urlsafe_b64decode(
            signature_b64 + "=" * (-len(signature_b64) % 4)
        )
        expected = hmac.new(key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(body)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor inválido ou adulterado") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("cursor inválido ou incompatível")
    return payload
