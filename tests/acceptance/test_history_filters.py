"""Журнал в диалоге: фильтры, сортировка, страницы и карточка (FR-07)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import BotUser, make_user
from tests.acceptance.helpers import create_budget, issue_invite_code
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def _post(user: BotUser, text: str) -> None:
    await user.send(text)
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))


async def test_history_shows_filters_and_counts(bot: None, test_settings: Settings) -> None:
    """FR-07: журнал открывается с условиями и числом подходящих записей."""
    user = make_user(test_settings, 908001)
    await create_budget(user)
    await _post(user, "продукты 500")
    await _post(user, "рестораны 800")

    await user.send("/history")
    text = user.text()
    assert "Фильтры: без ограничений" in text
    assert "по дате операции" in text
    assert "из 2" in text
    assert user.has_button("Фильтры")


async def test_history_filter_toggle_narrows_journal(bot: None, test_settings: Settings) -> None:
    """FR-07: условие «с комментарием» сужает журнал и снимается повторно."""
    user = make_user(test_settings, 908002)
    await create_budget(user)
    await _post(user, "продукты 500")
    await _post(user, "рестораны 800")
    await user.send("/history")
    await user.press(user.button_data("Фильтры"))
    assert "Отметьте условия" in user.text()

    await user.press(user.button_data("С комментарием"))
    assert "✓ С комментарием" in "".join(
        button.text for reply in user.last_replies for row in reply.buttons for button in row
    )
    await user.press(user.button_data("Показать"))
    assert "Подходящих записей нет" in user.text()

    await user.press(user.button_data("Фильтры"))
    await user.press(user.button_data("С комментарием"))
    await user.press(user.button_data("Показать"))
    assert "из 2" in user.text()


async def test_history_sort_switches_to_recently_added(bot: None, test_settings: Settings) -> None:
    """FR-07: доступен порядок «последние добавленные»."""
    user = make_user(test_settings, 908003)
    await create_budget(user)
    await _post(user, "продукты 500")
    await user.send("/history")
    await user.press(user.button_data("Последние добавленные"))
    assert "по времени добавления" in user.text()


async def test_history_search_by_comment_text(bot: None, test_settings: Settings) -> None:
    """FR-07: поиск по тексту комментария."""
    user = make_user(test_settings, 908004)
    await create_budget(user)
    await _post(user, "продукты 500")
    await user.send("Добавь комментарий: подарок маме")
    assert "подарок" in user.text().lower()

    await user.send("/history подарок")
    text = user.text()
    assert "Поиск по комментарию" in text
    assert "из 1" in text

    await user.send("/history отсутствует")
    assert "Подходящих записей нет" in user.text()


async def test_history_is_shared_between_members(bot: None, test_settings: Settings) -> None:
    """FR-07: у всех активных участников один общий журнал бюджета."""
    admin = make_user(test_settings, 908005)
    await create_budget(admin)
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 908006)
    await member.send(f"/join {code}")
    await _post(member, "продукты 700")

    await admin.send("/history")
    assert "из 1" in admin.text()
    await member.send("/history")
    assert "из 1" in member.text()


async def test_transaction_card_shows_history_and_links(bot: None, test_settings: Settings) -> None:
    """FR-07: карточка открывает историю изменения и связанные записи."""
    user = make_user(test_settings, 908007)
    await create_budget(user)
    await _post(user, "продукты 1000")
    await user.send("Здесь было 800, а не 1000")
    await user.press(user.button_data("Подтвердить"))
    card = user.text()
    assert "История изменения" in card
    assert "исправлена" in card


async def test_history_paging_keeps_all_entries(bot: None, test_settings: Settings) -> None:
    """FR-07: постраничный показ доступен и не теряет записи."""
    user = make_user(test_settings, 908008)
    await create_budget(user)
    for index in range(10):
        await _post(user, f"продукты {100 + index}")
    await user.send("/history")
    assert "Записи 1–8 из 10" in user.text()
    await user.press(user.button_data("Ещё →"))
    assert "Записи 9–10 из 10" in user.text()
    await user.press(user.button_data("← Назад"))
    assert "Записи 1–8 из 10" in user.text()
