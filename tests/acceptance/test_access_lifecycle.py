"""Прекращение доступа: выход, исключение, передача роли, удаление (A168–A180)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget, issue_invite_code
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def _budget_with_member(test_settings: Settings, admin_id: int, member_id: int, **kwargs):
    admin = make_user(test_settings, admin_id)
    await create_budget(admin, **kwargs)
    code = await issue_invite_code(admin)
    member = make_user(test_settings, member_id)
    await member.send(f"/join {code}")
    return admin, member


async def test_a168_member_leaves_and_loses_access(bot: None, test_settings: Settings) -> None:
    """CMD-06, A168: после выхода доступ прекращён, общие записи участника сохранены."""
    admin, member = await _budget_with_member(test_settings, 920001, 920002, categories="Продукты")
    await member.send("продукты 500")
    await member.press(member.button_data("Записать"))
    assert "Записано 500,00 ₽" in member.text()

    await member.send("/members")
    await member.press(member.button_data("Выйти из бюджета"))
    assert "Выйти из бюджета" in member.text()
    await member.press(member.button_data("Выйти"))
    assert "вышли из бюджета" in member.text()

    # Доступ прекращён: бюджет больше не выбран и запись невозможна.
    await member.send("/budget")
    assert "Сначала выберите бюджет" in member.text()
    await member.send("кофе 250")
    assert "Сначала выберите бюджет" in member.text()

    # Общая история сохранила запись ушедшего участника.
    await admin.send("/budget")
    assert "Учтённые расходы: 500,00 ₽" in admin.text()


async def test_a169_old_buttons_do_not_restore_access(bot: None, test_settings: Settings) -> None:
    """A169: старые кнопки после выхода не дают доступа к данным."""
    _admin, member = await _budget_with_member(test_settings, 920010, 920011, categories="Продукты")
    await member.send("/budget")
    stale_button = member.button_data("Все категории")

    await member.send("/members")
    await member.press(member.button_data("Выйти из бюджета"))
    await member.press(member.button_data("Выйти"))

    await member.press(stale_button)
    assert "Сначала выберите бюджет" in member.text()


async def test_a172_admin_cannot_leave_without_transfer(bot: None, test_settings: Settings) -> None:
    """CMD-08, A172: администратору предложена передача роли или удаление."""
    admin, _member = await _budget_with_member(test_settings, 920020, 920021)
    await admin.send("/members")
    await admin.press(admin.button_data("Выйти из бюджета"))
    text = admin.text()
    assert "администратор" in text.lower()
    assert admin.has_button("Передать роль")
    assert admin.has_button("Удалить бюджет")


async def test_a173_admin_transfer_swaps_roles_atomically(
    bot: None, test_settings: Settings
) -> None:
    """A173: ровно один администратор после передачи; повтор ничего не меняет."""
    admin, member = await _budget_with_member(test_settings, 920030, 920031)
    await admin.send("/members")
    await admin.press(admin.button_data("Выйти из бюджета"))
    await admin.press(admin.button_data("Передать роль"))
    await admin.press(admin.button_data("Участник"))
    assert "Предложение отправлено" in admin.text()

    await member.press("ws:acceptadmin:x")
    assert "стали администратором" in member.text()

    await member.send("/members")
    body = member.text()
    assert body.count("администратор") == 1
    assert member.has_button("Пригласить"), "новый администратор управляет кодами"

    # Повторное принятие не меняет роли снова.
    await member.press("ws:acceptadmin:x")
    assert "Активного предложения передачи нет" in member.text()

    await admin.send("/members")
    assert not admin.has_button("Пригласить"), "прежний администратор стал участником"


async def test_a170_removed_member_cannot_rejoin_by_code(
    bot: None, test_settings: Settings
) -> None:
    """A170: исключённый не входит по общему коду до разрешения администратора."""
    from sqlalchemy import select

    from fintracker.application.identity.membership import allow_rejoin, remove_member
    from fintracker.db.models.access import User, Workspace
    from fintracker.db.session import RuntimeRole, session_scope

    admin, member = await _budget_with_member(test_settings, 920040, 920041)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        workspace_id = (
            (await session.execute(select(Workspace.id).where(Workspace.state == "active")))
            .scalars()
            .first()
        )
        admin_id = (
            await session.execute(select(User.id).where(User.telegram_user_id == 920040))
        ).scalar_one()
        member_id = (
            await session.execute(select(User.id).where(User.telegram_user_id == 920041))
        ).scalar_one()

    await remove_member(
        test_settings,
        workspace_id=workspace_id,
        admin_user_id=admin_id,
        target_user_id=member_id,
        correlation_id="test-remove",
    )
    await member.send("/budget")
    assert "Сначала выберите бюджет" in member.text()

    code = await issue_invite_code(admin)
    await member.send(f"/join {code}")
    assert "закрыт" in member.text().lower()

    await allow_rejoin(
        test_settings,
        workspace_id=workspace_id,
        admin_user_id=admin_id,
        target_user_id=member_id,
        correlation_id="test-allow",
    )
    new_code = await issue_invite_code(admin)
    await member.send(f"/join {new_code}")
    assert "присоединились к бюджету" in member.text()


async def test_a171_rejoin_gets_member_role_and_new_generation(
    bot: None, test_settings: Settings
) -> None:
    """A171: повторное вступление даёт роль участника и новое поколение."""
    from sqlalchemy import select

    from fintracker.db.models.access import Membership, User
    from fintracker.db.session import RuntimeRole, session_scope

    admin, member = await _budget_with_member(test_settings, 920050, 920051)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        member_id = (
            await session.execute(select(User.id).where(User.telegram_user_id == 920051))
        ).scalar_one()
        first_generation = (
            await session.execute(
                select(Membership.generation).where(Membership.user_id == member_id)
            )
        ).scalar_one()

    await member.send("/members")
    await member.press(member.button_data("Выйти из бюджета"))
    await member.press(member.button_data("Выйти"))

    code = await issue_invite_code(admin)
    await member.send(f"/join {code}")
    assert "присоединились" in member.text()

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        membership = (
            await session.execute(select(Membership).where(Membership.user_id == member_id))
        ).scalar_one()
    assert membership.role == "member"
    assert membership.generation != first_generation, "новое поколение членства"


async def test_a176_a177_delete_closes_budget_and_spares_other(
    bot: None, test_settings: Settings
) -> None:
    """A176/A177: удаление закрывает один бюджет и не затрагивает второй."""
    admin = make_user(test_settings, 920060)
    await create_budget(admin, name="Первый бюджет", categories="Продукты")
    await admin.send("продукты 300")
    await admin.press(admin.button_data("Записать"))

    await create_budget(admin, name="Второй бюджет", categories="Транспорт")
    await admin.send("/budgets")
    first_button = admin.button_data("Первый бюджет")
    await admin.press(first_button)
    assert "Первый бюджет" in admin.text()

    await admin.send("/settings")
    await admin.press(admin.button_data("Удалить бюджет"))
    assert "Удалить бюджет «Первый бюджет»" in admin.text()
    assert "операций 1" in admin.text()

    await admin.send("удалить Первый бюджет")
    assert "удалён" in admin.text()

    # Удалённый бюджет закрыт для доступа.
    await admin.send("/budget")
    assert "Сначала выберите бюджет" in admin.text() or "недоступен" in admin.text()

    # Второй бюджет не затронут.
    await admin.send("/budgets")
    assert "Второй бюджет" in admin.text()
    assert "Первый бюджет" not in admin.text()


async def test_a175_member_cannot_delete_budget(bot: None, test_settings: Settings) -> None:
    """A175: участник не может удалить бюджет ни кнопкой, ни текстом."""
    admin, member = await _budget_with_member(test_settings, 920070, 920071, name="Общий бюджет")
    await member.press("ws:delete")
    assert "только администратор" in member.text().lower()

    await member.send("удалить Общий бюджет")
    assert "только администратор" in member.text().lower()

    await admin.send("/budget")
    assert "Общий бюджет" in admin.text(), "бюджет остался активен"


async def test_a157_joining_does_not_import_personal_data(
    bot: None, test_settings: Settings
) -> None:
    """A157: присоединение не переносит категории и доход из своего бюджета."""
    admin = make_user(test_settings, 920080)
    await create_budget(admin, name="Общий", categories="Продукты, Рестораны")
    code = await issue_invite_code(admin)

    member = make_user(test_settings, 920081)
    await create_budget(member, name="Личный", categories="Хобби, Книги", income="50000")
    await member.send(f"/join {code}")
    await member.send("/categories")
    body = member.text()
    assert "Продукты" in body or "Рестораны" in body
    assert "Хобби" not in body, "личные категории не перенесены"

    # Второй бюджет администратора недоступен участнику.
    await admin.send("/budgets")
    assert "Общий" in admin.text()
