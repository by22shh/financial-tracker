"""Read-only application audit: synthetic data in a uniquely named PostgreSQL DB.

Assertions express expected product behavior; no expected-failure annotations.
External AI and Telegram adapters are scripted; SQL/RLS and application are real.
Run from repository root with plugins tests.conftest and tests.acceptance.conftest.
"""
import datetime as dt
import json

import pytest
from sqlalchemy import select

from fintracker.application.conversation.service import handle
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.commitments.schedules import create_schedule, materialize_occurrences
from fintracker.core.errors import ProviderUnavailable
from fintracker.core.money import Money
from fintracker.db.models.catalog import Category
from fintracker.db.models.commitments import Goal, ScheduledItem, Occurrence
from fintracker.db.models.ledger import Transaction, TransactionRevision
from fintracker.db.models.platform import Draft, Candidate
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.schedule import ScheduleKind, ScheduleRule
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.integration.test_deep_audit import prepared, incoming, leased


async def post(user, text):
    await user.send(text)
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))


async def test_category_archive_is_accessible_and_can_restore(bot, test_settings):
    user = make_user(test_settings, 99004001)
    await create_budget(user)
    await post(user, "рестораны 800")
    await user.press("cat:manage")
    await user.press(user.button_data("Рестораны"))
    await user.press(user.button_data("Убрать в архив"))
    assert "убрана в архив" in user.text()
    await user.press("cat:manage")
    await user.press(user.button_data("Архив"))
    assert "Рестораны" in user.text(), user.text()


async def test_category_rename_button_saves_name(bot, test_settings):
    user = make_user(test_settings, 99004002)
    await create_budget(user)
    await user.press("cat:manage")
    await user.press(user.button_data("Рестораны"))
    await user.press(user.button_data("Переименовать"))
    await user.send("Кафе")
    if user.has_button("Подтвердить"):
        await user.press(user.button_data("Подтвердить"))
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        names = list((await session.execute(select(Category.name))).scalars())
    assert "Кафе" in names and "Рестораны" not in names, (user.text(), names)


async def test_goal_creation_button_enters_wizard(bot, test_settings):
    user = make_user(test_settings, 99004003)
    await create_budget(user)
    await user.send("/goals")
    await user.press(user.button_data("Добавить цель"))
    assert "Кнопка устарела" not in user.text() and "Действие недоступно" not in user.text(), user.text()


async def test_new_payment_button_enters_form(bot, test_settings):
    user = make_user(test_settings, 99004004)
    await create_budget(user)
    await user.send("напомни оплатить интернет 1000")
    await user.press(user.button_data("Создать платёж"))
    assert "Кнопка устарела" not in user.text(), user.text()


async def test_wizard_keeps_commitment_and_goal_inputs(bot, test_settings):
    user = make_user(test_settings, 99004005)
    await user.send("/start")
    await user.press("wiz:start")
    for value in ("Аудит мастера", "RUB", "Asia/Novosibirsk", "10.09.2026 — 09.10.2026"):
        await user.send(value)
    await user.press(user.button_data("календарный месяц"))
    await user.press("wiz:inc:exact")
    await user.send("100000")
    await user.send("Продукты, Жильё")
    await user.press("wiz:skip:limits")
    await user.send("Аренда = 20000 = 20.09")
    await user.send("Отпуск = 100000")
    await user.press("wiz:tpl:on")
    await user.press("wiz:publish")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        schedules = list((await session.execute(select(ScheduledItem.name))).scalars())
        goals = list((await session.execute(select(Goal.name))).scalars())
    assert "Аренда" in schedules and "Отпуск" in goals, (user.text(), schedules, goals)


def receipt(kind="receipt"):
    return json.dumps({
        "schema_version": "1.0", "document_kind": kind,
        "payment_confirmed": kind == "receipt", "total_decimal": "450.00", "currency": "RUB",
        "lines": [{"label": "Продукты", "amount_decimal": "450.00", "readable": True, "category_id": None}],
        "unreadable_lines": 0,
    })


@pytest.fixture
def media_environment(test_settings, monkeypatch):
    configured = test_settings.model_copy(deep=True)
    configured.ai.enabled = True
    async def download(*args, **kwargs):
        return b"local receipt fixture; scripted provider"
    monkeypatch.setattr("fintracker.application.intelligence.media_pipeline.download_attachment", download)
    provider = ScriptedAIProvider()
    set_provider_override(provider)
    yield configured, provider
    set_provider_override(None)


async def test_media_retry_button_reprocesses_saved_draft(bot, media_environment):
    settings, provider = media_environment
    user = make_user(settings, 99004006)
    await create_budget(user)
    provider.fail_with = ProviderUnavailable("temporary test failure")
    await user.send_photo()
    retry = user.button_data("Повторить разбор")
    calls = len(provider.calls)
    provider.fail_with = None
    provider.responses.append(receipt())
    await user.press(retry)
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        states = list((await session.execute(select(Draft.state))).scalars())
    assert len(provider.calls) > calls and user.has_button("Записать"), (user.text(), states)


async def test_invoice_paid_button_produces_confirmable_record(bot, media_environment):
    settings, provider = media_environment
    user = make_user(settings, 99004007)
    await create_budget(user)
    provider.responses.append(receipt("invoice"))
    await user.send_photo()
    await user.press(user.button_data("Да, оплачено"))
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        states = list((await session.execute(select(Draft.state))).scalars())
        candidates = list((await session.execute(select(Candidate.id))).scalars())
    assert user.has_button("Записать") and candidates, (user.text(), states, candidates)


async def test_ready_draft_edit_updates_same_draft(bot, test_settings):
    user = make_user(test_settings, 99004008)
    await create_budget(user)
    await user.send("продукты 450")
    confirm = user.button_data("Записать")
    await user.press(user.button_data("Изменить"))
    await user.send("600")
    await user.press(confirm)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        amounts = list((await session.execute(select(TransactionRevision.amount_minor))).scalars())
        drafts = list((await session.execute(select(Draft.state))).scalars())
    assert amounts == [60000], (user.text(), amounts, drafts)


async def test_payment_done_links_record_to_occurrence(owner_session, test_settings):
    f = await prepared(owner_session)
    await create_schedule(
        owner_session, f.uow, actor=f.actor, name="Интернет", direction="payment",
        rule=ScheduleRule(kind=ScheduleKind.ONCE, anchor_date=dt.date(2026, 9, 12)),
        currency="RUB", expected=Money(100000, "RUB"), category_id=f.categories["Продукты"],
    )
    occurrence = (await materialize_occurrences(owner_session, workspace_id=f.workspace.id, until_date=dt.date(2026, 9, 20)))[0]
    occurrence_id = occurrence.id
    await owner_session.commit()
    user = make_user(test_settings, f.user.telegram_user_id)
    await user.press("menu:payments")
    await user.press(user.button_data("Оплачено"))
    await post(user, "оплатил интернет 1000")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        value = await session.get(Occurrence, occurrence_id)
        assert value.state == "settled" and value.settled_minor == 100000, (value.state, value.settled_minor, user.text())


async def test_comment_search_survives_page_navigation(bot, test_settings):
    user = make_user(test_settings, 99004009)
    await create_budget(user)
    for i in range(9):
        await post(user, f"продукты {100 + i}. Комментарий: отпуск")
    await post(user, "продукты 777")
    await user.send("/history отпуск")
    assert "из 9" in user.text(), user.text()
    await user.press(user.button_data("Ещё →"))
    assert "из 9" in user.text() and "Поиск по комментарию" in user.text(), user.text()


async def test_reply_to_old_author_card_does_not_edit_latest_record(owner_session, test_settings):
    from fintracker.application.ingestion.process_event import handle_process_inbound_event
    from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
    f = await prepared(owner_session)
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        first = await leased(test_settings, incoming(f, "продукты 450", update_id=4001, message_id=41))
        await handle_process_inbound_event(test_settings, first)
        first_card_id = sender._next_id
        second = await leased(test_settings, incoming(f, "продукты 800", update_id=4002, message_id=42))
        await handle_process_inbound_event(test_settings, second)
        correction = incoming(f, "Добавь комментарий: первая покупка", update_id=4003, message_id=43)
        correction["message"]["reply_to_message"] = {"message_id": first_card_id}
        third = await leased(test_settings, correction)
        await handle_process_inbound_event(test_settings, third)
    finally:
        set_sender_override(None)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        rows = (await session.execute(select(TransactionRevision.amount_minor, TransactionRevision.note).join(
            Transaction, (Transaction.id == TransactionRevision.transaction_id) & (Transaction.current_revision == TransactionRevision.revision)
        ).order_by(TransactionRevision.amount_minor))).all()
    assert rows == [(45000, "первая покупка"), (80000, None)], rows


async def test_export_rechecks_member_after_snapshot(bot, test_settings, monkeypatch):
    from fintracker.application.identity.membership import remove_member
    from fintracker.application.integrations import exporter
    from fintracker.db.models.access import User, Workspace
    from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
    from tests.acceptance.helpers import issue_invite_code

    admin = make_user(test_settings, 99004010)
    await create_budget(admin)
    await post(admin, "продукты 98765")
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 99004011)
    await member.send(f"/join {code}")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        workspace = (await session.execute(select(Workspace))).scalar_one()
        member_id = (await session.execute(select(User.id).where(User.telegram_user_id == member.telegram_user_id))).scalar_one()
        workspace_id, admin_id = workspace.id, workspace.admin_user_id

    original = exporter.build_snapshot
    async def revoke_after_snapshot(*args, **kwargs):
        result = await original(*args, **kwargs)
        await remove_member(test_settings, workspace_id=workspace_id, admin_user_id=admin_id,
                            target_user_id=member_id, correlation_id="readiness-export-revoke")
        return result
    monkeypatch.setattr(exporter, "build_snapshot", revoke_after_snapshot)
    sender = RecordingSender()
    set_sender_override(sender)
    try:
        await member.press("exp:csv")
    finally:
        set_sender_override(None)
    assert sender.documents == [], (member.text(), sender.documents)


async def test_admin_transfer_is_actionable_by_recipient(bot, test_settings):
    from tests.acceptance.helpers import issue_invite_code
    admin = make_user(test_settings, 99004012)
    await create_budget(admin)
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 99004013)
    await member.send(f"/join {code}")
    await admin.send("/members")
    await admin.press(admin.button_data("Выйти из бюджета"))
    await admin.press(admin.button_data("Передать роль"))
    await admin.press(admin.button_data("Участник"))
    assert "Предложение отправлено" in admin.text()
    await member.send("/members")
    assert any(b.data.startswith("ws:acceptadmin:") for reply in member.last_replies for row in reply.buttons for b in row), member.text()


async def test_limit_button_changes_existing_limit(bot, test_settings):
    user = make_user(test_settings, 99004014)
    await create_budget(user, categories="Рестораны", limits="Рестораны = 5000")
    await user.press("cat:manage")
    await user.press(user.button_data("Рестораны"))
    await user.press(user.button_data("Задать лимит"))
    await user.send("8000")
    if user.has_button("Подтвердить"):
        await user.press(user.button_data("Подтвердить"))
    await user.send("/categories")
    assert "8\u00a0000,00" in user.text(), user.text()


async def test_confirm_incomplete_batch_never_partially_posts(bot, media_environment):
    from tests.integration.test_ai_contract import extraction_json
    test_settings, provider = media_environment
    user = make_user(test_settings, 99004015)
    await create_budget(user)
    payload = json.loads(extraction_json(candidate={"amount_decimal": "3000.00", "date_expression": "вчера", "description": "Бензин"}))
    payload["candidates"].append({**payload["candidates"][0], "candidate_key": "c2", "amount_decimal": None, "description": "Продукты", "ambiguities": [{"field": "amount", "reason": "missing", "options": []}]})
    payload["question"] = "Сколько стоили продукты?"
    provider.responses.append(json.dumps(payload))
    await user.send("Вчера бензин 3000, купил продукты")
    assert "сумма неизвестна" in user.text(), user.text()
    await user.press(user.button_data("Записать"))
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        transactions = list((await session.execute(select(Transaction.id))).scalars())
        states = list((await session.execute(select(Candidate.state))).scalars())
    assert transactions == [], (user.text(), transactions, states)


async def test_mixed_receipt_is_one_transaction_with_two_allocations(bot, media_environment):
    from fintracker.db.models.ledger import Allocation
    settings, provider = media_environment
    user = make_user(settings, 99004016)
    await create_budget(user, categories="Продукты, Дом")
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        ids = dict((await session.execute(select(Category.name, Category.id))).all())
    response = json.loads(receipt())
    response["total_decimal"] = "1400.00"
    response["lines"] = [{"label": name, "amount_decimal": amount, "readable": True, "category_id": str(ids[name])} for name, amount in (("Продукты", "1000.00"), ("Дом", "400.00"))]
    provider.responses.append(json.dumps(response))
    await user.send_photo()
    await user.press(user.button_data("Записать"))
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        transactions = list((await session.execute(select(Transaction.id))).scalars())
        allocations = (await session.execute(select(Allocation.transaction_id, Allocation.amount_minor))).all()
    assert len(transactions) == 1 and len(allocations) == 2, (user.text(), transactions, allocations)
