"""Люди, получатели, метки и счета (FR-03, FR-88, FR-89, CMD-13, CMD-14)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.normalize import clean_display_name, normalize_name
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.db.models.access import Beneficiary, Person
from fintracker.db.models.catalog import Account, Tag
from fintracker.db.uow import UnitOfWork

COMMON_BENEFICIARY_NAME = "Общее"


@dataclass(frozen=True, slots=True)
class PersonView:
    id: uuid.UUID
    name: str
    user_id: uuid.UUID | None
    aliases: tuple[str, ...]
    archived: bool
    version: int


@dataclass(frozen=True, slots=True)
class BeneficiaryView:
    id: uuid.UUID
    name: str
    kind: str
    person_id: uuid.UUID | None
    archived: bool
    version: int


@dataclass(frozen=True, slots=True)
class AccountView:
    id: uuid.UUID
    name: str
    currency: str
    mode: str
    account_type: str
    is_liquid: bool
    included_in_available: bool
    archived: bool
    version: int


async def create_person(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    name: str,
    aliases: tuple[str, ...] = (),
    user_id: uuid.UUID | None = None,
) -> PersonView:
    """Аналитический профиль человека; членство и приглашение не создаются (A189)."""
    workspace_id = actor.require_workspace()
    display = clean_display_name(name)
    if not display:
        raise ValidationFailed("Имя человека не может быть пустым")
    normalized = normalize_name(display)
    existing = (
        await session.execute(
            select(Person).where(
                Person.workspace_id == workspace_id,
                Person.normalized_name == normalized,
                Person.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return PersonView(
            id=existing.id,
            name=existing.name,
            user_id=existing.user_id,
            aliases=tuple(existing.aliases),
            archived=False,
            version=existing.version,
        )
    row = Person(
        workspace_id=workspace_id,
        name=display,
        normalized_name=normalized,
        aliases=[normalize_name(alias) for alias in aliases],
        user_id=user_id,
    )
    session.add(row)
    await session.flush()
    await uow.bump_revisions(workspace_id, catalog=True)
    return PersonView(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        aliases=tuple(row.aliases),
        archived=False,
        version=row.version,
    )


async def create_beneficiary(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    name: str,
    kind: str = "person",
    person_id: uuid.UUID | None = None,
) -> BeneficiaryView:
    """«Общее» не является человеком и не подменяет неизвестного (FR-03)."""
    workspace_id = actor.require_workspace()
    if kind not in {"person", "common"}:
        raise ValidationFailed("Вид получателя должен быть person или common")
    if kind == "common" and person_id is not None:
        raise ValidationFailed("У получателя «Общее» не бывает связи с человеком")
    display = clean_display_name(name)
    if not display:
        raise ValidationFailed("Название получателя не может быть пустым")
    normalized = normalize_name(display)
    existing = (
        await session.execute(
            select(Beneficiary).where(
                Beneficiary.workspace_id == workspace_id,
                Beneficiary.normalized_name == normalized,
                Beneficiary.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return BeneficiaryView(
            id=existing.id,
            name=existing.name,
            kind=existing.kind,
            person_id=existing.person_id,
            archived=False,
            version=existing.version,
        )
    row = Beneficiary(
        workspace_id=workspace_id,
        name=display,
        normalized_name=normalized,
        kind=kind,
        person_id=person_id,
    )
    session.add(row)
    await session.flush()
    await uow.bump_revisions(workspace_id, catalog=True)
    return BeneficiaryView(
        id=row.id,
        name=row.name,
        kind=row.kind,
        person_id=row.person_id,
        archived=False,
        version=row.version,
    )


async def ensure_common_beneficiary(
    session: AsyncSession, uow: UnitOfWork, *, actor: ActorContext
) -> BeneficiaryView:
    return await create_beneficiary(
        session, uow, actor=actor, name=COMMON_BENEFICIARY_NAME, kind="common"
    )


async def list_people(session: AsyncSession, *, workspace_id: uuid.UUID) -> list[PersonView]:
    rows = (
        (
            await session.execute(
                select(Person)
                .where(Person.workspace_id == workspace_id, Person.archived_at.is_(None))
                .order_by(Person.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        PersonView(
            id=row.id,
            name=row.name,
            user_id=row.user_id,
            aliases=tuple(row.aliases),
            archived=False,
            version=row.version,
        )
        for row in rows
    ]


async def list_beneficiaries(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> list[BeneficiaryView]:
    rows = (
        (
            await session.execute(
                select(Beneficiary)
                .where(Beneficiary.workspace_id == workspace_id, Beneficiary.archived_at.is_(None))
                .order_by(Beneficiary.kind.desc(), Beneficiary.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        BeneficiaryView(
            id=row.id,
            name=row.name,
            kind=row.kind,
            person_id=row.person_id,
            archived=False,
            version=row.version,
        )
        for row in rows
    ]


async def create_account(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    name: str,
    currency: str,
    mode: str = "reference",
    account_type: str = "card",
    is_liquid: bool = True,
    included_in_available: bool = False,
    opening_balance_minor: int | None = None,
    opening_date: dt.date | None = None,
) -> AccountView:
    """Счёт с явно выбранным охватом (R01, ADR-03).

    Reference и неизвестный счёт не дают достоверного банковского остатка.
    """
    workspace_id = actor.require_workspace()
    if mode not in {"full_tracking", "reference"}:
        raise ValidationFailed("Режим счёта должен быть full_tracking или reference")
    if account_type == "credit_card" and mode == "full_tracking":
        raise ValidationFailed(
            "P0 не принимает кредитную карту как обычный положительный счёт (FR-31)"
        )
    display = clean_display_name(name)
    normalized = normalize_name(display)
    existing = (
        await session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.normalized_name == normalized,
                Account.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Повтор создания не создаёт копию счёта (R01).
        return AccountView(
            id=existing.id,
            name=existing.name,
            currency=existing.currency,
            mode=existing.mode,
            account_type=existing.account_type,
            is_liquid=existing.is_liquid,
            included_in_available=existing.included_in_available,
            archived=False,
            version=existing.version,
        )
    if mode == "full_tracking" and (opening_balance_minor is None or opening_date is None):
        raise ValidationFailed("Для полного учёта нужны начальный остаток и дата начальной точки")
    row = Account(
        workspace_id=workspace_id,
        name=display,
        normalized_name=normalized,
        currency=currency,
        mode=mode,
        account_type=account_type,
        is_liquid=is_liquid,
        included_in_available=included_in_available and mode == "full_tracking",
        opening_cutoff_kind="date_start" if opening_date else None,
        opening_cutoff_date=opening_date,
    )
    session.add(row)
    await session.flush()

    if mode == "full_tracking" and opening_balance_minor is not None and opening_date is not None:
        from fintracker.db.models.ledger import AccountEntry, OpeningAdjustment

        adjustment = OpeningAdjustment(
            workspace_id=workspace_id,
            account_id=row.id,
            amount_minor=opening_balance_minor,
            effective_date=opening_date,
            kind="opening_balance",
            reason="Начальный остаток при настройке счёта",
            created_by=actor.user_id,
        )
        session.add(adjustment)
        await session.flush()
        if opening_balance_minor != 0:
            session.add(
                AccountEntry(
                    workspace_id=workspace_id,
                    account_id=row.id,
                    opening_adjustment_id=adjustment.id,
                    signed_minor=opening_balance_minor,
                    effective_date=opening_date,
                )
            )
    await uow.bump_revisions(workspace_id, catalog=True, data=True)
    return AccountView(
        id=row.id,
        name=row.name,
        currency=row.currency,
        mode=row.mode,
        account_type=row.account_type,
        is_liquid=row.is_liquid,
        included_in_available=row.included_in_available,
        archived=False,
        version=row.version,
    )


async def list_accounts(session: AsyncSession, *, workspace_id: uuid.UUID) -> list[AccountView]:
    rows = (
        (
            await session.execute(
                select(Account)
                .where(Account.workspace_id == workspace_id, Account.archived_at.is_(None))
                .order_by(Account.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        AccountView(
            id=row.id,
            name=row.name,
            currency=row.currency,
            mode=row.mode,
            account_type=row.account_type,
            is_liquid=row.is_liquid,
            included_in_available=row.included_in_available,
            archived=False,
            version=row.version,
        )
        for row in rows
    ]


async def create_tag(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    name: str,
) -> uuid.UUID:
    """Метка отдельно от категории; не создаёт новую строку расхода (FR-89)."""
    workspace_id = actor.require_workspace()
    display = clean_display_name(name)
    if not display:
        raise ValidationFailed("Название метки не может быть пустым")
    normalized = normalize_name(display)
    existing = (
        await session.execute(
            select(Tag.id).where(
                Tag.workspace_id == workspace_id,
                Tag.normalized_name == normalized,
                Tag.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = Tag(workspace_id=workspace_id, name=display, normalized_name=normalized)
    session.add(row)
    await session.flush()
    await uow.bump_revisions(workspace_id, catalog=True)
    return row.id


async def resolve_person_alias(
    session: AsyncSession, *, workspace_id: uuid.UUID, alias: str
) -> uuid.UUID | None:
    """Разрешить псевдоним человека; неподтверждённое соответствие остаётся None.

    Алиас не догадывается по совпадению имён (FR-88, A191).
    """
    normalized = normalize_name(alias)
    if not normalized:
        return None
    row = (
        await session.execute(
            select(Person).where(
                Person.workspace_id == workspace_id,
                Person.archived_at.is_(None),
                Person.normalized_name == normalized,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row.id
    candidates = (
        (
            await session.execute(
                select(Person).where(
                    Person.workspace_id == workspace_id, Person.archived_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    matches = [p.id for p in candidates if normalized in {str(a) for a in p.aliases}]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConflictError("Псевдоним подходит нескольким людям: уточните, кто именно")
    return None


async def get_person(
    session: AsyncSession, *, workspace_id: uuid.UUID, person_id: uuid.UUID
) -> PersonView:
    row = (
        await session.execute(
            select(Person).where(Person.workspace_id == workspace_id, Person.id == person_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Человек недоступен в этом бюджете")
    return PersonView(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        aliases=tuple(row.aliases),
        archived=row.archived_at is not None,
        version=row.version,
    )
