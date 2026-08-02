"""HTTP client for the read-only ADK process-analysis service."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import google.auth
import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token


class AdkProcessClientError(ValueError):
    """Safe public transport error without process content."""


def _headers(base_url: str) -> dict[str, str]:
    mode = os.environ.get("PJE_ADK_AUTH_MODE", "none").strip().casefold()
    if mode == "none":
        return {"Content-Type": "application/json"}
    request = Request()
    if mode == "id_token":
        token = id_token.fetch_id_token(request, base_url)
    elif mode == "adc_access":
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(request)
        token = credentials.token
    else:
        raise AdkProcessClientError("modo de autenticação ADK inválido")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def analyze_inventory(
    process_dossier_json: str,
    *,
    objective: str = "Produzir relatório cronológico completo com provas exatas.",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call an ADK HTTP endpoint and return its structured final response."""
    base_url = os.environ.get("PJE_ADK_AGENT_URL", "").strip().rstrip("/")
    if not base_url:
        raise AdkProcessClientError("PJE_ADK_AGENT_URL não configurada")
    app_name = os.environ.get("PJE_ADK_APP_NAME", "app").strip() or "app"
    timeout = float(os.environ.get("PJE_ADK_TIMEOUT_SECONDS", "240"))
    opaque_id = uuid.uuid4().hex
    user_id = f"mcp-{opaque_id}"
    session_id = ""
    events: list[dict[str, Any]] = []
    headers = _headers(base_url)
    with httpx.Client(timeout=timeout, headers=headers) as client:
        session_response = client.post(
            f"{base_url}/apps/{app_name}/users/{user_id}/sessions",
            json={"state": {"request_id": opaque_id}},
        )
        session_response.raise_for_status()
        session_id = str(session_response.json().get("id") or "")
        if not session_id:
            raise AdkProcessClientError("ADK não criou sessão")
        prompt = (
            f"{objective}\n"
            "Analise como inventário/sucessões. "
            f"DOSSIÊ_JSON={process_dossier_json}"
        )
        try:
            with client.stream(
                "POST",
                f"{base_url}/run_sse",
                json={
                    "app_name": app_name,
                    "user_id": user_id,
                    "session_id": session_id,
                    "new_message": {
                        "role": "user",
                        "parts": [{"text": prompt}],
                    },
                    "streaming": True,
                },
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    events.append(json.loads(line[6:]))
        finally:
            deletion = client.delete(
                f"{base_url}/apps/{app_name}/users/{user_id}/sessions/{session_id}"
            )
            deletion.raise_for_status()

    final_text = ""
    model_calls = 0
    total_tokens = 0
    retrieved_sources = []
    for event in events:
        if event.get("usageMetadata") and event.get("finishReason"):
            model_calls += 1
            total_tokens += int(event["usageMetadata"].get("totalTokenCount") or 0)
        for part in (event.get("content") or {}).get("parts") or []:
            function_response = part.get("functionResponse") or part.get(
                "function_response"
            )
            if function_response and function_response.get("name") in {
                "discovery_engine_search",
                "search_official_knowledge",
            }:
                retrieved_sources.append(
                    json.dumps(
                        function_response.get("response") or {},
                        ensure_ascii=False,
                    )
                )
            if event.get("author") != "process_orchestrator":
                continue
            if part.get("text"):
                final_text = str(part["text"])
    if not final_text:
        raise AdkProcessClientError("ADK não devolveu resposta final")
    try:
        report = json.loads(final_text)
    except json.JSONDecodeError as exc:
        raise AdkProcessClientError("resposta final ADK não é JSON") from exc
    return report, {
        "transport": "adk_http_sse",
        "model_calls": model_calls,
        "total_tokens": total_tokens,
        "event_count": len(events),
        "session_purged": True,
        "_retrieved_sources": retrieved_sources,
    }
