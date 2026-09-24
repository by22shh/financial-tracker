"""An edited error message must not end the amount-entry conversation."""

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_friendly_amount_error_keeps_goal_input_active(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 920001)
    await create_budget(user)
    await user.press("goal:new")
    await user.send("Отпуск = 10000")
    await user.send("/goals")
    await user.press(user.button_data("Отпуск"))
    await user.press(user.button_data("Отложить на цель"))
    await user.send("не число")
    assert user.last_replies[0].retry_input
    assert user.text().startswith("✍️")
    await user.send("500")
    assert "отложено" in user.text().lower()
    assert "500\xa0₽" in user.text()
    assert not user.has_button("Записать")


async def test_domain_rejection_keeps_goal_input_active(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 920002)
    await create_budget(user)
    await user.press("goal:new")
    await user.send("Отпуск = 10000")
    await user.send("/goals")
    await user.press(user.button_data("Отпуск"))
    await user.press(user.button_data("Отложить на цель"))
    await user.send("500")
    # После взноса сразу видна карточка цели с кнопками.
    await user.press(user.button_data("Потрачено"))
    await user.send("700")
    assert user.last_replies[0].retry_input
    await user.send("200")
    assert "использовано" in user.text().lower()
    assert not user.has_button("Записать")
