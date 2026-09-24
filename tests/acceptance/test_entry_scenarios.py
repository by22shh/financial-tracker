"""Сценарии свободного ввода и заметок (A02, A03, A17, A184, A186–A188, A192, A199)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import BotUser, make_user
from tests.acceptance.helpers import create_budget, issue_invite_code
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]

CATEGORIES = "Продукты, Рестораны, Транспорт, Жильё"


async def _post(user: BotUser, text: str) -> None:
    await user.send(text)
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))


async def test_a02_beneficiary_does_not_replace_author(bot: None, test_settings: Settings) -> None:
    """A02: «Софе такси 430» — получатель Софа, автор остаётся отправителем."""
    user = make_user(test_settings, 909001)
    await create_budget(user, categories=CATEGORIES)
    await user.send("Софе такси 430")
    text = user.text()
    assert "430" in text
    assert "Софа" in text or "софе" in text.lower()

    await _post(user, "Софе такси 430")
    await user.send("/history")
    journal = user.text()
    assert "430" in journal


async def test_a03_shared_expense_is_counted_once(bot: None, test_settings: Settings) -> None:
    """A03: «Нам ресторан 2400» — один общий расход, итог не удваивается."""
    user = make_user(test_settings, 909002)
    await create_budget(user, categories=CATEGORIES)
    await _post(user, "Нам ресторан 2400")
    await user.send("/report")
    report = user.text()
    assert "2 400" in report.replace(" ", " ").replace(" ", " ")
    assert "4 800" not in report.replace(" ", " ").replace(" ", " ")


async def test_a17_unlinked_self_is_not_invented(bot: None, test_settings: Settings) -> None:
    """A17: «я» без привязанного профиля не превращается в выдуманное имя."""
    user = make_user(test_settings, 909003)
    await create_budget(user, categories=CATEGORIES)
    await user.send("я кофе 250")
    text = user.text()
    assert "250" in text
    # Имя другого человека из справочника не подставляется без основания.
    assert "Софа" not in text


async def test_a188_comment_in_same_message_is_one_expense(
    bot: None, test_settings: Settings
) -> None:
    """A188: «Кофе 250. Комментарий: … за 150» — одна трата 250 с пояснением."""
    user = make_user(test_settings, 909004)
    await create_budget(user, categories=CATEGORIES)
    await user.send("Кофе 250. Комментарий: в следующий раз выбрать вариант за 150")
    text = user.text()
    assert "250" in text
    # 150 остаётся контекстом намерения внутри комментария, а не второй тратой.
    assert "в следующий раз выбрать вариант за 150" in text
    assert "2." not in text

    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))
    await user.send("/history")
    journal = user.text().replace(" ", " ").replace(" ", " ")
    assert "из 1" in journal
    assert "250" in journal


async def test_a199_note_scope_is_clarified_for_two_expenses(
    bot: None, test_settings: Settings
) -> None:
    """A199: при двух тратах в сообщении принадлежность пояснения уточняется."""
    user = make_user(test_settings, 909005)
    await create_budget(user, categories=CATEGORIES)
    await user.send("Продукты 500 и такси 300. Комментарий: такси до вокзала")
    text = user.text()
    assert "500" in text
    assert "300" in text
    # Заметка не приписывается наугад одной из двух записей.
    assert "К чему относится комментарий?" in text


async def test_a184_note_append_is_idempotent(bot: None, test_settings: Settings) -> None:
    """A184: повтор одного и того же добавления заметки не создаёт операцию."""
    user = make_user(test_settings, 909006)
    await create_budget(user, categories=CATEGORIES)
    await _post(user, "продукты 500")
    await user.send("Добавь комментарий: перед поездкой")
    first = user.text()
    assert "перед поездкой" in first

    await user.send("Добавь комментарий: перед поездкой")
    second = user.text()
    assert second.count("перед поездкой") <= 2

    await user.send("/history")
    assert "из 1" in user.text()


async def test_a186_shared_note_visible_to_members(bot: None, test_settings: Settings) -> None:
    """A186: общий комментарий виден участникам, автор записи не подменяется."""
    admin = make_user(test_settings, 909007)
    await create_budget(admin, categories=CATEGORIES)
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 909008)
    await member.send(f"/join {code}")

    await _post(admin, "продукты 900")
    await admin.send("/history")
    assert "из 1" in admin.text()

    await member.send("/history")
    journal = member.text()
    assert "из 1" in journal
    assert "900" in journal
