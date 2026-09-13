"""Голос и чеки через диалог (A18, A19, A20, A28, A31, A182, A183)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from fintracker.infra.asr.provider import ScriptedAsrProvider, set_asr_override
from tests.acceptance.conftest import BotUser, make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


@pytest.fixture
def ai_enabled(monkeypatch: pytest.MonkeyPatch, test_settings: Settings):
    """Настройки с включённым AI и ASR для контрактных проверок."""
    import os

    from fintracker.config import Settings as SettingsClass
    from fintracker.config import reset_settings_cache

    env = {
        "FINTRACKER_AI__ENABLED": "true",
        "FINTRACKER_AI__API_KEY": "test-key",
        "FINTRACKER_ASR__PROVIDER": "stub",
        "FINTRACKER_ASR__MODEL": "stub-asr",
        "FINTRACKER_ASR__API_KEY": "test-key",
    }
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    reset_settings_cache()
    yield SettingsClass()
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_cache()


async def _post(user: BotUser, text: str) -> None:
    await user.send(text)
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))


async def test_a20_audio_without_speech_keeps_problem_state(
    bot: None, ai_enabled: Settings, stub_download
) -> None:
    """A20: запись без речи сохраняет состояние проблемы и предлагает ввод текстом."""
    user = make_user(ai_enabled, 915001)
    await create_budget(user)

    provider = ScriptedAsrProvider(transcripts=[""], speech_detected=False)
    set_asr_override(provider)
    try:
        await user.send_voice(duration_seconds=6)
    finally:
        set_asr_override(None)
    text = user.text()
    assert "распозн" in text.lower() or "речь" in text.lower()
    assert user.has_button("Ручной ввод")

    # Запись не считается нулевым расходом: журнал пуст.
    await user.send("/history")
    assert (
        "нет записанных операций" in user.text().lower() or "Подходящих записей нет" in user.text()
    )


async def test_voice_without_configured_asr_keeps_material(
    bot: None, test_settings: Settings
) -> None:
    """FR-13, BL-02: без выбранной модели ASR голос сохраняется, а не теряется."""
    user = make_user(test_settings, 915002)
    await create_budget(user)
    await user.send_voice(duration_seconds=5)
    text = user.text()
    assert "не подключено" in text or "не выбрана модель" in text
    assert user.has_button("Ручной ввод")


async def test_a21_long_audio_is_refused_before_paid_call(bot: None, ai_enabled: Settings) -> None:
    """A21, NFR-11: длинная запись отклоняется до обращения к платному сервису."""
    provider = ScriptedAsrProvider(transcripts=["не должно вызваться"])
    set_asr_override(provider)
    try:
        user = make_user(ai_enabled, 915003)
        await create_budget(user)
        await user.send_voice(duration_seconds=ai_enabled.asr.max_audio_seconds + 10)
    finally:
        set_asr_override(None)
    assert "не обрабатывается" in user.text()
    assert provider.calls == 0, "платный вызов не выполнялся"


async def test_a31_photo_without_ai_key_keeps_draft(bot: None, test_settings: Settings) -> None:
    """A31, FR-18: без ключа AI фото сохраняется черновиком, ссылки не открываются."""
    user = make_user(test_settings, 915004)
    await create_budget(user)
    await user.send_photo(caption="Чек с QR")
    text = user.text()
    assert "черновик" in text.lower()
    assert user.has_button("Ручной ввод")
    # Внешняя ссылка из QR не открывается и не превращается в действие.
    assert "http" not in text.lower()


@pytest.fixture
def stub_download(monkeypatch: pytest.MonkeyPatch):
    """Загрузка файла Telegram подменяется: токен не нужен (BL-03)."""

    async def _download(settings, *, file_id: str) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + file_id.encode()

    monkeypatch.setattr(
        "fintracker.application.intelligence.media_pipeline.download_attachment", _download
    )
    return _download


def _voice_extraction(**candidate) -> str:
    import json

    base = {
        "candidate_key": "c1",
        "kind": "expense",
        "amount_decimal": "1500.00",
        "currency": "RUB",
        "currency_origin": "workspace_default",
        "date_expression": "вчера",
        "description": "Такси",
        "merchant": None,
        "category_id": None,
        "beneficiary_id": None,
        "spender_person_id": None,
        "account_id": None,
        "note": None,
        "evidence": {"amount": "1500", "date": "вчера"},
        "ambiguities": [],
    }
    base.update(candidate)
    return json.dumps(
        {
            "schema_version": "1.0",
            "intent": "record_transaction",
            "candidates": [base],
            "question": None,
        },
        ensure_ascii=False,
    )


async def test_a18_clear_voice_gives_correct_card(
    bot: None, ai_enabled: Settings, stub_download
) -> None:
    """A18: «вчера 1500 за такси Софе» — верная дата и сумма, расшифровка видна."""
    from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override

    user = make_user(ai_enabled, 915010)
    await create_budget(user, categories="Продукты, Транспорт")

    set_asr_override(ScriptedAsrProvider(transcripts=["вчера 1500 за такси Софе"]))
    set_provider_override(ScriptedAIProvider(responses=[_voice_extraction()]))
    try:
        await user.send_voice(duration_seconds=7)
    finally:
        set_asr_override(None)
        set_provider_override(None)

    text = user.text().replace(" ", " ").replace(" ", " ")
    assert "вчера 1500 за такси Софе" in text, "расшифровка показана для исправления"
    assert "1 500" in text
    assert "11 сентября" in text or "2026-09-11" in text
    assert user.has_button("Записать")


async def test_a19_ambiguous_voice_amount_asks_once(
    bot: None, ai_enabled: Settings, stub_download
) -> None:
    """A19: неоднозначная сумма даёт один короткий вопрос без автозаписи."""
    from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override

    user = make_user(ai_enabled, 915011)
    await create_budget(user)

    response = _voice_extraction(
        ambiguities=[{"field": "amount", "reason": "ambiguous", "options": ["1500", "15000"]}]
    )
    set_asr_override(ScriptedAsrProvider(transcripts=["тысяча пятьсот за такси"]))
    set_provider_override(ScriptedAIProvider(responses=[response]))
    try:
        await user.send_voice(duration_seconds=5)
    finally:
        set_asr_override(None)
        set_provider_override(None)

    text = user.text()
    assert "1500" in text and "15000" in text, "предложены числовые варианты"
    assert text.count("?") <= 2, "один короткий вопрос"

    await user.send("/history")
    assert (
        "нет записанных операций" in user.text().lower() or "Подходящих записей нет" in user.text()
    )


async def test_a182_voice_note_is_kept_for_correction(
    bot: None, ai_enabled: Settings, stub_download
) -> None:
    """A182: пояснение из голоса сохраняется в комментарии и доступно для правки."""
    from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override

    user = make_user(ai_enabled, 915012)
    await create_budget(user)

    response = _voice_extraction(note="в следующий раз выбрать дешевле")
    set_asr_override(
        ScriptedAsrProvider(transcripts=["такси 1500, в следующий раз выбрать дешевле"])
    )
    set_provider_override(ScriptedAIProvider(responses=[response]))
    try:
        await user.send_voice(duration_seconds=8)
    finally:
        set_asr_override(None)
        set_provider_override(None)

    assert "в следующий раз выбрать дешевле" in user.text()
    await user.press(user.button_data("Записать"))
    await user.send("/history дешевле")
    assert "из 1" in user.text()


def _receipt_json(lines: list[dict[str, object]], total: str = "1200.00") -> str:
    import json

    return json.dumps(
        {
            "schema_version": "1.0",
            "document_kind": "receipt",
            "payment_confirmed": True,
            "merchant": "Магазин",
            "date_expression": "сегодня",
            "currency": "RUB",
            "total_decimal": total,
            "lines": lines,
            "unreadable_lines": 0,
        },
        ensure_ascii=False,
    )


async def test_a28_album_of_one_receipt_is_single_package(
    bot: None, ai_enabled: Settings, stub_download
) -> None:
    """A28: несколько фото одного чека образуют один пакет без повторов строк."""
    from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override

    user = make_user(ai_enabled, 915020)
    await create_budget(user, categories="Продукты, Рестораны")

    lines = [
        {"label": "Молоко", "amount_decimal": "200.00", "quantity": "1"},
        {"label": "Хлеб", "amount_decimal": "1000.00", "quantity": "1"},
    ]
    provider = ScriptedAIProvider(responses=[_receipt_json(lines)])
    set_provider_override(provider)
    try:
        await user.send_photo(count=3, media_group_id="album-1")
    finally:
        set_provider_override(None)

    assert len(provider.calls) == 1, "альбом разобран одним вызовом"
    images = [
        item
        for item in provider.calls[0]["input"][0]["content"]
        if item.get("type") == "input_image"
    ]
    assert len(images) == 3, "все снимки переданы в одном пакете"
    text = user.text().replace(" ", " ").replace(" ", " ")
    assert "1 200" in text
    assert text.count("Молоко") == 1, "строка на перекрытии не удвоена"


async def test_a183_receipt_caption_is_linked_to_operation(
    bot: None, ai_enabled: Settings, stub_download
) -> None:
    """A183: подпись к чеку связана с этой операцией, сумма взята из чека."""
    from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override

    user = make_user(ai_enabled, 915021)
    await create_budget(user, categories="Продукты, Рестораны")

    lines = [{"label": "Продукты", "amount_decimal": "1200.00", "quantity": "1"}]
    set_provider_override(ScriptedAIProvider(responses=[_receipt_json(lines)]))
    try:
        await user.send_photo(caption="Купила Софа перед поездкой")
    finally:
        set_provider_override(None)

    text = user.text().replace(" ", " ").replace(" ", " ")
    assert "1 200" in text, "сумма взята из проверенного чека"
    assert "Софа" in text or "поездкой" in text, "подпись связана с этой операцией"
