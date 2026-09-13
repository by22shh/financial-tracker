"""Личные настройки участника и удаление аккаунта (CMD-26, CMD-30, CMD-31).

Личная настройка не меняет чужую доставку и не исключает участника из общей
аналитики (FR-54, FR-76).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.config import Settings
from fintracker.core.context import ActorContext, MembershipStatus, Role, WorkspaceState
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.logging import get_logger
from fintracker.db.models.access import (
    Membership,
    NotificationPreference,
    User,
    Workspace,
)
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

logger = get_logger("identity.preferences")

# Семейства уведомлений включаются и отключаются отдельно (FR-54).
NOTIFICATION_FAMILIES = (
    "shared_change",
    "threshold",
    "review",
    "reminder",
    "author_card",
)

DELIVERY_MODES = ("immediate", "digest", "off")


@dataclass(frozen=True, slots=True)
class PersonalPreferences:
    user_id: uuid.UUID
    workspace_id: uuid.UUID | None
    families: dict[str, str]
    quiet_hours_start: int
    quiet_hours_end: int
    timezone: str | None
    version: int

    def describe(self) -> str:
        lines = ["Мои уведомления:"]
        labels = {
            "shared_change": "Изменения участников",
            "threshold": "Пороги лимитов",
            "review": "Обзоры и анализ",
            "reminder": "Напоминания о платежах",
            "author_card": "Мои карточки записей",
        }
        for family in NOTIFICATION_FAMILIES:
            mode = self.families.get(family, "immediate")
            state = {
                "immediate": "сразу",
                "digest": "сводкой",
                "off": "выключено",
            }[mode]
            lines.append(f"• {labels[family]}: {state}")
        lines.append(
            f"Тихие часы: {self.quiet_hours_start}:00–{self.quiet_hours_end}:00"
            + (f" ({self.timezone})" if self.timezone else "")
        )
        lines.append(
            "Настройка личная: она не меняет доставку другим участникам и не "
            "скрывает ваши расходы из общей аналитики."
        )
        return "\n".join(lines)


async def get_preferences(
    session: AsyncSession, *, user_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> PersonalPreferences:
    row = (
        (
            await session.execute(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.workspace_id == workspace_id
                    if workspace_id is not None
                    else NotificationPreference.workspace_id.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return PersonalPreferences(
            user_id=user_id,
            workspace_id=workspace_id,
            families=dict.fromkeys(NOTIFICATION_FAMILIES, "immediate"),
            quiet_hours_start=22,
            quiet_hours_end=9,
            timezone=None,
            version=0,
        )
    families = {
        family: str(row.settings.get(family, "immediate")) for family in NOTIFICATION_FAMILIES
    }
    return PersonalPreferences(
        user_id=user_id,
        workspace_id=row.workspace_id,
        families=families,
        quiet_hours_start=row.quiet_hours_start,
        quiet_hours_end=row.quiet_hours_end,
        timezone=row.timezone,
        version=row.version,
    )


async def set_notification_family(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    family: str,
    mode: str,
    expected_version: int | None = None,
) -> PersonalPreferences:
    """Включить или отключить семейство уведомлений (FR-54, A65, A160)."""
    if family not in NOTIFICATION_FAMILIES:
        raise ValidationFailed(f"Неизвестное семейство уведомлений: {family}")
    if mode not in DELIVERY_MODES:
        raise ValidationFailed(f"Недопустимый режим доставки: {mode}")

    row = (
        (
            await session.execute(
                select(NotificationPreference)
                .where(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.workspace_id == workspace_id
                    if workspace_id is not None
                    else NotificationPreference.workspace_id.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = NotificationPreference(
            user_id=user_id,
            workspace_id=workspace_id,
            settings={family: mode},
        )
        session.add(row)
    else:
        if expected_version is not None and row.version != expected_version:
            raise ConflictError("Настройки изменились в другой сессии")
        settings_map = dict(row.settings)
        settings_map[family] = mode
        row.settings = settings_map
        row.version += 1
    await session.flush()
    return await get_preferences(session, user_id=user_id, workspace_id=workspace_id)


async def set_quiet_hours(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    start_hour: int,
    end_hour: int,
    timezone: str | None = None,
) -> PersonalPreferences:
    """Тихие часы в личном поясе получателя (FR-53, LIM-07)."""
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        raise ValidationFailed("Часы должны быть в диапазоне 0..23")
    if timezone is not None:
        from fintracker.core.calendar import validate_timezone

        validate_timezone(timezone)

    row = (
        (
            await session.execute(
                select(NotificationPreference)
                .where(
                    NotificationPreference.user_id == user_id,
                    NotificationPreference.workspace_id == workspace_id
                    if workspace_id is not None
                    else NotificationPreference.workspace_id.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = NotificationPreference(
            user_id=user_id,
            workspace_id=workspace_id,
            settings={},
            quiet_hours_start=start_hour,
            quiet_hours_end=end_hour,
            timezone=timezone,
        )
        session.add(row)
    else:
        row.quiet_hours_start = start_hour
        row.quiet_hours_end = end_hour
        if timezone is not None:
            row.timezone = timezone
        row.version += 1
    await session.flush()
    return await get_preferences(session, user_id=user_id, workspace_id=workspace_id)


async def set_input_preferences(
    session: AsyncSession,
    *,
    actor: ActorContext,
    autopost: bool | None = None,
    assume_self_spender: bool | None = None,
    large_amount_threshold_minor: int | None = None,
    expected_version: int | None = None,
) -> Membership:
    """Личный режим ввода: автозапись и порог крупной суммы (FR-19, CMD-26).

    Один участник не включает автозапись другому; порог задаётся явно, а не
    оценивается моделью.
    """
    workspace_id = actor.require_workspace()
    row = (
        await session.execute(
            select(Membership)
            .where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == actor.user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Членство недоступно")
    if expected_version is not None and row.version != expected_version:
        raise ConflictError("Настройки ввода изменились в другой сессии")
    if autopost is not None:
        row.autopost_enabled = autopost
    if assume_self_spender is not None:
        row.assume_self_spender = assume_self_spender
    if large_amount_threshold_minor is not None:
        if large_amount_threshold_minor <= 0:
            raise ValidationFailed("Порог крупной суммы должен быть положительным")
        row.large_amount_threshold_minor = large_amount_threshold_minor
    row.version += 1
    await session.flush()
    return row


async def update_workspace_settings(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    name: str | None = None,
    currency: str | None = None,
    expected_version: int | None = None,
) -> Workspace:
    """Общие настройки бюджета (CMD-30).

    Валюта меняется только в пустом бюджете: после денежных записей смена
    подписи валюты не допускается (R10, A21-валюта).
    """
    workspace_id = actor.require_workspace()
    if not actor.is_admin:
        from fintracker.core.errors import PermissionDenied

        raise PermissionDenied("Общие настройки меняет администратор бюджета")

    row = (
        await session.execute(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
    ).scalar_one()
    if expected_version is not None and row.version != expected_version:
        raise ConflictError("Настройки бюджета изменились в другой сессии")

    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise ValidationFailed("Название бюджета не может быть пустым")
        row.name = cleaned[:120]

    if currency is not None:
        from fintracker.core.money import normalize_currency
        from fintracker.db.models.ledger import Transaction

        normalized = normalize_currency(currency)
        if normalized != row.currency:
            has_money = (
                await session.execute(
                    select(Transaction.id).where(Transaction.workspace_id == workspace_id).limit(1)
                )
            ).scalar_one_or_none()
            if has_money is not None:
                raise ConflictError(
                    "В бюджете уже есть денежные записи: смена валюты подменила бы "
                    "их смысл. Создайте новый бюджет или проведите отдельную "
                    "согласованную процедуру конвертации."
                )
            row.currency = normalized

    row.version += 1
    await session.flush()
    await uow.bump_revisions(workspace_id, plan=True)
    return row


@dataclass(frozen=True, slots=True)
class AccountDeletionPreview:
    """Предпросмотр удаления личного аккаунта (CMD-31)."""

    user_id: uuid.UUID
    admin_workspaces: tuple[tuple[uuid.UUID, str], ...]
    member_workspaces: tuple[tuple[uuid.UUID, str], ...]

    @property
    def blocked(self) -> bool:
        return bool(self.admin_workspaces)


async def preview_account_deletion(
    settings: Settings, *, user_id: uuid.UUID
) -> AccountDeletionPreview:
    """Показать, что мешает удалить аккаунт (CMD-31).

    Сначала разрешаются административные роли, затем прекращаются членства.
    """
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        rows = (
            await session.execute(
                select(Membership.workspace_id, Membership.role, Workspace.name)
                .join(Workspace, Workspace.id == Membership.workspace_id)
                .where(
                    Membership.user_id == user_id,
                    Membership.status == MembershipStatus.ACTIVE.value,
                    Workspace.state == WorkspaceState.ACTIVE.value,
                )
            )
        ).all()
    admin = tuple((row[0], row[2]) for row in rows if row[1] == Role.ADMIN.value)
    member = tuple((row[0], row[2]) for row in rows if row[1] != Role.ADMIN.value)
    return AccountDeletionPreview(user_id=user_id, admin_workspaces=admin, member_workspaces=member)


async def delete_account(
    settings: Settings, *, user_id: uuid.UUID, correlation_id: str
) -> AccountDeletionPreview:
    """Возобновляемый отзыв всех членств и обезличивание (CMD-31, TZ §24).

    Совместные записи сохраняются с устойчивым авторским идентификатором;
    до завершения всех отзывов подтверждение полного удаления не выдаётся.
    """
    from fintracker.application.identity.membership import leave_workspace

    preview = await preview_account_deletion(settings, user_id=user_id)
    if preview.blocked:
        raise ConflictError(
            "Сначала передайте администрирование или удалите бюджеты, где вы администратор",
            details={"admin_workspaces": [str(item[0]) for item in preview.admin_workspaces]},
        )

    for workspace_id, _ in preview.member_workspaces:
        await leave_workspace(
            settings,
            workspace_id=workspace_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )

    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status="deleted", version=User.version + 1)
        )
    logger.info("account_deleted", user_id=str(user_id))
    return await preview_account_deletion(settings, user_id=user_id)
