"""Конфигурация приложения без секретов в коде (ADR-13, OPS-03).

Значения читаются из переменных окружения с префиксом ``FINTRACKER_``.
Профиль AI фиксирован ADR-17 и валидируется: скрытая подмена модели,
effort, провайдера или тарифного режима невозможна.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Зафиксированный профиль AI приложения (ADR-17) -------------------------
AI_PROFILE_VERSION = "ai-profile-1"
REQUIRED_AI_MODEL = "gpt-5.6-luna"
REQUIRED_AI_EFFORT = "medium"
REQUIRED_AI_SERVICE_TIER = "default"
REQUIRED_AI_BASE_URL = "https://api.openai.com/v1"


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class DatabaseSettings(BaseSettings):
    """DSN раздельных ролей: владелец схемы отделён от runtime ролей (SEC-02)."""

    owner_dsn: str = "postgresql+psycopg://fintracker_owner:devpassword@localhost:55432/fintracker"
    api_dsn: str = "postgresql+psycopg://fintracker_api:devpassword@localhost:55432/fintracker"
    worker_dsn: str = (
        "postgresql+psycopg://fintracker_worker:devpassword@localhost:55432/fintracker"
    )
    api_pool_size: int = 5
    api_max_overflow: int = 2
    worker_pool_size: int = 5
    worker_max_overflow: int = 2
    scheduler_pool_size: int = 2
    scheduler_max_overflow: int = 0
    echo_sql: bool = False

    @property
    def total_runtime_connections(self) -> int:
        return (
            self.api_pool_size
            + self.api_max_overflow
            + self.worker_pool_size
            + self.worker_max_overflow
            + self.scheduler_pool_size
            + self.scheduler_max_overflow
        )


class TelegramSettings(BaseSettings):
    bot_token: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    webhook_base_url: str = ""
    bot_id: int = 0
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

    def may_create_workspace(self, telegram_user_id: int) -> bool:
        if self.creation_mode == "open":
            return True
        return telegram_user_id in self.allowlist_ids


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
    price_input_per_mtok: Decimal = Decimal("1.25")
    price_cached_input_per_mtok: Decimal = Decimal("0.125")
    price_output_per_mtok: Decimal = Decimal("10.00")

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


class StorageSettings(BaseSettings):
    backend: Literal["filesystem", "s3"] = "filesystem"
    root: Path = Path("./var/objects")
    s3_endpoint: str = ""
    s3_bucket: str = "fintracker"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")


class SecurityLogSettings(BaseSettings):
    """Независимый журнал изменений доступа (ADR-14, SEC-10)."""

    backend: Literal["filesystem", "s3"] = "filesystem"
    root: Path = Path("./var/security-log")
    s3_endpoint: str = ""
    s3_bucket: str = "fintracker-security-log"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")


class SecretsSettings(BaseSettings):
    invite_hmac_key: SecretStr = SecretStr("change-me-invite-key")
    invite_hmac_key_version: int = 1
    cursor_hmac_key: SecretStr = SecretStr("change-me-cursor-key")


class LimitsSettings(BaseSettings):
    lock_timeout_ms: int = 2000
    statement_timeout_ms: int = 5000
    report_statement_timeout_ms: int = 10000
    batch_statement_timeout_ms: int = 60000
    idle_in_transaction_timeout_ms: int = 10000
    max_attachment_bytes: int = 15 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_image_side: int = 16_384
    max_album_files: int = 10
    max_album_bytes: int = 60 * 1024 * 1024
    max_import_rows: int = 5000
    max_merge_transactions: int = 5000
    max_note_chars: int = 2000
    draft_ttl_days: int = 7
    invite_default_ttl_days: int = 7
    invite_default_max_uses: int = 10
    invite_attempts_per_window: int = 5
    invite_attempt_window_minutes: int = 15
    proactive_messages_per_day: int = 2
    quiet_hours_start: int = 22
    quiet_hours_end: int = 9
    job_lease_seconds: int = 120
    job_lease_renew_seconds: int = 30
    job_max_attempts: int = 6
    interactive_parse_deadline_minutes: int = 10
    delivery_max_age_hours: int = 24


class ObservabilitySettings(BaseSettings):
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINTRACKER_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.DEV
    app_name: str = "fintracker"
    db: Annotated[DatabaseSettings, Field(default_factory=DatabaseSettings)]
    telegram: Annotated[TelegramSettings, Field(default_factory=TelegramSettings)]
    ai: Annotated[AISettings, Field(default_factory=AISettings)]
    asr: Annotated[ASRSettings, Field(default_factory=ASRSettings)]
    storage: Annotated[StorageSettings, Field(default_factory=StorageSettings)]
    security_log: Annotated[SecurityLogSettings, Field(default_factory=SecurityLogSettings)]
    secrets: Annotated[SecretsSettings, Field(default_factory=SecretsSettings)]
    limits: Annotated[LimitsSettings, Field(default_factory=LimitsSettings)]
    observability: Annotated[ObservabilitySettings, Field(default_factory=ObservabilitySettings)]

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        if self.env is Environment.PROD:
            weak = {"change-me-invite-key", "change-me-cursor-key", ""}
            if self.secrets.invite_hmac_key.get_secret_value() in weak:
                raise ValueError("В prod нужен настоящий INVITE_HMAC_KEY")
            if self.secrets.cursor_hmac_key.get_secret_value() in weak:
                raise ValueError("В prod нужен настоящий CURSOR_HMAC_KEY")
            if not self.telegram.configured:
                raise ValueError("В prod нужен TELEGRAM_BOT_TOKEN")
            if not self.telegram.webhook_secret.get_secret_value():
                raise ValueError("В prod нужен секрет webhook, отдельный от токена бота")
        # ADR-12: суммарный бюджет runtime соединений не более 30.
        if self.db.total_runtime_connections > 30:
            raise ValueError(
                f"Суммарный предел runtime соединений {self.db.total_runtime_connections} > 30"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
