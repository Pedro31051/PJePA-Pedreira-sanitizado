"""Standardized Error Taxonomy for FastMCP PJePA Tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ErrorCategory(str, Enum):
    AUTHENTICATION = "AUTHENTICATION"
    CONCURRENCY = "CONCURRENCY"
    EXTERNAL_TJPA = "EXTERNAL_TJPA"
    PARSING = "PARSING"
    JOB_ENGINE = "JOB_ENGINE"
    VALIDATION = "VALIDATION"
    INTERNAL = "INTERNAL"


class ErrorCode(str, Enum):
    AUTH_EXPIRED = "AUTH_EXPIRED"
    SINGLE_FLIGHT_TIMEOUT = "SINGLE_FLIGHT_TIMEOUT"
    TJPA_503_UNAVAILABLE = "TJPA_503_UNAVAILABLE"
    TJPA_HTTP_ERROR = "TJPA_HTTP_ERROR"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    PDF_CORRUPTED = "PDF_CORRUPTED"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_FAILED = "JOB_FAILED"
    JOB_CANCELLED = "JOB_CANCELLED"
    OCR_CONFIDENCE_LOW = "OCR_CONFIDENCE_LOW"
    INVALID_CNJ_FORMAT = "INVALID_CNJ_FORMAT"
    INVALID_PERFIL = "INVALID_PERFIL"
    UNEXPECTED_INTERNAL_ERROR = "UNEXPECTED_INTERNAL_ERROR"


@dataclass
class ErrorDetail:
    code: ErrorCode
    category: ErrorCategory
    message: str
    is_recoverable: bool
    origin: str  # "TJPA_EXTERNAL" | "INTERNAL_MCP"
    details: Optional[Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code.value if isinstance(self.code, Enum) else str(self.code),
            "category": self.category.value if isinstance(self.category, Enum) else str(self.category),
            "message": self.message,
            "is_recoverable": self.is_recoverable,
            "origin": self.origin,
            "details": self.details or {},
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> ErrorDetail:
        if not isinstance(data, dict):
            data = {}
        code_raw = data.get("code") or "UNEXPECTED_INTERNAL_ERROR"
        category_raw = data.get("category") or "INTERNAL"
        try:
            code = ErrorCode(code_raw)
        except ValueError:
            code = ErrorCode.UNEXPECTED_INTERNAL_ERROR
        try:
            category = ErrorCategory(category_raw)
        except ValueError:
            category = ErrorCategory.INTERNAL

        details = data.get("details", {})
        if details is not None and not isinstance(details, dict):
            details = {}

        return cls(
            code=code,
            category=category,
            message=str(data.get("message") or ""),
            is_recoverable=bool(data.get("is_recoverable", False)),
            origin=str(data.get("origin") or "INTERNAL_MCP"),
            details=details,
        )
