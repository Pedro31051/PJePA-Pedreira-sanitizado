"""Latency breakdown contract for operation telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class LatencyBreakdown:
    internal_mcp_ms: float = 0.0
    tjpa_external_ms: float = 0.0
    ocr_extraction_ms: float = 0.0
    domain_analysis_ms: float = 0.0
    total_duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "internal_mcp_ms": round(float(self.internal_mcp_ms), 2),
            "tjpa_external_ms": round(float(self.tjpa_external_ms), 2),
            "ocr_extraction_ms": round(float(self.ocr_extraction_ms), 2),
            "domain_analysis_ms": round(float(self.domain_analysis_ms), 2),
            "total_duration_ms": round(float(self.total_duration_ms), 2),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> LatencyBreakdown:
        if not isinstance(data, dict):
            data = {}

        def _to_float(v: Any) -> float:
            if v is None:
                return 0.0
            try:
                return float(v)
            except (ValueError, TypeError):
                return 0.0

        return cls(
            internal_mcp_ms=_to_float(data.get("internal_mcp_ms")),
            tjpa_external_ms=_to_float(data.get("tjpa_external_ms")),
            ocr_extraction_ms=_to_float(data.get("ocr_extraction_ms")),
            domain_analysis_ms=_to_float(data.get("domain_analysis_ms")),
            total_duration_ms=_to_float(data.get("total_duration_ms")),
        )
