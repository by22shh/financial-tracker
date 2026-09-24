"""UX-06/10/11: compact plan, active context and complete goal navigation."""

from __future__ import annotations

import pytest

from fintracker.application.conversation.keyboards import MAX_CALLBACK_BYTES
from fintracker.config import Settings
from tests.acceptance.conftest import BotUser, make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def _create_goals(user: BotUser, count: int) -> None:
    for index in range(1, count + 1):
        await user.press("goal:new")
        await user.send(f"Цель {index:02d} = {index * 1000}")


@pytest.mark.parametrize("count", [7, 12])
async def test_all_goals_are_reachable_with_pagination(
    bot: None, test_settings: Settings, count: int
) -> None:
    user = make_user(test_settings, 933000 + count)
    await create_budget(user)
    await _create_goals(user, count)

    await user.send("/goals")
    assert "Страница 1 из 2" in user.text()
    assert user.has_button("Цель 01")
    assert not user.has_button("Цель 07")
    assert user.has_button("Следующие")

    await user.press(user.button_data("Следующие"))
    assert "Страница 2 из 2" in user.text()
    assert user.has_button(f"Цель {count:02d}")
    assert user.has_button("Предыдущие")
    assert not user.has_button("Следующие")
    assert all(
        len(button.data.encode()) <= MAX_CALLBACK_BYTES
        for reply in user.last_replies
        for row in reply.buttons
        for button in row
    )

    await user.press(user.button_data("Предыдущие"))
    assert "Страница 1 из 2" in user.text()
    assert user.has_button("Цель 01")


async def test_goal_actions_use_plain_financial_language(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 933020)
    await create_budget(user)
    await _create_goals(user, 1)
    await user.send("/goals")
    await user.press(user.button_data("Цель 01"))

    assert user.has_button("Отложить на цель")
    assert user.has_button("Потрачено")
    assert user.has_button("Вернуть в бюджет")
    assert (
        "резерв"
        not in " ".join(
            button.text for reply in user.last_replies for row in reply.buttons for button in row
        ).lower()
    )


async def test_start_and_main_menu_name_active_budget_and_period(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 933021)
    await create_budget(user, name="Семейный бюджет")

    await user.send("/start")
    text = user.text()
    assert "Бюджет: Семейный бюджет" in text
    assert "10 сентября — 9 октября" in text
    first_button = user.last_replies[0].buttons[0][0]
    assert first_button.text == "📒 Открыть бюджет"
    assert first_button.data == "menu:main"
    assert user.has_button("Создать бюджет")
    assert user.has_button("Войти по коду")

    await user.press(first_button.data)
    assert user.text().startswith("📒 Семейный бюджет")
    assert "Период: 10 сентября — 9 октября" in user.text()


async def test_next_plan_shows_shared_basis_once_per_page(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 933022)
    names = [f"Категория {index:02d}" for index in range(1, 13)]
    await create_budget(user, categories=", ".join(names), limits=f"{names[-1]} = 300")

    await user.send("/plan")
    assert user.text().count("🔁 ") == 1
    assert "  Основание:" not in user.text()
    assert names[0] in user.text()

    await user.press(user.button_data("Следующие категории"))
    assert user.text().count("🔁 ") == 1
    assert "  Основание:" not in user.text()
    assert names[-1] in user.text()
