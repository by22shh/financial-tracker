"""Коды приглашений и вход по коду (FR-77, FR-78, CMD-04, CMD-05).

Секрет хранится как HMAC-SHA-256 и не попадает в AI, аналитику и логи.
Параметры по умолчанию: 12 символов, 7 дней, 10 применений (LIM-01).
Приглашение — изменяемый секрет, отдельный от постоянного ID бюджета.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.config import Settings
from fintracker.core.context import MembershipStatus, Role, WorkspaceState
from fintracker.core.errors import (
    ConflictError,
    NotFound,
    PermissionDenied,
    RateLimited,
    ValidationFailed,
)
from fintracker.core.ids import (
    format_invite_code,
    generate_invite_code,
    invite_digest,
    new_generation,
    normalize_invite_code,
    short_id,
)
from fintracker.core.logging import get_logger
from fintracker.db.models.access import (
    BudgetInvite,
    InviteAttempt,
    Membership,
    MembershipHistory,
    User,
    Workspace,
)
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

logger = get_logger("identity.invites")


@dataclass(frozen=True, slots=True)
class IssuedInvite:
    invite_id: uuid.UUID
    code: str
    formatted_code: str
    expires_at: dt.datetime
    max_uses: int
    deep_link: str | None


@dataclass(frozen=True, slots=True)
class InvitePreview:
    """До подтверждения показываются только название и правила доступа (FR-78)."""

    workspace_id: uuid.UUID
    workspace_name: str
    short_id: str
    role: Role
    already_member: bool
    member_count: int


async def issue_invite(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    settings: Settings,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID,
    ttl_days: int | None = None,
    max_uses: int | None = None,
) -> IssuedInvite:
    """Выпустить код приглашения. Доступно только администратору (FR-77, A150)."""
    membership = (
        await session.execute(
            select(Membership).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == created_by,
                Membership.status == MembershipStatus.ACTIVE.value,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise NotFound("Бюджет недоступен")
    if membership.role != Role.ADMIN.value:
        raise PermissionDenied("Создавать коды приглашений может только администратор")

    days = ttl_days if ttl_days is not None else settings.limits.invite_default_ttl_days
    uses = max_uses if max_uses is not None else settings.limits.invite_default_max_uses
    if days < 1 or days > 90:
        raise ValidationFailed("Срок действия кода должен быть от 1 до 90 дней")
    if uses < 1 or uses > 100:
        raise ValidationFailed("Число применений кода должно быть от 1 до 100")

    key = settings.secrets.invite_hmac_key.get_secret_value()
    key_version = settings.secrets.invite_hmac_key_version
    for _ in range(5):
        code = generate_invite_code()
        digest = invite_digest(code, key, key_version=key_version)
        collision = (
            await session.execute(
                select(BudgetInvite.id).where(BudgetInvite.secret_digest == digest)
            )
        ).scalar_one_or_none()
        if collision is None:
            break
    else:  # pragma: no cover - вероятность ничтожна, но проверяется явно
        raise ConflictError("Не удалось выпустить уникальный код, повторите попытку")

    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
    row = BudgetInvite(
        workspace_id=workspace_id,
        secret_digest=digest,
        digest_key_version=key_version,
        role=Role.MEMBER.value,
        expires_at=expires_at,
        max_uses=uses,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await uow.bump_revisions(workspace_id, acl=False)

    deep_link = None
    if settings.telegram.webhook_base_url or settings.telegram.configured:
        deep_link = f"https://t.me/<bot_username>?start=join_{code}"
    # Код не логируется: в журнал попадает только идентификатор приглашения.
    logger.info("invite_issued", invite_id=str(row.id), workspace_id=str(workspace_id))
    return IssuedInvite(
        invite_id=row.id,
        code=code,
        formatted_code=format_invite_code(code),
        expires_at=expires_at,
        max_uses=uses,
        deep_link=deep_link,
    )


async def revoke_invite(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    invite_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    """Отзыв закрывает новые входы и не исключает уже принятых людей (FR-77)."""
    membership = (
        await session.execute(
            select(Membership).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == actor_user_id,
                Membership.status == MembershipStatus.ACTIVE.value,
                Membership.role == Role.ADMIN.value,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise PermissionDenied("Отзывать коды может только администратор бюджета")
    result = await session.execute(
        update(BudgetInvite)
        .where(
            BudgetInvite.workspace_id == workspace_id,
            BudgetInvite.id == invite_id,
            BudgetInvite.revoked_at.is_(None),
        )
        .values(revoked_at=func.now(), version=BudgetInvite.version + 1)
        .returning(BudgetInvite.id)
    )
    if result.scalar_one_or_none() is None:
        raise NotFound("Приглашение недоступно или уже отозвано")


async def _check_attempt_limit(
    session: AsyncSession, settings: Settings, *, telegram_user_id: int
) -> None:
    """Стартовый предел пять неуспешных вводов за 15 минут (FR-78, LIM-02)."""
    window_start = dt.datetime.now(dt.UTC) - dt.timedelta(
        minutes=settings.limits.invite_attempt_window_minutes
    )
    failures = (
        await session.execute(
            select(func.count())
            .select_from(InviteAttempt)
            .where(
                InviteAttempt.telegram_user_id == telegram_user_id,
                InviteAttempt.succeeded.is_(False),
                InviteAttempt.attempted_at >= window_start,
            )
        )
    ).scalar_one()
    if int(failures) >= settings.limits.invite_attempts_per_window:
        raise RateLimited(
            "Слишком много неудачных попыток. Попробуйте позже и проверьте код у администратора."
        )


async def _record_attempt(
    session: AsyncSession, *, telegram_user_id: int, user_id: uuid.UUID | None, succeeded: bool
) -> None:
    session.add(
        InviteAttempt(telegram_user_id=telegram_user_id, user_id=user_id, succeeded=succeeded)
    )


def digest_for(settings: Settings, raw_code: str) -> str:
    """Проверочное значение кода: открытый секрет не сохраняется (SEC-04)."""
    return invite_digest(
        normalize_invite_code(raw_code),
        settings.secrets.invite_hmac_key.get_secret_value(),
        key_version=settings.secrets.invite_hmac_key_version,
    )


async def preview_invite(
    settings: Settings,
    *,
    user: User,
    raw_code: str | None = None,
    code_digest: str | None = None,
) -> InvitePreview:
    """Проверить код и показать название без финансовых данных (FR-78, A153).

    Принимается либо введённый участником код, либо его проверочное значение:
    обработка отложенного события не требует хранить открытый секрет (SEC-04).
    """
    if code_digest is None:
        if raw_code is None:
            raise ValidationFailed("Не указан код приглашения")
        code_digest = digest_for(settings, raw_code)
    digest = code_digest
    async with session_scope(settings, RuntimeRole.API, user_id=user.id) as session:
        await _check_attempt_limit(session, settings, telegram_user_id=user.telegram_user_id)
        # Код проверяется до того, как известен бюджет, поэтому используется
        # узкая SECURITY DEFINER функция минимального раскрытия (ADR-06).
        row = (
            await session.execute(
                text(
                    "SELECT invite_id, workspace_id, workspace_name, workspace_state, role, "
                    "expires_at, max_uses, used_uses, revoked_at "
                    "FROM resolve_invite_by_digest(:digest)"
                ),
                {"digest": digest},
            )
        ).one_or_none()
        now = dt.datetime.now(dt.UTC)
        invalid = (
            row is None
            or row.revoked_at is not None
            or row.expires_at <= now
            or row.used_uses >= row.max_uses
            or row.workspace_state != WorkspaceState.ACTIVE.value
        )
        if invalid or row is None:
            await _record_attempt(
                session, telegram_user_id=user.telegram_user_id, user_id=user.id, succeeded=False
            )
            # Недействительный код не раскрывает финансовые данные (A153).
            raise NotFound("Код недействителен, отозван или исчерпан")

        state = (
            await session.execute(
                text(
                    "SELECT status, role, rejoin_blocked, member_count "
                    "FROM self_membership_state(:workspace_id, :user_id)"
                ),
                {"workspace_id": row.workspace_id, "user_id": user.id},
            )
        ).one()
        if state.rejoin_blocked:
            await _record_attempt(
                session, telegram_user_id=user.telegram_user_id, user_id=user.id, succeeded=False
            )
            # Исключённый не входит по общему коду до разрешения админа (A170).
            raise PermissionDenied(
                "Вход в этот бюджет закрыт. Обратитесь к администратору бюджета."
            )
        await _record_attempt(
            session, telegram_user_id=user.telegram_user_id, user_id=user.id, succeeded=True
        )
        return InvitePreview(
            workspace_id=row.workspace_id,
            workspace_name=row.workspace_name,
            short_id=short_id(row.workspace_id),
            role=Role(row.role),
            already_member=state.status == MembershipStatus.ACTIVE.value,
            member_count=int(state.member_count or 0),
        )


@dataclass(frozen=True, slots=True)
class JoinResult:
    workspace_id: uuid.UUID
    workspace_name: str
    already_member: bool
    membership_generation: uuid.UUID


async def accept_invite(
    settings: Settings,
    *,
    user: User,
    correlation_id: str,
    raw_code: str | None = None,
    code_digest: str | None = None,
) -> JoinResult:
    """Вступить по коду: членство и квота меняются атомарно (FR-78, A154, A155)."""
    from fintracker.application.identity.security_change import run_security_change

    if code_digest is None:
        if raw_code is None:
            raise ValidationFailed("Не указан код приглашения")
        code_digest = digest_for(settings, raw_code)
    preview = await preview_invite(settings, user=user, code_digest=code_digest)
    digest = code_digest
    if preview.already_member:
        # Повтор не создаёт второе членство и не расходует квоту (A155).
        async with session_scope(
            settings, RuntimeRole.API, user_id=user.id, workspace_id=preview.workspace_id
        ) as session:
            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.workspace_id == preview.workspace_id,
                        Membership.user_id == user.id,
                    )
                )
            ).scalar_one()
            return JoinResult(
                workspace_id=preview.workspace_id,
                workspace_name=preview.workspace_name,
                already_member=True,
                membership_generation=membership.generation,
            )

    generation = new_generation()

    async def apply(session: AsyncSession, uow: UnitOfWork, workspace: Workspace) -> dict[str, str]:
        invite = (
            await session.execute(
                select(BudgetInvite)
                .where(
                    BudgetInvite.workspace_id == workspace.id,
                    BudgetInvite.secret_digest == digest,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = await uow.now()
        if (
            invite is None
            or invite.revoked_at is not None
            or invite.expires_at <= now
            or invite.used_uses >= invite.max_uses
        ):
            # При последнем месте из двух конкурентных запросов проходит один.
            raise ConflictError("Код исчерпан или отозван")

        membership = (
            await session.execute(
                select(Membership)
                .where(Membership.workspace_id == workspace.id, Membership.user_id == user.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if membership is not None and membership.status == MembershipStatus.ACTIVE.value:
            raise ConflictError("Вы уже участник этого бюджета")
        if membership is not None and membership.rejoin_blocked:
            raise PermissionDenied("Вход в этот бюджет закрыт администратором")

        invite.used_uses += 1
        invite.version += 1

        if membership is None:
            membership = Membership(
                workspace_id=workspace.id,
                user_id=user.id,
                role=Role.MEMBER.value,
                status=MembershipStatus.ACTIVE.value,
                generation=generation,
            )
            session.add(membership)
            previous_status = None
        else:
            # Повторное вступление получает новое поколение (FR-78, A171).
            previous_status = membership.status
            membership.status = MembershipStatus.ACTIVE.value
            membership.role = Role.MEMBER.value
            membership.generation = generation
            membership.access_version += 1
            membership.version += 1
            membership.left_at = None
        await session.flush()
        session.add(
            MembershipHistory(
                workspace_id=workspace.id,
                user_id=user.id,
                from_status=previous_status,
                to_status=MembershipStatus.ACTIVE.value,
                from_role=None,
                to_role=Role.MEMBER.value,
                generation=generation,
                initiated_by=user.id,
            )
        )
        await uow.emit(
            workspace_id=workspace.id,
            event_type="MemberJoined",
            aggregate_type="membership",
            aggregate_id=membership.id,
            payload={"user_id": str(user.id), "generation": str(generation)},
            actor_user_id=user.id,
        )
        return {"user_id": str(user.id), "invite_id": str(invite.id)}

    await run_security_change(
        settings,
        workspace_id=preview.workspace_id,
        kind="member_rejoin" if preview.already_member else "member_join",
        initiated_by=user.id,
        acting_user_id=user.id,
        apply=apply,
        correlation_id=correlation_id,
    )
    return JoinResult(
        workspace_id=preview.workspace_id,
        workspace_name=preview.workspace_name,
        already_member=False,
        membership_generation=generation,
    )
