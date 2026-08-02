"""Eval-gated model routing; the baseline remains unchanged by default."""

from __future__ import annotations

import os
from dataclasses import dataclass

BASELINE_MODEL = "gemini-3.5-flash"
SUPPORTED_CANDIDATES = {
    BASELINE_MODEL,
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
}


@dataclass(frozen=True)
class ModelPolicy:
    orchestrator: str
    document_worker: str
    inventory_specialist: str
    final_reviewer: str
    routing_enabled: bool


def _candidate(name: str, default: str) -> str:
    selected = os.environ.get(name, default).strip()
    if selected not in SUPPORTED_CANDIDATES:
        raise ValueError(f"modelo não aprovado na política: {selected}")
    return selected


def load_model_policy() -> ModelPolicy:
    enabled = os.environ.get("PJE_MODEL_ROUTING_ENABLED", "0").strip().casefold() in {
        "1",
        "true",
        "yes",
    }
    if not enabled:
        return ModelPolicy(
            orchestrator=BASELINE_MODEL,
            document_worker=BASELINE_MODEL,
            inventory_specialist=BASELINE_MODEL,
            final_reviewer=BASELINE_MODEL,
            routing_enabled=False,
        )
    return ModelPolicy(
        orchestrator=_candidate("PJE_ORCHESTRATOR_MODEL", BASELINE_MODEL),
        document_worker=_candidate("PJE_DOCUMENT_WORKER_MODEL", "gemini-3.6-flash"),
        inventory_specialist=_candidate("PJE_INVENTORY_MODEL", BASELINE_MODEL),
        final_reviewer=_candidate("PJE_FINAL_REVIEWER_MODEL", BASELINE_MODEL),
        routing_enabled=True,
    )
