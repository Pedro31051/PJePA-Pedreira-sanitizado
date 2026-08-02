"""Unit tests for Common Contracts Library & Error Taxonomy (`test_contracts_unit.py`).
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from contracts import (
    ErrorCategory,
    ErrorCode,
    ErrorDetail,
    EvidenceItem,
    GapDetail,
    LatencyBreakdown,
    MultidimensionalCompleteness,
    OperationResult,
)


def test_error_detail_serialization():
    err = ErrorDetail(
        code=ErrorCode.AUTH_EXPIRED,
        category=ErrorCategory.AUTHENTICATION,
        message="Session expired on TJPA",
        is_recoverable=True,
        origin="TJPA_EXTERNAL",
        details={"http_status": 401},
    )
    d = err.to_dict()
    assert d["code"] == "AUTH_EXPIRED"
    assert d["category"] == "AUTHENTICATION"
    assert d["is_recoverable"] is True
    assert d["origin"] == "TJPA_EXTERNAL"
    assert d["details"]["http_status"] == 401

    reconstructed = ErrorDetail.from_dict(d)
    assert reconstructed.code == ErrorCode.AUTH_EXPIRED
    assert reconstructed.category == ErrorCategory.AUTHENTICATION
    assert reconstructed.message == "Session expired on TJPA"


def test_multidimensional_completeness_calculation():
    comp = MultidimensionalCompleteness(
        source=1.0,
        documents=0.8,
        pages=1.0,
        metadata=0.9,
        domain_analysis=0.7,
        temporal_consistency=1.0,
    )
    # Average of [1.0, 0.8, 1.0, 0.9, 0.7, 1.0] = 5.4 / 6 = 0.9
    assert comp.overall_score == 0.9

    d = comp.to_dict()
    assert d["overall_score"] == 0.9
    assert d["documents"] == 0.8

    reconstructed = MultidimensionalCompleteness.from_dict(d)
    assert reconstructed.overall_score == 0.9


def test_latency_breakdown_serialization():
    lat = LatencyBreakdown(
        internal_mcp_ms=10.5,
        tjpa_external_ms=150.2,
        ocr_extraction_ms=45.0,
        domain_analysis_ms=12.1,
        total_duration_ms=217.8,
    )
    d = lat.to_dict()
    assert d["internal_mcp_ms"] == 10.5
    assert d["tjpa_external_ms"] == 150.2
    assert d["total_duration_ms"] == 217.8

    reconstructed = LatencyBreakdown.from_dict(d)
    assert reconstructed.internal_mcp_ms == 10.5
    assert reconstructed.tjpa_external_ms == 150.2


def test_evidence_and_gap_detail():
    ev = EvidenceItem(
        type="signature",
        reference="doc_101_p1",
        description="Digital signature verified",
        confidence=0.99,
        raw_data={"signer": "Juiz de Direito"},
    )
    ev_dict = ev.to_dict()
    assert ev_dict["confidence"] == 0.99
    assert ev_dict["raw_data"]["signer"] == "Juiz de Direito"

    gap = GapDetail(
        code="OCR_REQUIRED",
        description="Scanned page without native text",
        severity="HIGH",
        impact="Reduced text searchability",
    )
    gap_dict = gap.to_dict()
    assert gap_dict["severity"] == "HIGH"


def test_operation_result_envelope_serialization():
    comp = MultidimensionalCompleteness(documents=0.9)
    lat = LatencyBreakdown(total_duration_ms=100.0)
    err = ErrorDetail(
        code=ErrorCode.INVALID_CNJ_FORMAT,
        category=ErrorCategory.VALIDATION,
        message="Invalid CNJ format",
        is_recoverable=False,
        origin="INTERNAL_MCP",
    )
    gap = GapDetail(code="GAP1", description="Missing page", severity="LOW", impact="None")
    ev = EvidenceItem(type="stamp", reference="ref1", description="desc1")

    res = OperationResult(
        status="error",
        data={"numero_cnj": "000"},
        completeness=comp,
        gap_details=[gap],
        evidence=[ev],
        latency=lat,
        error=err,
    )
    d = res.to_dict()

    assert d["status"] == "error"
    assert d["completeness"]["documents"] == 0.9
    assert d["latency"]["total_duration_ms"] == 100.0
    assert d["error"]["code"] == "INVALID_CNJ_FORMAT"
    assert d["gap_details"][0]["code"] == "GAP1"
    assert d["evidence"][0]["type"] == "stamp"

    reconstructed = OperationResult.from_dict(d)
    assert reconstructed.status == "error"
    assert reconstructed.error.code == ErrorCode.INVALID_CNJ_FORMAT
