"""Independent assertions of required behavior for 597029c; no xfail."""

import datetime as dt
import json

import httpx
import pytest
from sqlalchemy import select, update

from fintracker.application.commitments.schedules import create_schedule, materialize_occurrences
from fintracker.application.conversation import pending
from fintracker.application.conversation.service import handle
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.core.money import Money
from fintracker.db.models.access import Membership
from fintracker.db.models.commitments import Goal, GoalMovement, Occurrence
from fintracker.db.models.platform import PendingAction
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.schedule import ScheduleKind, ScheduleRule
from fintracker.infra.ai.openai_client import OpenAIResponsesProvider
from fintracker.infra.ai.schemas import ExtractionCandidate, ExtractionResponse, json_schema_for
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.integration.test_deep_audit import prepared
from tests.readiness.test_flow_readiness import media_environment, receipt


@pytest.mark.parametrize("query", ["комментарий для семейного отпуска", "x" * 40])
async def test_history_query_fits_buttons_even_with_category(bot, test_settings, query):
    user = make_user(test_settings, 99159701)
    await create_budget(user)
    await user.send("/history " + query)
    await user.press(user.button_data("Фильтры"))
    await user.press(user.button_data("Рестораны"))
    assert user.last_replies


@pytest.mark.parametrize("field", ["ambiguities", "evidence"])
async def test_nested_provider_null_allowed_by_schema_is_accepted(test_settings, monkeypatch, field):
    candidate = ExtractionCandidate(candidate_key="c1", kind="expense", amount_decimal="450")
    payload = ExtractionResponse(schema_version="1.0", intent="record_transaction", candidates=[candidate]).model_dump()
    payload["candidates"][0][field] = None
    definition = json_schema_for(ExtractionResponse)["$defs"]["ExtractionCandidate"]["properties"][field]
    assert "null" in definition.get("type", []) or {"type": "null"} in definition.get("anyOf", [])
    async def post(client, url, **kwargs):
        return httpx.Response(200, json={"output_text": json.dumps(payload)})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await OpenAIResponsesProvider(test_settings.ai).structured(
        instructions="test", input_items=[], response_model=ExtractionResponse,
        prompt_version="audit", schema_name="audit",
    )
    assert result.parsed.candidates[0].amount_decimal == "450"


async def test_malformed_model_json_gets_bounded_schema_retry(test_settings, monkeypatch):
    calls = []
    async def post(client, url, **kwargs):
        calls.append(kwargs)
        text = '{"schema_version":' if len(calls) == 1 else json.dumps(
            ExtractionResponse(schema_version="1.0", intent="question").model_dump()
        )
        return httpx.Response(200, json={"output_text": text})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await OpenAIResponsesProvider(test_settings.ai).structured(
        instructions="test", input_items=[], response_model=ExtractionResponse,
        prompt_version="audit", schema_name="audit",
    )
    assert len(calls) == 2 and result.parsed.intent == "question"


async def test_reserve_retry_after_commit_is_idempotent(bot, test_settings, monkeypatch):
    user = make_user(test_settings, 99159702)
    await create_budget(user)
    await user.send("/goals")
    await user.press(user.button_data("Добавить цель"))
    await user.send("Отпуск = 100000")
    await user.send("/goals")
    await user.press(user.button_data("Отпуск"))
    await user.press(user.button_data("Выделить резерв"))
    message = IncomingMessage(telegram_user_id=user.telegram_user_id, chat_id=user.chat_id,
        kind=MessageKind.TEXT, text="5000", message_id=597001, correlation_id="same-source-retry")
    original_clear = pending.clear_pending
    async def fail_after_commit(*args, **kwargs):
        raise RuntimeError("simulated crash after goal write before pending cleanup")
    monkeypatch.setattr(pending, "clear_pending", fail_after_commit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await handle(test_settings, message)
    monkeypatch.setattr(pending, "clear_pending", original_clear)
    await handle(test_settings, message)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        allocated = await session.scalar(select(Goal.allocated_minor).where(Goal.name == "Отпуск"))
        movements = (await session.scalars(select(GoalMovement.change_minor))).all()
    assert allocated == 500000, (allocated, movements)


async def payment_fixture(owner_session, test_settings):
    f = await prepared(owner_session)
    await create_schedule(owner_session, f.uow, actor=f.actor, name="Интернет", direction="payment",
        rule=ScheduleRule(kind=ScheduleKind.ONCE, anchor_date=dt.date(2026, 9, 12)),
        currency="RUB", expected=Money(100000, "RUB"), category_id=f.categories["Продукты"])
    occurrence = (await materialize_occurrences(owner_session, workspace_id=f.workspace.id,
        until_date=dt.date(2026, 9, 20)))[0]
    occurrence_id = occurrence.id
    await owner_session.execute(update(Membership).where(Membership.id == f.actor.membership_id).values(autopost_enabled=False))
    await owner_session.commit()
    return make_user(test_settings, f.user.telegram_user_id), occurrence_id


async def test_second_text_does_not_steal_payment_binding(owner_session, test_settings):
    user, occurrence_id = await payment_fixture(owner_session, test_settings)
    await user.press("menu:payments")
    await user.press(user.button_data("Оплачено"))
    await user.send("продукты 1000")
    first_confirm = user.button_data("Записать")
    await user.send("продукты 450")
    await user.press(first_confirm)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        occurrence = await session.get(Occurrence, occurrence_id)
        assert occurrence.settled_minor == 100000, (occurrence.state, occurrence.settled_minor, user.text())


async def test_receipt_payment_is_bound_to_occurrence(owner_session, media_environment):
    settings, provider = media_environment
    user, occurrence_id = await payment_fixture(owner_session, settings)
    await user.press("menu:payments")
    await user.press(user.button_data("Оплачено"))
    provider.responses.append(receipt())
    await user.send_photo()
    await user.press(user.button_data("Записать"))
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        occurrence = await session.get(Occurrence, occurrence_id)
        assert occurrence.settled_minor == 45000, (occurrence.state, occurrence.settled_minor, user.text())


async def test_invalid_reserve_amount_keeps_pending(bot, test_settings):
    user = make_user(test_settings, 99159703)
    await create_budget(user)
    await user.send("/goals")
    await user.press(user.button_data("Добавить цель"))
    await user.send("Отпуск = 100000")
    await user.send("/goals")
    await user.press(user.button_data("Отпуск"))
    await user.press(user.button_data("Выделить резерв"))
    await user.send("0")
    assert "положительной" in user.text(), user.text()
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        kind = await session.scalar(select(PendingAction.kind))
    assert kind == "goal_allocate", (kind, user.text())
