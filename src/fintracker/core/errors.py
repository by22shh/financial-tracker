"""Типизированные доменные ошибки и их коды (DATA_CONTRACT §4).

Коды соответствуют контракту ответа: code, message, correlation_id, retryable.
Ни одна ошибка не раскрывает токены, SQL, чужие данные и детали инфраструктуры.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    DUPLICATE_CANDIDATE = "DUPLICATE_CANDIDATE"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    IMPORT_RECONCILIATION_FAILED = "IMPORT_RECONCILIATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    CONFLICT = "CONFLICT"


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.DUPLICATE_CANDIDATE: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.NEEDS_CLARIFICATION: 422,
    ErrorCode.IMPORT_RECONCILIATION_FAILED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.PROVIDER_UNAVAILABLE: 503,
    ErrorCode.TEMPORARILY_UNAVAILABLE: 503,
}


class DomainError(Exception):
    """Базовая ошибка домена с кодом контракта."""

    code: ErrorCode = ErrorCode.VALIDATION_FAILED
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        field_errors: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field_errors = field_errors or {}
        self.details = details or {}
        # Задержка, указанная внешней стороной: повтор не раньше неё (A101).
        self.retry_after = retry_after

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS[self.code]

    def to_payload(self, correlation_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "correlation_id": correlation_id,
            "retryable": self.retryable,
        }
        if self.field_errors:
            payload["field_errors"] = self.field_errors
        return payload


class ValidationFailed(DomainError):
    code = ErrorCode.VALIDATION_FAILED


class NeedsClarification(DomainError):
    code = ErrorCode.NEEDS_CLARIFICATION


class VersionConflict(DomainError):
    code = ErrorCode.VERSION_CONFLICT


class DuplicateCandidate(DomainError):
    code = ErrorCode.DUPLICATE_CANDIDATE


class RateLimited(DomainError):
    code = ErrorCode.RATE_LIMITED
    retryable = True


class QuotaExceeded(DomainError):
    code = ErrorCode.QUOTA_EXCEEDED


class ProviderUnavailable(DomainError):
    code = ErrorCode.PROVIDER_UNAVAILABLE
    retryable = True


class PermissionDenied(DomainError):
    code = ErrorCode.PERMISSION_DENIED


class NotFound(DomainError):
    """Неизвестный или недоступный объект.

    Намеренно не различает «нет объекта» и «нет доступа» для чужих ID (A106).
    """

    code = ErrorCode.NOT_FOUND


class IdempotencyConflict(DomainError):
    code = ErrorCode.IDEMPOTENCY_CONFLICT


class Unauthenticated(DomainError):
    code = ErrorCode.UNAUTHENTICATED


class TemporarilyUnavailable(DomainError):
    code = ErrorCode.TEMPORARILY_UNAVAILABLE
    retryable = True


class ImportReconciliationFailed(DomainError):
    code = ErrorCode.IMPORT_RECONCILIATION_FAILED


class ConflictError(DomainError):
    code = ErrorCode.CONFLICT
