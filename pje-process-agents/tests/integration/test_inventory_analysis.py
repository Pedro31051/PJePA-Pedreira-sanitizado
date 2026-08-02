import hashlib
import json
import os

import pytest
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

load_dotenv()
os.environ.setdefault("INTEGRATION_TEST", "TRUE")

pytestmark = pytest.mark.skipif(
    os.environ.get("PJE_RUN_LIVE_AGENT_TESTS") != "1",
    reason="teste com chamada faturável exige PJE_RUN_LIVE_AGENT_TESTS=1",
)

from app.agent import root_agent  # noqa: E402
from app.verifier import validate_inventory_report  # noqa: E402


def test_inventory_is_delegated_and_evidence_is_verified() -> None:
    texts = [
        "Em 10 de janeiro de 2025, ANA requereu a abertura do inventário de JOÃO, juntando certidão de óbito datada de 02 de janeiro de 2025.",
        "Em 20 de janeiro de 2025, o juízo nomeou ANA inventariante e determinou a apresentação das primeiras declarações.",
    ]
    dossier = {
        "process_number": "0000000-00.2025.8.14.0000",
        "class": "Inventário",
        "documents": [
            {
                "document_id": f"doc-{index}",
                "date": "2025-01-10" if index == 1 else "2025-01-20",
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "pages": [{"page": 1, "text": text}],
            }
            for index, text in enumerate(texts, start=1)
        ],
    }
    prompt = (
        "Analise este inventário ato a ato com provas exatas. DOSSIÊ_JSON="
        + json.dumps(dossier, ensure_ascii=False, separators=(",", ":"))
    )
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        user_id="inventory_test", app_name="inventory_test"
    )
    runner = Runner(
        agent=root_agent,
        session_service=session_service,
        app_name="inventory_test",
    )
    events = list(
        runner.run(
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text=prompt)]
            ),
            user_id="inventory_test",
            session_id=session.id,
        )
    )
    assert any(event.author == "inventory_specialist" for event in events)
    final_texts = [
        part.text
        for event in events
        if event.author == "process_orchestrator" and event.is_final_response()
        for part in ((event.content.parts or []) if event.content else [])
        if part.text
    ]
    assert final_texts
    report = validate_inventory_report(json.loads(final_texts[-1]), dossier)
    assert len(report.acts) == 2
    assert set(report.documents_reviewed) == {"doc-1", "doc-2"}
