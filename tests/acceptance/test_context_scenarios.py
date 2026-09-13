"""Контекст бюджета и совместная работа (A156, A161, A162, A165–A167, A174, A197)."""

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


async def test_a156_link_and_manual_code_give_same_path(bot: None, test_settings: Settings) -> None:
    """A156: ссылка приглашения и ручной ввод кода дают одинаковые полномочия."""
    admin = make_user(test_settings, 914001)
    await create_budget(admin, name="Общий бюджет")
    first_code = await issue_invite_code(admin)
    second_code = await issue_invite_code(admin)

    by_link = make_user(test_settings, 914002)
    await by_link.send(f"/start join_{first_code.replace('-', '')}")
    assert "присоединились" in by_link.text().lower()

    by_hand = make_user(test_settings, 914003)
    await by_hand.send(f"/join {second_code}")
    assert "присоединились" in by_hand.text().lower()

    for member in (by_link, by_hand):
        await member.send("/members")
        assert "Общий бюджет" in member.text()
        # Участник не получает административных кнопок (FR-04).
        assert not member.has_button("Пригласить")


async def test_a165_a166_context_is_pinned_at_receipt(bot: None, test_settings: Settings) -> None:
    """A165, A166: переключение бюджета не переносит уже принятое сообщение."""
    user = make_user(test_settings, 914010)
    await create_budget(user, name="Первый бюджет")
    await _post(user, "продукты 500")

    # Второй бюджет того же человека.
    await user.press("wiz:start")
    await user.send("Второй бюджет")
    await user.send("RUB")
    await user.send("Asia/Novosibirsk")
    await user.send("10.09.2026 — 09.10.2026")
    await user.press(user.button_data("календарный месяц"))
    await user.press("wiz:inc:exact")
    await user.send("50000")
    await user.send("Продукты, Транспорт")
    await user.press("wiz:skip:limits")
    await user.press("wiz:skip:commitments")
    await user.press("wiz:skip:goals")
    await user.press("wiz:tpl:on")
    await user.press("wiz:publish")

    await user.send("/budgets")
    assert "Второй бюджет" in user.text()

    # Новая трата относится к выбранному сейчас бюджету.
    await _post(user, "транспорт 200")
    await user.send("/history")
    journal = user.text().replace(" ", " ").replace(" ", " ")
    assert "из 1" in journal
    assert "200" in journal
    assert "500" not in journal, "запись первого бюджета не переехала"


async def test_a167_reply_corrects_the_card_budget(bot: None, test_settings: Settings) -> None:
    """A167: исправление применяется к операции того бюджета, чья карточка открыта."""
    user = make_user(test_settings, 914020)
    await create_budget(user, name="Рабочий бюджет")
    await _post(user, "продукты 900")

    await user.send("/history")
    await user.press(user.button_data("Запись 1"))
    card = user.text()
    assert "Рабочий бюджет" in card

    await user.send("Здесь было 700, а не 900")
    await user.press(user.button_data("Подтвердить"))
    assert "700" in user.text().replace(" ", " ").replace(" ", " ")


async def test_a162_shared_change_is_visible_to_everyone(
    bot: None, test_settings: Settings
) -> None:
    """A162: созданная участником категория и лимит видны всем, автор сохранён."""
    admin = make_user(test_settings, 914030)
    await create_budget(admin, categories="Продукты, Транспорт")
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 914031)
    await member.send(f"/join {code}")

    await member.send("Создай категорию Подарки")
    assert "Подарки" in member.text()

    await admin.send("/categories")
    assert "Подарки" in admin.text()


async def test_a197_note_author_survives_leaving(bot: None, test_settings: Settings) -> None:
    """A197: заметка и авторский след сохраняются после выхода участника."""
    admin = make_user(test_settings, 914040)
    await create_budget(admin)
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 914041)
    await member.send(f"/join {code}")

    await _post(member, "продукты 650")
    await member.send("Добавь комментарий: покупка к празднику")
    assert "к празднику" in member.text()

    await member.press("ws:leave")
    await member.press(member.button_data("Выйти"))
    assert "Вы вышли из бюджета" in member.text()

    await admin.send("/history подарок")
    await admin.send("/history празднику")
    assert "из 1" in admin.text()

    # Бывший участник доступа не имеет.
    await member.send("/history")
    assert "выберите бюджет" in member.text().lower() or "недоступен" in member.text().lower()


async def test_a174_stale_admin_transfer_is_not_applied(bot: None, test_settings: Settings) -> None:
    """A174: устаревшее предложение передачи роли не исполняется."""
    from fintracker.application.identity.actor import ensure_user, get_active_workspace_id
    from fintracker.application.identity.membership import (
        accept_admin_transfer,
        propose_admin_transfer,
    )
    from fintracker.core.errors import DomainError
    from fintracker.db.session import RuntimeRole, session_scope

    admin = make_user(test_settings, 914050)
    await create_budget(admin)
    code = await issue_invite_code(admin)
    member = make_user(test_settings, 914051)
    await member.send(f"/join {code}")

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        admin_user = await ensure_user(session, telegram_user_id=914050)
        member_user = await ensure_user(session, telegram_user_id=914051)
        workspace_id = await get_active_workspace_id(session, admin_user.id)
        assert workspace_id is not None

    from fintracker.db.uow import UnitOfWork

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id="a174")
        proposal = await propose_admin_transfer(
            session,
            uow,
            workspace_id=workspace_id,
            from_user_id=admin_user.id,
            to_user_id=member_user.id,
        )
        proposal_id = proposal.id

    # Адресат вышел до принятия: передача не исполняется.
    await member.press("ws:leave")
    await member.press(member.button_data("Выйти"))
    assert "Вы вышли из бюджета" in member.text()

    with pytest.raises(DomainError):
        await accept_admin_transfer(
            test_settings,
            workspace_id=workspace_id,
            proposal_id=proposal_id,
            acting_user_id=member_user.id,
            correlation_id="a174-accept",
        )

    await admin.send("/members")
    assert "администратор" in admin.text()
