"""Telemetry & Metrics Collector (`metrics_collector.py`).

Tracks technical metrics (tier extraction times, OCR fallback rates, cache efficiency)
and domain metrics (completeness score averages, missing requirements frequency,
domain profile distribution).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Tuple, Union


class MetricsCollector:
    def __init__(self):
        self._extraction_times: Dict[str, List[float]] = {}
        self._ocr_attempts: int = 0
        self._ocr_fallbacks: int = 0
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_negative_hits: int = 0
        self._completeness_scores: List[float] = []
        self._missing_requirements: Counter = Counter()
        self._domain_profile_counts: Counter = Counter()

    def record_extraction_time(self, tier: str, duration_seconds: float) -> None:
        """Record extraction duration in seconds for a specific tier (e.g. tier1_native)."""
        if tier not in self._extraction_times:
            self._extraction_times[tier] = []
        self._extraction_times[tier].append(float(duration_seconds))

    def record_latency(self, breakdown: Any) -> None:
        """Record LatencyBreakdown metrics."""
        if hasattr(breakdown, "internal_mcp_ms"):
            self.record_extraction_time("internal_mcp", breakdown.internal_mcp_ms / 1000.0)
            self.record_extraction_time("tjpa_external", breakdown.tjpa_external_ms / 1000.0)
            self.record_extraction_time("ocr_extraction", breakdown.ocr_extraction_ms / 1000.0)
            self.record_extraction_time("domain_analysis", breakdown.domain_analysis_ms / 1000.0)
            self.record_extraction_time("total_duration", breakdown.total_duration_ms / 1000.0)


    def record_ocr_execution(self, fallback_occurred: bool = False) -> None:
        """Record an OCR extraction attempt and whether a fallback occurred."""
        self._ocr_attempts += 1
        if fallback_occurred:
            self._ocr_fallbacks += 1

    def record_cache_access(self, hit_type: str) -> None:
        """Record cache event: 'hit', 'miss', or 'negative_hit'."""
        ht = hit_type.lower()
        if ht == "hit":
            self._cache_hits += 1
        elif ht == "miss":
            self._cache_misses += 1
        elif ht in ("negative_hit", "negative"):
            self._cache_negative_hits += 1

    def record_completeness_score(self, score: float) -> None:
        """Record process analysis completeness score (0.0 to 1.0 or 0 to 100)."""
        self._completeness_scores.append(float(score))

    def record_missing_requirements(self, requirements: Union[List[str], str]) -> None:
        """Record missing requirement items."""
        if isinstance(requirements, str):
            self._missing_requirements[requirements] += 1
        else:
            for req in requirements:
                self._missing_requirements[req] += 1

    def record_domain_profile(self, profile_name: str) -> None:
        """Record domain profile execution (e.g., 'inventory/v1', 'adverse-possession/v1')."""
        self._domain_profile_counts[profile_name] += 1

    def get_ocr_fallback_rate(self) -> float:
        if self._ocr_attempts == 0:
            return 0.0
        return round(self._ocr_fallbacks / self._ocr_attempts, 4)

    def get_cache_efficiency(self) -> float:
        total = self._cache_hits + self._cache_misses + self._cache_negative_hits
        if total == 0:
            return 0.0
        return round((self._cache_hits + self._cache_negative_hits) / total, 4)

    def get_average_completeness_score(self) -> float:
        if not self._completeness_scores:
            return 0.0
        return round(sum(self._completeness_scores) / len(self._completeness_scores), 4)

    def get_top_missing_requirements(self, n: int = 5) -> List[Tuple[str, int]]:
        return self._missing_requirements.most_common(n)

    def get_metrics_summary(self) -> Dict[str, Any]:
        """Generate comprehensive dictionary of technical and domain metrics."""
        avg_extraction_times = {
            tier: round(sum(times) / len(times), 4) if times else 0.0
            for tier, times in self._extraction_times.items()
        }

        return {
            "technical_metrics": {
                "extraction_time_per_tier_avg": avg_extraction_times,
                "extraction_time_per_tier_raw": self._extraction_times,
                "ocr_attempts": self._ocr_attempts,
                "ocr_fallbacks": self._ocr_fallbacks,
                "ocr_fallback_rate": self.get_ocr_fallback_rate(),
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "cache_negative_hits": self._cache_negative_hits,
                "cache_efficiency": self.get_cache_efficiency(),
            },
            "domain_metrics": {
                "total_analyses_evaluated": len(self._completeness_scores),
                "average_completeness_score": self.get_average_completeness_score(),
                "top_missing_requirements": self.get_top_missing_requirements(10),
                "domain_profile_counts": dict(self._domain_profile_counts),
            },
        }

    def reset(self) -> None:
        """Reset all metric counters."""
        self._extraction_times.clear()
        self._ocr_attempts = 0
        self._ocr_fallbacks = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_negative_hits = 0
        self._completeness_scores.clear()
        self._missing_requirements.clear()
        self._domain_profile_counts.clear()
