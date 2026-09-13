"""Уточняющие вопросы и свободные ответы (AR-06, A06, R07)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_bare_amount_answers_the_single_open_question(
    bot: None, test_settings: Settings
) -> None:
    """CMD-10, AR-06: при одном открытом вопросе «500» отвечает именно на него."""
    user = make_user(test_settings, 913001)
    await create_budget(user)
    await user.send("Купил продукты")
    assert "сумма" in user.text().lower()

    await user.send("500")
    text = user.text()
    assert "500" in text.replace(" ", " ").replace(" ", " ")
    assert user.has_button("Записать")

    await user.press(user.button_data("Записать"))
    await user.send("/history")
    journal = user.text().replace(" ", " ").replace(" ", " ")
    assert "из 1" in journal
    assert "500" in journal


async def test_two_open_questions_require_explicit_choice(
    bot: None, test_settings: Settings
) -> None:
    """AR-06: при двух открытых вопросах ответ не попадает в чужой наугад."""
    user = make_user(test_settings, 913002)
    await create_budget(user)
    await user.send("Купил продукты")
    await user.send("Заправил машину")

    await user.send("500")
    text = user.text()
    assert "несколько вопросов" in text.lower()
    assert user.has_button("Это новая трата")

    await user.press(user.button_data("1."))
    assert "500" in user.text().replace(" ", " ").replace(" ", " ")
    assert user.has_button("Записать")


async def test_answer_to_closed_question_does_not_touch_other_draft(
    bot: None, test_settings: Settings
) -> None:
    """AR-06: закрытый вопрос не изменяет другой черновик."""
    user = make_user(test_settings, 913003)
    await create_budget(user)
    await user.send("Купил продукты")
    await user.send("500")
    await user.press(user.button_data("Записать"))

    await user.send("Заправил машину")
    assert "сумма" in user.text().lower()

    # Ответ на уже закрытый вопрос не переписывает новую запись.
    await user.send("700")
    assert "700" in user.text().replace(" ", " ").replace(" ", " ")
    await user.press(user.button_data("Записать"))

    await user.send("/history")
    journal = user.text().replace(" ", " ").replace(" ", " ")
    assert "из 2" in journal
    assert "500" in journal
    assert "700" in journal
