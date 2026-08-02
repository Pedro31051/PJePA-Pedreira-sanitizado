"""Política isolada de origem e autenticação da extensão Chrome."""

from __future__ import annotations

import hmac
import os
from urllib.parse import urlsplit


def _production() -> bool:
    return os.environ.get("PJE_ENV", "").strip().casefold() == "production"


def allowed_origins() -> set[str]:
    raw = os.environ.get("PJE_EXTENSION_ALLOWED_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def origin_allowed(origin: str) -> bool:
    origin = origin.strip().rstrip("/")
    configured = allowed_origins()
    if configured:
        return origin in configured
    if _production():
        return False
    parsed = urlsplit(origin)
    return (
        parsed.scheme == "chrome-extension"
        or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"})
    )


def cors_headers(origin: str) -> dict[str, str]:
    headers = {
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": (
            "Authorization, X-Extension-Token, Content-Type"
        ),
        "Vary": "Origin",
    }
    if origin_allowed(origin):
        headers["Access-Control-Allow-Origin"] = origin.strip().rstrip("/")
    return headers


def token_status(authorization: str, extension_token: str) -> str:
    expected = os.environ.get("PJE_EXTENSION_TOKEN", "").strip()
    if not expected:
        return "misconfigured" if _production() else "disabled"
    supplied = extension_token.strip()
    if authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    return "valid" if hmac.compare_digest(supplied, expected) else "invalid"
