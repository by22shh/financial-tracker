"""OpenAI Responses API с проверкой структурированного ответа."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, get_args, get_origin

import httpx
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from fintracker.config import AISettings
from fintracker.core.errors import ProviderUnavailable, ValidationFailed
from fintracker.infra.ai.schemas import json_schema_for

logger = logging.getLogger(__name__)

# Ограниченная повторная попытка исправления структуры.
MAX_SCHEMA_RETRIES = 1


@dataclass(frozen=True, slots=True)
class AIResult:
    parsed: BaseModel


class AIProvider(Protocol):
    async def structured(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        response_model: type[BaseModel],
        schema_name: str,
        max_output_tokens: int | None = None,
    ) -> AIResult: ...


class OpenAIResponsesProvider:
    """Реальный клиент Responses API."""

    def __init__(self, settings: AISettings) -> None:
        self._settings = settings

    def _profile_payload(self) -> dict[str, Any]:
        # Обязательные параметры профиля AI.
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
                # обработку и не выставит счёт.
                raise ProviderUnavailable("Тайм-аут обращения к модели") from exc
            except httpx.TransportError as exc:
                raise ProviderUnavailable("Модель временно недоступна") from exc

            if response.status_code == 429:
                raise ProviderUnavailable("Достигнут предел частоты обращений к модели")
            if response.status_code >= 500:
                raise ProviderUnavailable(f"Ошибка провайдера {response.status_code}")
            if response.status_code >= 400:
                # Постоянная ошибка не повторяется автоматически.
                raise ValidationFailed(f"Провайдер отклонил запрос: {response.status_code}")

            data = response.json()
            text = _extract_output_text(data)
            if text is None:
                last_error = "пустой ответ"
                continue
            try:
                parsed = response_model.model_validate(
                    _normalize_null_defaults(response_model, text)
                )
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = (
                    exc.errors()[0]["msg"]
                    if isinstance(exc, ValidationError) and exc.errors()
                    else "схема не совпала"
                )
                logger.info("ai_schema_invalid attempt=%s schema=%s", attempt, schema_name)
                continue

            return AIResult(parsed=parsed)
        raise ValidationFailed(f"Модель не вернула корректный результат по схеме: {last_error}")


def _normalize_null_defaults(model: type[BaseModel], text: str) -> Any:
    """Map provider-permitted null defaults to Pydantic defaults recursively."""
    return _normalize_model(model, json.loads(text))


def _model_type(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for item in get_args(annotation):
        nested = _model_type(item)
        if nested is not None:
            return nested
    return None


def _normalize_value(annotation: Any, value: Any) -> Any:
    nested = _model_type(annotation)
    if nested is not None and isinstance(value, dict):
        return _normalize_model(nested, value)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (list, tuple) and args and isinstance(value, list):
        return [_normalize_value(args[0], item) for item in value]
    return value


def _normalize_model(model: type[BaseModel], payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    for name, model_field in model.model_fields.items():
        value = normalized.get(name)
        if value is None:
            if model_field.default is not PydanticUndefined:
                normalized[name] = model_field.default
                continue
            if model_field.default_factory is not None:
                normalized[name] = model_field.get_default(call_default_factory=True)
                continue
        normalized[name] = _normalize_value(model_field.annotation, value)
    return normalized


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


@dataclass
class ScriptedAIProvider:
    """Контролируемый провайдер для контрактных проверок.

    Заглушка проверяет контракт вызывающего кода без сетевых запросов.
    """

    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: Exception | None = None

    async def structured(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        response_model: type[BaseModel],
        schema_name: str,
        max_output_tokens: int | None = None,
    ) -> AIResult:
        self.calls.append(
            {
                "instructions": instructions,
                "input": input_items,
                "schema": schema_name,
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
        return AIResult(parsed=parsed)


def build_provider(settings: AISettings) -> AIProvider:
    """Собрать адаптер. Недоступность ключа — явная ошибка, не подмена модели."""
    if not settings.enabled:
        raise ProviderUnavailable(
            "AI не включён: задайте FINTRACKER_AI__ENABLED=true и ключ OpenAI"
        )
    return OpenAIResponsesProvider(settings)
