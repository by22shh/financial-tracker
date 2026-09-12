"""Проверки ограничений схемы на настоящей PostgreSQL 17 (AR-11, AR-12, ADR-07)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.config import Settings
from fintracker.core.ids import new_generation
from fintracker.db.models.access import Membership, User, Workspace
from fintracker.db.models.catalog import Category
from fintracker.db.models.planning import BudgetPeriod, PeriodPolicyRow
from fintracker.db.session import RuntimeRole, get_sessionmaker, set_rls_context
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]

TZ = "Asia/Novosibirsk"


async def _seed_workspace(session: AsyncSession, *, name: str = "Наш бюджет") -> tuple[User, Workspace]:
    user = User(id=uuid.uuid4(), telegram_user_id=int(uuid.uuid4().int % 10**9))
    session.add(user)
    await session.flush()
    workspace = Workspace(
        id=uuid.uuid4(),
        name=name,
        currency="RUB",
        timezone=TZ,
        state="active",
        admin_user_id=user.id,
    )
    session.add(workspace)
    await session.flush()
    session.add(
        Membership(
            workspace_id=workspace.id,
            user_id=user.id,
            role="admin",
            status="active",
            generation=new_generation(),
        )
    )
    await session.flush()
    return user, workspace


async def test_postgres_is_version_17(owner_session: AsyncSession) -> None:
    """ADR-02: проверки идут на PostgreSQL 17, не на SQLite."""
    version = (await owner_session.execute(text("SHOW server_version_num"))).scalar_one()
    assert int(version) >= 170000


async def test_periods_cannot_overlap(owner_session: AsyncSession) -> None:
    """ADR-07: exclusion constraint запрещает пересечение периодов бюджета."""
    _, workspace = await _seed_workspace(owner_session)
    policy = PeriodPolicyRow(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        version=1,
        anchor_date=dt.date(2026, 9, 10),
        anchor_day=10,
        mode="calendar_months",
        interval=1,
        timezone=TZ,
        first_end_exclusive=dt.date(2026, 10, 10),
        effective_from=dt.date(2026, 9, 10),
    )
    owner_session.add(policy)
    await owner_session.flush()
    owner_session.add(
        BudgetPeriod(
            workspace_id=workspace.id,
            policy_id=policy.id,
            policy_version=1,
            sequence=0,
            start_date=dt.date(2026, 9, 10),
            end_exclusive=dt.date(2026, 10, 10),
        )
    )
    await owner_session.flush()
    owner_session.add(
        BudgetPeriod(
            workspace_id=workspace.id,
            policy_id=policy.id,
            policy_version=1,
            sequence=1,
            start_date=dt.date(2026, 10, 5),  # пересекается с первым
            end_exclusive=dt.date(2026, 11, 5),
        )
    )
    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_session.flush()


async def test_periods_of_different_workspaces_may_overlap(owner_session: AsyncSession) -> None:
    """Ограничение действует только внутри одного бюджета (TZ §20)."""
    _, first = await _seed_workspace(owner_session, name="Первый")
    _, second = await _seed_workspace(owner_session, name="Второй")
    for workspace in (first, second):
        policy = PeriodPolicyRow(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            version=1,
            anchor_date=dt.date(2026, 9, 10),
            anchor_day=10,
            mode="calendar_months",
            interval=1,
            timezone=TZ,
            first_end_exclusive=dt.date(2026, 10, 10),
            effective_from=dt.date(2026, 9, 10),
        )
        owner_session.add(policy)
        await owner_session.flush()
        owner_session.add(
            BudgetPeriod(
                workspace_id=workspace.id,
                policy_id=policy.id,
                policy_version=1,
                sequence=0,
                start_date=dt.date(2026, 9, 10),
                end_exclusive=dt.date(2026, 10, 10),
            )
        )
    await owner_session.flush()


async def test_only_one_active_admin(owner_session: AsyncSession) -> None:
    """DATA_CONTRACT §2.1: не более одного администратора."""
    user, workspace = await _seed_workspace(owner_session)
    other = User(id=uuid.uuid4(), telegram_user_id=int(uuid.uuid4().int % 10**9))
    owner_session.add(other)
    await owner_session.flush()
    owner_session.add(
        Membership(
            workspace_id=workspace.id,
            user_id=other.id,
            role="admin",
            status="active",
            generation=new_generation(),
        )
    )
    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_session.flush()


async def test_active_workspace_requires_exactly_one_admin(owner_session: AsyncSession) -> None:
    """Deferred trigger: действующий бюджет не остаётся без администратора."""
    user, workspace = await _seed_workspace(owner_session)
    await owner_session.execute(
        text("UPDATE memberships SET role = 'member' WHERE workspace_id = :ws"),
        {"ws": workspace.id},
    )
    with pytest.raises(DBAPIError, match="ровно один администратор"):
        await owner_session.commit()


async def test_category_parent_must_be_same_workspace(owner_session: AsyncSession) -> None:
    """AR-11: составной FK запрещает связать объекты разных бюджетов."""
    _, first = await _seed_workspace(owner_session, name="Первый")
    _, second = await _seed_workspace(owner_session, name="Второй")
    parent = Category(
        id=uuid.uuid4(),
        workspace_id=first.id,
        name="Продукты",
        normalized_name="продукты",
    )
    owner_session.add(parent)
    await owner_session.flush()
    owner_session.add(
        Category(
            id=uuid.uuid4(),
            workspace_id=second.id,
            parent_id=parent.id,  # родитель из чужого бюджета
            name="Супермаркеты",
            normalized_name="супермаркеты",
        )
    )
    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_session.flush()


async def test_duplicate_active_category_name_rejected(owner_session: AsyncSession) -> None:
    """FR-21: одинаковые нормализованные имена внутри родителя не дублируются."""
    _, workspace = await _seed_workspace(owner_session)
    for _ in range(2):
        owner_session.add(
            Category(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                name="Продукты",
                normalized_name="продукты",
            )
        )
    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_session.flush()


async def test_rls_isolates_workspaces(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-12: runtime роль видит только выбранное пространство."""
    _, first = await _seed_workspace(owner_session, name="Первый")
    _, second = await _seed_workspace(owner_session, name="Второй")
    owner_session.add_all(
        [
            Category(
                id=uuid.uuid4(),
                workspace_id=first.id,
                name="Продукты",
                normalized_name="продукты",
            ),
            Category(
                id=uuid.uuid4(),
                workspace_id=second.id,
                name="Транспорт",
                normalized_name="транспорт",
            ),
        ]
    )
    await owner_session.commit()

    factory = get_sessionmaker(test_settings, RuntimeRole.API)
    async with factory() as session, session.begin():
        # Без контекста финансовые данные закрыты (SEC-03).
        visible = (await session.execute(text("SELECT count(*) FROM categories"))).scalar_one()
        assert visible == 0

        await set_rls_context(session, workspace_id=first.id)
        names = (await session.execute(text("SELECT name FROM categories"))).scalars().all()
        assert names == ["Продукты"]

        # Переиспользование соединения с другим контекстом не показывает чужие строки.
        await set_rls_context(session, workspace_id=second.id)
        names = (await session.execute(text("SELECT name FROM categories"))).scalars().all()
        assert names == ["Транспорт"]


async def test_rls_bootstrap_reveals_only_own_memberships(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-12: bootstrap раскрывает только собственные метаданные."""
    first_user, first = await _seed_workspace(owner_session, name="Первый")
    second_user, second = await _seed_workspace(owner_session, name="Второй")
    await owner_session.commit()

    factory = get_sessionmaker(test_settings, RuntimeRole.API)
    async with factory() as session, session.begin():
        await set_rls_context(session, user_id=first_user.id)
        rows = (
            await session.execute(text("SELECT workspace_id FROM memberships"))
        ).scalars().all()
        assert rows == [first.id]
        names = (await session.execute(text("SELECT name FROM workspaces"))).scalars().all()
        assert names == ["Первый"]
        # Финансовые данные без выбранного пространства недоступны.
        count = (await session.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        assert count == 0


async def test_runtime_role_cannot_update_account_entries(
    clean_db: None, test_settings: Settings
) -> None:
    """DATA_CONTRACT §2.4: прямая UPDATE движения счёта runtime ролью запрещена."""
    factory = get_sessionmaker(test_settings, RuntimeRole.API)
    async with factory() as session, session.begin():
        with pytest.raises(DBAPIError, match="permission denied|нет прав"):
            await session.execute(text("UPDATE account_entries SET signed_minor = 1"))


async def test_runtime_role_is_not_superuser_and_not_bypassrls(
    clean_db: None, test_settings: Settings
) -> None:
    """SEC-02: runtime роль не владелец, не superuser, без BYPASSRLS."""
    factory = get_sessionmaker(test_settings, RuntimeRole.API)
    async with factory() as session, session.begin():
        row = (
            await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        assert row.rolsuper is False
        assert row.rolbypassrls is False
