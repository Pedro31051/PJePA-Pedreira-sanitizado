"""Integração offline do endpoint da extensão com armazenamento temporário."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from starlette.testclient import TestClient

import server

SYNTHETIC_CNJ = "0000000-00.0000.8.00.0000"


def test_extension_endpoint_accepts_synthetic_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("PJE_ENV", "test")
    monkeypatch.setenv("PJE_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv(
        "PJE_AUDIT_MASTER_KEY",
        base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii"),
    )
    monkeypatch.setenv("PJE_EXTENSION_TOKEN", "synthetic-extension-token")

    payload = {
        "numero_cnj": SYNTHETIC_CNJ,
        "grau": "1g",
        "persona": "servidor",
        "base": {"process_number": SYNTHETIC_CNJ, "grau": "1g", "partes": []},
        "documents": [
            {
                "id": "synthetic-document-1",
                "tipo": "Documento sintético",
                "titulo": "Peça gerada exclusivamente para teste",
                "texto": "CONTEÚDO SINTÉTICO SEM DADOS PESSOAIS",
            }
        ],
        "expedientes": [],
    }

    client = TestClient(server._criar_app_http())
    response = client.post(
        "/analise_extensao",
        json=payload,
        headers={
            "Origin": "chrome-extension://synthetic-test-id",
            "X-Extension-Token": "synthetic-extension-token",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "sucesso"
    assert result["job_id"]


def test_extension_requires_explicit_origin_in_production(monkeypatch):
    monkeypatch.setenv("PJE_ENV", "production")
    monkeypatch.delenv("PJE_EXTENSION_ALLOWED_ORIGINS", raising=False)
    client = TestClient(server._criar_app_http())
    response = client.options(
        "/analise_extensao",
        headers={"Origin": "chrome-extension://unconfigured"},
    )
    assert response.status_code == 403
