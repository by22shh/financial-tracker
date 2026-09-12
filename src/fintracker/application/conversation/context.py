"""Общий контекст диалога: бюджет события, актор и справочные выборки.

Выделено отдельным модулем, чтобы маршрутизатор, кнопки и разделы не
образовывали циклических зависимостей.
"""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.conversation.keyboards import start_menu
from fintracker.application.conversation.types import IncomingMessage, Reply
from fintracker.application.identity.actor import get_active_workspace_id, resolve_actor
from fintracker.application.planning.periods import period_for_date
from fintracker.application.planning.plan import PeriodStatus, period_status
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.db.models.access import Membership, Person, User, Workspace
from fintracker.db.models.catalog import Category
from fintracker.db.session import RuntimeRole, session_scope

HELP_TEXT = """Что можно сделать:
• Отправьте сообщение о трате: «кофе 250», «вчера бензин 3000».
• Голосовое сообщение или фото чека обрабатываются так же.
• /add — форма ручного ввода, работает без AI.
• /budget — обзор периода, /history — журнал, /categories — статьи.
• /join — войти в бюджет по коду, /budgets — переключить бюджет.
• /cancel — отменить текущий диалог или черновик.
Ответьте на карточку операции, чтобы исправить её или добавить комментарий."""


async def active_context(
    settings: Settings, *, user_id: uuid.UUID, message: IncomingMessage
) -> uuid.UUID | None:
    """Бюджет события: закреплённый при приёме либо текущий выбор (FR-79).

    Позднее переключение не переносит обрабатываемый чек или голос.
    """
    if message.workspace_id is not None:
        return message.workspace_id
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        return await get_active_workspace_id(session, user_id)


async def load_actor(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    correlation_id: str,
    require_admin: bool = False,
) -> tuple[ActorContext, Workspace]:
    """Проверить активное членство и получить контекст действия (ADR-06)."""
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        actor = await resolve_actor(
            session,
            user=user,
            workspace_id=workspace_id,
            correlation_id=correlation_id,
            require_admin=require_admin,
        )
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        session.expunge(workspace)
        return actor, workspace


def no_budget_reply() -> list[Reply]:
    """Без выбранного бюджета трата не записывается в случайный (FR-79)."""
    return [
        Reply(
            text=(
                "Сначала выберите бюджет: создайте свой или войдите по коду.\n"
                "Запись без выбранного бюджета не выполняется."
            ),
            buttons=start_menu(returning=True),
        )
    ]


async def current_status(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> PeriodStatus:
    """Статус текущего периода; календарь материализуется при чтении (FR-92)."""
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        period = await period_for_date(session, workspace_id=workspace_id, day=today)
        return await period_status(
            session,
            workspace_id=workspace_id,
            period_id=period.id,
            currency=workspace.currency,
            today=today,
        )


async def category_paths(session: AsyncSession, *, workspace_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """Полный путь категории для показа и передачи в AI (FR-21)."""
    rows = (
        await session.execute(
            select(Category.id, Category.name, Category.parent_id).where(
                Category.workspace_id == workspace_id
            )
        )
    ).all()
    names = {row[0]: row[1] for row in rows}
    parents = {row[0]: row[2] for row in rows}
    paths: dict[uuid.UUID, str] = {}
    for category_id in names:
        parts = [names[category_id]]
        cursor = parents.get(category_id)
        depth = 0
        while cursor is not None and depth < 4:
            parts.append(names.get(cursor, "?"))
            cursor = parents.get(cursor)
            depth += 1
        paths[category_id] = " / ".join(reversed(parts))
    return paths


async def author_names(session: AsyncSession, *, workspace_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = (
        await session.execute(
            select(Membership.user_id, Person.name)
            .outerjoin(
                Person,
                (Person.workspace_id == Membership.workspace_id)
                & (Person.id == Membership.person_id),
            )
            .where(Membership.workspace_id == workspace_id)
        )
    ).all()
    return {row[0]: row[1] for row in rows if row[1]}
