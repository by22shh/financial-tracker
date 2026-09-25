"""Отдельный адаптер транскрипции аудио (ADR-17, OPEN-02 / BL-02).

Luna принимает текст и изображения, но не аудио напрямую. Конкретная ASR
модель остаётся отдельным решением; здесь зафиксирован контракт, ограничения
и раздельные метрики, чтобы качество и ошибки ASR оценивались отдельно от
извлечения и категоризации.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import httpx

from fintracker.config import ASRSettings
from fintracker.core.errors import ProviderUnavailable, ValidationFailed
from fintracker.core.logging import get_logger

logger = get_logger("asr")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    language: str | None
    duration_seconds: float
    segments: tuple[TranscriptSegment, ...]
    provider: str
    model: str
    cost: Decimal
    cost_currency: str
    duration_ms: int
    # Отсутствие речи — отдельное состояние, а не нулевой расход (FR-13).
    speech_detected: bool = True


class AsrProvider(Protocol):
    async def transcribe(
        self, *, audio: bytes, mime_type: str, duration_seconds: float, language: str = "ru"
    ) -> TranscriptResult: ...


class OpenAIAsrProvider:
    """Транскрипция через OpenAI. Модель задаётся конфигурацией (BL-02)."""

    def __init__(self, settings: ASRSettings) -> None:
        if not settings.model:
            raise ProviderUnavailable("Модель ASR не выбрана (BL-02)")
        self._settings = settings

    async def transcribe(
        self, *, audio: bytes, mime_type: str, duration_seconds: float, language: str = "ru"
    ) -> TranscriptResult:
        if duration_seconds > self._settings.max_audio_seconds:
            raise ValidationFailed(
                f"Запись длиннее {self._settings.max_audio_seconds} секунд не обрабатывается"
            )
        started = dt.datetime.now(dt.UTC)
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
        segments = tuple(
            TranscriptSegment(
                start_seconds=float(item.get("start", 0.0)),
                end_seconds=float(item.get("end", 0.0)),
                text=str(item.get("text", "")).strip(),
            )
            for item in body.get("segments", []) or []
        )
        minutes = Decimal(str(duration_seconds)) / Decimal(60)
        return TranscriptResult(
            text=text,
            language=body.get("language"),
            duration_seconds=duration_seconds,
            segments=segments,
            provider="openai",
            model=self._settings.model,
            cost=(minutes * self._settings.price_per_minute).quantize(Decimal("0.00000001")),
            cost_currency="USD",
            duration_ms=int((dt.datetime.now(dt.UTC) - started).total_seconds() * 1000),
            speech_detected=bool(text),
        )


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
        return TranscriptResult(
            text=text,
            language=language,
            duration_seconds=duration_seconds,
            segments=(),
            provider="stub",
            model="stub",
            cost=Decimal("0"),
            cost_currency="USD",
            duration_ms=1,
            speech_detected=bool(text) and self.speech_detected,
        )


_OVERRIDE: AsrProvider | None = None


def set_asr_override(provider: AsrProvider | None) -> None:
    global _OVERRIDE
    _OVERRIDE = provider


def build_asr(settings: ASRSettings) -> AsrProvider:
    if _OVERRIDE is not None:
        return _OVERRIDE
    if settings.provider == "openai":
        return OpenAIAsrProvider(settings)
    if settings.provider == "stub":
        return ScriptedAsrProvider()
    raise ProviderUnavailable(
        "Модель транскрипции не выбрана: задайте FINTRACKER_ASR__PROVIDER и модель (BL-02)"
    )
