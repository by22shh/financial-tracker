"""Shared Telegram and AI provider settings."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

AI_PROFILE_VERSION = "ai-profile-1"
REQUIRED_AI_MODEL = "gpt-5.6-luna"
REQUIRED_AI_EFFORT = "medium"
REQUIRED_AI_SERVICE_TIER = "default"
REQUIRED_AI_BASE_URL = "https://api.openai.com/v1"


class TelegramSettings(BaseSettings):
    bot_token: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    webhook_base_url: str = ""
    bot_id: int = 0
    # Имя бота без «@»: из него строится ссылка-приглашение t.me/<имя>?start=…
    bot_username: str = ""
    # Режим аудитории (OPEN-04 / BL-05). По умолчанию закрытый пилот.
    creation_mode: Literal["open", "allowlist"] = "allowlist"
    creation_allowlist: str = ""
    send_rate_per_chat_per_second: float = 1.0
    send_rate_global_per_second: float = 25.0

    @property
    def configured(self) -> bool:
        return bool(self.bot_token.get_secret_value())

    @property
    def allowlist_ids(self) -> frozenset[int]:
        raw = self.creation_allowlist.replace(";", ",").split(",")
        return frozenset(int(item) for item in (part.strip() for part in raw) if item.isdigit())


class AISettings(BaseSettings):
    """Профиль основной модели. Значения закреплены ADR-17."""

    enabled: bool = False
    base_url: str = REQUIRED_AI_BASE_URL
    api_key: SecretStr = SecretStr("")
    model: str = REQUIRED_AI_MODEL
    reasoning_effort: str = REQUIRED_AI_EFFORT
    service_tier: str = REQUIRED_AI_SERVICE_TIER
    request_timeout_seconds: float = 90.0
    max_output_tokens: int = 4096
    monthly_cost_limit: Decimal = Decimal("50.00")
    cost_currency: str = "USD"
    max_concurrent_interactive: int = 4
    max_concurrent_review: int = 1
    max_concurrent_per_workspace: int = 2
    price_input_per_mtok: Decimal = Decimal("0.20")
    price_cached_input_per_mtok: Decimal = Decimal("0.02")
    price_output_per_mtok: Decimal = Decimal("1.20")

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        if value != REQUIRED_AI_MODEL:
            raise ValueError(
                f"Профиль ADR-17 требует model={REQUIRED_AI_MODEL}; смена оформляется новым ADR"
            )
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def _check_effort(cls, value: str) -> str:
        if value != REQUIRED_AI_EFFORT:
            raise ValueError(f"Профиль ADR-17 требует reasoning.effort={REQUIRED_AI_EFFORT}")
        return value

    @field_validator("service_tier")
    @classmethod
    def _check_tier(cls, value: str) -> str:
        if value != REQUIRED_AI_SERVICE_TIER:
            raise ValueError(f"Профиль ADR-17 требует service_tier={REQUIRED_AI_SERVICE_TIER}")
        return value

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if normalized != REQUIRED_AI_BASE_URL:
            raise ValueError(
                "Профиль ADR-17 требует прямого OpenAI API; OpenRouter и прокси не разрешены"
            )
        return normalized

    @model_validator(mode="after")
    def _check_key(self) -> AISettings:
        if self.enabled and not self.api_key.get_secret_value():
            raise ValueError("AI включён, но OPENAI API ключ не задан (BL-01)")
        return self

    def request_profile(self) -> dict[str, object]:
        """Обязательные параметры запроса профиля (ADR-17)."""
        return {
            "model": self.model,
            "reasoning": {"effort": self.reasoning_effort},
            "service_tier": self.service_tier,
        }


class ASRSettings(BaseSettings):
    """Отдельная транскрипция аудио. Модель ещё не выбрана (OPEN-02 / BL-02)."""

    provider: Literal["none", "openai", "stub"] = "none"
    model: str = ""
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.openai.com/v1"
    max_audio_seconds: int = 180
    request_timeout_seconds: float = 90.0
    price_per_minute: Decimal = Decimal("0.006")

    @model_validator(mode="after")
    def _check(self) -> ASRSettings:
        if self.provider == "openai":
            if not self.model:
                raise ValueError("Для provider=openai нужно указать конкретную модель ASR (BL-02)")
            if not self.api_key.get_secret_value():
                raise ValueError("Для provider=openai нужен ключ ASR")
        return self

    @property
    def available(self) -> bool:
        return self.provider != "none"


class ObservabilitySettings(BaseSettings):
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
