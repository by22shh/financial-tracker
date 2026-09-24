"""Личные настройки, правила и исправление категории (FR-19, FR-24, FR-27, FR-54, CMD-26/27/31)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget, issue_invite_code
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_personal_settings_open_and_toggle(bot: None, test_settings: Settings) -> None:
    """CMD-26: уведомление меняется только после явного выбора режима."""
    user = make_user(test_settings, 907001)
    await create_budget(user)
    await user.send("/settings")
    assert user.has_button("Мои настройки")

    await user.press(user.button_data("Мои настройки"))
    text = user.text()
    assert "🔔 Уведомления" in text
    assert "✍️ Ввод операций" in text
    assert user.has_button("Правила")
    assert user.has_button("Аккаунт")
    assert not user.has_button("Удалить аккаунт")

    await user.press(user.button_data("Уведомления"))
    assert "Предупреждения о лимитах: сразу" in user.text()
    await user.press(user.button_data("Предупреждения о лимитах"))
    assert "Сейчас: сразу" in user.text()
    assert user.has_button("✓ Сразу")

    await user.press(user.button_data("Сводкой"))
    assert "Сейчас: сводкой" in user.text()
    assert user.has_button("✓ Сводкой")


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
    await admin.press(admin.button_data("Уведомления"))
    await admin.press(admin.button_data("Предупреждения о лимитах"))
    await admin.press(admin.button_data("Сводкой"))
    assert "Сейчас: сводкой" in admin.text()

    await member.press("set:personal")
    await member.press(member.button_data("Уведомления"))
    assert "Предупреждения о лимитах: сразу" in member.text()


async def test_input_preferences_toggle(bot: None, test_settings: Settings) -> None:
    """LIM-11, FR-19, CMD-26: автозапись и порог крупной суммы личные и явные."""
    user = make_user(test_settings, 907004)
    await create_budget(user)
    await user.press("set:personal")
    await user.press(user.button_data("Ввод операций"))
    assert "Автоматическое сохранение распознанных расходов: выключено" in user.text()

    await user.press(user.button_data("Автоматическое сохранение"))
    assert user.has_button("✓ Выключить")
    await user.press(user.button_data("Включить"))
    assert "Сейчас: Включить" in user.text()
    assert user.has_button("✓ Включить")

    await user.press(user.button_data("Ввод операций"))
    await user.press(user.button_data("Проверка крупных сумм"))
    assert user.has_button("✓ Порог не задан")
    await user.press(user.button_data("От 3"))
    assert "Сейчас:" in user.text()
    assert "3" in user.text()
    assert user.has_button("✓ От 3")

    await user.press(user.button_data("Порог не задан"))
    assert "Сейчас: не задан" in user.text()
    assert user.has_button("✓ Порог не задан")


async def test_quiet_hours_preset(bot: None, test_settings: Settings) -> None:
    """FR-53: тихие часы выбираются пресетом и видны в настройках."""
    user = make_user(test_settings, 907005)
    await create_budget(user)
    await user.press("set:personal")
    await user.press(user.button_data("Уведомления"))
    await user.press(user.button_data("Тихие часы"))
    assert user.has_button("✓ 22:00–09:00")
    await user.press(user.button_data("23:00–08:00"))
    assert "Сейчас: 23:00–08:00" in user.text()
    assert user.has_button("✓ 23:00–08:00")

    await user.press(user.button_data("Уведомления"))
    await user.press(user.button_data("Часовой пояс"))
    assert user.has_button("✓ Новосибирск")
    await user.press(user.button_data("Москва"))
    assert "Сейчас: Москва · UTC+3" in user.text()
    assert user.has_button("✓ Москва")

    await user.press(user.button_data("Уведомления"))
    await user.press(user.button_data("Тихие часы"))
    await user.press(user.button_data("22:00–09:00"))
    await user.press(user.button_data("Уведомления"))
    assert "Часовой пояс: Москва · UTC+3" in user.text()


async def test_settings_choices_are_explicit_versioned_and_fit_telegram(
    bot: None, test_settings: Settings
) -> None:
    """Старый выбор не перезаписывает новый, данные всех кнопок короче 64 байт."""
    user = make_user(test_settings, 907011)
    await create_budget(user)
    await user.press("set:personal")
    await user.press(user.button_data("Ввод операций"))
    await user.press(user.button_data("Кого считать плательщиком"))
    assert user.has_button("✓ Уточнять")
    await user.press(user.button_data("Всегда я"))
    assert "Сейчас: Всегда я" in user.text()
    assert user.has_button("✓ Всегда я")

    await user.press("set:notify")
    await user.press(user.button_data("Обзоры и анализ"))
    stale_off = user.button_data("Выключено")
    await user.press(user.button_data("Сводкой"))
    await user.press(stale_off)
    assert "изменились в другой сессии" in user.text()

    for reply in await user.press("set:personal"):
        for row in reply.buttons:
            for button in row:
                assert len(button.data.encode()) <= 64


async def test_category_correction_offers_rule(bot: None, test_settings: Settings) -> None:
    """FR-27, FR-24: перенос в другую статью и предложение запомнить правило."""
    user = make_user(test_settings, 907006)
    await create_budget(user)
    await user.send("шоколадка 250")
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))
    assert "250" in user.text()

    await user.send("Перенеси в Рестораны")
    proposal = user.text()
    assert "Рестораны" in proposal
    assert "Другие записи не изменятся" in proposal

    await user.press(user.button_data("Подтвердить"))
    applied = user.text()
    assert "Рестораны" in applied
    assert "Запомнить" in applied

    await user.press(user.button_data("Всегда сюда"))
    assert "Запомнил" in user.text()
    assert "для новых записей" in user.text()

    await user.press("set:rules")
    rules = user.text()
    assert "Правила категорий" in rules
    assert "Рестораны" in rules


async def test_rule_can_be_removed(bot: None, test_settings: Settings) -> None:
    """CMD-27: правило можно убрать, после чего оно не применяется."""
    user = make_user(test_settings, 907007)
    await create_budget(user)
    await user.send("шоколадка 250")
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))
    await user.send("Перенеси в Рестораны")
    await user.press(user.button_data("Подтвердить"))
    await user.press(user.button_data("Всегда сюда"))

    await user.press("set:rules")
    assert user.has_button("Убрать")
    await user.press(user.button_data("Убрать"))
    assert "Пока нет правил" in user.text()


async def test_account_deletion_blocked_for_admin(bot: None, test_settings: Settings) -> None:
    """CMD-31: удаление аккаунта блокируется, пока участник — администратор."""
    user = make_user(test_settings, 907008)
    await create_budget(user, name="Единственный бюджет")
    await user.press("set:personal")
    assert not user.has_button("Удалить")
    await user.press(user.button_data("Аккаунт"))
    await user.press(user.button_data("Перейти к удалению"))
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
    await member.press(member.button_data("Аккаунт"))
    await member.press(member.button_data("Перейти к удалению"))
    assert member.has_button("Подтвердить удаление")
    await member.press(member.button_data("Подтвердить удаление"))
    assert "Аккаунт удалён" in member.text()

    # Совместная запись остаётся в бюджете администратора.
    await admin.send("/report")
    assert "900" in admin.text()
