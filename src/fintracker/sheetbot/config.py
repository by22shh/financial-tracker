"""Configuration for the sheet-only bot; no budget database is required."""

from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fintracker.config import AISettings, ASRSettings, TelegramSettings


class SheetsSettings(BaseSettings):
    bridge_url: str = ""
    bridge_secret: SecretStr = SecretStr("")
    state_path: Path = Path("var/sheetbot.sqlite3")
    allowed_user_ids: str = ""
    timezone: str = "Asia/Novosibirsk"
    currency: str = "RUB"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("bridge_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        if value and not (
            value.startswith("https://script.google.com/macros/s/") and value.endswith("/exec")
        ):
            raise ValueError("Нужен URL опубликованного Google Apps Script, оканчивающийся /exec")
        return value

    @property
    def users(self) -> frozenset[int]:
        return frozenset(int(x.strip()) for x in self.allowed_user_ids.split(",") if x.strip())


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINTRACKER_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )
    telegram: Annotated[TelegramSettings, Field(default_factory=TelegramSettings)]
    ai: Annotated[AISettings, Field(default_factory=AISettings)]
    asr: Annotated[ASRSettings, Field(default_factory=ASRSettings)]
    sheets: Annotated[SheetsSettings, Field(default_factory=SheetsSettings)]

    @property
    def allowed_users(self) -> frozenset[int]:
        return self.sheets.users or self.telegram.allowlist_ids

    def missing(self) -> list[str]:
        result = []
        if not self.telegram.configured:
            result.append("FINTRACKER_TELEGRAM__BOT_TOKEN")
        if not self.allowed_users:
            result.append("FINTRACKER_SHEETS__ALLOWED_USER_IDS")
        if not self.sheets.bridge_url:
            result.append("FINTRACKER_SHEETS__BRIDGE_URL")
        if not self.sheets.bridge_secret.get_secret_value():
            result.append("FINTRACKER_SHEETS__BRIDGE_SECRET")
        if not self.ai.enabled:
            result.append("FINTRACKER_AI__ENABLED / API_KEY")
        if self.asr.provider != "openai":
            result.append("FINTRACKER_ASR__PROVIDER / MODEL / API_KEY")
        return result
