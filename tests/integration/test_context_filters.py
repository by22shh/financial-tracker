"""Комментарии, люди и фильтры (FR-87–FR-89, A181–A200, AR-19, R05)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.reports import FilterSpec, spending_report
from fintracker.application.catalog.directory import (
    create_person,
    create_tag,
    resolve_person_alias,
)
from fintracker.application.ledger.service import (
    load_current_spec,
    post_transaction,
    revise_transaction,
)
from fintracker.core.errors import ConflictError
from fintracker.db.models.ledger import TransactionRevision
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def test_a181_author_spender_beneficiary_are_independent(
    owner_session: AsyncSession,
) -> None:
    """A181: автор, совершивший покупку и получатель — разные поля."""
    fixture = await build_fixture(owner_session)
    sofa = await create_person(owner_session, fixture.uow, actor=fixture.actor, name="Софа")
    spec = expense_spec(
        fixture,
        amount=rub(1_200),
        category="Продукты",
        beneficiary="Общее",
        note="на ужин в выходные",
    )
    from dataclasses import replace

    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(spec, spender_person_id=sofa.id),
        origin="telegram_text",
    )
    _, revision, _ = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    assert revision.spender_person_id == sofa.id
    assert revision.note == "на ужин в выходные"

    from fintracker.db.models.ledger import Allocation

    allocation = (
        await owner_session.execute(
            select(Allocation).where(
                Allocation.transaction_id == posted.transaction_id,
                Allocation.revision == revision.revision,
            )
        )
    ).scalar_one()
    assert allocation.beneficiary_id == fixture.beneficiaries["Общее"]


async def test_a185_note_change_does_not_touch_money(owner_session: AsyncSession) -> None:
    """A185: изменение и удаление комментария не меняют сумму и остаток."""
    from dataclasses import replace

    fixture = await build_fixture(owner_session, limits={"Продукты": 1_000_000})
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        origin="form",
    )
    _, _, spec = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=replace(spec, note="первый комментарий"),
        expected_version=None,
        change_kind="note_changed",
    )
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=replace(spec, note=None),
        expected_version=None,
        change_kind="note_changed",
    )
    _, revision, _ = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    assert revision.note is None
    assert revision.amount_minor == 100_000

    # История ревизий сохранена, включая удалённый текст.
    revisions = (
        (
            await owner_session.execute(
                select(TransactionRevision.note)
                .where(TransactionRevision.transaction_id == posted.transaction_id)
                .order_by(TransactionRevision.revision)
            )
        )
        .scalars()
        .all()
    )
    assert revisions == [None, "первый комментарий", None]


async def test_note_length_limit_is_not_silently_truncated(
    owner_session: AsyncSession,
) -> None:
    """LIM-04/FR-87: превышение длины комментария не обрезается молча."""
    from dataclasses import replace

    fixture = await build_fixture(owner_session)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(100), category="Продукты"),
        origin="form",
    )
    _, _, spec = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    with pytest.raises(Exception):
        await revise_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            transaction_id=posted.transaction_id,
            new_spec=replace(spec, note="я" * 2001),
            expected_version=None,
            change_kind="note_changed",
        )


async def test_a189_person_profile_does_not_create_membership(
    owner_session: AsyncSession,
) -> None:
    """A189: аналитический профиль человека не создаёт членство и приглашение."""
    from fintracker.db.models.access import BudgetInvite, Membership

    fixture = await build_fixture(owner_session)
    person = await create_person(owner_session, fixture.uow, actor=fixture.actor, name="Марина")
    assert person.user_id is None
    memberships = (
        (
            await owner_session.execute(
                select(Membership).where(Membership.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(memberships) == 1, "новых членств не появилось"
    invites = (
        (
            await owner_session.execute(
                select(BudgetInvite).where(BudgetInvite.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert invites == []


async def test_a191_ambiguous_alias_requires_clarification(
    owner_session: AsyncSession,
) -> None:
    """A191: неподтверждённый алиас не разрешается догадкой по чужому профилю."""
    fixture = await build_fixture(owner_session)
    await create_person(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Анна",
        aliases=("моя девушка",),
    )
    await create_person(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Мария",
        aliases=("моя девушка",),
    )
    with pytest.raises(ConflictError, match="уточните"):
        await resolve_person_alias(
            owner_session, workspace_id=fixture.workspace.id, alias="моя девушка"
        )
    unknown = await resolve_person_alias(
        owner_session, workspace_id=fixture.workspace.id, alias="сосед"
    )
    assert unknown is None, "неизвестное имя остаётся неизвестным"


async def test_a193_ar19_combined_filters_do_not_multiply_sums(
    owner_session: AsyncSession,
) -> None:
    """A193/AR-19: составные фильтры не размножают суммы из-за меток и частей."""
    from dataclasses import replace

    from fintracker.domain.ledger.model import AllocationRole, AllocationSpec

    fixture = await build_fixture(owner_session, categories=("Продукты", "Дом"))
    sofa = await create_person(owner_session, fixture.uow, actor=fixture.actor, name="Софа")
    trip_tag = await create_tag(owner_session, fixture.uow, actor=fixture.actor, name="поездка")
    gift_tag = await create_tag(owner_session, fixture.uow, actor=fixture.actor, name="подарок")

    # Смешанный чек: Продукты/Софа 1000 и Дом/Ниджат 400.
    total = rub(1_400)
    mixed = expense_spec(fixture, amount=total, category="Продукты")
    mixed = replace(
        mixed,
        spender_person_id=sofa.id,
        note="поездка на выходные",
        tag_ids=(trip_tag, gift_tag),
        allocations=(
            AllocationSpec(
                role=AllocationRole.EXPENSE,
                amount=rub(1_000),
                category_id=fixture.categories["Продукты"],
                beneficiary_id=fixture.beneficiaries["Софа"],
            ),
            AllocationSpec(
                role=AllocationRole.EXPENSE,
                amount=rub(400),
                category_id=fixture.categories["Дом"],
                beneficiary_id=fixture.beneficiaries["Ниджат"],
            ),
        ),
    )
    await post_transaction(
        owner_session, fixture.uow, actor=fixture.actor, spec=mixed, origin="telegram_photo"
    )

    period = (fixture.period.start_date, fixture.period.end_exclusive)

    # Фильтр «Продукты» даёт 1000 и одну покупку, полный чек показан отдельно.
    groceries = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(category_ids=(fixture.categories["Продукты"],)),
    )
    assert groceries.total_minor == 100_000
    assert groceries.transaction_count == 1
    assert groceries.matched_transaction_total_minor == 140_000

    # Фильтр «Продукты + Ниджат» даёт 0: такой части в чеке нет.
    incompatible = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(
            category_ids=(fixture.categories["Продукты"],),
            beneficiary_ids=(fixture.beneficiaries["Ниджат"],),
        ),
    )
    assert incompatible.total_minor == 0

    # Две совпадающие метки не удваивают сумму.
    tagged = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(tag_ids=(trip_tag, gift_tag), tag_mode="any"),
    )
    assert tagged.total_minor == 140_000
    assert tagged.transaction_count == 1

    # Составной фильтр: человек + автор + категория + текст комментария.
    combined = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(
            category_ids=(fixture.categories["Продукты"],),
            spender_person_ids=(sofa.id,),
            actor_user_ids=(fixture.user.id,),
            note_query="поездк",
        ),
    )
    assert combined.total_minor == 100_000
    assert combined.transaction_count == 1


async def test_a195_text_search_and_person_filter_differ(
    owner_session: AsyncSession,
) -> None:
    """A195: имя в комментарии не заменяет структурированное поле."""
    from dataclasses import replace

    fixture = await build_fixture(owner_session)
    nidzhat = await create_person(owner_session, fixture.uow, actor=fixture.actor, name="Ниджат")
    sofa = await create_person(owner_session, fixture.uow, actor=fixture.actor, name="Софа")
    spec = expense_spec(fixture, amount=rub(2_000), category="Продукты")
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(spec, spender_person_id=nidzhat.id, note="подарок Софе"),
        origin="form",
    )
    period = (fixture.period.start_date, fixture.period.end_exclusive)

    by_text = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(note_query="Софе"),
    )
    assert by_text.total_minor == 200_000, "текстовый поиск находит заметку"

    by_person = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(spender_person_ids=(sofa.id,)),
    )
    assert by_person.total_minor == 0, "фильтр по человеку не включает эту запись"


async def test_a194_unknown_spender_is_not_attributed(
    owner_session: AsyncSession,
) -> None:
    """A194: записи с неизвестным человеком не приписываются другим."""
    from dataclasses import replace

    fixture = await build_fixture(owner_session)
    sofa = await create_person(owner_session, fixture.uow, actor=fixture.actor, name="Софа")
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(300), category="Продукты"),
        origin="form",
    )
    known = expense_spec(fixture, amount=rub(700), category="Продукты")
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(known, spender_person_id=sofa.id),
        origin="form",
    )
    period = (fixture.period.start_date, fixture.period.end_exclusive)
    by_sofa = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(spender_person_ids=(sofa.id,)),
    )
    assert by_sofa.total_minor == 70_000, "неизвестный человек не попал в выборку"


async def test_note_search_is_case_insensitive_and_escapes_wildcards(
    owner_session: AsyncSession,
) -> None:
    """A193/DATA_CONTRACT §3: поиск без учёта регистра, литералы экранируются."""
    from dataclasses import replace

    fixture = await build_fixture(owner_session)
    spec = expense_spec(fixture, amount=rub(500), category="Продукты")
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(spec, note="Поездка 100% оплачена"),
        origin="form",
    )
    period = (fixture.period.start_date, fixture.period.end_exclusive)

    lower = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(note_query="поездка"),
    )
    upper = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(note_query="ПОЕЗДКА"),
    )
    assert lower.total_minor == upper.total_minor == 50_000

    # Символ '%' ищется буквально, а не как шаблон.
    literal = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(note_query="100%"),
    )
    assert literal.total_minor == 50_000
    nothing = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(note_query="%%%"),
    )
    assert nothing.total_minor == 0


async def test_has_note_filters(owner_session: AsyncSession) -> None:
    """FR-89: состояния «есть комментарий» и «без комментария»."""
    from dataclasses import replace

    fixture = await build_fixture(owner_session)
    spec = expense_spec(fixture, amount=rub(100), category="Продукты")
    await post_transaction(
        owner_session, fixture.uow, actor=fixture.actor, spec=spec, origin="form"
    )
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=replace(spec, note="есть"),
        origin="form",
    )
    period = (fixture.period.start_date, fixture.period.end_exclusive)
    with_note = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(has_note=True),
    )
    without_note = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=period[0],
        date_to_exclusive=period[1],
        filters=FilterSpec(has_note=False),
    )
    assert with_note.total_minor == 10_000
    assert without_note.total_minor == 10_000
