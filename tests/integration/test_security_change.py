"""Протокол изменения доступа и конкурентные деньги (AR-05, AR-17, AR-31, AR-32, B7, B8)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.identity.security_change import run_security_change
from fintracker.config import Settings
from fintracker.core.errors import ConflictError, TemporarilyUnavailable
from fintracker.db.models.access import SecurityChange, Workspace
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.security_log import SecurityLogConflict
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


class _BrokenLog:
    """Журнал доступа, недоступный на выбранном шаге (AR-31)."""

    def __init__(self, *, fail_on: str) -> None:
        self.fail_on = fail_on
        self.prepared: list[uuid.UUID] = []
        self.committed: list[uuid.UUID] = []

    async def write_prepared(self, **kwargs):  # type: ignore[no-untyped-def]
        if self.fail_on == "prepared":
            raise OSError("журнал недоступен")
        self.prepared.append(kwargs["operation_id"])
        return {"version_key": "v1", "digest": "d1"}

    async def write_committed(self, **kwargs):  # type: ignore[no-untyped-def]
        if self.fail_on == "committed":
            raise OSError("журнал недоступен")
        self.committed.append(kwargs["operation_id"])
        return {"version_key": "v2", "digest": "d2"}

    async def read_last(self, **kwargs):  # type: ignore[no-untyped-def]
        return None


async def test_ar31_unavailable_journal_stops_before_success(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-31: без доступного журнала изменение не объявляется выполненным."""
    fixture = await build_fixture(owner_session, telegram_user_id=6600)
    await owner_session.commit()

    async def apply(session, uow, workspace):  # type: ignore[no-untyped-def]
        return {"changed": "nothing"}

    with pytest.raises(TemporarilyUnavailable):
        await run_security_change(
            test_settings,
            workspace_id=fixture.workspace.id,
            kind="member_remove",
            initiated_by=fixture.user.id,
            apply=apply,
            correlation_id="ar31",
            security_log=_BrokenLog(fail_on="prepared"),
            acting_user_id=fixture.user.id,
        )

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        rows = (await session.execute(select(SecurityChange))).scalars().all()
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == fixture.workspace.id))
        ).scalar_one()
    assert rows and rows[0].state != "completed", "успех не объявляется раньше конца"
    assert workspace.security_fence is not None, "fence остаётся до завершения"


async def test_ar31_second_change_waits_for_fence(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-31: второе изменение доступа не начинается при активном fence."""
    fixture = await build_fixture(owner_session, telegram_user_id=6601)
    await owner_session.commit()

    async def apply(session, uow, workspace):  # type: ignore[no-untyped-def]
        return {"changed": "nothing"}

    with pytest.raises(TemporarilyUnavailable):
        await run_security_change(
            test_settings,
            workspace_id=fixture.workspace.id,
            kind="member_remove",
            initiated_by=fixture.user.id,
            apply=apply,
            correlation_id="ar31-a",
            security_log=_BrokenLog(fail_on="prepared"),
            acting_user_id=fixture.user.id,
        )

    with pytest.raises(TemporarilyUnavailable):
        await run_security_change(
            test_settings,
            workspace_id=fixture.workspace.id,
            kind="member_remove",
            initiated_by=fixture.user.id,
            apply=apply,
            correlation_id="ar31-b",
            security_log=_BrokenLog(fail_on="committed"),
            acting_user_id=fixture.user.id,
        )


async def test_ar32_acl_revision_grows_with_applied_change(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """ADR-14, SEC-10, AR-32: изменение доступа повышает версию ACL.

    После завершения fence снимается.
    """
    fixture = await build_fixture(owner_session, telegram_user_id=6602)
    await owner_session.commit()

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        before = (
            await session.execute(
                select(Workspace.acl_revision).where(Workspace.id == fixture.workspace.id)
            )
        ).scalar_one()

    async def apply(session, uow, workspace):  # type: ignore[no-untyped-def]
        return {"kind": "noop"}

    result = await run_security_change(
        test_settings,
        workspace_id=fixture.workspace.id,
        kind="member_remove",
        initiated_by=fixture.user.id,
        apply=apply,
        correlation_id="ar32",
        acting_user_id=fixture.user.id,
    )
    assert result.applied_acl_revision == before + 1

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == fixture.workspace.id))
        ).scalar_one()
        change = (
            await session.execute(
                select(SecurityChange).where(SecurityChange.operation_id == result.operation_id)
            )
        ).scalar_one()
    assert workspace.acl_revision == before + 1
    assert workspace.security_fence is None, "fence снят после завершения"
    assert change.state == "completed"

    # Повтор завершённой операции безопасен и не повышает версию повторно.
    repeat = await run_security_change(
        test_settings,
        workspace_id=fixture.workspace.id,
        kind="member_remove",
        initiated_by=fixture.user.id,
        apply=apply,
        correlation_id="ar32-repeat",
        operation_id=result.operation_id,
        acting_user_id=fixture.user.id,
    )
    assert repeat.applied_acl_revision == result.applied_acl_revision
    assert SecurityLogConflict is not None
    assert ConflictError is not None


async def test_ar17_two_refunds_cannot_exceed_returnable(
    owner_session: AsyncSession,
) -> None:
    """AR-17: два возврата не исчерпывают одну часть чека дважды."""
    from fintracker.application.ledger.operations import post_refund, refundable_parts
    from fintracker.application.ledger.service import post_transaction
    from tests.integration.factories import TZ
    from tests.integration.test_money_scenarios import expense_spec, rub

    fixture = await build_fixture(owner_session)
    purchase = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        origin="form",
    )
    parts = await refundable_parts(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=purchase.transaction_id,
    )
    await post_refund(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id: rub(600)},
        occurred_date=DAY,
        timezone=TZ,
    )
    with pytest.raises(ConflictError):
        await post_refund(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            source_transaction_id=purchase.transaction_id,
            parts={parts[0].stable_line_id: rub(600)},
            occurred_date=DAY,
            timezone=TZ,
        )
    remaining = await refundable_parts(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=purchase.transaction_id,
    )
    assert remaining[0].refundable_minor == 40_000


async def test_ar05_late_model_answer_does_not_override_manual_value(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-05: поздний ответ модели не перезаписывает ручную правку 650."""
    from dataclasses import replace

    from fintracker.application.ledger.service import (
        load_current_spec,
        post_transaction,
        revise_transaction,
    )
    from fintracker.core.money import Money
    from fintracker.db.models.ledger import Transaction
    from fintracker.db.models.platform import Candidate, Draft
    from tests.integration.test_money_scenarios import expense_spec, rub

    fixture = await build_fixture(owner_session, telegram_user_id=6603)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(700), category="Продукты"),
        origin="telegram_text",
    )
    _, _, spec = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    manual = Money(65_000, "RUB")
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=replace(
            spec,
            amount=manual,
            allocations=(replace(spec.allocations[0], amount=manual),),
            cash_legs=tuple(replace(leg, signed=-manual) for leg in spec.cash_legs),
        ),
        expected_version=None,
    )

    # Поздний результат модели приходит черновиком и не меняет проведённую запись.
    now = dt.datetime.now(dt.UTC)
    draft = Draft(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        owner_membership_generation=fixture.actor.membership_generation,
        source_kind="text",
        state="ready",
        raw_text="кофе 700",
        expires_at=now + dt.timedelta(days=3),
        delete_raw_after=now + dt.timedelta(days=7),
    )
    owner_session.add(draft)
    await owner_session.flush()
    owner_session.add(
        Candidate(
            workspace_id=fixture.workspace.id,
            draft_id=draft.id,
            candidate_key="c1",
            state="ready",
            fields={"amount_minor": 70_000, "occurred_date": DAY.isoformat()},
            ambiguities=[],
        )
    )
    await owner_session.flush()

    _, revision, _ = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    assert revision.amount_minor == 65_000, "ручное значение сохранено"
    transactions = (
        (
            await owner_session.execute(
                select(Transaction).where(Transaction.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(transactions) == 1, "второй записи не появилось"
