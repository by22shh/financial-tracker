"""Independent E2E probes; assertions express required behavior, no xfail."""

import datetime as dt

from sqlalchemy import select, update

from fintracker.application.commitments.schedules import create_schedule, materialize_occurrences
from fintracker.core.money import Money
from fintracker.db.models.access import Membership, Workspace
from fintracker.db.models.catalog import Category
from fintracker.db.models.commitments import Goal, Occurrence, ScheduledItem
from fintracker.db.models.ledger import TransactionRevision
from fintracker.db.models.planning import BudgetLine
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.schedule import ScheduleKind, ScheduleRule
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.integration.test_deep_audit import prepared
from tests.readiness.test_flow_readiness import media_environment, receipt


async def post(user, text):
    await user.send(text)
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))


async def test_cancel_clears_goal_creation_before_next_expense(bot, test_settings):
    user = make_user(test_settings, 99005001)
    await create_budget(user)
    await user.send("/goals")
    await user.press(user.button_data("Добавить цель"))
    await user.send("/cancel")
    await post(user, "продукты 450")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        goals = list((await session.execute(select(Goal.name))).scalars())
        amounts = list((await session.execute(select(TransactionRevision.amount_minor))).scalars())
    assert not goals and amounts == [45000], (user.text(), goals, amounts)


async def test_invalid_limit_keeps_continuation_for_corrected_input(bot, test_settings):
    user = make_user(test_settings, 99005002)
    await create_budget(user, limits="Рестораны = 5000")
    await user.press("cat:manage")
    await user.press(user.button_data("Рестораны"))
    await user.press(user.button_data("Задать лимит"))
    await user.send("ошибка")
    assert "Не понял сумму лимита" in user.text(), user.text()
    await user.send("8000")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        limits = list((await session.execute(select(BudgetLine.limit_minor))).scalars())
    assert 800000 in limits, (user.text(), limits)


async def test_switching_budget_does_not_retarget_pending_goal(bot, test_settings):
    user = make_user(test_settings, 99005003)
    await create_budget(user, name="Бюджет А")
    await create_budget(user, name="Бюджет Б")
    await user.send("/budgets")
    await user.press(user.button_data("Бюджет А"))
    await user.send("/goals")
    await user.press(user.button_data("Добавить цель"))
    await user.send("/budgets")
    await user.press(user.button_data("Бюджет Б"))
    await user.send("Отпуск = 100000")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        rows = (
            await session.execute(
                select(Goal.name, Workspace.name).join(Workspace, Workspace.id == Goal.workspace_id)
            )
        ).all()
    assert ("Отпуск", "Бюджет Б") not in rows, (user.text(), rows)


async def test_new_category_can_receive_first_limit(bot, test_settings):
    user = make_user(test_settings, 99005004)
    await create_budget(user)
    await user.send("Создай категорию Питомцы")
    await user.press("cat:manage")
    await user.press(user.button_data("Питомцы"))
    await user.press(user.button_data("Задать лимит"))
    await user.send("8000")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        limits = list(
            (
                await session.execute(
                    select(BudgetLine.limit_minor)
                    .join(Category, Category.id == BudgetLine.category_id)
                    .where(Category.name == "Питомцы")
                )
            ).scalars()
        )
    assert 800000 in limits, (user.text(), limits)


async def test_old_draft_confirmation_does_not_settle_unrelated_payment(
    owner_session, test_settings
):
    f = await prepared(owner_session)
    await create_schedule(
        owner_session,
        f.uow,
        actor=f.actor,
        name="Интернет",
        direction="payment",
        rule=ScheduleRule(kind=ScheduleKind.ONCE, anchor_date=dt.date(2026, 9, 12)),
        currency="RUB",
        expected=Money(100000, "RUB"),
        category_id=f.categories["Продукты"],
    )
    occurrence = (
        await materialize_occurrences(
            owner_session, workspace_id=f.workspace.id, until_date=dt.date(2026, 9, 20)
        )
    )[0]
    occurrence_id = occurrence.id
    await owner_session.execute(
        update(Membership)
        .where(Membership.id == f.actor.membership_id)
        .values(autopost_enabled=False)
    )
    await owner_session.commit()
    user = make_user(test_settings, f.user.telegram_user_id)
    await user.send("продукты 450")
    old_confirm = user.button_data("Записать")
    await user.press("menu:payments")
    await user.press(user.button_data("Оплачено"))
    await user.press(old_confirm)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        value = await session.get(Occurrence, occurrence_id)
        assert value.settled_minor == 0, (value.state, value.settled_minor, user.text())


async def test_long_comment_search_keeps_full_filter_on_next_page(bot, test_settings):
    user = make_user(test_settings, 99005005)
    await create_budget(user)
    query = "abcdefghijklmnop_target"
    for i in range(9):
        await post(user, f"продукты {100 + i}. Комментарий: {query}")
    await post(user, "продукты 777. Комментарий: abcdefghijklmnop_other")
    await user.send(f"/history {query}")
    assert "из 9" in user.text(), user.text()
    await user.press(user.button_data("Ещё →"))
    assert "из 9" in user.text() and query in user.text(), user.text()


async def test_goal_open_exposes_allocation_action(bot, test_settings):
    user = make_user(test_settings, 99005006)
    await create_budget(user)
    await user.send("/goals")
    await user.press(user.button_data("Добавить цель"))
    await user.send("Отпуск = 100000")
    assert "создана" in user.text(), user.text()
    await user.send("/goals")
    await user.press(user.button_data("Отпуск"))
    assert any(
        word in button.text.casefold()
        for reply in user.last_replies
        for row in reply.buttons
        for button in row
        for word in ("выделить", "пополнить", "внести", "резерв")
    ), user.text()


async def test_invalid_payment_date_does_not_silently_create_today(bot, test_settings):
    user = make_user(test_settings, 99005007)
    await create_budget(user)
    await user.send("напомни оплатить интернет 1000")
    await user.press(user.button_data("Создать платёж"))
    await user.send("Интернет = 1000 = 99.99")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        schedules = list((await session.execute(select(ScheduledItem.name))).scalars())
    assert not schedules, (user.text(), schedules)


async def test_comment_search_survives_sort_change(bot, test_settings):
    user = make_user(test_settings, 99005008)
    await create_budget(user)
    await post(user, "продукты 100. Комментарий: отпуск")
    await post(user, "продукты 777")
    await user.send("/history отпуск")
    assert "из 1" in user.text(), user.text()
    await user.press(user.button_data("Последние добавленные"))
    assert "из 1" in user.text() and "Поиск по комментарию" in user.text(), user.text()


async def test_receipt_amount_can_be_corrected_before_posting(bot, media_environment):
    settings, provider = media_environment
    user = make_user(settings, 99005009)
    await create_budget(user)
    provider.responses.append(receipt())
    await user.send_photo()
    await user.press(user.button_data("Изменить"))
    await user.send("600")
    await user.press(user.button_data("Записать"))
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        amounts = list((await session.execute(select(TransactionRevision.amount_minor))).scalars())
    assert amounts == [60000], (user.text(), amounts)
