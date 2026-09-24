"""Профиль участника в бюджете: имя, «кто потратил» и «для кого» (FR-04, FR-88).

Каждый участник получает в бюджете запись человека и получателя со своим
именем из Telegram. Благодаря этому список участников, история, уведомления,
разрезы «я потратил» и «для меня» показывают людей по именам, а не по
служебным идентификаторам. Профиль не даёт доступа и не меняет права.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.normalize import clean_display_name, normalize_name
from fintracker.config import Settings
from fintracker.core.errors import ValidationFailed
from fintracker.db.models.access import Beneficiary, Membership, Person
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

MAX_NAME = 60


async def _free_name(
    session: AsyncSession,
    *,
    model: type[Person] | type[Beneficiary],
    workspace_id: uuid.UUID,
    name: str,
) -> str:
    """Имя без конфликта с активной записью справочника: «Маша», «Маша 2»…"""
    candidate = name
    for index in range(2, 50):
        taken = (
            await session.execute(
                select(model.id).where(
                    model.workspace_id == workspace_id,
                    model.normalized_name == normalize_name(candidate),
                    model.archived_at.is_(None),
                )
            )
        ).first()
        if taken is None:
            return candidate
        candidate = f"{name} {index}"
    return f"{name} {uuid.uuid4().hex[:4]}"


async def ensure_member_profile(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    name: str | None,
) -> None:
    """Связать участника с его профилем человека, если связи ещё нет."""
    display = clean_display_name(name or "")[:MAX_NAME]
    if not display:
        return
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        # Быстрая проверка без блокировки: обычно профиль уже связан.
        linked = (
            await session.execute(
                select(Membership.person_id, Membership.beneficiary_id).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == user_id,
                    Membership.status == "active",
                )
            )
        ).one_or_none()
        if linked is None or (linked[0] is not None and linked[1] is not None):
            return
        membership = (
            await session.execute(
                select(Membership)
                .where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == user_id,
                    Membership.status == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if membership is None or (
            membership.person_id is not None and membership.beneficiary_id is not None
        ):
            return
        uow = UnitOfWork(session=session, correlation_id=uuid.uuid4().hex)
        if membership.person_id is None:
            person = (
                await session.execute(
                    select(Person).where(
                        Person.workspace_id == workspace_id,
                        Person.user_id == user_id,
                        Person.archived_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if person is None:
                # Ранее добавленный без аккаунта человек с тем же именем
                # принадлежит этому участнику: его прошлые покупки сохраняются.
                person = (
                    await session.execute(
                        select(Person).where(
                            Person.workspace_id == workspace_id,
                            Person.normalized_name == normalize_name(display),
                            Person.user_id.is_(None),
                            Person.archived_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if person is not None:
                    person.user_id = user_id
            if person is None:
                person_name = await _free_name(
                    session, model=Person, workspace_id=workspace_id, name=display
                )
                person = Person(
                    workspace_id=workspace_id,
                    name=person_name,
                    normalized_name=normalize_name(person_name),
                    aliases=[],
                    user_id=user_id,
                )
                session.add(person)
                await session.flush()
            membership.person_id = person.id
            display = person.name
        if membership.beneficiary_id is None:
            beneficiary = (
                await session.execute(
                    select(Beneficiary).where(
                        Beneficiary.workspace_id == workspace_id,
                        Beneficiary.person_id == membership.person_id,
                        Beneficiary.archived_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if beneficiary is None:
                beneficiary_name = await _free_name(
                    session, model=Beneficiary, workspace_id=workspace_id, name=display
                )
                beneficiary = Beneficiary(
                    workspace_id=workspace_id,
                    name=beneficiary_name,
                    normalized_name=normalize_name(beneficiary_name),
                    kind="person",
                    person_id=membership.person_id,
                )
                session.add(beneficiary)
                await session.flush()
            membership.beneficiary_id = beneficiary.id
        await uow.bump_revisions(workspace_id, catalog=True)


async def rename_member_profile(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    name: str,
) -> str:
    """Изменить своё имя в бюджете; записи и права не меняются."""
    display = clean_display_name(name)[:MAX_NAME]
    if not display:
        raise ValidationFailed("Имя не может быть пустым")
    await ensure_member_profile(settings, user_id=user_id, workspace_id=workspace_id, name=display)
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace_id, Membership.user_id == user_id
                )
            )
        ).scalar_one()
        person = await session.get(Person, membership.person_id)
        if person is None:  # pragma: no cover - профиль создан выше
            raise ValidationFailed("Профиль недоступен")
        normalized = normalize_name(display)
        clash = (
            await session.execute(
                select(Person.id).where(
                    Person.workspace_id == workspace_id,
                    Person.normalized_name == normalized,
                    Person.archived_at.is_(None),
                    Person.id != person.id,
                )
            )
        ).first()
        if clash is not None:
            raise ValidationFailed(f"Имя «{display}» уже занято в этом бюджете. Выберите другое.")
        person.name = display
        person.normalized_name = normalized
        person.version += 1
        if membership.beneficiary_id is not None:
            beneficiary = await session.get(Beneficiary, membership.beneficiary_id)
            if beneficiary is not None:
                taken = (
                    await session.execute(
                        select(Beneficiary.id).where(
                            Beneficiary.workspace_id == workspace_id,
                            Beneficiary.normalized_name == normalized,
                            Beneficiary.archived_at.is_(None),
                            Beneficiary.id != beneficiary.id,
                        )
                    )
                ).first()
                if taken is None:
                    beneficiary.name = display
                    beneficiary.normalized_name = normalized
                    beneficiary.version += 1
        uow = UnitOfWork(session=session, correlation_id=uuid.uuid4().hex)
        await uow.bump_revisions(workspace_id, catalog=True)
    return display
