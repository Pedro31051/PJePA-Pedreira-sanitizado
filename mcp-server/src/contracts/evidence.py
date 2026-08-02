"""Evidence item and Gap detail dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class EvidenceItem:
    type: str
    reference: str
    description: str
    confidence: float = 1.0
    raw_data: Optional[Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "reference": self.reference,
            "description": self.description,
            "confidence": round(float(self.confidence), 4),
            "raw_data": self.raw_data or {},
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> EvidenceItem:
        if not isinstance(data, dict):
            data = {}
        conf_val = data.get("confidence")
        try:
            conf = float(conf_val) if conf_val is not None else 1.0
        except (ValueError, TypeError):
            conf = 1.0

        return cls(
            type=str(data.get("type", "generic")),
            reference=str(data.get("reference", "")),
            description=str(data.get("description", "")),
            confidence=conf,
            raw_data=data.get("raw_data") if isinstance(data.get("raw_data"), dict) else {},
        )


@dataclass
class GapDetail:
    code: str
    description: str
    severity: str = "MEDIUM"  # "LOW", "MEDIUM", "HIGH", "CRITICAL"
    impact: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "description": self.description,
            "severity": self.severity,
            "impact": self.impact,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> GapDetail:
        if not isinstance(data, dict):
            data = {}
        return cls(
            code=str(data.get("code") or "UNKNOWN_GAP"),
            description=str(data.get("description") or ""),
            severity=str(data.get("severity") or "MEDIUM"),
            impact=str(data.get("impact") or ""),
        )
