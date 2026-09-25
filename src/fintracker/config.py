"""Shared Telegram and AI provider settings."""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

REQUIRED_AI_MODEL = "gpt-5.6-luna"
REQUIRED_AI_EFFORT = "medium"
REQUIRED_AI_SERVICE_TIER = "default"
REQUIRED_AI_BASE_URL = "https://api.openai.com/v1"


class TelegramSettings(BaseSettings):
    bot_token: SecretStr = SecretStr("")
    creation_allowlist: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.bot_token.get_secret_value())

    @property
    def allowlist_ids(self) -> frozenset[int]:
        raw = self.creation_allowlist.replace(";", ",").split(",")
        return frozenset(int(item) for item in (part.strip() for part in raw) if item.isdigit())


class AISettings(BaseSettings):
    """Профиль основной модели распознавания расходов."""

    enabled: bool = False
    base_url: str = REQUIRED_AI_BASE_URL
    api_key: SecretStr = SecretStr("")
    model: str = REQUIRED_AI_MODEL
    reasoning_effort: str = REQUIRED_AI_EFFORT
    service_tier: str = REQUIRED_AI_SERVICE_TIER
    request_timeout_seconds: float = 90.0
    max_output_tokens: int = 4096

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        if value != REQUIRED_AI_MODEL:
            raise ValueError(f"Профиль AI требует model={REQUIRED_AI_MODEL}")
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def _check_effort(cls, value: str) -> str:
        if value != REQUIRED_AI_EFFORT:
            raise ValueError(f"Профиль AI требует reasoning.effort={REQUIRED_AI_EFFORT}")
        return value

    @field_validator("service_tier")
    @classmethod
    def _check_tier(cls, value: str) -> str:
        if value != REQUIRED_AI_SERVICE_TIER:
            raise ValueError(f"Профиль AI требует service_tier={REQUIRED_AI_SERVICE_TIER}")
        return value

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if normalized != REQUIRED_AI_BASE_URL:
            raise ValueError(
                "Профиль AI требует прямого OpenAI API; OpenRouter и прокси не разрешены"
            )
        return normalized

    @model_validator(mode="after")
    def _check_key(self) -> AISettings:
        if self.enabled and not self.api_key.get_secret_value():
            raise ValueError("AI включён, но OPENAI API ключ не задан")
        return self


class ASRSettings(BaseSettings):
    """Настройки транскрипции голосовых сообщений."""

    provider: Literal["none", "openai", "stub"] = "none"
    model: str = ""
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.openai.com/v1"
    max_audio_seconds: int = 180
    request_timeout_seconds: float = 90.0

    @model_validator(mode="after")
    def _check(self) -> ASRSettings:
        if self.provider == "openai":
            if not self.model:
                raise ValueError("Для provider=openai нужно указать конкретную модель ASR")
            if not self.api_key.get_secret_value():
                raise ValueError("Для provider=openai нужен ключ ASR")
        return self
