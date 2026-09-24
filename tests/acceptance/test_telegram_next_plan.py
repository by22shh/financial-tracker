"""Next-period navigation and current remainder preview through callbacks."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_next_period_opens_next_plan_and_rollover_preview(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 998817)
    await create_budget(user, limits="Продукты 20000")
    await user.send("/budget")
    await user.press(user.button_data("Следующий период"))
    assert "План на" in user.text()
    assert "10 октября — 9 ноября" in user.text()
    await user.press(user.button_data("Перенести остатки"))
    assert "Перенос остатков" in user.text()
    await user.press(user.button_data("Продукты"))
    assert "Проверьте перенос" in user.text()
    assert "предварительная сумма" in user.text()
    assert not user.has_button("Подтвердить перенос")


async def test_next_plan_exposes_every_category_with_navigation(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 998818)
    names = [f"Категория {index:02d}" for index in range(1, 13)]
    await create_budget(user, categories=", ".join(names), limits=f"{names[-1]} = 300")
    await user.send("/budget")
    await user.press(user.button_data("Следующий период"))
    assert "Страница 1 из 2" in user.text()
    assert names[0] in user.text()
    assert names[-1] not in user.text()
    await user.press(user.button_data("Следующие категории"))
    assert "Страница 2 из 2" in user.text()
    assert names[-1] in user.text()
    assert "300" in user.text()
    assert not user.has_button("Следующие категории")
    await user.press(user.button_data("Предыдущие категории"))
    assert names[0] in user.text()
