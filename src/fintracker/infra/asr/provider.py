"""Транскрипция голосовых сообщений через OpenAI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from fintracker.config import ASRSettings
from fintracker.core.errors import ProviderUnavailable, ValidationFailed


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    speech_detected: bool = True


class AsrProvider(Protocol):
    async def transcribe(
        self, *, audio: bytes, mime_type: str, duration_seconds: float, language: str = "ru"
    ) -> TranscriptResult: ...


class OpenAIAsrProvider:
    """Транскрипция через OpenAI. Модель задаётся конфигурацией."""

    def __init__(self, settings: ASRSettings) -> None:
        if not settings.model:
            raise ProviderUnavailable("Модель ASR не выбрана")
        self._settings = settings

    async def transcribe(
        self, *, audio: bytes, mime_type: str, duration_seconds: float, language: str = "ru"
    ) -> TranscriptResult:
        if duration_seconds > self._settings.max_audio_seconds:
            raise ValidationFailed(
                f"Запись длиннее {self._settings.max_audio_seconds} секунд не обрабатывается"
            )
        files = {"file": ("audio.ogg", audio, mime_type)}
        data = {
            "model": self._settings.model,
            "language": language,
            "response_format": "verbose_json" if self._settings.model == "whisper-1" else "json",
        }
        headers = {"Authorization": f"Bearer {self._settings.api_key.get_secret_value()}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
                response = await client.post(
                    f"{self._settings.base_url}/audio/transcriptions",
                    files=files,
                    data=data,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable("Тайм-аут транскрипции") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable("Сервис транскрипции недоступен") from exc

        if response.status_code == 429:
            raise ProviderUnavailable("Предел частоты обращений к сервису транскрипции")
        if response.status_code >= 400:
            raise ProviderUnavailable(f"Ошибка транскрипции {response.status_code}")

        body = response.json()
        text = str(body.get("text") or "").strip()
        return TranscriptResult(text=text, speech_detected=bool(text))


@dataclass
class ScriptedAsrProvider:
    """Контролируемый провайдер для контрактных проверок."""

    transcripts: list[str] = field(default_factory=list)
    calls: int = 0
    fail_with: Exception | None = None
    speech_detected: bool = True

    async def transcribe(
        self, *, audio: bytes, mime_type: str, duration_seconds: float, language: str = "ru"
    ) -> TranscriptResult:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        text = self.transcripts.pop(0) if self.transcripts else ""
        return TranscriptResult(text=text, speech_detected=bool(text) and self.speech_detected)


def build_asr(settings: ASRSettings) -> AsrProvider:
    if settings.provider == "openai":
        return OpenAIAsrProvider(settings)
    if settings.provider == "stub":
        return ScriptedAsrProvider()
    raise ProviderUnavailable(
        "Модель транскрипции не выбрана: задайте FINTRACKER_ASR__PROVIDER и модель"
    )
