"""Личные настройки, правила и исправление категории (FR-19, FR-24, FR-27, FR-54, CMD-26/27/31)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget, issue_invite_code
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_personal_settings_open_and_toggle(bot: None, test_settings: Settings) -> None:
    """CMD-26: раздел «Мои настройки» открывается и переключает семейство."""
    user = make_user(test_settings, 907001)
    await create_budget(user)
    await user.send("/settings")
    assert user.has_button("Мои настройки")

    await user.press(user.button_data("Мои настройки"))
    text = user.text()
    assert "Мои уведомления" in text
    assert "Пороги лимитов: сразу" in text
    assert "не меняет доставку другим участникам" in text

    await user.press(user.button_data("Пороги лимитов"))
    assert "Пороги лимитов: сводкой" in user.text()
    await user.press(user.button_data("Пороги лимитов"))
    assert "Пороги лимитов: выключено" in user.text()


async def test_personal_settings_do_not_affect_other_member(
    bot: None, test_settings: Settings
) -> None:
    """FR-54, A65: личная настройка не меняет доставку другому участнику."""
    admin = make_user(test_settings, 907002)
    await create_budget(admin)
    code = await issue_invite_code(admin)

    member = make_user(test_settings, 907003)
    await member.send(f"/join {code}")

    await admin.press("set:personal")
    await admin.press(admin.button_data("Пороги лимитов"))
    assert "Пороги лимитов: сводкой" in admin.text()

    await member.press("set:personal")
    assert "Пороги лимитов: сразу" in member.text()


async def test_input_preferences_toggle(bot: None, test_settings: Settings) -> None:
    """LIM-11, FR-19, CMD-26: автозапись и порог крупной суммы личные и явные."""
    user = make_user(test_settings, 907004)
    await create_budget(user)
    await user.press("set:personal")
    assert "Автозапись уверенных разборов: выключена" in user.text()

    await user.press(user.button_data("Автозапись: включить"))
    assert "Автозапись уверенных разборов: включена" in user.text()

    await user.press(user.button_data("Порог 3"))
    threshold_line = next(
        line for line in user.text().splitlines() if "Порог подтверждения" in line
    )
    assert "не задан" not in threshold_line
    assert "3" in threshold_line


async def test_quiet_hours_preset(bot: None, test_settings: Settings) -> None:
    """FR-53: тихие часы выбираются пресетом и видны в настройках."""
    user = make_user(test_settings, 907005)
    await create_budget(user)
    await user.press("set:personal")
    await user.press(user.button_data("Тихие часы 23–8"))
    assert "Тихие часы: 23:00–8:00" in user.text()


async def test_category_correction_offers_rule(bot: None, test_settings: Settings) -> None:
    """FR-27, FR-24: перенос в другую статью и предложение запомнить правило."""
    user = make_user(test_settings, 907006)
    await create_budget(user)
    await user.send("кофе 250")
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))
    assert "250" in user.text()

    await user.send("Перенеси в Рестораны")
    proposal = user.text()
    assert "Рестораны" in proposal
    assert "Прошлые записи не переклассифицируются" in proposal

    await user.press(user.button_data("Подтвердить"))
    applied = user.text()
    assert "Рестораны" in applied
    assert "Всегда относить" in applied

    await user.press(user.button_data("Всегда сюда"))
    assert "Запомнил" in user.text()
    assert "только для новых записей" in user.text()

    await user.press("set:rules")
    rules = user.text()
    assert "Правила классификации" in rules
    assert "Рестораны" in rules


async def test_rule_can_be_removed(bot: None, test_settings: Settings) -> None:
    """CMD-27: правило можно убрать, после чего оно не применяется."""
    user = make_user(test_settings, 907007)
    await create_budget(user)
    await user.send("кофе 250")
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))
    await user.send("Перенеси в Рестораны")
    await user.press(user.button_data("Подтвердить"))
    await user.press(user.button_data("Всегда сюда"))

    await user.press("set:rules")
    assert user.has_button("Убрать")
    await user.press(user.button_data("Убрать"))
    assert "Правил пока нет" in user.text()


async def test_account_deletion_blocked_for_admin(bot: None, test_settings: Settings) -> None:
    """CMD-31: удаление аккаунта блокируется, пока участник — администратор."""
    user = make_user(test_settings, 907008)
    await create_budget(user, name="Единственный бюджет")
    await user.press("set:personal")
    await user.press(user.button_data("Удалить аккаунт"))
    text = user.text()
    assert "Единственный бюджет" in text
    assert "передайте администрирование" in text
    assert not user.has_button("Подтвердить удаление")


async def test_account_deletion_for_plain_member(bot: None, test_settings: Settings) -> None:
    """CMD-31: обычный участник удаляет аккаунт, совместные записи сохраняются."""
    admin = make_user(test_settings, 907009)
    await create_budget(admin)
    code = await issue_invite_code(admin)

    member = make_user(test_settings, 907010)
    await member.send(f"/join {code}")
    await member.send("продукты 900")
    if member.has_button("Записать"):
        await member.press(member.button_data("Записать"))

    await member.press("set:personal")
    await member.press(member.button_data("Удалить аккаунт"))
    assert member.has_button("Подтвердить удаление")
    await member.press(member.button_data("Подтвердить удаление"))
    assert "Аккаунт удалён" in member.text()

    # Совместная запись остаётся в бюджете администратора.
    await admin.send("/report")
    assert "900" in admin.text()
