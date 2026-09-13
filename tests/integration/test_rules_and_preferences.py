"""Правила классификации и личные настройки (FR-19, FR-23, FR-24, FR-54, CMD-26/27/30/31)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.rules import (
    RulePriority,
    archive_rule,
    classify,
    create_rule,
    learn_from_correction,
    list_rules,
)
from fintracker.application.identity.preferences import (
    NOTIFICATION_FAMILIES,
    get_preferences,
    set_input_preferences,
    set_notification_family,
    set_quiet_hours,
    update_workspace_settings,
)
from fintracker.application.ledger.service import post_transaction
from fintracker.core.context import MembershipStatus, Role
from fintracker.core.errors import ConflictError, NotFound, PermissionDenied, ValidationFailed
from fintracker.core.ids import new_generation
from fintracker.db.models.access import Membership, User
from fintracker.db.models.catalog import Category, CategoryAlias, ClassificationRule
from tests.conftest import requires_pg
from tests.integration.factories import Fixture, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def _second_member(session: AsyncSession, fixture: Fixture) -> tuple[User, uuid.UUID]:
    """Второй активный участник того же бюджета."""
    user = User(id=uuid.uuid4(), telegram_user_id=int(uuid.uuid4().int % 10**9))
    session.add(user)
    await session.flush()
    membership = Membership(
        workspace_id=fixture.workspace.id,
        user_id=user.id,
        role=Role.MEMBER.value,
        status=MembershipStatus.ACTIVE.value,
        generation=new_generation(),
    )
    session.add(membership)
    await session.flush()
    return user, membership.id


async def test_member_rule_beats_workspace_rule(owner_session: AsyncSession) -> None:
    """FR-23: личное правило участника приоритетнее общего правила бюджета."""
    fixture = await build_fixture(owner_session)
    await create_rule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        keyword="кофе",
        category_id=fixture.categories["Продукты"],
        scope="workspace",
    )
    await create_rule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        keyword="кофе",
        category_id=fixture.categories["Рестораны"],
        scope="member",
    )

    match = await classify(
        owner_session,
        workspace_id=fixture.workspace.id,
        membership_id=fixture.actor.membership_id,
        text="кофе 250",
    )
    assert match is not None
    assert match.category_id == fixture.categories["Рестораны"]
    assert match.basis == "rule_member"

    # Для другого участника личное правило первого не применяется (FR-23).
    _, other_membership = await _second_member(owner_session, fixture)
    other = await classify(
        owner_session,
        workspace_id=fixture.workspace.id,
        membership_id=other_membership,
        text="кофе 250",
    )
    assert other is not None
    assert other.category_id == fixture.categories["Продукты"]
    assert other.basis == "rule_workspace"


async def test_rule_beats_alias_and_alias_beats_nothing(owner_session: AsyncSession) -> None:
    """FR-23: правило важнее синонима, синоним важнее отсутствия совпадения."""
    fixture = await build_fixture(owner_session)
    owner_session.add(
        CategoryAlias(
            workspace_id=fixture.workspace.id,
            category_id=fixture.categories["Продукты"],
            alias="бензин",
            normalized_alias="бензин",
            created_by=fixture.user.id,
        )
    )
    await owner_session.flush()

    by_alias = await classify(
        owner_session,
        workspace_id=fixture.workspace.id,
        membership_id=fixture.actor.membership_id,
        text="бензин 3000",
    )
    assert by_alias is not None
    assert by_alias.basis == "alias"
    assert by_alias.category_id == fixture.categories["Продукты"]

    await create_rule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        keyword="бензин",
        category_id=fixture.categories["Транспорт"],
        scope="workspace",
    )
    by_rule = await classify(
        owner_session,
        workspace_id=fixture.workspace.id,
        membership_id=fixture.actor.membership_id,
        text="бензин 3000",
    )
    assert by_rule is not None
    assert by_rule.basis == "rule_workspace"
    assert by_rule.category_id == fixture.categories["Транспорт"]

    assert (
        await classify(
            owner_session,
            workspace_id=fixture.workspace.id,
            membership_id=fixture.actor.membership_id,
            text="непонятная строка",
        )
        is None
    )


async def test_more_specific_rule_wins_inside_level(owner_session: AsyncSession) -> None:
    """FR-23: внутри одного уровня приоритетнее более узкое условие."""
    fixture = await build_fixture(owner_session)
    await create_rule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        keyword="кофе",
        category_id=fixture.categories["Продукты"],
        scope="workspace",
    )
    await create_rule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        keyword="кофе с собой",
        category_id=fixture.categories["Рестораны"],
        scope="workspace",
    )
    match = await classify(
        owner_session,
        workspace_id=fixture.workspace.id,
        membership_id=fixture.actor.membership_id,
        text="кофе с собой 250",
    )
    assert match is not None
    assert match.category_id == fixture.categories["Рестораны"]


async def test_equally_specific_conflict_is_refused(owner_session: AsyncSession) -> None:
    """FR-23: одинаково подходящие правила не разрешаются случайным порядком."""
    fixture = await build_fixture(owner_session)
    for category, keyword in (("Продукты", "кофе"), ("Рестораны", "чай")):
        owner_session.add(
            ClassificationRule(
                workspace_id=fixture.workspace.id,
                scope="workspace",
                owner_membership_id=None,
                priority=int(RulePriority.WORKSPACE),
                specificity=4,
                condition={"contains": keyword},
                action={"category_id": str(fixture.categories[category]), "beneficiary_id": None},
                created_by=fixture.user.id,
            )
        )
    await owner_session.flush()

    with pytest.raises(ConflictError):
        await classify(
            owner_session,
            workspace_id=fixture.workspace.id,
            membership_id=fixture.actor.membership_id,
            text="кофе и чай 500",
        )


async def test_learning_does_not_reclassify_past(owner_session: AsyncSession) -> None:
    """FR-24: правило из исправления действует только вперёд."""
    fixture = await build_fixture(owner_session)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(250), category="Продукты", note="кофе"),
        origin="form",
    )
    rule = await learn_from_correction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        keyword="кофе",
        category_id=fixture.categories["Рестораны"],
    )
    assert rule.category_id == fixture.categories["Рестораны"]

    from fintracker.application.ledger.service import load_current_spec

    _, _, spec = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    # Прошлая операция сохраняет исходную категорию.
    assert spec.allocations[0].category_id == fixture.categories["Продукты"]

    match = await classify(
        owner_session,
        workspace_id=fixture.workspace.id,
        membership_id=fixture.actor.membership_id,
        text="кофе 250",
    )
    assert match is not None
    assert match.category_id == fixture.categories["Рестораны"]


async def test_rule_for_archived_category_is_refused(owner_session: AsyncSession) -> None:
    """FR-24: правило не может указывать на архивную категорию."""
    fixture = await build_fixture(owner_session)
    category = (
        await owner_session.execute(
            select(Category).where(Category.id == fixture.categories["Транспорт"])
        )
    ).scalar_one()
    category.archived_at = dt.datetime.now(dt.UTC)
    await owner_session.flush()

    with pytest.raises(NotFound):
        await create_rule(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            keyword="метро",
            category_id=fixture.categories["Транспорт"],
        )


async def test_archived_rule_stops_matching(owner_session: AsyncSession) -> None:
    """CMD-27: архивное правило перестаёт применяться и видно в списке."""
    fixture = await build_fixture(owner_session)
    rule = await create_rule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        keyword="метро",
        category_id=fixture.categories["Транспорт"],
    )
    await archive_rule(owner_session, fixture.uow, actor=fixture.actor, rule_id=rule.id)
    assert (
        await classify(
            owner_session,
            workspace_id=fixture.workspace.id,
            membership_id=fixture.actor.membership_id,
            text="метро 70",
        )
        is None
    )
    active = await list_rules(
        owner_session,
        workspace_id=fixture.workspace.id,
        membership_id=fixture.actor.membership_id,
    )
    assert all(item.id != rule.id for item in active)
    with_archived = await list_rules(
        owner_session,
        workspace_id=fixture.workspace.id,
        membership_id=fixture.actor.membership_id,
        include_archived=True,
    )
    assert any(item.id == rule.id and item.archived for item in with_archived)


async def test_notification_family_is_personal(owner_session: AsyncSession) -> None:
    """FR-54, A65: отключение семейства не меняет доставку другому участнику."""
    fixture = await build_fixture(owner_session)
    other_user, _ = await _second_member(owner_session, fixture)

    updated = await set_notification_family(
        owner_session,
        user_id=fixture.user.id,
        workspace_id=fixture.workspace.id,
        family="threshold",
        mode="off",
    )
    assert updated.families["threshold"] == "off"
    assert updated.families["reminder"] == "immediate"

    other = await get_preferences(
        owner_session, user_id=other_user.id, workspace_id=fixture.workspace.id
    )
    assert other.families["threshold"] == "immediate"
    assert set(other.families) == set(NOTIFICATION_FAMILIES)


async def test_notification_family_validation_and_conflict(owner_session: AsyncSession) -> None:
    """CMD-26: неизвестное семейство и устаревшая версия отклоняются."""
    fixture = await build_fixture(owner_session)
    with pytest.raises(ValidationFailed):
        await set_notification_family(
            owner_session,
            user_id=fixture.user.id,
            workspace_id=fixture.workspace.id,
            family="unknown_family",
            mode="off",
        )
    with pytest.raises(ValidationFailed):
        await set_notification_family(
            owner_session,
            user_id=fixture.user.id,
            workspace_id=fixture.workspace.id,
            family="review",
            mode="never",
        )
    current = await set_notification_family(
        owner_session,
        user_id=fixture.user.id,
        workspace_id=fixture.workspace.id,
        family="review",
        mode="digest",
    )
    with pytest.raises(ConflictError):
        await set_notification_family(
            owner_session,
            user_id=fixture.user.id,
            workspace_id=fixture.workspace.id,
            family="review",
            mode="off",
            expected_version=current.version + 5,
        )


async def test_quiet_hours_saved_in_personal_timezone(owner_session: AsyncSession) -> None:
    """FR-53, LIM-07: тихие часы хранятся в личном поясе получателя."""
    fixture = await build_fixture(owner_session)
    prefs = await set_quiet_hours(
        owner_session,
        user_id=fixture.user.id,
        workspace_id=fixture.workspace.id,
        start_hour=23,
        end_hour=8,
        timezone="Europe/Moscow",
    )
    assert (prefs.quiet_hours_start, prefs.quiet_hours_end) == (23, 8)
    assert prefs.timezone == "Europe/Moscow"
    with pytest.raises(ValidationFailed):
        await set_quiet_hours(
            owner_session,
            user_id=fixture.user.id,
            workspace_id=fixture.workspace.id,
            start_hour=25,
            end_hour=8,
        )


async def test_input_preferences_are_per_member(owner_session: AsyncSession) -> None:
    """FR-19, CMD-26: автозапись и порог крупной суммы личные."""
    fixture = await build_fixture(owner_session)
    other_user, _ = await _second_member(owner_session, fixture)

    membership = await set_input_preferences(
        owner_session,
        actor=fixture.actor,
        autopost=True,
        large_amount_threshold_minor=500_000,
    )
    assert membership.autopost_enabled is True
    assert membership.large_amount_threshold_minor == 500_000

    other = (
        await owner_session.execute(
            select(Membership).where(
                Membership.workspace_id == fixture.workspace.id,
                Membership.user_id == other_user.id,
            )
        )
    ).scalar_one()
    assert other.autopost_enabled is False

    with pytest.raises(ValidationFailed):
        await set_input_preferences(
            owner_session, actor=fixture.actor, large_amount_threshold_minor=0
        )


async def test_currency_change_refused_when_money_exists(owner_session: AsyncSession) -> None:
    """CMD-30: валюта меняется только в пустом бюджете."""
    fixture = await build_fixture(owner_session)
    renamed = await update_workspace_settings(
        owner_session, fixture.uow, actor=fixture.actor, currency="USD"
    )
    assert renamed.currency == "USD"

    await update_workspace_settings(owner_session, fixture.uow, actor=fixture.actor, currency="RUB")
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(100), category="Продукты"),
        origin="form",
    )
    with pytest.raises(ConflictError):
        await update_workspace_settings(
            owner_session, fixture.uow, actor=fixture.actor, currency="USD"
        )


async def test_workspace_settings_require_admin(owner_session: AsyncSession) -> None:
    """CMD-30: общие настройки бюджета меняет только администратор."""
    fixture = await build_fixture(owner_session)
    _, other_membership = await _second_member(owner_session, fixture)
    from dataclasses import replace

    member_actor = replace(fixture.actor, role=Role.MEMBER, membership_id=other_membership)
    with pytest.raises(PermissionDenied):
        await update_workspace_settings(
            owner_session, fixture.uow, actor=member_actor, name="Чужое имя"
        )
