from __future__ import annotations

from unittest.mock import patch

from google.adk.sessions.in_memory_session_service import InMemorySessionService

from app.app_utils import services


def test_zero_retention_is_default_and_forces_memory_session():
    services.get_session_service.cache_clear()
    with patch.dict(
        "os.environ",
        {
            "GOOGLE_CLOUD_AGENT_ENGINE_ID": "persistent-engine",
            "SESSION_SERVICE_URI": "sqlite:///must-not-be-used.db",
        },
        clear=False,
    ):
        assert services.zero_retention_enabled() is True
        assert isinstance(services.get_session_service(), InMemorySessionService)
    services.get_session_service.cache_clear()
