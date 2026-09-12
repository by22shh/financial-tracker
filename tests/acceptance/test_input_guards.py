"""Защитные правила ввода: вопросы, гипотезы, ограничения носителей (A09–A21, A33)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_a09_hypothetical_does_not_create_expense(bot: None, test_settings: Settings) -> None:
    """A09: «если завтра потрачу 3000» не проводит ни одной операции."""
    user = make_user(test_settings, 910001)
    await create_budget(user, categories="Рестораны, Продукты")
    await user.send("Если завтра потрачу 3000 на ресторан, что останется?")
    assert "расход не записан" in user.text()
    await user.send("/budget")
    assert "Учтённые расходы: 0,00 ₽" in user.text()


async def test_a10_negated_purchase_is_not_recorded(bot: None, test_settings: Settings) -> None:
    """A10: «Хотел купить за 5000, но передумал» — покупка не записана."""
    user = make_user(test_settings, 910002)
    await create_budget(user)
    await user.send("Хотел купить за 5000, но передумал")
    assert "не состоялась" in user.text()
    await user.send("/budget")
    assert "Учтённые расходы: 0,00 ₽" in user.text()


async def test_a11_question_returns_report_not_expense(bot: None, test_settings: Settings) -> None:
    """A11: вопрос о тратах даёт ответ с явными датами и не создаёт расход."""
    user = make_user(test_settings, 910003)
    await create_budget(user)
    await user.send("Сколько потрачено за месяц?")
    text = user.text()
    assert "Период:" in text
    assert "10 сентября — 9 октября" in text
    await user.send("/budget")
    assert "Учтённые расходы: 0,00 ₽" in user.text()


async def test_a12_limit_change_requires_confirmation(bot: None, test_settings: Settings) -> None:
    """A12: «Поставь лимит 8000» не меняет план без подтверждения."""
    user = make_user(test_settings, 910004)
    await create_budget(user, categories="Рестораны", limits="Рестораны = 5000")
    await user.send("Поставь лимит на рестораны 8000")
    assert "подтверждения" in user.text()
    await user.send("/categories")
    assert "5 000,00 ₽" in user.text(), "старый лимит сохранён"


async def test_a15_autopost_disabled_shows_confirmation(bot: None, test_settings: Settings) -> None:
    """A15: при выключенной автозаписи даже однозначный текст подтверждается."""
    user = make_user(test_settings, 910005)
    await create_budget(user, categories="Продукты")
    await user.send("продукты 250")
    assert "Проверьте запись перед сохранением" in user.text()
    assert user.has_button("Записать")


async def test_a06_missing_amount_asks_without_inventing(
    bot: None, test_settings: Settings
) -> None:
    """A06: без суммы бот запрашивает её и сохраняет распознанное описание."""
    user = make_user(test_settings, 910006)
    await create_budget(user, categories="Продукты")
    await user.send("Купил продукты")
    text = user.text()
    assert "сумма" in text.lower()
    assert "сумма неизвестна" in text


async def test_a04_two_expenses_create_batch(bot: None, test_settings: Settings) -> None:
    """A04: два расхода в одном сообщении дают пакет с разными датами."""
    user = make_user(test_settings, 910007)
    await create_budget(user, categories="Транспорт, Продукты")
    await user.send("Вчера бензин 3000, сегодня продукты 1800")
    text = user.text()
    assert "1." in text and "2." in text
    assert "3 000,00 ₽" in text
    assert "1 800,00 ₽" in text
    await user.press(user.button_data("Записать"))
    assert "Записано операций: 2" in user.text()
    await user.send("/budget")
    assert "Учтённые расходы: 4 800,00 ₽" in user.text()


async def test_a05_incomplete_batch_stays_draft(bot: None, test_settings: Settings) -> None:
    """A05: пакет с недостающей суммой остаётся черновиком целиком."""
    user = make_user(test_settings, 910008)
    await create_budget(user, categories="Транспорт, Продукты")
    await user.send("Купил продукты, вчера бензин 3000")
    await user.send("/budget")
    assert "Учтённые расходы: 0,00 ₽" in user.text(), "нет частичного сохранения"


async def test_a21_long_voice_rejected_before_paid_processing(
    bot: None, test_settings: Settings
) -> None:
    """A21: запись длиннее продуктового лимита отклоняется до платной обработки."""
    user = make_user(test_settings, 910009)
    await create_budget(user)
    await user.send_voice(duration_seconds=400)
    assert "не обрабатывается" in user.text()
    assert "текстом" in user.text()


async def test_a33_oversized_file_is_refused_safely(bot: None, test_settings: Settings) -> None:
    """A33: файл больше 15 MB отклоняется без бесконечной задачи."""
    user = make_user(test_settings, 910010)
    await create_budget(user)
    await user.send_photo(size_bytes=20 * 1024 * 1024)
    assert "больше допустимых 15 MB" in user.text()


async def test_voice_without_asr_keeps_draft_not_zero_expense(
    bot: None, test_settings: Settings
) -> None:
    """FR-13: недоступность ASR не считается нулевым расходом."""
    user = make_user(test_settings, 910011)
    await create_budget(user)
    await user.send_voice(duration_seconds=8)
    text = user.text()
    assert "не подключено" in text or "Голос сохранён" in text
    await user.send("/budget")
    assert "Учтённые расходы: 0,00 ₽" in user.text()


async def test_photo_without_ai_degrades_to_manual(bot: None, test_settings: Settings) -> None:
    """A103/NFR-14: при недоступном AI учёт продолжает работать через форму."""
    user = make_user(test_settings, 910012)
    await create_budget(user, categories="Продукты")
    await user.send_photo(caption="чек из магазина")
    assert "учёт продолжает работать" in user.text()
    assert user.has_button("Ручной ввод")

    await user.send("1200 | Продукты | сегодня | ужин")
    assert "Записано 1 200,00 ₽" in user.text()


async def test_no_budget_selected_blocks_recording(bot: None, test_settings: Settings) -> None:
    """FR-79: без выбранного бюджета трата не записывается в случайный."""
    user = make_user(test_settings, 910013)
    await user.send("/start")
    await user.send("кофе 250")
    assert "Сначала выберите бюджет" in user.text()
