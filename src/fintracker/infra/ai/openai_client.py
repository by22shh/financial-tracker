"""Адаптер OpenAI Responses API по профилю ADR-17.

Model ID gpt-5.6-luna, reasoning.effort=medium, service_tier=default, прямой
вызов https://api.openai.com/v1/responses. Автоматический переход на другую
модель, effort, провайдера или режим Flex/Fast запрещён этим профилем.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from fintracker.config import AI_PROFILE_VERSION, AISettings
from fintracker.core.errors import ProviderUnavailable, ValidationFailed
from fintracker.core.logging import get_logger
from fintracker.infra.ai.schemas import json_schema_for

logger = get_logger("ai.openai")

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)

# Ограниченная повторная попытка исправления структуры (AI-03).
MAX_SCHEMA_RETRIES = 1


@dataclass(frozen=True, slots=True)
class AIUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            # Reasoning tokens входят в оплачиваемый output и не прибавляются
            # второй раз к output_tokens (раздел 26 ТЗ).
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True, slots=True)
class AIResult:
    parsed: BaseModel
    raw_text: str
    usage: AIUsage
    cost: Decimal
    cost_currency: str
    requested_model: str
    returned_model: str | None
    reasoning_effort: str
    service_tier: str
    profile_version: str
    prompt_version: str
    schema_version: str
    provider_request_id: str | None
    duration_ms: int
    retries: int


class AIProvider(Protocol):
    async def structured(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        response_model: type[BaseModel],
        prompt_version: str,
        schema_name: str,
        max_output_tokens: int | None = None,
    ) -> AIResult: ...


def estimate_cost(settings: AISettings, usage: AIUsage) -> Decimal:
    """Стоимость по актуальному тарифу: вход, кеш и выход (раздел 26 ТЗ)."""
    million = Decimal(1_000_000)
    plain_input = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (
        Decimal(plain_input) * settings.price_input_per_mtok / million
        + Decimal(usage.cached_input_tokens) * settings.price_cached_input_per_mtok / million
        + Decimal(usage.output_tokens) * settings.price_output_per_mtok / million
    ).quantize(Decimal("0.00000001"))


def upper_bound_cost(settings: AISettings, *, input_tokens: int, max_output: int) -> Decimal:
    """Верхняя оценка стоимости до запроса — основа резервирования (ADR-12)."""
    return estimate_cost(settings, AIUsage(input_tokens=input_tokens, output_tokens=max_output))


class OpenAIResponsesProvider:
    """Реальный клиент Responses API."""

    def __init__(self, settings: AISettings) -> None:
        self._settings = settings

    def _profile_payload(self) -> dict[str, Any]:
        # Обязательные параметры профиля ADR-17.
        return {
            "model": self._settings.model,
            "reasoning": {"effort": self._settings.reasoning_effort},
            "service_tier": self._settings.service_tier,
        }

    async def structured(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        response_model: type[BaseModel],
        prompt_version: str,
        schema_name: str,
        max_output_tokens: int | None = None,
    ) -> AIResult:
        payload: dict[str, Any] = {
            **self._profile_payload(),
            "instructions": instructions,
            "input": input_items,
            "max_output_tokens": max_output_tokens or self._settings.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": json_schema_for(response_model),
                }
            },
        }
        started = dt.datetime.now(dt.UTC)
        headers = {
            "Authorization": f"Bearer {self._settings.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        last_error: str | None = None
        for attempt in range(MAX_SCHEMA_RETRIES + 1):
            body = dict(payload)
            if attempt and last_error:
                body["input"] = [
                    *input_items,
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Предыдущий ответ не прошёл проверку схемы: "
                                    f"{last_error}. Верни корректный JSON по схеме."
                                ),
                            }
                        ],
                    },
                ]
            try:
                async with httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds
                ) as client:
                    response = await client.post(
                        f"{self._settings.base_url}/responses", json=body, headers=headers
                    )
            except httpx.TimeoutException as exc:
                # Сетевой timeout не доказывает, что провайдер прекратил
                # обработку и не выставит счёт (ADR-12).
                raise ProviderUnavailable("Тайм-аут обращения к модели") from exc
            except httpx.TransportError as exc:
                raise ProviderUnavailable("Модель временно недоступна") from exc

            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                raise ProviderUnavailable(
                    "Достигнут предел частоты обращений к модели",
                    details={"retry_after": retry_after},
                )
            if response.status_code >= 500:
                raise ProviderUnavailable(f"Ошибка провайдера {response.status_code}")
            if response.status_code >= 400:
                # Постоянная ошибка не повторяется автоматически (TECH-06).
                raise ValidationFailed(f"Провайдер отклонил запрос: {response.status_code}")

            data = response.json()
            text = _extract_output_text(data)
            usage = _extract_usage(data)
            if text is None:
                last_error = "пустой ответ"
                continue
            try:
                parsed = response_model.model_validate_json(text)
            except ValidationError as exc:
                last_error = exc.errors()[0]["msg"] if exc.errors() else "схема не совпала"
                logger.info("ai_schema_invalid", attempt=attempt, schema=schema_name)
                continue

            duration_ms = int((dt.datetime.now(dt.UTC) - started).total_seconds() * 1000)
            return AIResult(
                parsed=parsed,
                raw_text=text,
                usage=usage,
                cost=estimate_cost(self._settings, usage),
                cost_currency=self._settings.cost_currency,
                requested_model=self._settings.model,
                returned_model=data.get("model"),
                reasoning_effort=self._settings.reasoning_effort,
                service_tier=data.get("service_tier") or self._settings.service_tier,
                profile_version=AI_PROFILE_VERSION,
                prompt_version=prompt_version,
                schema_version=schema_name,
                provider_request_id=data.get("id"),
                duration_ms=duration_ms,
                retries=attempt,
            )
        raise ValidationFailed(f"Модель не вернула корректный результат по схеме: {last_error}")


def _extract_output_text(data: dict[str, Any]) -> str | None:
    """Достать текстовый результат из ответа Responses API."""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                value = part.get("text")
                if isinstance(value, str):
                    chunks.append(value)
    joined = "".join(chunks).strip()
    return joined or None


def _extract_usage(data: dict[str, Any]) -> AIUsage:
    usage = data.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return AIUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_input_tokens=int(details.get("cached_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_tokens=int(output_details.get("reasoning_tokens") or 0),
    )


@dataclass
class ScriptedAIProvider:
    """Контролируемый провайдер для контрактных проверок.

    Детерминированная заглушка не доказывает качество распознавания (раздел 1
    ACCEPTANCE): она проверяет только контракт вызывающего кода.
    """

    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: Exception | None = None
    usage: AIUsage = field(default_factory=lambda: AIUsage(1200, 0, 300, 120))

    async def structured(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        response_model: type[BaseModel],
        prompt_version: str,
        schema_name: str,
        max_output_tokens: int | None = None,
    ) -> AIResult:
        self.calls.append(
            {
                "instructions": instructions,
                "input": input_items,
                "schema": schema_name,
                "prompt_version": prompt_version,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with
        if not self.responses:
            raise ProviderUnavailable("Нет подготовленного ответа")
        raw = self.responses.pop(0)
        retries = 0
        while True:
            try:
                parsed = response_model.model_validate_json(raw)
                break
            except ValidationError as exc:
                retries += 1
                if retries > MAX_SCHEMA_RETRIES or not self.responses:
                    raise ValidationFailed("Схема ответа не совпала") from exc
                raw = self.responses.pop(0)
        return AIResult(
            parsed=parsed,
            raw_text=raw,
            usage=self.usage,
            cost=Decimal("0.001"),
            cost_currency="USD",
            requested_model="gpt-5.6-luna",
            returned_model="gpt-5.6-luna",
            reasoning_effort="medium",
            service_tier="default",
            profile_version=AI_PROFILE_VERSION,
            prompt_version=prompt_version,
            schema_version=schema_name,
            provider_request_id="scripted",
            duration_ms=5,
            retries=retries,
        )


_OVERRIDE: AIProvider | None = None


def set_provider_override(provider: AIProvider | None) -> None:
    global _OVERRIDE
    _OVERRIDE = provider


def build_provider(settings: AISettings) -> AIProvider:
    """Собрать адаптер. Недоступность ключа — явная ошибка, не подмена модели."""
    if _OVERRIDE is not None:
        return _OVERRIDE
    if not settings.enabled:
        raise ProviderUnavailable(
            "AI не включён: задайте FINTRACKER_AI__ENABLED=true и ключ OpenAI (BL-01)"
        )
    return OpenAIResponsesProvider(settings)


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
