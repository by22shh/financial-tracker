"""Регрессии пользовательских путей из аудита 24 сентября 2026 года."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from fintracker.application.conversation.service import handle
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.ingestion.accept_update import callback_ack, sanitize_payload
from fintracker.config import Settings
from fintracker.db.models.access import Workspace
from fintracker.db.models.ledger import Transaction, TransactionRevision
from fintracker.db.models.platform import ThresholdEvent
from fintracker.db.session import RuntimeRole, session_scope
from tests.acceptance.conftest import BotUser, make_user
from tests.acceptance.helpers import create_budget, issue_invite_code


async def _send_as(user: BotUser, text: str, name: str) -> str:
    """Сообщение с именем из профиля Telegram, как его передаёт адаптер."""
    message = IncomingMessage(
        telegram_user_id=user.telegram_user_id,
        chat_id=user.chat_id,
        kind=MessageKind.COMMAND if text.startswith("/") else MessageKind.TEXT,
        text=text,
        message_id=int(uuid.uuid4().int % 10**6),
        received_at=user.clock,
        correlation_id=uuid.uuid4().hex,
        display_name=name,
    )
    user.last_replies = await handle(user.settings, message)
    return _t(user)


def _t(user: BotUser) -> str:
    """Текст ответа с обычными пробелами вместо неразрывных в суммах."""
    return user.text().replace("\xa0", " ")


async def _posted_amounts(settings: Settings) -> list[int]:
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        return list(
            (
                await session.execute(
                    select(TransactionRevision.amount_minor)
                    .join(
                        Transaction,
                        (Transaction.id == TransactionRevision.transaction_id)
                        & (Transaction.current_revision == TransactionRevision.revision),
                    )
                    .where(Transaction.status == "posted")
                )
            ).scalars()
        )


async def test_delete_phrase_never_deletes_budget_without_button(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931001)
    await create_budget(user, name="Кафе")
    await user.send("удалить Кафе")
    await user.send("/budget")
    assert "Кафе" in _t(user), "фраза без кнопки не удаляет бюджет"

    await user.press("ws:delete")
    await user.send("Кофе")
    assert "Название не совпало" in _t(user)
    await user.press(user.button_data("Отмена"))
    assert "Бюджет не удалён" in _t(user)
    await user.send("Кафе")
    await user.send("/budget")
    assert "Кафе" in _t(user), "после отмены название не удаляет бюджет"

    await user.press("ws:delete")
    await user.send("Кафе")
    assert "удалён" in _t(user)


async def test_join_by_button_accepts_plain_code_and_keeps_setup(
    bot: None, test_settings: Settings
) -> None:
    admin = make_user(test_settings, 931011)
    await create_budget(admin, name="Семья")
    code = await issue_invite_code(admin)

    member = make_user(test_settings, 931012)
    await member.send("/start")
    await member.press("wiz:start")
    await member.send("Мой бюджет")
    await member.press("join:start")
    assert "код приглашения" in _t(member)
    await member.send(code.lower().replace("-", " "))
    assert "Вы присоединились к бюджету «Семья»" in _t(member)
    await member.send("/start")
    assert member.has_button("Продолжить настройку"), "своя настройка не потеряна"


def test_plain_invite_code_is_redacted_before_storage() -> None:
    update = {"update_id": 1, "message": {"text": "ABCD-EFGH-JKMN", "from": {"id": 1}}}
    sanitized, code = sanitize_payload(update)
    assert code == "ABCDEFGHJKMN"
    assert sanitized["message"]["text"] == "<invite-code-redacted>"
    untouched, none = sanitize_payload({"update_id": 2, "message": {"text": "кофе 250"}})
    assert none is None and untouched["message"]["text"] == "кофе 250"


def test_callback_is_acknowledged_in_webhook_response() -> None:
    assert callback_ack({"callback_query": {"id": "42", "data": "menu:main"}}) == {
        "method": "answerCallbackQuery",
        "callback_query_id": "42",
    }
    assert callback_ack({"message": {"text": "кофе 250"}}) is None


async def test_new_expense_is_not_swallowed_by_pending_edit(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931021)
    await create_budget(user, categories="Продукты, Транспорт")
    await user.send("кофе 250")
    await user.press(user.button_data("Изменить"))
    await user.send("такси 700")
    assert "Прошлое действие отменено" in _t(user)
    assert "700 ₽" in _t(user) and "Транспорт" in _t(user)
    await user.press(user.button_data("Записать"))
    assert await _posted_amounts(test_settings) == [70000]


async def test_draft_edit_shows_new_values_and_accepts_date(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931022)
    await create_budget(user, categories="Продукты")
    await user.send("продукты 450")
    assert "Категория: Продукты" in _t(user)
    await user.press(user.button_data("Изменить"))
    await user.send("600")
    assert "Изменено: сумма — 600 ₽" in _t(user)
    assert "600 ₽" in _t(user) and "Проверьте запись" in _t(user)
    await user.press(user.button_data("Изменить"))
    await user.send("вчера")
    assert "Изменено: дата" in _t(user)


async def test_draft_category_can_be_picked_with_buttons(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931023)
    await create_budget(user, categories="Продукты, Досуг")
    await user.send("шоколадка 120")
    assert "Категория: Без категории" in _t(user)
    await user.press(user.button_data("Категория"))
    await user.press(user.button_data("Досуг"))
    assert "Категория: Досуг" in _t(user)
    await user.press(user.button_data("Записать"))
    assert "Досуг" in _t(user)


async def test_unfinished_second_budget_does_not_eat_expenses(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931031)
    await create_budget(user, categories="Продукты")
    await user.press("wiz:start")
    await user.send("Отпуск")
    await user.send("продукты 300")
    assert "Не узнал валюту" not in _t(user)
    assert "Настройка нового бюджета не потеряна" in _t(user)
    assert "300 ₽" in _t(user)


async def test_keyword_categories_and_type_are_visible_before_saving(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931041)
    await create_budget(user, categories="Продукты питания, Транспорт, Рестораны")
    await user.send("вчера такси 400")
    text = _t(user)
    assert "Расход · 400 ₽" in text and "Категория: Транспорт" in text
    assert "вчера такси" not in text, "дата не попадает в описание"
    await user.send("зарплата 100000")
    assert "Доход · 100 000 ₽" in _t(user)


async def test_negation_does_not_hide_real_purchase(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 931042)
    await create_budget(user, categories="Продукты")
    await user.send("в магазине не было молока, взял кефир 90")
    assert "90 ₽" in _t(user) and "покупки не было" not in _t(user)


async def test_bare_date_is_not_recorded_as_money(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 931043)
    await create_budget(user, categories="Продукты")
    await user.send("25.09")
    assert "Похоже, это дата" in _t(user)
    await user.send("привет")
    assert user.has_button("Добавить трату")


async def test_payment_from_reminder_text_is_created_and_paid_in_one_tap(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931051)
    await create_budget(user, categories="Продукты, Связь")
    await user.send("напомни оплатить интернет 900 25 числа")
    assert "Название: Интернет" in _t(user) and "Сумма: 900 ₽" in _t(user)
    await user.press(user.button_data("Каждый месяц"))
    assert "Платёж «Интернет» создан" in _t(user)
    assert "Категория расхода: Связь" in _t(user)
    await user.press("menu:payments")
    assert "Интернет" in _t(user)
    await user.press(user.button_data("Интернет"))
    await user.press(user.button_data("Оплачено"))
    assert "оплачен" in _t(user)
    assert await _posted_amounts(test_settings) == [90000]

    await user.press("pay:list")
    await user.press(user.button_data("Интернет"))
    await user.press(user.button_data("Удалить платёж"))
    await user.press(user.button_data("Удалить"))
    assert "удалён" in _t(user)


async def test_manual_form_uses_buttons_and_raises_limit_warning(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931061)
    await create_budget(user, categories="Продукты", limits="Продукты 1000")
    await user.press("menu:add")
    await user.send("950")
    await user.press(user.button_data("Продукты"))
    await user.press(user.button_data("Сегодня"))
    await user.press(user.button_data("без комментария"))
    assert "Расход записан · 950 ₽" in _t(user)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        types = list((await session.execute(select(ThresholdEvent.threshold_type))).scalars())
    assert types == ["approach_90"], "лимит отслеживается и для ручного ввода"


async def test_members_are_named_from_telegram_profile(bot: None, test_settings: Settings) -> None:
    admin = make_user(test_settings, 931071)
    await create_budget(admin, name="Дом")
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 931072)
    await _send_as(member, f"/join {code}", "Маша")
    await _send_as(admin, "/members", "Иван")
    text = _t(admin)
    assert "Маша — участник" in text and "Иван (вы)" in text
    assert "Участник " not in text.replace("Маша — участник", "")

    await admin.press("ws:remove")
    await admin.press(admin.button_data("Маша"))
    await admin.press(admin.button_data("Исключить"))
    assert "Маша больше не участвует" in _t(admin)


async def test_completeness_can_be_confirmed_and_report_link_works(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 931081)
    await create_budget(user, categories="Продукты")
    await user.press("menu:check")
    await user.press(user.button_data("Все траты внесены"))
    assert "отмечен полным" in _t(user)
    await user.press("menu:report")
    assert "Итоги периода" in _t(user)
    await user.press("noop:once")
    assert "изменена только эта запись" in _t(user)


async def test_budget_can_be_renamed_and_period_changed(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 931091)
    await create_budget(user, name="Старое", categories="Продукты")
    await user.press("set:bname")
    await user.send("Новое")
    assert "переименован" in _t(user)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        names = list((await session.execute(select(Workspace.name))).scalars())
    assert names == ["Новое"]
    await user.press("set:period")
    await user.press(user.button_data("Неделя"))
    assert "Начнёт действовать" in _t(user)
    await user.press(user.button_data("Применить"))
    assert "Правило периода изменено" in _t(user)
    assert "каждую неделю" in _t(user) or "7" in _t(user)


async def test_goal_can_be_renamed_and_removed(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 931101)
    await create_budget(user, categories="Продукты")
    await user.press("goal:new")
    await user.send("Отпуск")
    await user.send("100000")
    await user.press("menu:goals")
    await user.press(user.button_data("Отпуск"))
    await user.press(user.button_data("Изменить"))
    await user.press(user.button_data("Название"))
    await user.send("Море")
    assert "🎯 Море" in _t(user)
    await user.press(user.button_data("Изменить"))
    await user.press(user.button_data("Удалить цель"))
    await user.press(user.button_data("Удалить"))
    assert "Цель удалена" in _t(user)
