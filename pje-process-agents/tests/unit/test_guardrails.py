import hashlib

import pytest

from app.guardrails import DossierRejected, dossier_to_json


def _dossier():
    text = "Certidão de óbito juntada aos autos em 10/01/2025."
    return {
        "process_number": "0000000-00.2025.8.14.0000",
        "documents": [
            {
                "document_id": "doc-1",
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "pages": [{"page": 1, "text": text}],
            }
        ],
    }


def test_accepts_text_only_dossier() -> None:
    serialized = dossier_to_json(_dossier())
    assert '"text_only":true' in serialized


def test_rejects_credentials() -> None:
    payload = _dossier()
    payload["documents"][0]["password"] = "nao-enviar"
    with pytest.raises(DossierRejected, match="credencial"):
        dossier_to_json(payload)
