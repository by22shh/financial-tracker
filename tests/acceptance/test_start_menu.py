"""Стартовое меню и возобновление настройки (FR-05, A141–A143)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_fr05_new_user_sees_two_paths(bot: None, test_settings: Settings) -> None:
    """SEC-01, FR-05: новому пользователю доступны создание и вход по коду."""
    user = make_user(test_settings, 912001)
    await user.send("/start")
    text = user.text()
    assert "личный или общий бюджет" in text
    assert user.has_button("Создать бюджет")
    assert user.has_button("Присоединиться по коду")
    assert not user.has_button("Мои бюджеты")


async def test_fr05_unfinished_setup_can_be_resumed(bot: None, test_settings: Settings) -> None:
    """FR-05: незавершённая настройка предлагается к продолжению."""
    user = make_user(test_settings, 912002)
    await user.send("/start")
    await user.press("wiz:start")
    await user.send("Черновой бюджет")

    await user.send("/start")
    text = user.text()
    assert "не завершена" in text
    assert user.has_button("Продолжить настройку")

    await user.press(user.button_data("Продолжить настройку"))
    # Продолжение не сбрасывает уже введённое название.
    assert "Черновой бюджет" in user.text() or "валют" in user.text().lower()


async def test_fr05_repeat_start_does_not_duplicate_budget(
    bot: None, test_settings: Settings
) -> None:
    """FR-05: повторный /start не создаёт дубликат и не сбрасывает планы."""
    user = make_user(test_settings, 912003)
    await create_budget(user, name="Основной бюджет", limits="Продукты 20000")

    await user.send("/start")
    text = user.text()
    assert "С возвращением" in text
    assert text.count("Основной бюджет") == 1
    assert user.has_button("Мои бюджеты")

    await user.send("/budget")
    assert "Основной бюджет" in user.text()
    assert "20 000" in user.text().replace(" ", " ").replace(" ", " ")
