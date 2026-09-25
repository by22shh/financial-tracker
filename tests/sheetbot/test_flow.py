import asyncio
import contextlib
import io
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update
from pydantic import SecretStr

from fintracker.config import ASRSettings, TelegramSettings
from fintracker.core.errors import ValidationFailed
from fintracker.infra.ai.openai_client import ScriptedAIProvider
from fintracker.infra.asr.provider import ScriptedAsrProvider
from fintracker.sheetbot.bridge import BridgeError, SheetsBridge
from fintracker.sheetbot.config import BotSettings, SheetsSettings
from fintracker.sheetbot.extraction import extract
from fintracker.sheetbot.models import Catalog, Category, Sheet
from fintracker.sheetbot.runtime import consume, receive
from fintracker.sheetbot.service import SheetBot
from fintracker.sheetbot.store import Store


@pytest.fixture
def catalog():
    return Catalog(
        id=10,
        title="10.09 - 09.10",
        revision="rev1",
        categories=[Category(id="food", label="Продукты / Супермаркеты")],
        dates=[date(2026, 9, 24), date(2026, 9, 25)],
    )


def result(amount=125050, category="food", when="2026-09-25"):
    return json.dumps(
        {
            "expenses": [
                {
                    "amount_minor": amount,
                    "category_id": category,
                    "date": when,
                    "description": "Продукты",
                }
            ],
            "clarification": None,
        }
    )


def update(number=1, text="продукты 1250,50", user=100):
    return {
        "update_id": number,
        "message": {
            "message_id": number,
            "from": {"id": user},
            "chat": {"id": user, "type": "private"},
            "date": 1790301600,
            "text": text,
        },
    }


@pytest.fixture
def setup(tmp_path, catalog):
    settings = BotSettings(
        _env_file=None,
        telegram=TelegramSettings(),
        sheets=SheetsSettings(allowed_user_ids="100,200", state_path=tmp_path / "state.sqlite"),
    )
    store = Store(settings.sheets.state_path)
    bridge = AsyncMock(spec=SheetsBridge)
    bridge.sheets.return_value = [
        Sheet(id=20, title="Предыдущий"),
        Sheet(id=10, title=catalog.title),
    ]

    async def latest():
        return await SheetsBridge.latest_catalog(bridge)

    bridge.latest_catalog.side_effect = latest
    bridge.catalog.return_value = catalog
    bridge.write.return_value = {"count": 1}
    ai = ScriptedAIProvider(responses=[result()])
    asr = ScriptedAsrProvider(transcripts=["продукты 1250,50"])
    bot = SimpleNamespace(
        id=123,
        get_file=AsyncMock(return_value=SimpleNamespace(file_path="voice/file.ogg", file_size=4)),
        download_file=AsyncMock(return_value=io.BytesIO(b"opus")),
    )
    service = SheetBot(settings, store, bridge, ai, asr, bot)
    yield service, store, bridge, ai, asr
    store.db.close()


async def dispatch(setup, item):
    service, store, *_ = setup
    store.enqueue(item)
    return await service.handle(item)


async def test_start_explains_automatic_destination(setup):
    reply = await dispatch(setup, update(text="/start"))
    assert "последний лист" in reply.text
    assert "/sheets" not in reply.text
    assert reply.model_dump().keys() == {"text", "parse_mode"}
    assert "бюджет" not in reply.text.lower()


async def test_real_telegram_updates_survive_inbox_and_receive_replies(setup):
    service, store, bridge, ai, asr = setup
    sender = {"id": 100, "is_bot": False, "first_name": "Test"}
    start = update(text="/start")
    start["message"]["from"] = sender
    selection = {
        "update_id": 2,
        "callback_query": {
            "id": "selection",
            "from": sender,
            "chat_instance": "test",
            "data": "sheet:10",
            "message": start["message"],
        },
    }
    text = update(number=3)
    text["message"]["from"] = sender
    voice = update(number=4)
    voice["message"]["from"] = sender
    del voice["message"]["text"]
    voice["message"]["voice"] = {
        "file_id": "voice",
        "file_unique_id": "unique",
        "duration": 4,
        "file_size": 4,
    }
    updates = [Update.model_validate(item) for item in [start, selection, text, voice]]
    ai.responses = [result(), result()]
    bot = service.bot
    bot.get_updates = AsyncMock(side_effect=[updates, asyncio.CancelledError()])
    bot.answer_callback_query = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await receive(bot, store)

    sent = []
    complete = asyncio.Event()

    async def send(chat_id, reply, **kwargs):
        sent.append((chat_id, reply, kwargs))
        if len(sent) == 4:
            complete.set()

    bot.send_message = AsyncMock(side_effect=send)
    worker = asyncio.create_task(consume(bot, store, service))
    try:
        await asyncio.wait_for(complete.wait(), timeout=2)
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    assert all(chat_id == 100 for chat_id, _, _ in sent)
    assert "последний лист" in sent[0][1]
    assert "последний лист" in sent[1][1]
    assert all("reply_markup" not in kwargs for _, _, kwargs in sent)
    assert all(kwargs["parse_mode"] == "HTML" for _, _, kwargs in sent)
    assert all("✅ <b>Записано</b>" in reply for _, reply, _ in sent[2:])
    assert bridge.write.await_count == 2
    assert asr.calls == 1
    assert store.next_event() is None
    bot.answer_callback_query.assert_awaited_once_with("selection")


async def test_old_buttons_and_sheets_command_cannot_choose_a_destination(setup):
    _, _, bridge, _, _ = setup
    for index, data in enumerate(["sheet:20", "budget:create"], 1):
        reply = await dispatch(
            setup,
            {
                "update_id": index,
                "callback_query": {
                    "from": {"id": 100},
                    "data": data,
                    "message": {"chat": {"id": 100, "type": "private"}},
                },
            },
        )
        assert "последний лист" in reply.text
        assert reply.model_dump().keys() == {"text", "parse_mode"}
    reply = await dispatch(setup, update(3, "/sheets"))
    assert "последний лист" in reply.text
    bridge.catalog.assert_not_awaited()
    bridge.write.assert_not_awaited()


async def test_saved_legacy_selection_is_ignored(setup):
    _, store, bridge, _, _ = setup
    store.db.execute("ALTER TABLE users ADD COLUMN sheet_id INTEGER")
    store.db.execute("INSERT INTO users(id,sheet_id) VALUES (100,20)")
    store.db.commit()
    await dispatch(setup, update())
    assert bridge.write.call_args.kwargs["catalog"].id == 10


async def test_new_last_sheet_is_used_for_next_expense(setup, catalog):
    _, _, bridge, ai, _ = setup
    ai.responses = [result(), result()]
    await dispatch(setup, update())
    bridge.sheets.return_value.append(Sheet(id=5, title="Новый период"))
    bridge.catalog.return_value = catalog.model_copy(update={"id": 5, "title": "Новый период"})
    await dispatch(setup, update(2))
    assert [call.kwargs["catalog"].id for call in bridge.write.call_args_list] == [10, 5]
    assert [call.args[0] for call in bridge.catalog.call_args_list] == [10, 5]


async def test_missing_last_sheet_never_writes(setup):
    _, _, bridge, ai, _ = setup
    bridge.sheets.return_value = []
    reply = await dispatch(setup, update())
    assert "нет доступных" in reply.text
    assert not ai.calls
    bridge.write.assert_not_awaited()


async def test_expense_is_written_without_confirmation(setup):
    _service, _store, bridge, ai, _ = setup
    reply = await dispatch(setup, update())
    assert "Записано" in reply.text
    assert "1 250,50 ₽" in reply.text
    bridge.write.assert_awaited_once()
    assert bridge.write.call_args.kwargs["key"] == "telegram:123:100:1"
    assert bridge.write.call_args.kwargs["catalog"].id == 10
    assert len(ai.calls) == 1


async def test_formatted_responses_escape_external_text_and_survive_retry(setup, catalog):
    service, store, _, ai, _ = setup
    catalog.title = "Мой <лист> & отчёт"
    catalog.categories[0].label = "Кофе <бар> & чай"
    item = update()
    reply = await dispatch(setup, item)
    assert reply.parse_mode == "HTML"
    assert "&lt;лист&gt; &amp; отчёт" in reply.text
    assert "&lt;бар&gt; &amp; чай" in reply.text
    assert await service.handle(item) == reply

    ai.responses = [json.dumps({"expenses": [], "clarification": "Сколько за <кофе> & чай?"})]
    reply = await dispatch(setup, update(2))
    assert "&lt;кофе&gt; &amp; чай?" in reply.text
    assert reply.parse_mode == "HTML"

    # Cached messages from the previous version must not be interpreted as HTML.
    store.enqueue(update(3))
    store.save(3, "reply", {"text": "Старый <неформатированный> ответ"})
    old_reply = await service.handle(update(3))
    assert old_reply.parse_mode is None
    assert old_reply.text == "Старый <неформатированный> ответ"


async def test_retry_after_ambiguous_timeout_uses_identical_prepared_write(setup):
    service, _store, bridge, ai, _ = setup
    bridge.write.side_effect = [BridgeError("timeout", retryable=True), {"count": 1}]
    item = update()
    with pytest.raises(BridgeError):
        await dispatch(setup, item)
    # A newly added sheet cannot redirect a possibly committed expense on retry.
    bridge.sheets.return_value.append(Sheet(id=30, title="Новый период"))
    reply = await service.handle(item)
    assert "Записано" in reply.text
    assert len(ai.calls) == 1
    assert bridge.write.call_args_list[0] == bridge.write.call_args_list[1]
    await service.handle(item)
    assert bridge.write.await_count == 2  # cached receipt, no additional write


async def test_voice_takes_same_path(setup):
    _, _store, bridge, _, asr = setup
    item = update(text="")
    item["message"]["voice"] = {"file_id": "f", "duration": 4, "file_size": 4}
    reply = await dispatch(setup, item)
    assert asr.calls == 1
    assert "Записано" in reply.text
    assert bridge.write.call_args.kwargs["expenses"][0].amount_minor == 125050


async def test_long_or_silent_voice_never_writes(setup):
    _, _store, bridge, _, asr = setup
    item = update(text="")
    item["message"]["voice"] = {"file_id": "f", "duration": 999}
    assert "секунд" in (await dispatch(setup, item)).text
    assert asr.calls == 0
    item["update_id"] = 2
    item["message"]["voice"]["duration"] = 2
    asr.speech_detected = False
    assert "разобрать речь" in (await dispatch(setup, item)).text
    bridge.write.assert_not_awaited()


async def test_clarification_keeps_original_expense(setup):
    _, store, bridge, ai, _ = setup
    ai.responses = [json.dumps({"expenses": [], "clarification": "Сколько потратили?"}), result()]
    reply = await dispatch(setup, update(text="купил продукты"))
    assert "Сколько" in reply.text
    bridge.write.assert_not_awaited()
    await dispatch(setup, update(2, "1250,50"))
    context = json.loads(ai.calls[1]["input"][0]["content"][0]["text"])
    assert context["previous_text"] == "купил продукты"
    assert store.user(100)["pending"] is None
    bridge.write.assert_awaited_once()


async def test_cancel_clears_pending_for_user_without_selection(setup):
    _, store, _, _, _ = setup
    store.pending(100, "старый расход")
    await dispatch(setup, update(text="/cancel"))
    assert store.user(100)["pending"] is None


async def test_new_period_does_not_inherit_old_clarification(setup, catalog):
    _, store, bridge, ai, _ = setup
    ai.responses = [json.dumps({"expenses": [], "clarification": "Сколько?"})]
    await dispatch(setup, update(text="купил продукты"))
    bridge.sheets.return_value.append(Sheet(id=30, title="Новый период"))
    bridge.catalog.return_value = catalog.model_copy(update={"id": 30, "title": "Новый период"})
    reply = await dispatch(setup, update(2, "500"))
    assert "Пришлите расход целиком" in reply.text
    assert store.user(100)["pending"] is None
    assert len(ai.calls) == 1
    bridge.write.assert_not_awaited()


async def test_permissions_group_messages_and_edits(setup):
    _, _store, bridge, ai, _ = setup
    reply = await dispatch(setup, update(user=999))
    assert "Доступ" in reply.text
    group = update(2)
    group["message"]["chat"]["type"] = "group"
    assert await dispatch(setup, group) is None
    edited = {"update_id": 3, "edited_message": update()["message"]}
    assert await dispatch(setup, edited) is None
    assert not ai.calls
    bridge.write.assert_not_awaited()


async def test_invalid_category_and_out_of_period_never_write(setup):
    _, _store, bridge, ai, _ = setup
    ai.responses = [result(category="invented"), result(when="2026-08-20")]
    assert "Не записано" in (await dispatch(setup, update())).text
    assert "нет даты" in (await dispatch(setup, update(2))).text
    bridge.write.assert_not_awaited()


async def test_changed_layout_is_rejected_without_success(setup):
    _, _store, bridge, _, _ = setup
    bridge.write.side_effect = BridgeError("Структура листа изменилась")
    assert "Не записано" in (await dispatch(setup, update())).text


async def test_multiple_expenses_single_atomic_request(setup):
    _, _store, bridge, ai, _ = setup
    body = json.loads(result())
    body["expenses"].append({**body["expenses"][0], "amount_minor": 20000})
    ai.responses = [json.dumps(body)]
    await dispatch(setup, update())
    bridge.write.assert_awaited_once()
    assert len(bridge.write.call_args.kwargs["expenses"]) == 2


async def test_uncertain_batch_has_no_partial_writes(catalog):
    payload = json.loads(result())
    payload["clarification"] = "Уточните вторую сумму"
    ai = ScriptedAIProvider(responses=[json.dumps(payload)])
    parsed = await extract(
        ai,
        text="две покупки",
        previous_text=None,
        catalog=catalog,
        reference_date=date(2026, 9, 25),
        currency="RUB",
    )
    assert not parsed.expenses


@pytest.mark.parametrize("amount", [0, -20, 12.5])
async def test_invalid_amount_rejected(catalog, amount):
    ai = ScriptedAIProvider(responses=[result(amount=amount)])
    with pytest.raises(ValidationFailed):
        await extract(
            ai,
            text="кофе",
            previous_text=None,
            catalog=catalog,
            reference_date=date(2026, 9, 25),
            currency="RUB",
        )


def test_durable_inbox_deduplicates_and_scrubs_payload(tmp_path):
    path = tmp_path / "inbox.sqlite"
    store = Store(path)
    item = update()
    store.enqueue(item)
    store.enqueue(item)
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert store.offset() == 2
    store.db.close()
    store = Store(path)
    assert json.loads(store.next_event()["payload"]) == item
    store.finish(1)
    assert store.next_event() is None
    assert store.db.execute("SELECT payload FROM events").fetchone()[0] == "{}"
    store.db.close()


def test_configuration_is_closed_by_default():
    settings = BotSettings(_env_file=None, telegram=TelegramSettings())
    assert not settings.allowed_users
    assert "FINTRACKER_SHEETS__ALLOWED_USER_IDS" in settings.missing()


async def test_asr_json_format_for_transcribe_model(monkeypatch):
    import httpx

    from fintracker.infra.asr.provider import OpenAIAsrProvider

    seen = []

    def handle(request):
        seen.append(request.content)
        return httpx.Response(200, json={"text": "кофе 250"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    provider = OpenAIAsrProvider(
        ASRSettings(provider="openai", model="gpt-4o-mini-transcribe", api_key=SecretStr("test"))
    )
    result = await provider.transcribe(audio=b"opus", mime_type="audio/ogg", duration_seconds=2)
    assert result.text == "кофе 250"
    assert b"verbose_json" not in seen[0]
    assert b"json" in seen[0]


async def test_relative_date_uses_original_message_across_midnight(setup):
    _, _store, _, ai, _ = setup
    ai.responses = [json.dumps({"expenses": [], "clarification": "Сколько?"}), result()]
    first = update(text="вчера продукты")
    await dispatch(setup, first)
    second = update(2, "500")
    second["message"]["date"] += 86400
    await dispatch(setup, second)
    contexts = [json.loads(c["input"][0]["content"][0]["text"]) for c in ai.calls]
    assert contexts[0]["reference_date"] == contexts[1]["reference_date"]


async def test_future_date_never_written(catalog):
    catalog.dates.append(date(2026, 9, 26))
    ai = ScriptedAIProvider(responses=[result(when="2026-09-26")])
    parsed = await extract(
        ai,
        text="завтра еда 500",
        previous_text=None,
        catalog=catalog,
        reference_date=date(2026, 9, 25),
        currency="RUB",
    )
    assert not parsed.expenses
    assert "будущем" in parsed.clarification


async def test_bridge_classifies_unknown_network_outcome_as_retryable(monkeypatch):
    import httpx

    original = httpx.AsyncClient

    def handler(request):
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    bridge = SheetsBridge(
        SheetsSettings(
            bridge_url="https://script.google.com/macros/s/test/exec",
            bridge_secret=SecretStr("test"),
        )
    )
    with pytest.raises(BridgeError) as error:
        await bridge.sheets()
    assert error.value.retryable


async def test_bridge_uses_secret_and_propagates_permanent_error(monkeypatch):
    import httpx

    original = httpx.AsyncClient

    def handler(request):
        body = json.loads(request.content)
        assert body["secret"] == "test"
        assert body["action"] == "catalog"
        assert body["sheet_id"] == 10
        return httpx.Response(200, json={"ok": False, "error": "Нет категории", "retryable": False})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    bridge = SheetsBridge(
        SheetsSettings(
            bridge_url="https://script.google.com/macros/s/test/exec",
            bridge_secret=SecretStr("test"),
        )
    )
    with pytest.raises(BridgeError) as error:
        await bridge.catalog(10)
    assert not error.value.retryable
