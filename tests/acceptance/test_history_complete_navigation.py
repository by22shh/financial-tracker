"""All visible history rows and category filters remain reachable through buttons."""

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import BotUser, make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


def buttons(user: BotUser):
    result = [b for reply in user.last_replies for row in reply.buttons for b in row]
    assert all(len(b.data.encode()) <= 64 for b in result)
    return result


async def post(user: BotUser, text: str):
    await user.send(text)
    await user.press(user.button_data("Записать"))
    assert "записан" in user.text().lower()


async def test_all_eight_visible_entries_open_and_paging_returns_to_them(
    bot: None, test_settings: Settings
):
    user = make_user(test_settings, 939101)
    await create_budget(user)
    for amount in range(101, 110):
        await post(user, f"Продукты {amount}")
    await user.send("/history")
    assert "Записи 1–8 из 9" in user.text()
    opening = [b for b in buttons(user) if b.data.startswith("tx:open:")]
    assert all("Продукты" in b.text and "₽" in b.text for b in opening)
    assert all(not b.text.startswith("Запись ") for b in opening)
    assert len({b.data for b in opening}) == 8
    assert all(
        len(row) <= 2
        for reply in user.last_replies
        for row in reply.buttons
        if any(b.data.startswith("tx:open:") for b in row)
    )
    next_page = user.button_data("Ещё →")
    for button in opening:
        await user.press(button.data)
        cards = [r for r in user.last_replies if r.transaction_id]
        assert len(cards) == 1
        assert cards[0].transaction_id.hex.startswith(button.data.split(":")[-1])
    await user.press(next_page)
    assert "Записи 9–9 из 9" in user.text()
    last = next(b for b in buttons(user) if b.data.startswith("tx:open:"))
    assert "Продукты" in last.text and "Продукты" in last.text
    back = user.button_data("← Назад")
    await user.press(last.data)
    assert any(r.transaction_id for r in user.last_replies)
    await user.press(back)
    assert [b.data for b in buttons(user) if b.data.startswith("tx:open:")] == [
        b.data for b in opening
    ]


async def test_twelve_category_filters_keep_search_sort_flags_selection_and_back(
    bot: None, test_settings: Settings
):
    user = make_user(test_settings, 939102)
    names = [f"Категория {letter}" for letter in "АБВГДЕЖЗИКЛ"] + ["Продукты"]
    await create_budget(user, categories=", ".join(names))
    await post(user, "Продукты 101. Комментарий: контекстовый поиск")
    await post(user, "Продукты 202")
    await post(user, "Категория Л 303. Комментарий: контекстовый поиск")
    await user.send("/history контекстовый")
    await user.press(user.button_data("Последние добавленные"))
    await user.press(user.button_data("Фильтры"))
    await user.press(user.button_data("Мои записи"))
    await user.press(user.button_data("С комментарием"))
    first = [b.text for b in buttons(user) if b.text.startswith("Категория:")]
    assert len(first) == 6
    await user.press(user.button_data("Категории →"))
    second = [b.text for b in buttons(user) if b.text.startswith("Категория:")]
    assert len(second) == 6
    assert {name.removeprefix("Категория: ") for name in first + second} == set(names)
    await user.press(user.button_data("← Категории"))
    assert [b.text for b in buttons(user) if b.text.startswith("Категория:")] == first
    await user.press(user.button_data("Категории →"))
    await user.press(user.button_data("Категория: Продукты"))
    assert "страница 2 из 2" in user.text()
    assert "Выбрана категория: Продукты" in user.text()
    await user.press(user.button_data("← Категории"))
    await user.press(user.button_data("Категории →"))
    assert user.has_button("✓ Категория: Продукты")
    await user.press(user.button_data("С отменёнными"))
    assert "страница 2 из 2" in user.text()
    assert user.has_button("✓ Мои записи")
    assert user.has_button("✓ С комментарием")
    buttons(user)
    await user.press(user.button_data("Показать"))
    assert "из 1" in user.text()
    assert "101" in user.text()
    assert "Поиск: «контекстовый»" in user.text()
    assert "по времени добавления" in user.text()
    await user.press(user.button_data("Фильтры"))
    await user.press(user.button_data("Снять категорию"))
    await user.press(user.button_data("Показать"))
    assert "из 2" in user.text()
