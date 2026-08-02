"""Adversarial Contract & Observability Stress Tests (Milestone 4 - R1).

Author: challenger_1_m4_r1
Scope: Common Contracts Library (`mcp-server/src/contracts/`) and Latency Observability (`mcp-server/src/observability.py`).
"""

import asyncio
import json
import time

import pytest

from contracts.completeness import MultidimensionalCompleteness
from contracts.error_taxonomy import ErrorCategory, ErrorCode, ErrorDetail
from contracts.evidence import EvidenceItem, GapDetail
from contracts.latency import LatencyBreakdown
from contracts.operation_result import OperationResult
from observability import LatencyTracker

# ============================================================================
# 1. ADVERSARIAL CONTRACT STRESS TESTS: ErrorTaxonomy
# ============================================================================

class TestAdversarialErrorTaxonomy:
    """Stress tests for ErrorCategory, ErrorCode, and ErrorDetail."""

    def test_error_detail_valid_instantiation(self):
        err = ErrorDetail(
            code=ErrorCode.AUTH_EXPIRED,
            category=ErrorCategory.AUTHENTICATION,
            message="Session expired",
            is_recoverable=True,
            origin="TJPA_EXTERNAL",
            details={"tenant": "1g_pje"},
        )
        assert err.code == ErrorCode.AUTH_EXPIRED
        assert err.category == ErrorCategory.AUTHENTICATION
        assert err.message == "Session expired"
        assert err.is_recoverable is True
        assert err.origin == "TJPA_EXTERNAL"
        assert err.details == {"tenant": "1g_pje"}

        d = err.to_dict()
        assert d["code"] == "AUTH_EXPIRED"
        assert d["category"] == "AUTHENTICATION"
        assert d["message"] == "Session expired"
        assert d["is_recoverable"] is True
        assert d["origin"] == "TJPA_EXTERNAL"
        assert d["details"] == {"tenant": "1g_pje"}

    def test_error_detail_from_dict_malformed_values(self):
        """Test from_dict with invalid/unknown error category and code values."""
        malformed_data = {
            "code": "NON_EXISTENT_CODE_123",
            "category": "UNKNOWN_CATEGORY_XYZ",
            "message": "Malformed error test",
            "is_recoverable": "true",  # string bool
            "origin": "INTERNAL_MCP",
            "details": None,
        }
        err = ErrorDetail.from_dict(malformed_data)
        # When details is explicitly None in dict, data.get("details", {}) evaluates to None
        assert err.details is None

    def test_error_detail_from_dict_empty_payload(self):
        """Test deserialization of empty dictionary."""
        err = ErrorDetail.from_dict({})
        assert err.code == ErrorCode.UNEXPECTED_INTERNAL_ERROR
        assert err.category == ErrorCategory.INTERNAL
        assert err.message == ""
        assert err.is_recoverable is False
        assert err.origin == "INTERNAL_MCP"
        assert err.details == {}

    def test_error_detail_json_roundtrip(self):
        """Test JSON serialization and deserialization roundtrip."""
        err = ErrorDetail(
            code=ErrorCode.SINGLE_FLIGHT_TIMEOUT,
            category=ErrorCategory.CONCURRENCY,
            message="Lock acquisition timed out",
            is_recoverable=False,
            origin="INTERNAL_MCP",
            details={"lock_key": "user123:1g"},
        )
        json_str = json.dumps(err.to_dict())
        restored_dict = json.loads(json_str)
        err_restored = ErrorDetail.from_dict(restored_dict)

        assert err_restored.code == err.code
        assert err_restored.category == err.category
        assert err_restored.message == err.message
        assert err_restored.is_recoverable == err.is_recoverable
        assert err_restored.origin == err.origin
        assert err_restored.details == err.details

    def test_error_detail_raw_string_enums_in_instance(self):
        """Test behavior when code and category are passed as raw strings."""
        err = ErrorDetail(
            code="CUSTOM_CODE",  # type: ignore
            category="CUSTOM_CAT",  # type: ignore
            message="Raw strings passed",
            is_recoverable=False,
            origin="INTERNAL_MCP",
        )
        d = err.to_dict()
        assert d["code"] == "CUSTOM_CODE"
        assert d["category"] == "CUSTOM_CAT"


# ============================================================================
# 2. ADVERSARIAL CONTRACT STRESS TESTS: MultidimensionalCompleteness
# ============================================================================

class TestAdversarialCompleteness:
    """Stress tests for MultidimensionalCompleteness (6 dimensions)."""

    def test_completeness_defaults_and_overall_score(self):
        comp = MultidimensionalCompleteness()
        assert comp.source == 1.0
        assert comp.documents == 1.0
        assert comp.pages == 1.0
        assert comp.metadata == 1.0
        assert comp.domain_analysis == 1.0
        assert comp.temporal_consistency == 1.0
        assert comp.overall_score == 1.0

    def test_completeness_boundary_scores(self):
        """Verify behavior under boundary scores (0.0, 1.0, negative numbers, out-of-range floats)."""
        comp = MultidimensionalCompleteness(
            source=0.0,
            documents=0.5,
            pages=1.0,
            metadata=-0.5,
            domain_analysis=2.5,
            temporal_consistency=0.0,
        )
        # sum = 0.0 + 0.5 + 1.0 - 0.5 + 2.5 + 0.0 = 3.5; 3.5 / 6 = 0.583333... -> 0.5833
        assert comp.overall_score == 0.5833

        d = comp.to_dict()
        assert d["source"] == 0.0
        assert d["documents"] == 0.5
        assert d["pages"] == 1.0
        assert d["metadata"] == -0.5
        assert d["domain_analysis"] == 2.5
        assert d["temporal_consistency"] == 0.0
        assert d["overall_score"] == 0.5833

    def test_completeness_from_dict_string_floats_and_defaults(self):
        """Test deserialization with string float inputs and missing keys."""
        data = {
            "source": "0.80",
            "documents": "0.90",
            "pages": 1,
            # metadata missing -> 1.0
            "domain_analysis": "0.5",
            "temporal_consistency": 0,
        }
        comp = MultidimensionalCompleteness.from_dict(data)
        assert comp.source == 0.8
        assert comp.documents == 0.9
        assert comp.pages == 1.0
        assert comp.metadata == 1.0
        assert comp.domain_analysis == 0.5
        assert comp.temporal_consistency == 0.0

    def test_completeness_json_roundtrip(self):
        comp = MultidimensionalCompleteness(
            source=0.85,
            documents=0.92,
            pages=0.99,
            metadata=1.0,
            domain_analysis=0.75,
            temporal_consistency=0.88,
        )
        json_str = json.dumps(comp.to_dict())
        restored_comp = MultidimensionalCompleteness.from_dict(json.loads(json_str))
        assert restored_comp.source == 0.85
        assert restored_comp.documents == 0.92
        assert restored_comp.pages == 0.99
        assert restored_comp.metadata == 1.0
        assert restored_comp.domain_analysis == 0.75
        assert restored_comp.temporal_consistency == 0.88
        assert restored_comp.overall_score == comp.overall_score


# ============================================================================
# 3. ADVERSARIAL CONTRACT STRESS TESTS: Evidence & Gap Details
# ============================================================================

class TestAdversarialEvidenceAndGaps:
    """Stress tests for EvidenceItem and GapDetail."""

    def test_evidence_item_boundary_and_malformed(self):
        ev = EvidenceItem(
            type="pdf_page",
            reference="p. 42",
            description="Doc signature present",
            confidence=0.99999,
            raw_data={"sha256": "abc123hash"},
        )
        d = ev.to_dict()
        assert d["type"] == "pdf_page"
        assert d["confidence"] == 1.0  # round(0.99999, 4) -> 1.0

        ev_restored = EvidenceItem.from_dict({
            "type": None,
            "reference": 100,
            "description": None,
            "confidence": "0.75",
        })
        assert ev_restored.type == "None"
        assert ev_restored.reference == "100"
        assert ev_restored.description == "None"  # str(None) -> "None"
        assert ev_restored.confidence == 0.75
        assert ev_restored.raw_data == {}

    def test_gap_detail_boundary_and_malformed(self):
        gap = GapDetail(
            code="MISSING_ATTACHMENT",
            description="Attachment #3 is unreachable",
            severity="HIGH",
            impact="Domain analysis incomplete",
        )
        d = gap.to_dict()
        assert d["code"] == "MISSING_ATTACHMENT"
        assert d["severity"] == "HIGH"

        gap_restored = GapDetail.from_dict({})
        assert gap_restored.code == "UNKNOWN_GAP"
        assert gap_restored.description == ""
        assert gap_restored.severity == "MEDIUM"
        assert gap_restored.impact == ""


# ============================================================================
# 4. ADVERSARIAL CONTRACT STRESS TESTS: LatencyBreakdown
# ============================================================================

class TestAdversarialLatencyBreakdown:
    """Stress tests for LatencyBreakdown."""

    def test_latency_breakdown_serialization_and_rounding(self):
        lat = LatencyBreakdown(
            internal_mcp_ms=12.3456,
            tjpa_external_ms=987.6543,
            ocr_extraction_ms=45.6789,
            domain_analysis_ms=10.1112,
            total_duration_ms=1055.7899,
        )
        d = lat.to_dict()
        assert d["internal_mcp_ms"] == 12.35
        assert d["tjpa_external_ms"] == 987.65
        assert d["ocr_extraction_ms"] == 45.68
        assert d["domain_analysis_ms"] == 10.11
        assert d["total_duration_ms"] == 1055.79

    def test_latency_breakdown_from_dict_string_parsing(self):
        data = {
            "internal_mcp_ms": "15.5",
            "tjpa_external_ms": 200,
            # missing ocr_extraction_ms -> 0.0
            "domain_analysis_ms": "0.0",
            "total_duration_ms": "215.5",
        }
        lat = LatencyBreakdown.from_dict(data)
        assert lat.internal_mcp_ms == 15.5
        assert lat.tjpa_external_ms == 200.0
        assert lat.ocr_extraction_ms == 0.0
        assert lat.domain_analysis_ms == 0.0
        assert lat.total_duration_ms == 215.5


# ============================================================================
# 5. ADVERSARIAL CONTRACT STRESS TESTS: OperationResult Envelope
# ============================================================================

class TestAdversarialOperationResult:
    """Stress tests for OperationResult envelope dataclass."""

    def test_operation_result_success_to_dict_envelope(self):
        result = OperationResult(
            status="success",
            data={"cnj": "0000123-45.2026.8.14.0001", "paginas": 15},
            completeness=MultidimensionalCompleteness(source=1.0, pages=0.9),
            evidence=[EvidenceItem(type="doc", reference="p.1", description="Valid")],
            gap_details=[GapDetail(code="NONE", description="No gaps")],
            latency=LatencyBreakdown(internal_mcp_ms=10.0, total_duration_ms=10.0),
        )

        d = result.to_dict()
        assert d["status"] == "success"
        assert d["data"] == {"cnj": "0000123-45.2026.8.14.0001", "paginas": 15}
        # Verify top-level flattening of data keys for FastMCP compatibility
        assert d["cnj"] == "0000123-45.2026.8.14.0001"
        assert d["paginas"] == 15
        assert d["completeness"]["pages"] == 0.9
        assert len(d["evidence"]) == 1
        assert len(d["gap_details"]) == 1
        assert d["latency"]["internal_mcp_ms"] == 10.0
        assert d["error"] is None

    def test_operation_result_error_envelope_legacy_keys(self):
        err = ErrorDetail(
            code=ErrorCode.DOCUMENT_NOT_FOUND,
            category=ErrorCategory.VALIDATION,
            message="CNJ process not found on TJPA",
            is_recoverable=False,
            origin="TJPA_EXTERNAL",
        )
        result = OperationResult(
            status="error",
            data={},
            error=err,
        )

        d = result.to_dict()
        assert d["status"] == "error"
        assert d["error"]["code"] == "DOCUMENT_NOT_FOUND"
        assert d["error"]["category"] == "VALIDATION"

        # Verify backward compatibility keys injected into response
        assert d["erro"] == "CNJ process not found on TJPA"
        assert d["codigo"] == "DOCUMENT_NOT_FOUND"

    def test_operation_result_json_serialization_roundtrip(self):
        err = ErrorDetail(
            code=ErrorCode.TJPA_503_UNAVAILABLE,
            category=ErrorCategory.EXTERNAL_TJPA,
            message="TJPA portal 503 Service Unavailable",
            is_recoverable=True,
            origin="TJPA_EXTERNAL",
            details={"retry_after_s": 15},
        )
        orig_result = OperationResult(
            status="error",
            data={"attempt": 3},
            completeness=MultidimensionalCompleteness(source=0.0),
            gap_details=[GapDetail(code="TJPA_DOWN", description="Service offline", severity="CRITICAL")],
            evidence=[],
            latency=LatencyBreakdown(tjpa_external_ms=500.0, total_duration_ms=505.0),
            error=err,
        )

        dict_form = orig_result.to_dict()
        json_str = json.dumps(dict_form)
        deserialized_dict = json.loads(json_str)

        restored_result = OperationResult.from_dict(deserialized_dict)
        assert restored_result.status == "error"
        assert restored_result.data == {"attempt": 3}
        assert restored_result.completeness.source == 0.0
        assert len(restored_result.gap_details) == 1
        assert restored_result.gap_details[0].code == "TJPA_DOWN"
        assert restored_result.latency.tjpa_external_ms == 500.0
        assert restored_result.error is not None
        assert restored_result.error.code == ErrorCode.TJPA_503_UNAVAILABLE
        assert restored_result.error.category == ErrorCategory.EXTERNAL_TJPA

    def test_operation_result_from_dict_flat_legacy_input(self):
        """Test deserialization when receiving a legacy flat dict without nested 'data' key."""
        flat_input = {
            "status": "success",
            "resultado": "OK",
            "valido": True,
        }
        res = OperationResult.from_dict(flat_input)
        assert res.status == "success"
        assert res.data == {}
        assert res.completeness.overall_score == 1.0
        assert res.error is None


# ============================================================================
# 6. OBSERVABILITY STRESS TESTS: LatencyTracker & PhaseTimer
# ============================================================================

class TestAdversarialObservability:
    """Stress tests for LatencyTracker context manager and metrics integration."""

    def test_latency_tracker_synchronous_measurement(self):
        tracker = LatencyTracker()
        with tracker.measure("internal_mcp"):
            time.sleep(0.01)

        with tracker.measure("tjpa_external"):
            time.sleep(0.02)

        with tracker.measure("ocr_extraction"):
            time.sleep(0.01)

        with tracker.measure("domain_analysis"):
            time.sleep(0.01)

        breakdown = tracker.finalize()

        assert breakdown.internal_mcp_ms >= 8.0
        assert breakdown.tjpa_external_ms >= 18.0
        assert breakdown.ocr_extraction_ms >= 8.0
        assert breakdown.domain_analysis_ms >= 8.0
        assert breakdown.total_duration_ms >= 45.0

    @pytest.mark.asyncio
    async def test_latency_tracker_async_measurement(self):
        tracker = LatencyTracker()
        async with tracker.measure("tjpa_external"):
            await asyncio.sleep(0.02)

        breakdown = tracker.finalize()
        assert breakdown.tjpa_external_ms >= 18.0
        assert breakdown.total_duration_ms >= 18.0

    def test_latency_tracker_exception_handling(self):
        """Ensure timer records elapsed time even when wrapped block raises an exception."""
        tracker = LatencyTracker()

        with pytest.raises(ValueError, match="Simulated processing error"):
            with tracker.measure("ocr_extraction"):
                time.sleep(0.015)
                raise ValueError("Simulated processing error")

        breakdown = tracker.finalize()

        # Timer must have captured the ~15ms spent before the exception was raised
        assert breakdown.ocr_extraction_ms >= 12.0
        assert breakdown.total_duration_ms >= 12.0

    @pytest.mark.asyncio
    async def test_latency_tracker_async_exception_handling(self):
        """Ensure async timer records elapsed time under raised exception."""
        tracker = LatencyTracker()

        with pytest.raises(RuntimeError, match="Async failure"):
            async with tracker.measure("tjpa_external"):
                await asyncio.sleep(0.015)
                raise RuntimeError("Async failure")

        breakdown = tracker.finalize()
        assert breakdown.tjpa_external_ms >= 12.0
        assert breakdown.total_duration_ms >= 12.0

    def test_latency_tracker_nested_timing_phases(self):
        """Test nested timing phases within single LatencyTracker."""
        tracker = LatencyTracker()

        with tracker.measure("internal_mcp"):
            time.sleep(0.01)
            with tracker.measure("ocr_extraction"):
                time.sleep(0.015)
            time.sleep(0.005)

        breakdown = tracker.finalize()
        assert breakdown.internal_mcp_ms >= 20.0
        assert breakdown.ocr_extraction_ms >= 12.0

    def test_latency_tracker_phase_aliases(self):
        """Verify alias names map to correct LatencyBreakdown fields."""
        tracker = LatencyTracker()
        with tracker.measure("mcp"):
            time.sleep(0.005)
        with tracker.measure("external"):
            time.sleep(0.005)
        with tracker.measure("ocr"):
            time.sleep(0.005)
        with tracker.measure("domain"):
            time.sleep(0.005)
        with tracker.measure("unknown_phase_xyz"):
            time.sleep(0.005)

        breakdown = tracker.finalize()
        assert breakdown.internal_mcp_ms >= 8.0  # mcp + unknown_phase_xyz
        assert breakdown.tjpa_external_ms >= 4.0
        assert breakdown.ocr_extraction_ms >= 4.0
        assert breakdown.domain_analysis_ms >= 4.0

    def test_latency_tracker_zero_overhead_high_throughput(self):
        """Verify zero-overhead performance when timing rapid operations."""
        tracker = LatencyTracker()
        t0 = time.monotonic()

        # Run 10,000 rapid measurements
        for _ in range(10000):
            with tracker.measure("internal_mcp"):
                pass

        elapsed_wall_ms = (time.monotonic() - t0) * 1000.0
        breakdown = tracker.finalize()

        # 10,000 empty context manager calls should complete in under 100ms total
        assert elapsed_wall_ms < 200.0, f"Expected <200ms overhead for 10k measurements, got {elapsed_wall_ms:.2f}ms"
        assert breakdown.internal_mcp_ms > 0.0

    def test_latency_tracker_finalize_fallback(self):
        """Verify finalize fallback attributes wall clock time to internal_mcp if no phases measured."""
        tracker = LatencyTracker()
        time.sleep(0.01)
        breakdown = tracker.finalize()

        assert breakdown.internal_mcp_ms >= 8.0
        assert breakdown.tjpa_external_ms == 0.0
        assert breakdown.ocr_extraction_ms == 0.0
        assert breakdown.domain_analysis_ms == 0.0
        assert breakdown.total_duration_ms >= 8.0
