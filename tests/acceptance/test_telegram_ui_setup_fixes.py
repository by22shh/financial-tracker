"""Regression cases reproduced in Telegram on 20 September (TG-01/02/06)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_new_setup_requires_explicit_choice_and_cancel_stops_input(
    bot: None,
    test_settings: Settings,
) -> None:
    user = make_user(test_settings, 921001)
    await user.send("/start")
    await user.press(user.button_data("Создать бюджет"))
    await user.send("Старый черновик")
    await user.send("/start")
    resume = user.button_data("Продолжить настройку")
    new = user.button_data("Создать бюджет")
    assert resume != new
    await user.press(new)
    assert "незавершённая настройка" in user.text()
    restart = user.button_data("Начать заново")
    await user.press(user.button_data("Продолжить настройку"))
    assert "Валюта бюджета" in user.text()
    await user.press(restart)
    assert "Как его назвать" in user.text()
    await user.send("Новый черновик")
    await user.press(restart)
    assert "уже изменилась" in user.text()
    await user.send("/cancel")
    assert "Настройка бюджета отменена" in user.text()
    await user.press("wiz:cur:RUB")
    assert "завершена или отменена" in user.text()
    await user.send("/start")
    assert not user.has_button("Продолжить настройку")
    await user.press(user.button_data("Создать бюджет"))
    assert "Как его назвать" in user.text()


async def test_cancel_setup_preserves_published_budget(
    bot: None,
    test_settings: Settings,
) -> None:
    user = make_user(test_settings, 921002)
    await create_budget(user, name="Основной", categories="Еда", limits="Еда 1000")
    await user.send("/start")
    await user.press(user.button_data("Создать бюджет"))
    await user.send("Временный")
    await user.send("/cancel")
    await user.send("/budget")
    assert "Основной" in user.text()
    assert "1 000" in user.text().replace("\xa0", " ")
    await user.send("/start")
    assert "Временный" not in user.text()


async def test_category_management_reassignment_and_archive_are_paginated(
    bot: None,
    test_settings: Settings,
) -> None:
    user = make_user(test_settings, 921003)
    names = [f"Статья {i:02d}" for i in range(12)]
    await create_budget(user, categories=", ".join(names))
    await user.press("cat:manage")
    first = [
        b.text
        for r in user.last_replies
        for row in r.buttons
        for b in row
        if b.text.startswith("Статья")
    ]
    assert len(first) == 8
    await user.press(user.button_data("Далее"))
    second = [
        b.text
        for r in user.last_replies
        for row in r.buttons
        for b in row
        if b.text.startswith("Статья")
    ]
    assert len(second) == 4 and set(first + second) == set(names)
    await user.press(user.button_data(second[-1]))
    assert second[-1] in user.text()
    # У пустой категории нет записей для переноса: экран выбора открывается напрямую.
    await user.press(user.button_data("Задать лимит").replace("cat:limit:", "cat:move:"))
    targets = [
        b.text
        for r in user.last_replies
        for row in r.buttons
        for b in row
        if b.text.startswith("Статья")
    ]
    await user.press(user.button_data("Далее"))
    targets += [
        b.text
        for r in user.last_replies
        for row in r.buttons
        for b in row
        if b.text.startswith("Статья")
    ]
    assert len(targets) == 11 and second[-1] not in targets
    for name in names:
        await user.press("cat:manage")
        if not user.has_button(name):
            await user.press(user.button_data("Далее"))
        await user.press(user.button_data(name))
        await user.press(user.button_data("Убрать в архив"))
    await user.press("cat:archive")
    assert user.has_button("Далее")
    await user.press(user.button_data("Далее"))
    assert user.has_button(f"Вернуть {names[-1]}")
    await user.press(user.button_data(f"Вернуть {names[-1]}"))
    await user.press("cat:manage")
    assert user.has_button(names[-1])


async def test_invalid_planned_date_keeps_commitment_step(
    bot: None,
    test_settings: Settings,
) -> None:
    user = make_user(test_settings, 921004)
    await user.send("/start")
    await user.press("wiz:start")
    for answer in ("Тест даты", "RUB", "Asia/Novosibirsk", "10.09.2026 — 09.10.2026"):
        await user.send(answer)
    await user.press(user.button_data("календарный месяц"))
    await user.press("wiz:inc:later")
    await user.send("Еда")
    await user.press("wiz:skip:limits")
    await user.send("Интернет = 900 = 31.02")
    assert "Не удалось разобрать дату" in user.text()
    await user.send("Интернет = 900 = 25.09")
    assert "На что будем копить" in user.text()
