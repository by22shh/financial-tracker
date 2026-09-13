"""Категории и исправления (A115, A118, A120, A126, FR-21, FR-22, FR-33)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import BotUser, make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]

CATEGORIES = "Продукты, Рестораны, Транспорт"


async def _post(user: BotUser, text: str) -> None:
    await user.send(text)
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))


async def test_a115_create_category_and_move_record(bot: None, test_settings: Settings) -> None:
    """A115: создать статью и перенести в неё запись; общий расход не меняется."""
    user = make_user(test_settings, 911001)
    await create_budget(user, categories=CATEGORIES)
    await _post(user, "продукты 1200")
    await user.send("/report")
    before = user.text().replace(" ", " ").replace(" ", " ")
    assert "1 200" in before

    await user.send("Перенеси в Подарки")
    assert "Создать её и перенести сюда эту запись" in user.text()
    await user.press(user.button_data("Создать и перенести"))
    card = user.text()
    assert "Подарки" in card

    await user.send("/report")
    after = user.text().replace(" ", " ").replace(" ", " ")
    assert "1 200" in after
    assert "Подарки" in after


async def test_a118_archive_keeps_expenses_in_reports(bot: None, test_settings: Settings) -> None:
    """A118: архивная статья уходит из обычного выбора, расходы остаются в отчёте."""
    user = make_user(test_settings, 911002)
    await create_budget(user, categories=CATEGORIES)
    await _post(user, "рестораны 800")

    await user.press("cat:manage")
    await user.press(user.button_data("Рестораны"))
    assert "Связанных операций: 1" in user.text()
    await user.press(user.button_data("Убрать в архив"))
    assert "убрана в архив" in user.text()

    await user.send("/report")
    report = user.text().replace(" ", " ").replace(" ", " ")
    assert "800" in report

    await user.press("cat:manage")
    buttons = [
        button.text for reply in user.last_replies for row in reply.buttons for button in row
    ]
    assert "Рестораны" not in buttons


async def test_a120_reassign_shows_links_before_archive(bot: None, test_settings: Settings) -> None:
    """A120: перед переносом показаны связи, будущие записи не идут в архив."""
    user = make_user(test_settings, 911003)
    await create_budget(user, categories=CATEGORIES)
    await _post(user, "кофе 800")
    await user.send("Перенеси в Рестораны")
    if user.has_button("Подтвердить"):
        await user.press(user.button_data("Подтвердить"))
    if user.has_button("Всегда сюда"):
        await user.press(user.button_data("Всегда сюда"))

    await user.press("cat:manage")
    await user.press(user.button_data("Рестораны"))
    await user.press(user.button_data("Перенести и убрать"))
    text = user.text()
    assert "Куда перенести записи" in text
    assert "Правил классификации" in text

    await user.press(user.button_data("Продукты"))
    assert "Записи перенесены" in user.text()

    await user.send("/report")
    report = user.text().replace(" ", " ").replace(" ", " ")
    assert "800" in report
    assert "Продукты" in report


async def test_a126_conflicting_correction_is_reported(bot: None, test_settings: Settings) -> None:
    """A126: устаревшее подтверждение показывает конфликт, правка не теряется."""
    user = make_user(test_settings, 911004)
    await create_budget(user, categories=CATEGORIES)
    await _post(user, "продукты 1000")

    await user.send("Здесь было 900, а не 1000")
    stale = user.button_data("Подтвердить")

    await user.send("Здесь было 800, а не 1000")
    await user.press(user.button_data("Подтвердить"))
    assert "800" in user.text().replace(" ", " ").replace(" ", " ")

    await user.press(stale)
    conflict = user.text()
    assert "измен" in conflict.lower() or "конфликт" in conflict.lower()

    await user.send("/history")
    journal = user.text().replace(" ", " ").replace(" ", " ")
    assert "800" in journal
    assert "900" not in journal
