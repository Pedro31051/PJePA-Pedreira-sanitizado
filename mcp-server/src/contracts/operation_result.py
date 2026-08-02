"""Standardized OperationResult envelope for FastMCP tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .completeness import MultidimensionalCompleteness
from .error_taxonomy import ErrorDetail
from .evidence import EvidenceItem, GapDetail
from .latency import LatencyBreakdown


@dataclass
class OperationResult:
    status: str  # "success" | "partial" | "error"
    data: Dict[str, Any] = field(default_factory=dict)
    completeness: MultidimensionalCompleteness = field(
        default_factory=MultidimensionalCompleteness
    )
    gap_details: List[GapDetail] = field(default_factory=list)
    evidence: List[EvidenceItem] = field(default_factory=list)
    latency: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    error: Optional[ErrorDetail] = None

    def to_dict(self) -> Dict[str, Any]:
        res: Dict[str, Any] = dict(self.data) if self.data else {}
        res["status"] = self.status
        res["data"] = self.data
        res["completeness"] = (
            self.completeness.to_dict()
            if hasattr(self.completeness, "to_dict")
            else self.completeness
        )
        res["gap_details"] = [
            g.to_dict() if hasattr(g, "to_dict") else g for g in self.gap_details
        ]
        res["evidence"] = [
            e.to_dict() if hasattr(e, "to_dict") else e for e in self.evidence
        ]
        res["latency"] = (
            self.latency.to_dict()
            if hasattr(self.latency, "to_dict")
            else self.latency
        )

        if self.error is not None:
            err_dict = (
                self.error.to_dict()
                if hasattr(self.error, "to_dict")
                else self.error
            )
            res["error"] = err_dict
            if "erro" not in res and isinstance(err_dict, dict):
                res["erro"] = err_dict.get("message", "")
            if "codigo" not in res and isinstance(err_dict, dict):
                res["codigo"] = err_dict.get("code", "")
        else:
            res["error"] = None

        return res

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> OperationResult:
        if not isinstance(data, dict):
            data = {}
        comp_dict = data.get("completeness")
        comp = (
            MultidimensionalCompleteness.from_dict(comp_dict)
            if isinstance(comp_dict, dict)
            else MultidimensionalCompleteness()
        )

        lat_dict = data.get("latency")
        lat = (
            LatencyBreakdown.from_dict(lat_dict)
            if isinstance(lat_dict, dict)
            else LatencyBreakdown()
        )

        gaps = [
            GapDetail.from_dict(g) if isinstance(g, dict) else g
            for g in (data.get("gap_details") or [])
            if isinstance(g, (dict, GapDetail))
        ]
        evidences = [
            EvidenceItem.from_dict(e) if isinstance(e, dict) else e
            for e in (data.get("evidence") or [])
            if isinstance(e, (dict, EvidenceItem))
        ]

        err_dict = data.get("error")
        err = ErrorDetail.from_dict(err_dict) if isinstance(err_dict, dict) else None

        return cls(
            status=str(data.get("status") or "error"),
            data=data.get("data") if isinstance(data.get("data"), dict) else {},
            completeness=comp,
            gap_details=gaps,
            evidence=evidences,
            latency=lat,
            error=err,
        )
