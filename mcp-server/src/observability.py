"""Observability & Latency Tracking (`observability.py`).

Provides LatencyTracker context manager for measuring granular operational latencies
(internal MCP, TJPA external network, OCR extraction, domain analysis).
"""
from __future__ import annotations

import time
from contextvars import ContextVar, Token
from typing import Optional

from contracts.latency import LatencyBreakdown
from metrics_collector import MetricsCollector

_global_metrics_collector = MetricsCollector()

_current_latency_tracker: ContextVar[Optional[LatencyTracker]] = ContextVar(
    "_current_latency_tracker", default=None
)


def get_current_latency_tracker() -> Optional[LatencyTracker]:
    return _current_latency_tracker.get()


def set_current_latency_tracker(tracker: Optional[LatencyTracker]) -> Token:
    return _current_latency_tracker.set(tracker)


def record_subphase(phase: str, duration_ms: float) -> None:
    tracker = get_current_latency_tracker()
    if tracker:
        tracker.add_phase_ms(phase, duration_ms)



def get_metrics_collector() -> MetricsCollector:
    return _global_metrics_collector


class PhaseTimer:
    def __init__(self, tracker: LatencyTracker, phase: str):
        self.tracker = tracker
        self.phase = phase
        self.t0: float = 0.0

    def __enter__(self) -> PhaseTimer:
        self.t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        elapsed = (time.monotonic() - self.t0) * 1000.0
        self.tracker.add_phase_ms(self.phase, elapsed)

    async def __aenter__(self) -> PhaseTimer:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)


class LatencyTracker:
    def __init__(self):
        self.breakdown = LatencyBreakdown()
        self._start_wall = time.monotonic()

    def measure(self, phase: str) -> PhaseTimer:
        return PhaseTimer(self, phase)

    def add_phase_ms(self, phase: str, duration_ms: float) -> None:
        if phase in ("internal_mcp", "mcp"):
            self.breakdown.internal_mcp_ms += duration_ms
        elif phase in ("tjpa_external", "external", "tjpa"):
            self.breakdown.tjpa_external_ms += duration_ms
        elif phase in ("ocr_extraction", "ocr"):
            self.breakdown.ocr_extraction_ms += duration_ms
        elif phase in ("domain_analysis", "domain"):
            self.breakdown.domain_analysis_ms += duration_ms
        else:
            self.breakdown.internal_mcp_ms += duration_ms

    def finalize(self) -> LatencyBreakdown:
        self.breakdown.total_duration_ms = (time.monotonic() - self._start_wall) * 1000.0
        # If no specific phase recorded, attribute total duration to internal_mcp_ms
        phases_sum = (
            self.breakdown.internal_mcp_ms
            + self.breakdown.tjpa_external_ms
            + self.breakdown.ocr_extraction_ms
            + self.breakdown.domain_analysis_ms
        )
        if phases_sum == 0.0:
            self.breakdown.internal_mcp_ms = self.breakdown.total_duration_ms

        _global_metrics_collector.record_extraction_time(
            "total_duration", self.breakdown.total_duration_ms / 1000.0
        )
        return self.breakdown
