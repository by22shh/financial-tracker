"""Денежные сценарии A34–A47 и числовые примеры B1–B10 на PostgreSQL 17."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.ledger.operations import (
    net_spending_for_line,
    post_cash_withdrawal,
    post_mixed_payment,
    post_refund,
    post_transfer,
    refundable_parts,
    settle_receivable,
)
from fintracker.application.ledger.service import (
    account_balance,
    post_transaction,
    restore_transaction,
    revise_transaction,
    void_transaction,
)
from fintracker.application.planning.plan import period_status
from fintracker.core.errors import ConflictError
from fintracker.core.money import Money
from fintracker.db.models.ledger import OpeningAdjustment
from fintracker.domain.ledger.model import (
    AllocationRole,
    AllocationSpec,
    CashLegSpec,
    CoverageMode,
    TransactionSpec,
    TransactionType,
)
from tests.conftest import requires_pg
from tests.integration.factories import TZ, Fixture, build_fixture

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


def rub(value: int) -> Money:
    """Сумма в рублях из целых рублей."""
    return Money(value * 100, "RUB")


def expense_spec(
    fixture: Fixture,
    *,
    amount: Money,
    category: str,
    beneficiary: str | None = None,
    account: str | None = None,
    occurred: dt.date = DAY,
    note: str | None = None,
) -> TransactionSpec:
    return TransactionSpec(
        transaction_type=TransactionType.EXPENSE,
        amount=amount,
        occurred_date=occurred,
        timezone=TZ,
        note=note,
        allocations=(
            AllocationSpec(
                role=AllocationRole.EXPENSE,
                amount=amount,
                category_id=fixture.categories[category],
                beneficiary_id=fixture.beneficiaries[beneficiary] if beneficiary else None,
            ),
        ),
        cash_legs=(
            CashLegSpec(
                signed=-amount,
                account_id=fixture.accounts[account] if account else None,
                coverage=CoverageMode.TRACKED if account else CoverageMode.UNKNOWN,
            ),
        ),
    )


async def test_a34_transfer_between_own_accounts(owner_session: AsyncSession) -> None:
    """A34: перевод 10 000 даёт −10 000 и +10 000, расход и доход равны нулю."""
    fixture = await build_fixture(owner_session)
    await post_transfer(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        amount=rub(10_000),
        from_account_id=fixture.accounts["Карта"],
        to_account_id=fixture.accounts["Кошелёк"],
        occurred_date=DAY,
        timezone=TZ,
    )
    assert (
        await account_balance(
            owner_session,
            workspace_id=fixture.workspace.id,
            account_id=fixture.accounts["Карта"],
        )
        == -1_000_000
    )
    assert (
        await account_balance(
            owner_session,
            workspace_id=fixture.workspace.id,
            account_id=fixture.accounts["Кошелёк"],
        )
        == 1_000_000
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.total_fact_minor == 0


async def test_a35_withdrawal_then_cash_purchase(owner_session: AsyncSession) -> None:
    """A35: снятие 5000 и наличная покупка 700 — кошелёк вырос на 4300."""
    fixture = await build_fixture(owner_session)
    await post_cash_withdrawal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        amount=rub(5_000),
        card_account_id=fixture.accounts["Карта"],
        cash_account_id=fixture.accounts["Кошелёк"],
        occurred_date=DAY,
        timezone=TZ,
    )
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(700), category="Продукты", account="Кошелёк"),
        origin="form",
    )
    assert (
        await account_balance(
            owner_session,
            workspace_id=fixture.workspace.id,
            account_id=fixture.accounts["Кошелёк"],
        )
        == 430_000
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.total_fact_minor == 70_000


async def test_a36_partial_refund_reduces_net_spending(owner_session: AsyncSession) -> None:
    """ADR-10, FR-25, A36: покупка 1000 и возврат 300 дают чистый расход 700; возврат не доход."""
    fixture = await build_fixture(owner_session)
    purchase = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты", account="Карта"),
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
        parts={parts[0].stable_line_id: rub(300)},
        occurred_date=DAY,
        timezone=TZ,
        account_id=fixture.accounts["Карта"],
    )
    net = await net_spending_for_line(
        owner_session,
        workspace_id=fixture.workspace.id,
        category_id=fixture.categories["Продукты"],
        beneficiary_id=None,
        date_from=fixture.period.start_date,
        date_to_exclusive=fixture.period.end_exclusive,
    )
    assert net == 70_000
    # Возврат вернул деньги на счёт, но не создал дохода.
    assert (
        await account_balance(
            owner_session,
            workspace_id=fixture.workspace.id,
            account_id=fixture.accounts["Карта"],
        )
        == -70_000
    )


async def test_a38_second_refund_cannot_exceed_purchase(owner_session: AsyncSession) -> None:
    """A38: второй возврат сверх суммы покупки не проходит автоматически."""
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
    line = parts[0].stable_line_id
    await post_refund(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        source_transaction_id=purchase.transaction_id,
        parts={line: rub(700)},
        occurred_date=DAY,
        timezone=TZ,
    )
    with pytest.raises(ConflictError, match="не возвращённую"):
        await post_refund(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            source_transaction_id=purchase.transaction_id,
            parts={line: rub(400)},
            occurred_date=DAY,
            timezone=TZ,
        )


async def test_a41_a42_mixed_payment_and_settlement(owner_session: AsyncSession) -> None:
    """CMD-22, A41/A42: оплата 3000 за двоих; потребление 1500, возмещение закрывает долг."""
    fixture = await build_fixture(owner_session)
    await post_mixed_payment(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        total=rub(3_000),
        own_share=rub(1_500),
        counterparty_label="Друг",
        counterparty_person_id=None,
        category_id=fixture.categories["Рестораны"],
        beneficiary_id=None,
        occurred_date=DAY,
        timezone=TZ,
        account_id=fixture.accounts["Карта"],
    )
    # Со счёта ушло 3000, в потребление входит только собственная доля.
    assert (
        await account_balance(
            owner_session,
            workspace_id=fixture.workspace.id,
            account_id=fixture.accounts["Карта"],
        )
        == -300_000
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.total_fact_minor == 150_000

    from sqlalchemy import select

    from fintracker.db.models.ledger import Receivable

    receivable = (
        await owner_session.execute(
            select(Receivable).where(Receivable.workspace_id == fixture.workspace.id)
        )
    ).scalar_one()
    assert receivable.outstanding_minor == 150_000

    await settle_receivable(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        receivable_id=receivable.id,
        amount=rub(1_500),
        occurred_date=DAY,
        timezone=TZ,
        account_id=fixture.accounts["Карта"],
    )
    await owner_session.refresh(receivable)
    assert receivable.outstanding_minor == 0
    assert receivable.status == "settled"
    # Возмещение не создало нового дохода: потребление осталось 1500.
    status_after = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status_after.total_fact_minor == 150_000
    assert (
        await account_balance(
            owner_session,
            workspace_id=fixture.workspace.id,
            account_id=fixture.accounts["Карта"],
        )
        == -150_000
    )


async def test_a45_a46_ar15_revision_sequence(owner_session: AsyncSession) -> None:
    """CMD-11, AR-15: 10000 → расход 1000 → правка 800 → заметка → void → restore."""
    fixture = await build_fixture(owner_session)
    account_id = fixture.accounts["Карта"]
    adjustment = OpeningAdjustment(
        workspace_id=fixture.workspace.id,
        account_id=account_id,
        amount_minor=10_000,
        effective_date=dt.date(2026, 9, 1),
        kind="opening_balance",
        reason="Начальный остаток проверки",
        created_by=fixture.user.id,
    )
    owner_session.add(adjustment)
    await owner_session.flush()
    from fintracker.db.models.ledger import AccountEntry

    owner_session.add(
        AccountEntry(
            workspace_id=fixture.workspace.id,
            account_id=account_id,
            opening_adjustment_id=adjustment.id,
            signed_minor=10_000,
            effective_date=dt.date(2026, 9, 1),
        )
    )
    await owner_session.flush()

    async def balance() -> int:
        return await account_balance(
            owner_session, workspace_id=fixture.workspace.id, account_id=account_id
        )

    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(
            fixture, amount=Money(1_000, "RUB"), category="Продукты", account="Карта"
        ),
        origin="form",
    )
    assert await balance() == 9_000

    corrected = expense_spec(
        fixture, amount=Money(800, "RUB"), category="Продукты", account="Карта"
    )
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=corrected,
        expected_version=posted.entity_version,
    )
    assert await balance() == 9_200

    with_note = expense_spec(
        fixture,
        amount=Money(800, "RUB"),
        category="Продукты",
        account="Карта",
        note="Комментарий не меняет деньги",
    )
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=with_note,
        expected_version=None,
        change_kind="note_changed",
    )
    assert await balance() == 9_200

    await void_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
    )
    assert await balance() == 10_000

    # Повторная отмена безопасна (A46).
    await void_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
    )
    assert await balance() == 10_000

    await restore_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
    )
    assert await balance() == 9_200

    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    # Отчёт показывает 800, а не сумму всех исторических ревизий.
    assert status.total_fact_minor == 800


async def test_a45_correction_blocked_by_linked_refund(owner_session: AsyncSession) -> None:
    """Дополнение к A45–A46: правка ниже суммы возврата показывает конфликт."""
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
        parts={parts[0].stable_line_id: rub(800)},
        occurred_date=DAY,
        timezone=TZ,
    )
    with pytest.raises(ConflictError, match="возвраты"):
        await revise_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            transaction_id=purchase.transaction_id,
            new_spec=expense_spec(fixture, amount=rub(500), category="Продукты"),
            expected_version=None,
        )
    with pytest.raises(ConflictError, match="возвраты"):
        await void_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            transaction_id=purchase.transaction_id,
        )


async def test_a47_date_change_moves_between_periods(owner_session: AsyncSession) -> None:
    """A47: изменение даты через границу 9/10 переносит запись между периодами."""
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    from fintracker.application.planning.periods import ensure_periods, period_for_date

    await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 20)
    )
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(
            fixture, amount=rub(500), category="Продукты", occurred=dt.date(2026, 9, 9)
        ),
        origin="form",
    )
    september = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 9)
    )
    october = await period_for_date(
        owner_session, workspace_id=fixture.workspace.id, day=dt.date(2026, 9, 10)
    )
    assert september.id != october.id

    before = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=september.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert before.total_fact_minor == 50_000

    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=expense_spec(
            fixture, amount=rub(500), category="Продукты", occurred=dt.date(2026, 9, 10)
        ),
        expected_version=posted.entity_version,
    )
    after_old = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=september.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    after_new = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=october.id,
        currency="RUB",
        today=dt.date(2026, 9, 12),
    )
    assert after_old.total_fact_minor == 0
    assert after_new.total_fact_minor == 50_000
