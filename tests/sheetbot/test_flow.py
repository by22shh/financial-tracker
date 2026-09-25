import io
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from fintracker.config import ASRSettings, TelegramSettings
from fintracker.core.errors import ValidationFailed
from fintracker.infra.ai.openai_client import ScriptedAIProvider
from fintracker.infra.asr.provider import ScriptedAsrProvider
from fintracker.sheetbot.bridge import BridgeError, SheetsBridge
from fintracker.sheetbot.config import BotSettings, SheetsSettings
from fintracker.sheetbot.extraction import extract
from fintracker.sheetbot.models import Catalog, Category, Sheet
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
    bridge.sheets.return_value = [Sheet(id=10, title=catalog.title), Sheet(id=20, title="Другой")]
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


async def test_start_only_offers_worksheets(setup):
    reply = await dispatch(setup, update(text="/start"))
    assert [row[0]["callback_data"] for row in reply.buttons] == ["sheet:10", "sheet:20"]
    assert "бюджет" not in reply.text.lower()


async def test_select_is_persistent_per_user_and_old_buttons_are_gone(setup):
    service, store, _bridge, *_ = setup
    item = {
        "update_id": 1,
        "callback_query": {
            "from": {"id": 100},
            "data": "sheet:10",
            "message": {"chat": {"id": 100, "type": "private"}},
        },
    }
    reply = await dispatch(setup, item)
    assert "Лист:" in reply.text
    assert store.user(100)["sheet_id"] == 10
    assert store.user(200)["sheet_id"] is None
    second = Store(service.settings.sheets.state_path)
    assert second.user(100)["sheet_id"] == 10
    second.db.close()
    item["update_id"] = 2
    item["callback_query"]["data"] = "budget:create"
    reply = await dispatch(setup, item)
    assert reply.buttons[0][0]["callback_data"] == "sheet:10"


async def test_expense_is_written_without_confirmation(setup):
    _service, store, bridge, ai, _ = setup
    store.select(100, 10)
    reply = await dispatch(setup, update())
    assert "Записано" in reply.text
    assert "1 250,50 RUB" in reply.text
    bridge.write.assert_awaited_once()
    assert bridge.write.call_args.kwargs["key"] == "telegram:123:100:1"
    assert bridge.write.call_args.kwargs["catalog"].id == 10
    assert len(ai.calls) == 1


async def test_retry_after_ambiguous_timeout_uses_identical_prepared_write(setup):
    service, store, bridge, ai, _ = setup
    store.select(100, 10)
    bridge.write.side_effect = [BridgeError("timeout", retryable=True), {"count": 1}]
    item = update()
    with pytest.raises(BridgeError):
        await dispatch(setup, item)
    # Even an external sheet switch cannot redirect an in-flight expense.
    store.select(100, 20)
    reply = await service.handle(item)
    assert "Записано" in reply.text
    assert len(ai.calls) == 1
    assert bridge.write.call_args_list[0] == bridge.write.call_args_list[1]
    await service.handle(item)
    assert bridge.write.await_count == 2  # cached receipt, no additional write


async def test_voice_takes_same_path(setup):
    _, store, bridge, _, asr = setup
    store.select(100, 10)
    item = update(text="")
    item["message"]["voice"] = {"file_id": "f", "duration": 4, "file_size": 4}
    reply = await dispatch(setup, item)
    assert asr.calls == 1
    assert "Записано" in reply.text
    assert bridge.write.call_args.kwargs["expenses"][0].amount_minor == 125050


async def test_long_or_silent_voice_never_writes(setup):
    _, store, bridge, _, asr = setup
    store.select(100, 10)
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
    store.select(100, 10)
    ai.responses = [json.dumps({"expenses": [], "clarification": "Сколько потратили?"}), result()]
    reply = await dispatch(setup, update(text="купил продукты"))
    assert "Сколько" in reply.text
    bridge.write.assert_not_awaited()
    await dispatch(setup, update(2, "1250,50"))
    context = json.loads(ai.calls[1]["input"][0]["content"][0]["text"])
    assert context["previous_text"] == "купил продукты"
    assert store.user(100)["pending"] is None
    bridge.write.assert_awaited_once()


async def test_cancel_and_sheet_switch_clear_pending(setup):
    _, store, _, _, _ = setup
    store.select(100, 10)
    store.pending(100, "старый расход")
    await dispatch(setup, update(text="/cancel"))
    assert store.user(100)["pending"] is None
    store.pending(100, "старый расход")
    store.select(100, 20)
    assert store.user(100)["pending"] is None


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
    _, store, bridge, ai, _ = setup
    store.select(100, 10)
    ai.responses = [result(category="invented"), result(when="2026-08-20")]
    assert "Не записано" in (await dispatch(setup, update())).text
    assert "нет даты" in (await dispatch(setup, update(2))).text
    bridge.write.assert_not_awaited()


async def test_changed_layout_is_rejected_without_success(setup):
    _, store, bridge, _, _ = setup
    store.select(100, 10)
    bridge.write.side_effect = BridgeError("Структура листа изменилась")
    assert "Не записано" in (await dispatch(setup, update())).text


async def test_multiple_expenses_single_atomic_request(setup):
    _, store, bridge, ai, _ = setup
    store.select(100, 10)
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
    _, store, _, ai, _ = setup
    store.select(100, 10)
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
