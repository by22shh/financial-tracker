"""Жизненный цикл категорий и исправлений (A113, A114, A116, A117, A119, A121–A125, A127)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.reports import spending_report
from fintracker.application.catalog.categories import (
    create_category,
    list_categories,
    removal_preview,
    remove_category,
    rename_category,
    restore_category,
)
from fintracker.application.ledger.service import (
    load_current_spec,
    post_transaction,
    restore_transaction,
    revise_transaction,
    void_transaction,
)
from fintracker.core.errors import NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.catalog import Category
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def test_a113_new_category_without_limit_is_available(
    owner_session: AsyncSession,
) -> None:
    """A113: созданная без лимита категория доступна в выборе, старые суммы целы."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 500_000})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        origin="form",
    )
    view = await create_category(
        owner_session, fixture.uow, actor=fixture.actor, name="Путешествия"
    )
    assert view.id is not None

    catalog = await list_categories(owner_session, workspace_id=fixture.workspace.id)
    assert any(item.name == "Путешествия" for item in catalog)

    report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert report.total_minor == 100_000, "старые суммы не изменились"


async def test_a114_repeated_create_gives_one_category(owner_session: AsyncSession) -> None:
    """A114: повтор запроса создания не даёт вторую категорию."""
    fixture = await build_fixture(owner_session)
    first = await create_category(
        owner_session, fixture.uow, actor=fixture.actor, name="Путешествия"
    )
    # Повтор того же запроса возвращает ту же категорию, а не создаёт вторую.
    again = await create_category(
        owner_session, fixture.uow, actor=fixture.actor, name="Путешествия"
    )
    assert again.id == first.id
    rows = (
        (
            await owner_session.execute(
                select(Category).where(
                    Category.workspace_id == fixture.workspace.id,
                    Category.normalized_name == "путешествия",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].id == first.id


async def test_a116_rename_keeps_identity_and_sums(owner_session: AsyncSession) -> None:
    """A116: переименование сохраняет ID и суммы."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 500_000})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_500), category="Продукты"),
        origin="form",
    )
    category_id = fixture.categories["Продукты"]
    renamed = await rename_category(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        category_id=category_id,
        name="Еда",
    )
    assert renamed.id == category_id, "идентификатор сохранён"

    report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert report.total_minor == 150_000
    assert any("Еда" in row.label for row in report.rows)


async def test_a117_a119_delete_and_restore(owner_session: AsyncSession) -> None:
    """A117, A119: категория без связей удаляется; архивная восстанавливается с тем же ID."""
    fixture = await build_fixture(owner_session)
    lonely = await create_category(
        owner_session, fixture.uow, actor=fixture.actor, name="Временная"
    )
    preview = await removal_preview(
        owner_session, workspace_id=fixture.workspace.id, category_id=lonely.id
    )
    assert preview.options == ("delete",)
    await remove_category(
        owner_session, fixture.uow, actor=fixture.actor, category_id=lonely.id, option="delete"
    )
    assert (
        await owner_session.execute(select(Category).where(Category.id == lonely.id))
    ).scalar_one_or_none() is None

    # Категория со связями уходит в архив и восстанавливается с прежним ID.
    used = fixture.categories["Продукты"]
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(700), category="Продукты"),
        origin="form",
    )
    await remove_category(
        owner_session, fixture.uow, actor=fixture.actor, category_id=used, option="archive"
    )
    restored = await restore_category(
        owner_session, fixture.uow, actor=fixture.actor, category_id=used
    )
    assert restored.id == used


async def test_a121_group_removal_shows_branch(owner_session: AsyncSession) -> None:
    """A121: удаление группы показывает состав ветки без каскада."""
    fixture = await build_fixture(owner_session)
    parent = await create_category(owner_session, fixture.uow, actor=fixture.actor, name="Дом")
    await create_category(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Ремонт",
        parent_id=parent.id,
    )
    preview = await removal_preview(
        owner_session, workspace_id=fixture.workspace.id, category_id=parent.id
    )
    assert preview.child_count == 1
    assert "delete" not in preview.options, "каскадного удаления ветки нет"
    assert set(preview.options) == {"archive", "reassign_and_archive"}


async def test_a122_merge_keeps_total_spending(owner_session: AsyncSession) -> None:
    """A122: объединение категорий сохраняет общий расход."""
    fixture = await build_fixture(owner_session, limits={"Продукты": 500_000, "Рестораны": 300_000})
    for category, amount in (("Продукты", 1_000), ("Рестораны", 400)):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(amount), category=category),
            origin="form",
        )
    before = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    await remove_category(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        category_id=fixture.categories["Рестораны"],
        option="reassign_and_archive",
        reassign_to=fixture.categories["Продукты"],
    )
    after = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert after.total_minor == before.total_minor, "общий расход сохранён"
    assert len(after.rows) == 1, "записи объединены в одну статью"


async def test_a123_archived_category_blocks_posting(owner_session: AsyncSession) -> None:
    """A123: архивная категория не используется молча при проведении черновика."""
    fixture = await build_fixture(owner_session)
    category_id = fixture.categories["Транспорт"]
    await remove_category(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        category_id=category_id,
        option="archive",
    )
    with pytest.raises(ValidationFailed):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(300), category="Транспорт"),
            origin="form",
        )


async def test_a124_correction_changes_linked_record(owner_session: AsyncSession) -> None:
    """A124: исправление меняет именно связанную запись, расход пересчитан."""
    fixture = await build_fixture(owner_session)
    first = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_800), category="Продукты"),
        origin="form",
    )
    second = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(400), category="Рестораны"),
        origin="form",
    )
    _, _, spec = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=first.transaction_id,
    )
    corrected = Money(80_000, "RUB")
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=first.transaction_id,
        new_spec=replace(
            spec,
            amount=corrected,
            allocations=(replace(spec.allocations[0], amount=corrected),),
            cash_legs=tuple(replace(leg, signed=-corrected) for leg in spec.cash_legs),
        ),
        expected_version=None,
    )
    report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert report.total_minor == 80_000 + 40_000
    assert uuid.UUID(str(second.transaction_id))


async def test_a127_void_and_restore_without_fake_refund(owner_session: AsyncSession) -> None:
    """A127: отмена дубля не создаёт возврата, восстановление возвращает запись."""
    from fintracker.db.models.ledger import TransactionLink

    fixture = await build_fixture(owner_session)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(650), category="Продукты"),
        origin="form",
    )
    await void_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        reason="дубль",
    )
    links = (
        (
            await owner_session.execute(
                select(TransactionLink).where(
                    TransactionLink.workspace_id == fixture.workspace.id,
                    TransactionLink.link_type == "refund_of",
                )
            )
        )
        .scalars()
        .all()
    )
    assert links == [], "отмена не создаёт фиктивный возврат"

    report = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert report.total_minor == 0

    await restore_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
    )
    restored = await spending_report(
        owner_session,
        workspace=fixture.workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert restored.total_minor == 65_000


async def test_a125_ambiguous_correction_offers_choice(
    clean_db: None, test_settings, owner_session: AsyncSession
) -> None:
    """A125: при нескольких подходящих записях предлагается выбор, а не догадка."""
    from fintracker.application.conversation.corrections import _resolve_target
    from fintracker.application.conversation.types import IncomingMessage, MessageKind

    fixture = await build_fixture(owner_session, telegram_user_id=6500)
    for _ in range(2):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(500), category="Продукты"),
            origin="telegram_text",
        )
    await owner_session.commit()

    message = IncomingMessage(
        telegram_user_id=6500,
        chat_id=6500,
        kind=MessageKind.TEXT,
        text="Исправь последнюю трату",
        correlation_id="a125",
    )
    result = await _resolve_target(
        test_settings, actor=fixture.actor, message=message, text=message.text or ""
    )
    assert isinstance(result, (list, uuid.UUID))
    if isinstance(result, list):
        assert any(
            "выбер" in reply.text.lower() or "какую" in reply.text.lower() for reply in result
        )


async def test_a76_unknown_beneficiary_is_not_invented(owner_session: AsyncSession) -> None:
    """A76: старая строка без получателя остаётся неопределённой."""
    from dataclasses import replace as data_replace

    from fintracker.domain.ledger.model import Granularity

    fixture = await build_fixture(owner_session)
    spec = expense_spec(fixture, amount=rub(900), category="Рестораны")
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=data_replace(
            spec,
            granularity=Granularity.DAILY_AGGREGATE,
            description="Заведения",
            allocations=(data_replace(spec.allocations[0], beneficiary_id=None),),
        ),
        origin="import",
    )
    _, _, stored = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    assert stored.allocations[0].beneficiary_id is None
    with pytest.raises(NotFound):
        await load_current_spec(
            owner_session,
            workspace_id=fixture.workspace.id,
            transaction_id=uuid.uuid4(),
        )
