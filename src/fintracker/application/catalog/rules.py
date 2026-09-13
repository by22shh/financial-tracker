"""Правила классификации и обучение на исправлениях (FR-23, FR-24, CMD-27).

Приоритет: явное указание отправителя → подтверждённое персональное правило
в этом бюджете → подтверждённое общее правило → точный алиас → ранее
исправленные похожие примеры → модельная классификация → уточнение.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import IntEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.normalize import normalize_name
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, PermissionDenied, ValidationFailed
from fintracker.db.models.catalog import Category, CategoryAlias, ClassificationRule
from fintracker.db.uow import UnitOfWork


class RulePriority(IntEnum):
    """Меньшее значение — выше приоритет (FR-23)."""

    MEMBER = 10
    WORKSPACE = 20
    ALIAS = 30
    EXAMPLE = 40


@dataclass(frozen=True, slots=True)
class RuleView:
    id: uuid.UUID
    scope: str
    keyword: str
    category_id: uuid.UUID
    category_name: str
    beneficiary_id: uuid.UUID | None
    priority: int
    specificity: int
    version: int
    archived: bool


@dataclass(frozen=True, slots=True)
class ClassificationMatch:
    category_id: uuid.UUID
    beneficiary_id: uuid.UUID | None
    basis: str
    rule_id: uuid.UUID | None


async def create_rule(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    keyword: str,
    category_id: uuid.UUID,
    scope: str = "member",
    beneficiary_id: uuid.UUID | None = None,
) -> RuleView:
    """Сохранить правило классификации (FR-24, A-обучение на исправлениях).

    По умолчанию правило личное в рамках текущего бюджета; общее изменение
    видно всем и требует прав FR-04.
    """
    workspace_id = actor.require_workspace()
    if scope not in {"member", "workspace"}:
        raise ValidationFailed("Область правила должна быть member или workspace")
    normalized = normalize_name(keyword)
    if not normalized:
        raise ValidationFailed("Условие правила не может быть пустым")

    category = (
        await session.execute(
            select(Category).where(
                Category.workspace_id == workspace_id,
                Category.id == category_id,
                Category.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if category is None:
        raise NotFound("Категория недоступна или находится в архиве")

    owner = actor.membership_id if scope == "member" else None
    existing = (
        await session.execute(
            select(ClassificationRule).where(
                ClassificationRule.workspace_id == workspace_id,
                ClassificationRule.scope == scope,
                ClassificationRule.owner_membership_id.is_(owner)
                if owner is None
                else ClassificationRule.owner_membership_id == owner,
                ClassificationRule.archived_at.is_(None),
                ClassificationRule.condition["contains"].astext == normalized,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Повтор не создаёт второе правило; меняется только цель.
        existing.action = {
            "category_id": str(category_id),
            "beneficiary_id": str(beneficiary_id) if beneficiary_id else None,
        }
        existing.version += 1
        await session.flush()
        row = existing
    else:
        row = ClassificationRule(
            workspace_id=workspace_id,
            scope=scope,
            owner_membership_id=owner,
            priority=int(RulePriority.MEMBER if scope == "member" else RulePriority.WORKSPACE),
            # Более узкое правило приоритетнее общего внутри уровня (FR-23).
            specificity=len(normalized),
            condition={"contains": normalized},
            action={
                "category_id": str(category_id),
                "beneficiary_id": str(beneficiary_id) if beneficiary_id else None,
            },
            created_by=actor.user_id,
        )
        session.add(row)
        await session.flush()

    await uow.bump_revisions(workspace_id, catalog=True)
    return RuleView(
        id=row.id,
        scope=row.scope,
        keyword=normalized,
        category_id=category_id,
        category_name=category.name,
        beneficiary_id=beneficiary_id,
        priority=row.priority,
        specificity=row.specificity,
        version=row.version,
        archived=False,
    )


async def list_rules(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    membership_id: uuid.UUID | None,
    include_archived: bool = False,
) -> list[RuleView]:
    """Правила, видимые этому участнику: общие и его личные (CMD-27).

    Архивные показываются по явному запросу: участник должен видеть, почему
    правило перестало применяться, а не только его отсутствие.
    """
    statement = select(ClassificationRule).where(
        ClassificationRule.workspace_id == workspace_id,
    )
    if not include_archived:
        statement = statement.where(ClassificationRule.archived_at.is_(None))
    rows = (
        (
            await session.execute(
                statement.order_by(
                    ClassificationRule.priority, ClassificationRule.specificity.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    names = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Category.id, Category.name).where(Category.workspace_id == workspace_id)
            )
        ).all()
    }
    result: list[RuleView] = []
    for rule in rows:
        if rule.scope == "member" and rule.owner_membership_id != membership_id:
            continue
        raw_category = rule.action.get("category_id")
        if not raw_category:
            continue
        category_id = uuid.UUID(str(raw_category))
        result.append(
            RuleView(
                id=rule.id,
                scope=rule.scope,
                keyword=str(rule.condition.get("contains", "")),
                category_id=category_id,
                category_name=names.get(category_id, "?"),
                beneficiary_id=(
                    uuid.UUID(str(rule.action["beneficiary_id"]))
                    if rule.action.get("beneficiary_id")
                    else None
                ),
                priority=rule.priority,
                specificity=rule.specificity,
                version=rule.version,
                archived=rule.archived_at is not None,
            )
        )
    return result


async def archive_rule(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    rule_id: uuid.UUID,
    expected_version: int | None = None,
) -> None:
    """Архивировать правило; уже записанные траты не переклассифицируются."""
    workspace_id = actor.require_workspace()
    row = (
        await session.execute(
            select(ClassificationRule)
            .where(
                ClassificationRule.workspace_id == workspace_id,
                ClassificationRule.id == rule_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Правило недоступно")
    if row.scope == "member" and row.owner_membership_id != actor.membership_id:
        raise PermissionDenied("Личное правило изменяет только его автор")
    uow.check_expected_version(row.version, expected_version, label="Правило")
    row.archived_at = func.now()
    row.version += 1
    await session.flush()
    await uow.bump_revisions(workspace_id, catalog=True)


async def classify(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    membership_id: uuid.UUID | None,
    text: str,
) -> ClassificationMatch | None:
    """Применить правила по приоритету FR-23 без обращения к модели.

    Конфликт одинаково приоритетных правил не разрешается случайным порядком:
    при равном приоритете и одинаковой узости совпадение считается спорным
    и не применяется.
    """
    normalized = normalize_name(text)
    if not normalized:
        return None

    rules = (
        (
            await session.execute(
                select(ClassificationRule)
                .where(
                    ClassificationRule.workspace_id == workspace_id,
                    ClassificationRule.archived_at.is_(None),
                )
                .order_by(
                    ClassificationRule.priority,
                    ClassificationRule.specificity.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    applicable: list[ClassificationRule] = []
    for rule in rules:
        if rule.scope == "member" and rule.owner_membership_id != membership_id:
            continue
        keyword = str(rule.condition.get("contains", ""))
        if keyword and keyword in normalized:
            applicable.append(rule)

    if applicable:
        best = applicable[0]
        ties = [
            rule
            for rule in applicable
            if rule.priority == best.priority and rule.specificity == best.specificity
        ]
        if len(ties) > 1:
            targets = {str(rule.action.get("category_id")) for rule in ties}
            if len(targets) > 1:
                raise ConflictError(
                    "Несколько одинаково подходящих правил указывают разные категории: "
                    "уточните правило"
                )
        raw_category = best.action.get("category_id")
        if raw_category:
            return ClassificationMatch(
                category_id=uuid.UUID(str(raw_category)),
                beneficiary_id=(
                    uuid.UUID(str(best.action["beneficiary_id"]))
                    if best.action.get("beneficiary_id")
                    else None
                ),
                basis="rule_member" if best.scope == "member" else "rule_workspace",
                rule_id=best.id,
            )

    alias = (
        await session.execute(
            select(CategoryAlias.category_id, CategoryAlias.normalized_alias).where(
                CategoryAlias.workspace_id == workspace_id
            )
        )
    ).all()
    for category_id, normalized_alias in alias:
        if normalized_alias and normalized_alias in normalized:
            return ClassificationMatch(
                category_id=category_id,
                beneficiary_id=None,
                basis="alias",
                rule_id=None,
            )
    return None


async def learn_from_correction(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    keyword: str,
    category_id: uuid.UUID,
    scope: str = "member",
) -> RuleView:
    """Предложение «Всегда относить такие операции сюда» (FR-24).

    Однократная покупка не переназначает прошлые расходы: правило действует
    только для будущих записей.
    """
    return await create_rule(
        session,
        uow,
        actor=actor,
        keyword=keyword,
        category_id=category_id,
        scope=scope,
    )
