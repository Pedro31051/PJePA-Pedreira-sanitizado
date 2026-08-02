"""Multidimensional completeness schema (6 dimensions).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class MultidimensionalCompleteness:
    source: float = 1.0
    documents: float = 1.0
    pages: float = 1.0
    metadata: float = 1.0
    domain_analysis: float = 1.0
    temporal_consistency: float = 1.0

    @property
    def overall_score(self) -> float:
        scores = [
            float(self.source),
            float(self.documents),
            float(self.pages),
            float(self.metadata),
            float(self.domain_analysis),
            float(self.temporal_consistency),
        ]
        return round(sum(scores) / len(scores), 4)

    def to_dict(self) -> Dict[str, float]:
        return {
            "source": round(float(self.source), 4),
            "documents": round(float(self.documents), 4),
            "pages": round(float(self.pages), 4),
            "metadata": round(float(self.metadata), 4),
            "domain_analysis": round(float(self.domain_analysis), 4),
            "temporal_consistency": round(float(self.temporal_consistency), 4),
            "overall_score": self.overall_score,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> MultidimensionalCompleteness:
        if not isinstance(data, dict):
            data = {}

        def _to_float(v: Any, default: float = 1.0) -> float:
            if v is None:
                return default
            try:
                return float(v)
            except (ValueError, TypeError):
                return default

        return cls(
            source=_to_float(data.get("source"), 1.0),
            documents=_to_float(data.get("documents"), 1.0),
            pages=_to_float(data.get("pages"), 1.0),
            metadata=_to_float(data.get("metadata"), 1.0),
            domain_analysis=_to_float(data.get("domain_analysis"), 1.0),
            temporal_consistency=_to_float(data.get("temporal_consistency"), 1.0),
        )
