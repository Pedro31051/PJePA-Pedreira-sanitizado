"""Common Contracts Library & Error Taxonomy for FastMCP PJePA.
"""
from .completeness import MultidimensionalCompleteness
from .error_taxonomy import ErrorCategory, ErrorCode, ErrorDetail
from .evidence import EvidenceItem, GapDetail
from .latency import LatencyBreakdown
from .operation_result import OperationResult

__all__ = [
    "ErrorCategory",
    "ErrorCode",
    "ErrorDetail",
    "LatencyBreakdown",
    "MultidimensionalCompleteness",
    "EvidenceItem",
    "GapDetail",
    "OperationResult",
]
