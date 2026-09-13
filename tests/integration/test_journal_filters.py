"""Фильтры и сортировка журнала (FR-07, FR-89, R05)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.journal import list_journal
from fintracker.application.analytics.reports import FilterSpec
from fintracker.application.ledger.operations import post_refund, refundable_parts
from fintracker.application.ledger.service import post_transaction, void_transaction
from fintracker.core.errors import ValidationFailed
from tests.conftest import requires_pg
from tests.integration.factories import Fixture, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def _spend(
    session: AsyncSession,
    fixture: Fixture,
    *,
    category: str = "Продукты",
    amount: int = 1_000,
    day: dt.date = DAY,
    note: str | None = None,
    account: str | None = None,
    origin: str = "form",
) -> uuid.UUID:
    posted = await post_transaction(
        session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(
            fixture,
            amount=rub(amount),
            category=category,
            occurred=day,
            note=note,
            account=account,
        ),
        origin=origin,
    )
    return posted.transaction_id


async def test_journal_filters_by_category_and_account(owner_session: AsyncSession) -> None:
    """CMD-12, FR-07: фильтры по статье и счёту сужают журнал."""
    fixture = await build_fixture(owner_session)
    await _spend(owner_session, fixture, category="Продукты", account="Карта")
    await _spend(owner_session, fixture, category="Рестораны", account="Кошелёк")

    by_category = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        filters=FilterSpec(category_ids=(fixture.categories["Продукты"],)),
    )
    assert by_category.total == 1

    by_account = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        filters=FilterSpec(account_ids=(fixture.accounts["Кошелёк"],)),
    )
    assert by_account.total == 1
    assert by_account.entries[0].amount_minor == rub(1_000).minor


async def test_journal_filters_by_note_and_presence(owner_session: AsyncSession) -> None:
    """FR-07: поиск по тексту комментария и признак его наличия."""
    fixture = await build_fixture(owner_session)
    await _spend(owner_session, fixture, note="подарок маме")
    await _spend(owner_session, fixture, note=None)

    found = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        filters=FilterSpec(note_query="подар"),
    )
    assert found.total == 1

    with_note = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        filters=FilterSpec(has_note=True),
    )
    assert with_note.total == 1

    # Спецсимволы поиска не расширяют выборку (DATA_CONTRACT §3).
    escaped = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        filters=FilterSpec(note_query="%"),
    )
    assert escaped.total == 0


async def test_journal_excludes_voided_until_asked(owner_session: AsyncSession) -> None:
    """FR-07: отменённые записи видны только по явному условию."""
    fixture = await build_fixture(owner_session)
    transaction_id = await _spend(owner_session, fixture)
    await void_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=transaction_id,
        reason="ошибка",
    )
    default = await list_journal(owner_session, workspace_id=fixture.workspace.id)
    assert default.total == 0

    with_voided = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        filters=FilterSpec(include_voided=True),
    )
    assert with_voided.total == 1
    assert with_voided.entries[0].status == "voided"


async def test_journal_filters_by_type_and_origin(owner_session: AsyncSession) -> None:
    """FR-07: фильтры по типу операции и источнику записи."""
    fixture = await build_fixture(owner_session)
    purchase = await _spend(owner_session, fixture, amount=2_000)
    parts = await refundable_parts(
        owner_session, workspace_id=fixture.workspace.id, transaction_id=purchase
    )
    await post_refund(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        source_transaction_id=purchase,
        parts={parts[0].stable_line_id: rub(500)},
        occurred_date=DAY,
        timezone="Asia/Novosibirsk",
    )
    await _spend(owner_session, fixture, amount=700, origin="import")

    refunds = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        filters=FilterSpec(transaction_types=("refund",)),
    )
    assert refunds.total == 1

    imported = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        filters=FilterSpec(origins=("import",)),
    )
    assert imported.total == 1
    assert imported.entries[0].origin == "import"


async def test_journal_sort_by_added_differs_from_occurred(owner_session: AsyncSession) -> None:
    """FR-07: «последние добавленные» не совпадают с последними по дате."""
    fixture = await build_fixture(owner_session)
    old_day = await _spend(owner_session, fixture, amount=100, day=dt.date(2026, 9, 11))
    new_day = await _spend(owner_session, fixture, amount=200, day=dt.date(2026, 9, 13))
    later_but_older = await _spend(owner_session, fixture, amount=300, day=dt.date(2026, 9, 10))
    assert {old_day, new_day, later_but_older}

    by_date = await list_journal(owner_session, workspace_id=fixture.workspace.id)
    assert by_date.entries[0].occurred_date == dt.date(2026, 9, 13)

    by_added = await list_journal(owner_session, workspace_id=fixture.workspace.id, sort="added")
    assert by_added.entries[0].occurred_date == dt.date(2026, 9, 10)

    with pytest.raises(ValidationFailed):
        await list_journal(owner_session, workspace_id=fixture.workspace.id, sort="random")


async def test_journal_pages_without_losing_rows(owner_session: AsyncSession) -> None:
    """FR-07: постраничный показ не теряет и не дублирует записи."""
    fixture = await build_fixture(owner_session)
    for index in range(10):
        await _spend(owner_session, fixture, amount=100 + index, day=DAY)

    first = await list_journal(owner_session, workspace_id=fixture.workspace.id, limit=4)
    second = await list_journal(owner_session, workspace_id=fixture.workspace.id, limit=4, offset=4)
    third = await list_journal(owner_session, workspace_id=fixture.workspace.id, limit=4, offset=8)
    seen = [entry.transaction_id for entry in (*first.entries, *second.entries, *third.entries)]
    assert first.total == 10
    assert len(seen) == 10
    assert len(set(seen)) == 10


async def test_journal_period_bounds_are_half_open(owner_session: AsyncSession) -> None:
    """FR-07, ADR-07: граница периода не включает следующий день."""
    fixture = await build_fixture(owner_session)
    await _spend(owner_session, fixture, day=dt.date(2026, 9, 12))
    await _spend(owner_session, fixture, day=dt.date(2026, 9, 13))

    page = await list_journal(
        owner_session,
        workspace_id=fixture.workspace.id,
        date_from=dt.date(2026, 9, 12),
        date_to_exclusive=dt.date(2026, 9, 13),
    )
    assert page.total == 1
    assert page.entries[0].occurred_date == dt.date(2026, 9, 12)
