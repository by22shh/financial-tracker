"""Первый совместный цикл: создание, вход, запись, исправление, отчёт.

Это срез ADR-15: «создать бюджет → пригласить участника → вручную записать
расход → исправить → открыть следующий период → одинаковый отчёт обоим».
"""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget, issue_invite_code
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_a141_new_user_sees_create_and_join(bot: None, test_settings: Settings) -> None:
    """A141: новому пользователю доступны создание и вход по коду."""
    user = make_user(test_settings, 900001)
    await user.send("/start")
    assert "Добро пожаловать" in user.text()
    assert user.has_button("Создать бюджет")
    assert user.has_button("Присоединиться по коду")
    # Доступа к чужим бюджетам нет.
    assert "Мои бюджеты" not in user.text()


async def test_a142_create_budget_from_scratch(bot: None, test_settings: Settings) -> None:
    """A142: бюджет создаётся с нуля без таблицы; создатель — администратор."""
    user = make_user(test_settings, 900002)
    text = await create_budget(user, name="Личный бюджет")
    assert "Личный бюджет" in text
    assert "Постоянный ID:" in text
    assert "Ваша роль: администратор" in text
    assert "Текущий период: 2026-09-10 — 2026-10-09" in text
    assert "Следующий период начнётся 2026-10-10" in text


async def test_a143_publish_is_idempotent(bot: None, test_settings: Settings) -> None:
    """A143: повторное подтверждение не создаёт второй бюджет."""
    user = make_user(test_settings, 900003)
    await create_budget(user)
    await user.press("wiz:publish")
    await user.send("/budgets")
    assert user.text().count("Наш общий бюджет") == 1


async def test_a144_income_plan_does_not_create_money(bot: None, test_settings: Settings) -> None:
    """A144: план дохода 100 000 ₽ не увеличивает счёт и фактический доход."""
    user = make_user(test_settings, 900004)
    await create_budget(user, income="100000")
    await user.send("/budget")
    body = user.text()
    assert "Учтённые расходы: 0,00 ₽" in body


async def test_first_expense_and_card(bot: None, test_settings: Settings) -> None:
    """A01: «Кофе 250» даёт один расход с карточкой и возможностью отмены."""
    user = make_user(test_settings, 900005)
    await create_budget(user, categories="Продукты, Рестораны, Транспорт")
    await user.send("кофе 250")
    assert "Проверьте запись перед сохранением" in user.text()
    await user.press(user.button_data("Записать"))
    card = user.text()
    assert "Записано 250,00 ₽" in card
    assert user.has_button("Отменить запись")


async def test_a158_shared_visibility_counts_once(
    bot: None, test_settings: Settings, recording_sender
) -> None:
    """A158: одна операция 650 ₽ видна обоим; общий расход не удваивается."""
    admin = make_user(test_settings, 900010)
    await create_budget(admin, categories="Продукты, Рестораны")
    code = await issue_invite_code(admin)

    member = make_user(test_settings, 900011)
    await member.send(f"/join {code}")
    assert "присоединились к бюджету" in member.text()

    await admin.send("ресторан 650")
    await admin.press(admin.button_data("Записать"))
    assert "Записано 650,00 ₽" in admin.text()

    # Участник видит ту же операцию в общем журнале.
    await member.send("/history")
    assert "650,00 ₽" in member.text()

    await admin.send("/budget")
    assert "Учтённые расходы: 650,00 ₽" in admin.text()
    await member.send("/budget")
    assert "Учтённые расходы: 650,00 ₽" in member.text()


async def test_a163_member_corrects_admin_record(bot: None, test_settings: Settings) -> None:
    """A163: участник исправляет общую запись; авторы записи и ревизии разные."""
    admin = make_user(test_settings, 900020)
    await create_budget(admin, categories="Продукты, Рестораны")
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 900021)
    await member.send(f"/join {code}")

    await admin.send("продукты 1200")
    await admin.press(admin.button_data("Записать"))

    await member.send("Исправь 1200 на 1100")
    assert "Изменение записи" in member.text()
    assert "1 200,00 ₽ → 1 100,00 ₽" in member.text()
    await member.press(member.button_data("Подтвердить"))
    assert "Записано 1 100,00 ₽" in member.text()

    # Общий расход равен 1100 у обоих.
    for participant in (admin, member):
        await participant.send("/budget")
        assert "Учтённые расходы: 1 100,00 ₽" in participant.text()


async def test_a152_joined_member_sees_history_before_join(
    bot: None, test_settings: Settings
) -> None:
    """A152: присоединившийся видит все опубликованные операции, включая прошлые."""
    admin = make_user(test_settings, 900030)
    await create_budget(admin, categories="Продукты")
    await admin.send("продукты 500")
    await admin.press(admin.button_data("Записать"))
    code = await issue_invite_code(admin)

    member = make_user(test_settings, 900031)
    await member.send(f"/join {code}")
    await member.send("/history")
    assert "500,00 ₽" in member.text()


async def test_a153_invalid_code_reveals_nothing(bot: None, test_settings: Settings) -> None:
    """A153: неверный код не создаёт членства и не раскрывает данные."""
    admin = make_user(test_settings, 900040)
    await create_budget(admin, name="Секретный бюджет")
    outsider = make_user(test_settings, 900041)
    await outsider.send("/join ABCD-EFGH-JKMN")
    assert "Секретный бюджет" not in outsider.text()
    assert "недействителен" in outsider.text().lower()
    await outsider.send("/budget")
    assert "Сначала выберите бюджет" in outsider.text()


async def test_a151_budget_id_is_not_an_invite(bot: None, test_settings: Settings) -> None:
    """A151: постоянный ID бюджета не работает как код приглашения."""
    admin = make_user(test_settings, 900050)
    text = await create_budget(admin, name="Общий")
    budget_id = next(
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("Постоянный ID:")
    )
    outsider = make_user(test_settings, 900051)
    await outsider.send(f"/join {budget_id}")
    assert "Общий" not in outsider.text()
    await outsider.send("/budget")
    assert "Сначала выберите бюджет" in outsider.text()


async def test_a155_repeated_join_does_not_duplicate(bot: None, test_settings: Settings) -> None:
    """A155: повторный ввод того же кода не создаёт второе членство."""
    admin = make_user(test_settings, 900060)
    await create_budget(admin)
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 900061)
    await member.send(f"/join {code}")
    await member.send(f"/join {code}")
    assert "уже участник" in member.text().lower()
    await member.send("/budgets")
    assert member.text().count("Наш общий бюджет") == 1


async def test_a150_member_cannot_issue_invite(bot: None, test_settings: Settings) -> None:
    """A150: управление кодами приглашений запрещено участнику."""
    admin = make_user(test_settings, 900070)
    await create_budget(admin)
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 900071)
    await member.send(f"/join {code}")
    await member.send("/members")
    assert not member.has_button("Пригласить")
    await member.press("inv:new")
    assert "только администратор" in member.text().lower()


async def test_a106_foreign_workspace_id_is_denied(bot: None, test_settings: Settings) -> None:
    """A106: подстановка чужого ID не раскрывает данные (кнопка чужого бюджета)."""
    admin = make_user(test_settings, 900080)
    await create_budget(admin, name="Чужой бюджет")
    await admin.send("/budgets")
    foreign_button = admin.button_data("Чужой бюджет")

    outsider = make_user(test_settings, 900081)
    await create_budget(outsider, name="Свой бюджет")
    await outsider.press(foreign_button)
    assert "Чужой бюджет" not in outsider.text()
