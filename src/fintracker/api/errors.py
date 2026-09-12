"""Единый формат ошибки API (DATA_CONTRACT §4)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from fintracker.core.errors import DomainError
from fintracker.core.logging import get_logger

logger = get_logger("api.errors")


def correlation_id_of(request: Request) -> str:
    existing = request.headers.get("X-Correlation-Id")
    if existing and len(existing) <= 64:
        return existing
    return uuid.uuid4().hex


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    correlation_id = correlation_id_of(request)
    logger.info(
        "domain_error",
        code=exc.code.value,
        correlation_id=correlation_id,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=exc.to_payload(correlation_id),
        headers={"X-Correlation-Id": correlation_id},
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Ответ не раскрывает stack trace, SQL и чужие данные (DATA_CONTRACT §4)."""
    correlation_id = correlation_id_of(request)
    logger.error(
        "unexpected_error",
        error_type=type(exc).__name__,
        correlation_id=correlation_id,
        path=request.url.path,
    )
    payload: dict[str, Any] = {
        "code": "INTERNAL_ERROR",
        "message": "Внутренняя ошибка сервиса. Повторите попытку позже.",
        "correlation_id": correlation_id,
        "retryable": True,
    }
    return JSONResponse(
        status_code=500, content=payload, headers={"X-Correlation-Id": correlation_id}
    )
