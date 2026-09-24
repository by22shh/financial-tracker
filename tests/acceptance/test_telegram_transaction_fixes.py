"""Regression paths reproduced in Telegram UI audit TG-04/05/11/12."""

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_direct_edit_note_category_and_income(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 999901)
    await create_budget(user)
    await user.send("Продукты 800. Комментарий: исходная заметка")
    await user.press(user.button_data("Записать"))
    await user.press(user.button_data("Изменить"))
    assert all(user.has_button(field) for field in ("Сумма", "Дата", "Категория", "Комментарий"))
    await user.press(user.button_data("Комментарий"))
    await user.send("первая строка\nдополнение: оплатила Анна")
    assert "дополнение: оплатила Анна" in user.text()
    await user.press(user.button_data("Изменить"))
    await user.press(user.button_data("Сумма"))
    await user.send("неверный ввод")
    assert "Не понял сумму" in user.text()
    await user.send("0")
    assert "больше нуля" in user.text()
    await user.send("900")
    assert "Подтвердите" in user.text()
    await user.press(user.button_data("Подтвердить"))
    assert "900" in user.text()
    await user.press(user.button_data("Изменить"))
    await user.press(user.button_data("Дата"))
    await user.send("15.09.2026")
    await user.press(user.button_data("Подтвердить"))
    assert "15 сентября 2026" in user.text()
    await user.press(user.button_data("Изменить"))
    await user.press(user.button_data("Категория"))
    await user.press(user.button_data("Рестораны"))
    assert "Категория: Рестораны" in user.text()
    await user.send("Добавь комментарий: последняя строка")
    assert "дополнение: оплатила Анна" in user.text()
    assert "последняя строка" in user.text()
    await user.send("зарплата 10000 рестораны")
    await user.press(user.button_data("Записать"))
    assert "Доход записан" in user.text()
    assert "Категория: Рестораны" not in user.text()


async def test_refund_and_duplicate_confirmation(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 999902)
    await create_budget(user)
    await user.send("Продукты 800")
    await user.press(user.button_data("Записать"))
    await user.send("возврат за покупку 100")
    await user.press(user.button_data("Записать"))
    assert "За какую покупку" in user.text()
    await user.press(user.button_data("800"))
    assert "Возврат" in user.text()
    confirm = user.button_data("Записать")
    await user.press(confirm)
    assert "Возврат записан" in user.text()
    await user.press(confirm)
    assert "Возврат" in user.text()
    await user.send("/budget")
    assert "700" in user.text()
    await user.send("возврат за покупку 800")
    await user.press(user.button_data("Записать"))
    await user.press(user.button_data("800"))
    assert "больше, чем осталось вернуть" in user.text()
    assert not user.has_button("Записать")
    await user.press(user.button_data("Изменить сумму"))
    await user.send("ошибка ввода")
    await user.send("50")
    await user.press(user.button_data("Записать"))
    await user.press(user.button_data("Записать"))
    assert "Возврат" in user.text()
    await user.send("/budget")
    assert "650" in user.text()


async def test_transfer_creates_reference_accounts_and_posts_once(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 999903)
    await create_budget(user)
    await user.send("перевёл 5000 между своими счетами")
    await user.press(user.button_data("Записать"))
    assert "Счетов пока нет" in user.text()
    await user.press(user.button_data("Новый счёт"))
    await user.send("Карта")
    await user.press(user.button_data("Карта"))
    assert "На какой счёт" in user.text()
    await user.press(user.button_data("Новый счёт"))
    await user.send("Накопления")
    await user.press(user.button_data("Накопления"))
    assert "Карта → Накопления" in user.text()
    confirm = user.button_data("Записать")
    await user.press(confirm)
    assert "Перевод записан" in user.text()
    assert "Карта → Накопления" in user.text()
    await user.press(confirm)
    assert "Перевод" in user.text()
    await user.send("/history")
    assert "из 1" in user.text()
    assert "Перевод ·" in user.text()
    assert "Карта → Накопления" in user.text()
    history_buttons = [
        button
        for reply in user.last_replies
        for row in reply.buttons
        for button in row
        if button.data.startswith("tx:open:")
    ]
    assert len(history_buttons) == 1
    assert "Перевод" in history_buttons[0].text
    assert "Карта → Накопления" in history_buttons[0].text
    assert len(history_buttons[0].data.encode()) <= 64
    await user.send("/budget")
    assert "Учтённые расходы: 0" in user.text()
