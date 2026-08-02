from __future__ import annotations

from unittest.mock import patch

import pytest

from app import model_policy


def test_baseline_is_stable_until_routing_eval_is_enabled():
    with patch.dict("os.environ", {}, clear=True):
        policy = model_policy.load_model_policy()
    assert policy.routing_enabled is False
    assert {
        policy.orchestrator,
        policy.document_worker,
        policy.inventory_specialist,
        policy.final_reviewer,
    } == {"gemini-3.5-flash"}


def test_candidate_routing_is_explicit_and_allowlisted():
    with patch.dict(
        "os.environ",
        {
            "PJE_MODEL_ROUTING_ENABLED": "1",
            "PJE_DOCUMENT_WORKER_MODEL": "gemini-3.6-flash",
        },
        clear=True,
    ):
        policy = model_policy.load_model_policy()
    assert policy.routing_enabled is True
    assert policy.document_worker == "gemini-3.6-flash"

    with patch.dict(
        "os.environ",
        {"PJE_MODEL_ROUTING_ENABLED": "1", "PJE_INVENTORY_MODEL": "unknown"},
        clear=True,
    ):
        with pytest.raises(ValueError):
            model_policy.load_model_policy()
