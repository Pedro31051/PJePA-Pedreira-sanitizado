"""Unit tests for Latency Observability & Telemetry (`test_observability.py`).
"""

import asyncio
import time

import pytest

from contracts.latency import LatencyBreakdown
from contracts.operation_result import OperationResult
from observability import LatencyTracker, get_metrics_collector


@pytest.mark.asyncio
async def test_latency_tracker_phases():
    tracker = LatencyTracker()

    with tracker.measure("internal_mcp"):
        time.sleep(0.01)

    async with tracker.measure("tjpa_external"):
        await asyncio.sleep(0.02)

    with tracker.measure("ocr_extraction"):
        time.sleep(0.005)

    with tracker.measure("domain_analysis"):
        time.sleep(0.005)

    breakdown = tracker.finalize()

    assert breakdown.internal_mcp_ms > 5.0
    assert breakdown.tjpa_external_ms > 15.0
    assert breakdown.ocr_extraction_ms > 2.0
    assert breakdown.domain_analysis_ms > 2.0
    assert breakdown.total_duration_ms >= breakdown.internal_mcp_ms + breakdown.tjpa_external_ms


def test_metrics_collector_integration():
    collector = get_metrics_collector()
    collector.reset()

    lat = LatencyBreakdown(
        internal_mcp_ms=10.0,
        tjpa_external_ms=50.0,
        ocr_extraction_ms=15.0,
        domain_analysis_ms=5.0,
        total_duration_ms=80.0,
    )
    collector.record_latency(lat)

    summary = collector.get_metrics_summary()
    tech = summary["technical_metrics"]["extraction_time_per_tier_avg"]
    assert tech["internal_mcp"] == 0.01
    assert tech["tjpa_external"] == 0.05
    assert tech["ocr_extraction"] == 0.015
    assert tech["domain_analysis"] == 0.005
    assert tech["total_duration"] == 0.08


def test_operation_result_with_latency():
    tracker = LatencyTracker()
    with tracker.measure("internal_mcp"):
        time.sleep(0.01)
    lat = tracker.finalize()

    res = OperationResult(status="success", data={"val": 123}, latency=lat)
    d = res.to_dict()

    assert d["status"] == "success"
    assert d["latency"]["internal_mcp_ms"] > 5.0
    assert d["latency"]["total_duration_ms"] > 5.0
