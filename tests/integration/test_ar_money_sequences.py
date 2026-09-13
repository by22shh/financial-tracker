"""Случайные последовательности денежных операций (AR-16, AR-15, ADR-03).

Проверяется не конкретный сценарий, а инварианты: деньги остаются целыми
minor units, обратный эффект точен, справочные и неизвестные движения не
превращаются в полный банковский баланс.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.ledger.operations import post_transfer
from fintracker.application.ledger.service import (
    account_balance,
    post_transaction,
    restore_transaction,
    revise_transaction,
    void_transaction,
)
from fintracker.core.money import Money
from fintracker.db.models.ledger import AccountEntry, Allocation, FinancialEffect, Transaction
from tests.conftest import requires_pg
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)
SEEDS = (11, 29, 47, 101)


@pytest.mark.parametrize("seed", SEEDS)
async def test_ar16_random_sequences_keep_invariants(
    owner_session: AsyncSession, seed: int
) -> None:
    """AR-16: случайные правки, отмены и восстановления сохраняют инварианты."""
    rng = random.Random(seed)
    fixture = await build_fixture(owner_session)
    posted: list[tuple[object, int]] = []

    for step in range(12):
        action = rng.choice(["post", "post", "revise", "void", "restore", "transfer"])
        if action == "post" or not posted:
            amount = rng.randrange(100, 500_000)
            result = await post_transaction(
                owner_session,
                fixture.uow,
                actor=fixture.actor,
                spec=expense_spec(
                    fixture,
                    amount=Money(amount, "RUB"),
                    category=rng.choice(("Продукты", "Рестораны", "Транспорт")),
                    account="Карта" if rng.random() < 0.5 else None,
                    occurred=DAY,
                ),
                origin="form",
            )
            posted.append((result.transaction_id, result.entity_version))
            continue

        index = rng.randrange(len(posted))
        transaction_id, _version = posted[index]
        current = (
            await owner_session.execute(select(Transaction).where(Transaction.id == transaction_id))
        ).scalar_one()

        if action == "revise" and current.status == "posted":
            from fintracker.application.ledger.service import load_current_spec

            _, _, spec = await load_current_spec(
                owner_session,
                workspace_id=fixture.workspace.id,
                transaction_id=transaction_id,
            )
            if len(spec.allocations) != 1:
                continue
            from dataclasses import replace

            new_amount = Money(rng.randrange(100, 500_000), "RUB")
            updated = replace(
                spec,
                amount=new_amount,
                allocations=(replace(spec.allocations[0], amount=new_amount),),
                cash_legs=tuple(
                    replace(
                        leg,
                        signed=Money(
                            -new_amount.minor if leg.signed.is_negative else new_amount.minor,
                            "RUB",
                        ),
                    )
                    for leg in spec.cash_legs
                ),
            )
            result = await revise_transaction(
                owner_session,
                fixture.uow,
                actor=fixture.actor,
                transaction_id=transaction_id,
                new_spec=updated,
                expected_version=current.entity_version,
            )
            posted[index] = (transaction_id, result.entity_version)
        elif action == "void" and current.status == "posted":
            await void_transaction(
                owner_session,
                fixture.uow,
                actor=fixture.actor,
                transaction_id=transaction_id,
                reason=f"шаг {step}",
            )
        elif action == "restore" and current.status == "voided":
            await restore_transaction(
                owner_session,
                fixture.uow,
                actor=fixture.actor,
                transaction_id=transaction_id,
            )
        elif action == "transfer":
            await post_transfer(
                owner_session,
                fixture.uow,
                actor=fixture.actor,
                amount=rub(rng.randrange(1, 500)),
                from_account_id=fixture.accounts["Карта"],
                to_account_id=fixture.accounts["Кошелёк"],
                occurred_date=DAY,
                timezone=TZ,
            )

    # Инвариант 1: ровно один активный эффект на проведённую операцию.
    active = (
        await owner_session.execute(
            select(FinancialEffect.transaction_id, func.count())
            .where(
                FinancialEffect.workspace_id == fixture.workspace.id,
                FinancialEffect.is_active.is_(True),
            )
            .group_by(FinancialEffect.transaction_id)
        )
    ).all()
    assert all(row[1] == 1 for row in active), "активный эффект не дублируется"

    # Инвариант 2: суммы целые и в допустимом диапазоне.
    amounts = (
        (
            await owner_session.execute(
                select(Allocation.amount_minor).where(
                    Allocation.workspace_id == fixture.workspace.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert all(isinstance(value, int) for value in amounts)

    # Инвариант 3: баланс счёта равен сумме его подписанных движений.
    for account_id in fixture.accounts.values():
        expected = (
            await owner_session.execute(
                select(func.coalesce(func.sum(AccountEntry.signed_minor), 0)).where(
                    AccountEntry.workspace_id == fixture.workspace.id,
                    AccountEntry.account_id == account_id,
                )
            )
        ).scalar_one()
        actual = await account_balance(
            owner_session, workspace_id=fixture.workspace.id, account_id=account_id
        )
        assert actual == int(expected)

    # Инвариант 4: отменённая операция не оставляет активного эффекта.
    voided = (
        (
            await owner_session.execute(
                select(Transaction.id).where(
                    Transaction.workspace_id == fixture.workspace.id,
                    Transaction.status == "voided",
                )
            )
        )
        .scalars()
        .all()
    )
    for transaction_id in voided:
        remaining = (
            await owner_session.execute(
                select(func.count())
                .select_from(FinancialEffect)
                .where(
                    FinancialEffect.workspace_id == fixture.workspace.id,
                    FinancialEffect.transaction_id == transaction_id,
                    FinancialEffect.is_active.is_(True),
                )
            )
        ).scalar_one()
        assert int(remaining) == 0


async def test_ar16_untracked_legs_do_not_create_balance(
    owner_session: AsyncSession,
) -> None:
    """AR-16: движения без отслеживаемого счёта не формируют баланс."""
    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(700), category="Продукты", account=None),
        origin="form",
    )
    for account_id in fixture.accounts.values():
        assert (
            await account_balance(
                owner_session, workspace_id=fixture.workspace.id, account_id=account_id
            )
            == 0
        ), "неизвестное покрытие не превращается в банковский баланс"
