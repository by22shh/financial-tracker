"""Конкурентные сценарии AR-07–AR-10, A98, A99, A154, A164 на PostgreSQL 17."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.common import body_hash_of, find_existing_result, store_result
from fintracker.application.identity.invites import accept_invite, issue_invite
from fintracker.application.ledger.service import post_transaction, revise_transaction
from fintracker.config import Settings
from fintracker.core.errors import IdempotencyConflict, VersionConflict
from fintracker.db.models.access import BudgetInvite, Membership, User
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def test_ar07_a98_concurrent_edits_conflict_without_overwrite(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-07/A98: две правки с одной expected_version — одна успешна, вторая 409."""
    fixture = await build_fixture(owner_session)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        origin="form",
    )
    workspace_id = fixture.workspace.id
    actor = fixture.actor
    version = posted.entity_version
    transaction_id = posted.transaction_id
    categories = dict(fixture.categories)
    beneficiaries = dict(fixture.beneficiaries)
    accounts = dict(fixture.accounts)
    await owner_session.commit()

    class _Fixture:
        def __init__(self) -> None:
            self.categories = categories
            self.beneficiaries = beneficiaries
            self.accounts = accounts

    proxy = _Fixture()

    async def edit(amount: int) -> str:
        async with session_scope(
            test_settings, RuntimeRole.OWNER, workspace_id=workspace_id
        ) as session:
            uow = UnitOfWork(session=session, correlation_id="concurrent")
            await uow.lock_workspace(workspace_id)
            try:
                await revise_transaction(
                    session,
                    uow,
                    actor=actor,
                    transaction_id=transaction_id,
                    new_spec=expense_spec(proxy, amount=rub(amount), category="Продукты"),
                    expected_version=version,
                )
            except VersionConflict:
                return "conflict"
            return "ok"

    results = await asyncio.gather(edit(800), edit(900))
    assert sorted(results) == ["conflict", "ok"], "одна правка прошла, вторая — конфликт"

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=workspace_id
    ) as session:
        from fintracker.application.ledger.service import load_current_spec

        _, revision, _ = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
    assert revision.amount_minor in {80_000, 90_000}
    assert revision.revision == 2, "перетирания не произошло"


async def test_ar09_a154_last_invite_use_goes_to_one_of_two(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-09/A154: при последнем применении кода проходит ровно один вход."""
    fixture = await build_fixture(owner_session)
    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        pass
    uow = UnitOfWork(session=owner_session, correlation_id="invite")
    invite = await issue_invite(
        owner_session,
        uow,
        settings=test_settings,
        workspace_id=fixture.workspace.id,
        created_by=fixture.user.id,
        max_uses=1,
    )
    first = User(id=uuid.uuid4(), telegram_user_id=971001)
    second = User(id=uuid.uuid4(), telegram_user_id=971002)
    owner_session.add_all([first, second])
    await owner_session.commit()

    results = await asyncio.gather(
        accept_invite(test_settings, raw_code=invite.code, user=first, correlation_id="join-1"),
        accept_invite(test_settings, raw_code=invite.code, user=second, correlation_id="join-2"),
        return_exceptions=True,
    )
    successes = [item for item in results if not isinstance(item, Exception)]
    assert len(successes) == 1, "вошёл ровно один человек"

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        row = (
            await session.execute(select(BudgetInvite).where(BudgetInvite.id == invite.invite_id))
        ).scalar_one()
        members = (
            (
                await session.execute(
                    select(Membership).where(
                        Membership.workspace_id == fixture.workspace.id,
                        Membership.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert row.used_uses == 1, "квота не отрицательна и не превышена"
    assert len(members) == 2, "администратор и один участник"


async def test_ar10_a173_two_admin_transfers_keep_single_admin(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-10: два одновременных предложения не создают двух администраторов."""
    from fintracker.application.identity.membership import propose_admin_transfer
    from fintracker.core.errors import ConflictError
    from fintracker.core.ids import new_generation

    fixture = await build_fixture(owner_session)
    member = User(id=uuid.uuid4(), telegram_user_id=972001)
    owner_session.add(member)
    await owner_session.flush()
    owner_session.add(
        Membership(
            workspace_id=fixture.workspace.id,
            user_id=member.id,
            role="member",
            status="active",
            generation=new_generation(),
        )
    )
    await owner_session.commit()

    async def propose() -> str:
        async with session_scope(
            test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
        ) as session:
            uow = UnitOfWork(session=session, correlation_id="transfer")
            try:
                await propose_admin_transfer(
                    session,
                    uow,
                    workspace_id=fixture.workspace.id,
                    from_user_id=fixture.user.id,
                    to_user_id=member.id,
                )
            except ConflictError:
                return "conflict"
            return "ok"

    results = await asyncio.gather(propose(), propose(), return_exceptions=True)
    normalized = [item if isinstance(item, str) else "conflict" for item in results]
    assert normalized.count("ok") == 1, "активно одно предложение передачи"

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        admins = (
            (
                await session.execute(
                    select(Membership).where(
                        Membership.workspace_id == fixture.workspace.id,
                        Membership.role == "admin",
                        Membership.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(admins) == 1


async def test_a99_same_idempotency_key_other_payload_conflicts(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A99: один ключ идемпотентности с другим содержимым даёт конфликт."""
    fixture = await build_fixture(owner_session)
    first_body = body_hash_of({"amount_minor": 25_000, "category": "Продукты"})
    await store_result(
        owner_session,
        user_id=fixture.user.id,
        workspace_id=fixture.workspace.id,
        command="post_transaction",
        idempotency_key="key-1",
        body_hash=first_body,
        entity_id=uuid.uuid4(),
        entity_revision=1,
        result={"status": "posted"},
    )
    await owner_session.flush()

    same = await find_existing_result(
        owner_session,
        user_id=fixture.user.id,
        workspace_id=fixture.workspace.id,
        command="post_transaction",
        idempotency_key="key-1",
        body_hash=first_body,
    )
    assert same is not None, "повтор того же содержимого возвращает результат"

    with pytest.raises(IdempotencyConflict):
        await find_existing_result(
            owner_session,
            user_id=fixture.user.id,
            workspace_id=fixture.workspace.id,
            command="post_transaction",
            idempotency_key="key-1",
            body_hash=body_hash_of({"amount_minor": 99_000}),
        )


async def test_ar08_revoked_access_blocks_posting(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-08: после завершённого отзыва доступа проведение невозможно."""
    from fintracker.application.identity.actor import resolve_actor
    from fintracker.application.identity.membership import remove_member
    from fintracker.core.errors import NotFound
    from fintracker.core.ids import new_generation

    fixture = await build_fixture(owner_session)
    member = User(id=uuid.uuid4(), telegram_user_id=973001)
    owner_session.add(member)
    await owner_session.flush()
    owner_session.add(
        Membership(
            workspace_id=fixture.workspace.id,
            user_id=member.id,
            role="member",
            status="active",
            generation=new_generation(),
        )
    )
    await owner_session.commit()

    await remove_member(
        test_settings,
        workspace_id=fixture.workspace.id,
        admin_user_id=fixture.user.id,
        target_user_id=member.id,
        correlation_id="remove",
    )

    async with session_scope(
        test_settings,
        RuntimeRole.OWNER,
        user_id=member.id,
        workspace_id=fixture.workspace.id,
    ) as session:
        stored = (await session.execute(select(User).where(User.id == member.id))).scalar_one()
        with pytest.raises(NotFound):
            await resolve_actor(session, user=stored, workspace_id=fixture.workspace.id)
