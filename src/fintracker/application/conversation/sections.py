"""Разделы интерфейса: обзор, категории, история, участники, настройки."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import replace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.conversation import views
from fintracker.application.conversation.context import (
    author_names,
    category_paths,
    current_status,
)
from fintracker.application.conversation.entry import (
    CandidateFields,
    load_draft,
    post_draft,
)
from fintracker.application.conversation.keyboards import (
    Button,
    callback,
    short,
    start_menu,
    transaction_card,
)
from fintracker.application.conversation.types import Reply
from fintracker.application.identity.actor import list_budgets
from fintracker.application.planning.periods import period_for_date
from fintracker.application.planning.plan import line_key, period_status
from fintracker.config import Settings
from fintracker.core.context import ActorContext, Role
from fintracker.core.errors import DomainError, NotFound, ValidationFailed
from fintracker.core.logging import get_logger
from fintracker.core.money import Money
from fintracker.db.models.access import Beneficiary, Person, Workspace
from fintracker.db.models.catalog import Account
from fintracker.db.models.ledger import Allocation, CashLeg, Transaction, TransactionRevision
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

logger = get_logger("conversation.sections")


async def list_budgets_reply(settings: Settings, *, user_id: uuid.UUID) -> list[Reply]:
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        budgets = await list_budgets(session, user_id)
    if not budgets:
        return [Reply(text=views.empty_state("budgets"), buttons=start_menu(returning=False))]
    rows: list[tuple[Button, ...]] = []
    lines = ["📒 Мои бюджеты", ""]
    for item in budgets:
        role = "👑 администратор" if item.role is Role.ADMIN else "участник"
        current = " — сейчас открыт" if item.is_active_context else ""
        lines.append(f"{'✅' if item.is_active_context else '•'} {item.name}{current}")
        lines.append(f"   Ваша роль: {role}")
        rows.append(
            (
                Button(
                    f"{'✅ ' if item.is_active_context else ''}{item.name}",
                    callback("ws", "use", short(item.workspace_id)),
                ),
            )
        )
    if len(budgets) > 1:
        lines.extend(["", "Нажмите на бюджет, чтобы переключиться. Новые траты попадут в него."])
    rows.append(
        (
            Button("➕ Создать бюджет", callback("wiz", "start")),
            Button("🔑 Войти по коду", callback("join", "start")),
        )
    )
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def budget_overview(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    status = await current_status(settings, actor=actor, workspace=workspace)
    rows: list[tuple[Button, ...]] = []
    if status.pending_drafts:
        rows.append(
            (
                Button(
                    f"✍️ Не сохранено: {status.pending_drafts}",
                    callback("menu", "drafts"),
                ),
            )
        )
    rows.extend(
        [
            (
                Button("🗂 Все категории", callback("menu", "categories")),
                Button("🧮 Как посчитано", callback("budget", "explain")),
            ),
            (
                Button("🕘 Прошлые периоды", callback("budget", "past")),
                Button("📅 Следующий период", callback("budget", "next")),
            ),
        ]
    )
    if status.completeness == "incomplete":
        rows.append((Button("✅ Проверить учёт", callback("menu", "check")),))
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [
        Reply(
            text=views.budget_overview(status, workspace_name=workspace.name),
            buttons=tuple(rows),
        )
    ]


async def drafts_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Несохранённые записи участника: их можно открыть и дописать (FR-38)."""
    from fintracker.db.models.platform import Candidate, Draft

    workspace_id = actor.require_workspace()
    now = dt.datetime.now(dt.UTC)
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        drafts = (
            (
                await session.execute(
                    select(Draft)
                    .where(
                        Draft.workspace_id == workspace_id,
                        Draft.owner_user_id == actor.user_id,
                        Draft.state.in_(("needs_clarification", "ready")),
                        Draft.expires_at > now,
                    )
                    .order_by(Draft.created_at.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        first_fields: dict[uuid.UUID, dict[str, object]] = {}
        counts: dict[uuid.UUID, int] = {}
        if drafts:
            rows_db = (
                await session.execute(
                    select(Candidate.draft_id, Candidate.fields)
                    .where(
                        Candidate.workspace_id == workspace_id,
                        Candidate.draft_id.in_([draft.id for draft in drafts]),
                        Candidate.state.in_(("draft", "ready", "needs_clarification")),
                    )
                    .order_by(Candidate.candidate_key)
                )
            ).all()
            for draft_id, fields in rows_db:
                counts[draft_id] = counts.get(draft_id, 0) + 1
                first_fields.setdefault(draft_id, dict(fields))
    visible = [draft for draft in drafts if draft.id in first_fields]
    if not visible:
        return [
            Reply(
                text=views.empty_state("drafts"),
                buttons=((Button("🏠 Меню", callback("menu", "main")),),),
            )
        ]
    lines = [
        "✍️ Не сохранённые записи",
        "",
        "Они не входят в расходы, пока вы их не запишете. Откройте запись, чтобы "
        "проверить и сохранить или отменить её.",
    ]
    buttons: list[tuple[Button, ...]] = []
    for draft in visible:
        fields = CandidateFields.from_payload(first_fields[draft.id])
        amount = (
            Money(fields.amount_minor, fields.currency or workspace.currency).format()
            if fields.amount_minor is not None
            else "без суммы"
        )
        title = (fields.description or draft.raw_text or "запись")[:24]
        extra = counts.get(draft.id, 1) - 1
        label = f"{amount} · {title}" + (f" +{extra}" if extra else "")
        buttons.append((Button(label, callback("dr", "open", short(draft.id))),))
    buttons.append((Button("← Бюджет", callback("menu", "budget")),))
    return [Reply(text="\n".join(lines), buttons=tuple(buttons))]


async def quality_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """«Проверить учёт»: что мешает считать период полным (R02, FR-69)."""
    from fintracker.application.analytics.coverage import quality_check

    status = await current_status(settings, actor=actor, workspace=workspace)
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        check = await quality_check(session, workspace_id=workspace_id, currency=workspace.currency)
    completeness = {
        "incomplete": "не подтверждена",
        "reconciled_source": "сверена с выпиской",
        "confirmed_complete": "подтверждена",
    }.get(status.completeness, "не подтверждена")
    lines = [
        "✅ Проверка учёта",
        f"Период: {views.format_range(status.start_date, status.end_inclusive)}",
        f"Полнота: {completeness}",
        "",
    ]
    issues = 0
    rows: list[tuple[Button, ...]] = []
    if check.pending_drafts:
        issues += 1
        lines.append(f"✍️ Не сохранено записей: {check.pending_drafts}")
        rows.append((Button("✍️ Открыть несохранённые", callback("menu", "drafts")),))
    if check.uncategorized_count:
        issues += 1
        lines.append(
            f"🗂 Без категории: {check.uncategorized_count} "
            f"{views.plural(check.uncategorized_count, 'запись', 'записи', 'записей')} на "
            f"{views.money(check.uncategorized_minor, workspace.currency)}"
        )
        rows.append((Button("🧾 История", callback("menu", "history")),))
    if check.possible_duplicates:
        issues += 1
        lines.append(f"🔁 Возможные дубли: {check.possible_duplicates}")
    if not issues:
        lines.append("Всё разобрано: несохранённых записей и трат без категории нет.")
    lines.extend(
        [
            "",
            "Если все траты периода внесены, отметьте учёт полным — тогда отчёты "
            "покажут прогноз и остаток будет считаться экономией.",
        ]
    )
    if status.completeness != "confirmed_complete":
        rows.append(
            (
                Button("✅ Все траты внесены", callback("cov", "ok")),
                Button("Есть пропуски", callback("cov", "gap")),
            )
        )
    rows.append((Button("← Бюджет", callback("menu", "budget")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def set_completeness(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, complete: bool
) -> list[Reply]:
    from fintracker.application.analytics.coverage import set_period_completeness

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        period = await period_for_date(session, workspace_id=workspace_id, day=today)
        # Участник без прав администратора подтверждает только свои траты:
        # это не закрывает пропуски всего бюджета (FR-69).
        scope_person = None if actor.is_admin else actor.person_id
        if not actor.is_admin and scope_person is None:
            return [
                Reply(
                    text="ℹ️ Полноту учёта бюджета подтверждает администратор.",
                    buttons=((Button("📒 Бюджет", callback("menu", "budget")),),),
                )
            ]
        await set_period_completeness(
            session,
            uow,
            actor=actor,
            period_id=period.id,
            status="confirmed_complete" if complete else "incomplete",
            basis="Отмечено участником в разделе «Проверить учёт»",
            scope_person_id=scope_person,
        )
    if not complete:
        text = (
            "📝 Отмечено: в периоде есть пропуски\n\nДобавьте недостающие траты — "
            "отчёты пока не считают остаток экономией."
        )
    elif actor.is_admin:
        text = (
            "✅ Учёт периода отмечен полным\n\nОтчёты покажут прогноз, а остаток "
            "лимитов будет считаться экономией."
        )
    else:
        text = (
            "✅ Ваши траты за период отмечены полными\n\nПолноту всего бюджета "
            "подтверждает администратор."
        )
    return [
        Reply(
            text=text,
            buttons=(
                (
                    Button("📒 Бюджет", callback("menu", "budget")),
                    Button("📊 Аналитика", callback("menu", "analytics")),
                ),
            ),
        )
    ]


async def categories_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, page: int = 0
) -> list[Reply]:
    """Категории бюджета: план и справочник без лимита (FR-21, A162)."""
    from fintracker.application.catalog.categories import list_categories

    status = await current_status(settings, actor=actor, workspace=workspace)
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        catalog = await list_categories(session, workspace_id=workspace_id)
    planned = {line.category_id for line in status.lines}
    extra = [item for item in catalog if item.id not in planned]
    # Категории без лимита и без трат не занимают по три строки каждая:
    # они перечислены одной строкой в конце (FR-06, A162).
    idle_names = [
        line.category_name
        for line in status.lines
        if line.effective_limit_minor is None and line.fact_minor == 0 and not line.beneficiary_name
    ] + [item.name for item in extra]
    active_status = replace(
        status,
        lines=tuple(
            line
            for line in status.lines
            if not (
                line.effective_limit_minor is None
                and line.fact_minor == 0
                and not line.beneficiary_name
            )
        ),
    )
    body, has_more = views.category_lines(active_status, page=page)
    if idle_names and page == 0:
        tail = "▫️ Без лимита и без трат: " + ", ".join(idle_names)
        body = f"{body}\n\n{tail}" if active_status.lines else tail
    body = f"Период: {views.format_range(status.start_date, status.end_inclusive)}\n\n{body}"
    rows: list[tuple[Button, ...]] = [
        (
            Button("➕ Добавить категорию", callback("cat", "new")),
            Button("⚙️ Управление", callback("cat", "manage")),
        )
    ]
    if has_more:
        rows.append((Button("Показать ещё", callback("cat", "page", str(page + 1))),))
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text=f"🗂 Категории бюджета\n\n{body}", buttons=tuple(rows))]


async def history_view(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    note_query: str | None = None,
) -> list[Reply]:
    """Общий журнал бюджета с фильтрами и сортировкой (FR-07)."""
    from fintracker.application.conversation.history_flow import JournalView, journal_view

    replies = await journal_view(
        settings,
        actor=actor,
        workspace=workspace,
        view=JournalView(flags="", sort="o", offset=0, category=""),
        note_query=note_query,
    )
    if replies and "Подходящих записей нет" in replies[0].text and not note_query:
        from fintracker.application.conversation.keyboards import main_menu

        return [Reply(text=views.empty_state("history"), buttons=main_menu())]
    return replies


async def members_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.identity.membership import list_members

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        members = await list_members(session, workspace_id=workspace_id)
    lines = [f"👥 Участники бюджета «{workspace.name}»", ""]
    for member in members:
        role = "👑 администратор" if member.role is Role.ADMIN else "участник"
        you = " (вы)" if member.user_id == actor.user_id else ""
        lines.append(f"• {member.display_name}{you} — {role}")
    # Предложение передать администрирование доступно получателю действием,
    # а не скрытой командой (FR-82, G-18).
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        from fintracker.db.models.access import AdminTransferProposal

        proposal = (
            await session.execute(
                select(AdminTransferProposal).where(
                    AdminTransferProposal.workspace_id == workspace_id,
                    AdminTransferProposal.to_user_id == actor.user_id,
                    AdminTransferProposal.state == "pending",
                )
            )
        ).scalar_one_or_none()
        proposal_id = proposal.id if proposal is not None else None

    rows: list[tuple[Button, ...]] = []
    if proposal_id is not None:
        lines.extend(["", "👑 Вам предлагают стать администратором этого бюджета."])
        rows.append(
            (
                Button("✅ Принять роль", callback("ws", "acceptadmin", short(proposal_id))),
                Button("✕ Отказаться", callback("ws", "declineadmin", short(proposal_id))),
            )
        )
    if actor.is_admin:
        rows.append((Button("🔗 Пригласить", callback("inv", "new")),))
        if len(members) > 1:
            rows.append(
                (
                    Button("👑 Передать роль", callback("ws", "transfer")),
                    Button("➖ Исключить", callback("ws", "remove")),
                )
            )
    else:
        lines.extend(["", "Пригласить новых участников может администратор."])
    rows.append((Button("✏️ Моё имя в бюджете", callback("set", "name")),))
    rows.append((Button("🚪 Выйти из бюджета", callback("ws", "leave")),))
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


def timezone_label(timezone: str) -> str:
    """Город и смещение вместо системного имени «Europe/Moscow»."""
    from fintracker.application.conversation.settings_flow import TIMEZONE_PRESETS

    for label, stored in TIMEZONE_PRESETS.values():
        if stored == timezone:
            return label
    now = dt.datetime.now(ZoneInfo(timezone))
    offset = now.utcoffset() or dt.timedelta()
    hours = int(offset.total_seconds() // 3600)
    city = timezone.rsplit("/", 1)[-1].replace("_", " ")
    return f"{city} · UTC{hours:+d}" if hours else f"{city} · UTC"


async def settings_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.planning.periods import (
        latest_policy,
        policy_from_row,
        upcoming_periods,
    )

    workspace_id = actor.require_workspace()
    status = await current_status(settings, actor=actor, workspace=workspace)
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        policy = policy_from_row(await latest_policy(session, workspace_id))
        upcoming = await upcoming_periods(
            session,
            workspace_id=workspace_id,
            from_date=status.end_inclusive + dt.timedelta(days=1),
            count=2,
        )
    lines = [
        f"⚙️ Настройки бюджета «{workspace.name}»",
        "",
        f"Валюта: {workspace.currency}",
        f"Часовой пояс: {timezone_label(workspace.timezone)}",
        "",
        f"📅 Период: {policy.describe()}",
        f"Сейчас: {views.format_range(status.start_date, status.end_inclusive)}",
    ]
    if upcoming:
        lines.append(
            "Дальше: "
            + ", ".join(views.format_range(item.start, item.end_inclusive) for item in upcoming)
        )
    rows: list[tuple[Button, ...]] = [(Button("👤 Мои настройки", callback("set", "personal")),)]
    if actor.is_admin:
        rows.append(
            (
                Button("📅 Период", callback("set", "period")),
                Button("✏️ Название", callback("set", "bname")),
            )
        )
        rows.append((Button("🗑 Удалить бюджет", callback("ws", "delete")),))
    else:
        rows.append((Button("📅 Период", callback("set", "period")),))
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def transaction_card_reply(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    detailed: bool = False,
    confirmation: bool = False,
) -> list[Reply]:
    """Краткая карточка операции; редкие реквизиты раскрываются отдельно."""
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        transaction = (
            await session.execute(
                select(Transaction).where(
                    Transaction.workspace_id == workspace_id, Transaction.id == transaction_id
                )
            )
        ).scalar_one_or_none()
        if transaction is None:
            raise NotFound("Операция недоступна")
        revision = (
            await session.execute(
                select(TransactionRevision).where(
                    TransactionRevision.workspace_id == workspace_id,
                    TransactionRevision.transaction_id == transaction_id,
                    TransactionRevision.revision == transaction.current_revision,
                )
            )
        ).scalar_one()
        allocations = (
            (
                await session.execute(
                    select(Allocation).where(
                        Allocation.workspace_id == workspace_id,
                        Allocation.transaction_id == transaction_id,
                        Allocation.revision == revision.revision,
                    )
                )
            )
            .scalars()
            .all()
        )
        paths = await category_paths(session, workspace_id=workspace_id)
        authors = await author_names(session, workspace_id=workspace_id)
        beneficiary_name = None
        if allocations and allocations[0].beneficiary_id:
            beneficiary_name = (
                await session.execute(
                    select(Beneficiary.name).where(
                        Beneficiary.workspace_id == workspace_id,
                        Beneficiary.id == allocations[0].beneficiary_id,
                    )
                )
            ).scalar_one_or_none()
        spender_name = None
        if revision.spender_person_id:
            spender_name = (
                await session.execute(
                    select(Person.name).where(
                        Person.workspace_id == workspace_id,
                        Person.id == revision.spender_person_id,
                    )
                )
            ).scalar_one_or_none()
        period = await period_for_date(
            session, workspace_id=workspace_id, day=revision.occurred_date
        )
        status = await period_status(
            session,
            workspace_id=workspace_id,
            period_id=period.id,
            currency=workspace.currency,
            today=today,
        )
        created_by = transaction.created_by
        transaction_status = transaction.status
        amount_minor = revision.amount_minor
        currency = revision.currency
        occurred_date = revision.occurred_date
        note = revision.note
        transaction_type = revision.transaction_type
        cash_legs = (
            await session.execute(
                select(CashLeg.signed_minor, Account.name)
                .outerjoin(
                    Account,
                    (Account.workspace_id == CashLeg.workspace_id)
                    & (Account.id == CashLeg.account_id),
                )
                .where(
                    CashLeg.workspace_id == workspace_id,
                    CashLeg.transaction_id == transaction_id,
                    CashLeg.revision == revision.revision,
                )
            )
        ).all()
        outgoing_account = next((name for signed, name in cash_legs if signed < 0 and name), None)
        incoming_account = next((name for signed, name in cash_legs if signed > 0 and name), None)
        account_flow = (
            f"{outgoing_account} → {incoming_account}"
            if outgoing_account and incoming_account
            else None
        )
        # История изменения, части распределения и связанные возвраты (FR-07).
        revisions = (
            (
                await session.execute(
                    select(TransactionRevision)
                    .where(
                        TransactionRevision.workspace_id == workspace_id,
                        TransactionRevision.transaction_id == transaction_id,
                    )
                    .order_by(TransactionRevision.revision)
                )
            )
            .scalars()
            .all()
        )
        from fintracker.db.models.ledger import TransactionLink

        link_rows = (
            (
                await session.execute(
                    select(TransactionLink).where(
                        TransactionLink.workspace_id == workspace_id,
                        TransactionLink.status == "active",
                        (TransactionLink.source_transaction_id == transaction_id)
                        | (TransactionLink.target_transaction_id == transaction_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        attachment_count = 0
        from fintracker.db.models.platform import Attachment as AttachmentRow

        attachment_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AttachmentRow)
                    .where(
                        AttachmentRow.workspace_id == workspace_id,
                        AttachmentRow.transaction_id == transaction_id,
                    )
                )
            ).scalar_one()
        )
        history_lines = [
            f"• Версия {item.revision}: {views.change_kind_label(item.change_kind)}"
            + (f" — {authors[item.changed_by]}" if item.changed_by in authors else "")
            for item in revisions
        ]
        allocation_lines = [
            f"• {paths.get(item.category_id, 'Без категории')}"
            f": {views.money(item.amount_minor, currency)} "
            f"({views.allocation_role_label(item.economic_role)})"
            if item.category_id
            else f"• Без категории: {views.money(item.amount_minor, currency)}"
            for item in allocations
        ]
        link_lines = []
        for link in link_rows:
            direction = (
                "возврат по этой записи"
                if link.target_transaction_id == transaction_id
                else "связана с записью"
            )
            link_lines.append(
                f"• {views.link_type_label(link.link_type)}: {direction}, "
                f"{views.money(link.amount_minor, currency)}"
            )

    line = None
    if (
        transaction_type in {"expense", "refund"}
        and len(allocations) == 1
        and allocations[0].category_id
    ):
        key = line_key(allocations[0].category_id, allocations[0].beneficiary_id)
        line = next(
            (
                item
                for item in status.lines
                if line_key(item.category_id, item.beneficiary_id) == key
            ),
            None,
        )
    if len(allocations) > 1:
        category_path = f"Категорий: {len(allocations)}"
    elif allocations and allocations[0].category_id:
        category_path = paths.get(allocations[0].category_id, "Без категории")
    else:
        category_path = "Без категории"

    text = views.transaction_card(
        workspace_name=workspace.name,
        amount_minor=amount_minor,
        currency=currency,
        category_path=category_path,
        beneficiary=beneficiary_name,
        spender=spender_name,
        account=account_flow,
        occurred_date=occurred_date,
        period=(period.start_date, period.end_exclusive - dt.timedelta(days=1)),
        line=line,
        author_name=authors.get(created_by) if created_by != actor.user_id else None,
        note=note,
        is_voided=transaction_status == "voided",
        transaction_type=transaction_type,
        confirmation=confirmation,
        detailed=detailed,
    )
    extra: list[str] = []
    if detailed and len(allocation_lines) > 1:
        extra.append("\n🗂 Распределение суммы")
        extra.extend(allocation_lines)
    if detailed and link_lines:
        extra.append("\n🔗 Связанные записи")
        extra.extend(link_lines)
    if detailed and attachment_count:
        extra.append(f"Вложений: {attachment_count}")
    if detailed and len(history_lines) > 1:
        extra.append("\n🕘 История изменений")
        extra.extend(history_lines)
    if extra:
        text = text + "\n" + "\n".join(extra)
    return [
        Reply(
            text=text,
            buttons=transaction_card(transaction_id, detailed=detailed),
            transaction_id=transaction_id,
        )
    ]


async def confirm_draft(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    origin: str,
) -> list[Reply]:
    """Провести подтверждённый черновик и показать карточку (FR-11)."""
    from fintracker.application.conversation.transaction_flow import special_action

    details = await special_action(settings, actor=actor, workspace=workspace, draft_id=draft_id)
    if details is not None:
        return details
    workspace_id = actor.require_workspace()
    settled_note: str | None = None
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        draft, candidates = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        if draft.state in {"cancelled", "expired"}:
            raise NotFound("Черновик больше не активен")
        try:
            posted = await post_draft(
                session,
                uow,
                actor=actor,
                draft=draft,
                candidates=candidates,
                timezone=workspace.timezone,
                workspace_currency=workspace.currency,
                origin=origin,
            )
        except ValidationFailed as exc:
            # Неполный или неподдержанный здесь тип остаётся черновиком с
            # уточнением: подтверждение не обходит проверки (AUD-08).
            draft.state = "needs_clarification"
            draft.version += 1
            draft.failure_reason = exc.message[:200]
            await session.flush()
            return [
                Reply(
                    text=(
                        f"⚠️ Пока не могу записать: {exc.message.rstrip('.')}.\n\n"
                        "Запись сохранена — исправьте её или отмените."
                    ),
                    buttons=(
                        (
                            Button("✏️ Изменить", callback("dr", "edit", short(draft_id))),
                            Button("✕ Отменить", callback("dr", "cancel", short(draft_id))),
                        ),
                    ),
                )
            ]
        if posted:
            # Пороговые события пересчитываются под той же блокировкой (FR-52).
            from fintracker.application.delivery.thresholds import evaluate_thresholds
            from fintracker.application.planning.periods import period_for_date

            today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
            period = await period_for_date(session, workspace_id=workspace_id, day=today)
            await evaluate_thresholds(
                session,
                uow,
                workspace=workspace,
                period_id=period.id,
                today=today,
            )
            # Нажатая ранее кнопка «Оплачено» закрывает свой экземпляр той же
            # транзакцией: связь с обязательством не теряется (FR-46, G-15).
            settled_note = await _settle_pending_occurrence(
                settings,
                session,
                uow,
                actor=actor,
                workspace=workspace,
                draft_id=draft_id,
                transaction_ids=posted,
            )
    if not posted:
        return [
            Reply(
                text="ℹ️ Записывать нечего: все траты из сообщения отменены.",
                buttons=((Button("🏠 Меню", callback("menu", "main")),),),
            )
        ]
    if len(posted) == 1:
        replies = await transaction_card_reply(
            settings,
            actor=actor,
            workspace=workspace,
            transaction_id=posted[0],
            confirmation=True,
        )
        if settled_note:
            replies.append(Reply(text=settled_note))
        return replies
    total = await batch_total(settings, actor=actor, transaction_ids=posted)
    count_word = views.plural(len(posted), "трата", "траты", "трат")
    return [
        Reply(
            text=(
                f"✅ Записано: {len(posted)} {count_word} на {total}\n\n"
                "Каждую можно открыть и исправить в истории."
            ),
            buttons=(
                (
                    Button("🧾 История", callback("menu", "history")),
                    Button("🏠 Меню", callback("menu", "main")),
                ),
            ),
        )
    ]


async def _settle_pending_occurrence(
    settings: Settings,
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    transaction_ids: list[uuid.UUID],
) -> str | None:
    """Закрыть ожидаемый платёж, выбранный кнопкой «Оплачено» (FR-46, G-15)."""
    from fintracker.application.commitments.schedules import settle_occurrence
    from fintracker.application.conversation.pending import clear_pending, peek_pending
    from fintracker.db.models.commitments import Occurrence
    from fintracker.db.models.ledger import FinancialEffect

    if len(transaction_ids) != 1:
        return None
    pending = await peek_pending(settings, user_id=actor.user_id, workspace_id=actor.workspace_id)
    if pending is None or pending.kind != "occurrence_settle":
        return None
    if pending.payload.get("draft_id") != str(draft_id):
        return None

    workspace_id = actor.require_workspace()
    occurrence_id = uuid.UUID(str(pending.payload["occurrence_id"]))
    occurrence = await session.get(Occurrence, occurrence_id)
    if occurrence is None or occurrence.workspace_id != workspace_id:
        return None
    revision = (
        await session.execute(
            select(TransactionRevision)
            .join(
                Transaction,
                (Transaction.workspace_id == TransactionRevision.workspace_id)
                & (Transaction.id == TransactionRevision.transaction_id)
                & (Transaction.current_revision == TransactionRevision.revision),
            )
            .where(
                TransactionRevision.workspace_id == workspace_id,
                TransactionRevision.transaction_id == transaction_ids[0],
            )
        )
    ).scalar_one()
    effect = (
        await session.execute(
            select(FinancialEffect).where(
                FinancialEffect.workspace_id == workspace_id,
                FinancialEffect.transaction_id == transaction_ids[0],
                FinancialEffect.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if effect is None:
        return None
    remaining = (occurrence.expected_minor or revision.amount_minor) - occurrence.settled_minor
    amount = Money(min(revision.amount_minor, max(0, remaining)), revision.currency)
    if amount.minor <= 0:
        return None
    try:
        await settle_occurrence(
            session,
            uow,
            actor=actor,
            occurrence_id=occurrence_id,
            effect_id=effect.id,
            transaction_id=transaction_ids[0],
            amount=amount,
        )
    except DomainError as exc:
        return f"⚠️ Платёж не отмечен оплаченным: {exc.message}"
    await clear_pending(settings, user_id=actor.user_id, workspace_id=actor.workspace_id)
    return f"🗓 Платёж отмечен оплаченным: {amount.format()}."


async def batch_total(
    settings: Settings, *, actor: ActorContext, transaction_ids: list[uuid.UUID]
) -> str:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rows = (
            await session.execute(
                select(TransactionRevision.amount_minor, TransactionRevision.currency)
                .join(
                    Transaction,
                    (Transaction.workspace_id == TransactionRevision.workspace_id)
                    & (Transaction.id == TransactionRevision.transaction_id)
                    & (Transaction.current_revision == TransactionRevision.revision),
                )
                .where(
                    TransactionRevision.workspace_id == workspace_id,
                    TransactionRevision.transaction_id.in_(transaction_ids),
                )
            )
        ).all()
    if not rows:
        return "0"
    currency = rows[0][1]
    return Money(sum(row[0] for row in rows), currency).format()


_KIND_LABELS = {
    "expense": "Расход",
    "income": "Доход",
    "refund": "Возврат",
    "transfer": "Перевод",
}


def draft_summary(
    candidates: Sequence[CandidateFields],
    currency: str,
    category_names: dict[uuid.UUID, str] | None = None,
) -> str:
    """Что будет записано: тип, сумма, описание, категория и дата.

    Категория и тип видны до сохранения: «зарплата» не должна выглядеть как
    расход, а трата «Без категории» — обнаруживаться только после записи.
    """
    names = category_names or {}
    blocks: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        amount = (
            Money(candidate.amount_minor, candidate.currency or currency).format()
            if candidate.amount_minor is not None
            else "сумма не указана"
        )
        kind = _KIND_LABELS.get(candidate.kind, "Операция")
        date_part = (
            views.format_date(candidate.occurred_date, with_year=True)
            if candidate.occurred_date
            else "дата не указана"
        )
        prefix = f"{index}. " if len(candidates) > 1 else ""
        lines = [f"{prefix}{kind} · {amount}"]
        if candidate.description:
            lines.append(candidate.description[:120])
        if candidate.kind == "expense":
            if candidate.parts:
                lines.append(f"Категорий: {len(candidate.parts)}")
            else:
                category = (
                    names.get(candidate.category_id, "Без категории")
                    if candidate.category_id
                    else "Без категории"
                )
                lines.append(f"Категория: {category}")
        lines.append(f"Дата: {date_part}")
        if candidate.note:
            lines.append(f"Комментарий: {candidate.note}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _clarify_rows(
    draft_id: uuid.UUID, ambiguities: Sequence[dict[str, object]]
) -> list[tuple[Button, ...]]:
    """Кнопки-ответы на открытый вопрос: ответить можно одним нажатием."""
    code = short(draft_id)
    fields = {str(item.get("field")) for item in ambiguities}
    rows: list[tuple[Button, ...]] = []
    if "spender_person_id" in fields:
        name = next(
            (
                str(item.get("value"))
                for item in ambiguities
                if item.get("field") == "spender_person_id"
            ),
            "",
        )
        label = f"➕ Добавить «{name[:20]}»" if name else "➕ Добавить человека"
        rows.append(
            (
                Button(label, callback("clr", "person", code)),
                Button("Без имени", callback("clr", "noperson", code)),
            )
        )
    if "date" in fields or "occurred_date" in fields:
        rows.append(
            (
                Button("✅ Уже купил — сегодня", callback("clr", "today", code)),
                Button("💭 Это план", callback("clr", "plan", code)),
            )
        )
    if "note" in fields:
        rows.append(
            (
                Button("💬 Ко всем тратам", callback("clr", "noteall", code)),
                Button("Без комментария", callback("clr", "nonote", code)),
            )
        )
    return rows


def draft_buttons(
    draft_id: uuid.UUID,
    *,
    can_pick_category: bool,
    ambiguities: Sequence[dict[str, object]] = (),
) -> tuple[tuple[Button, ...], ...]:
    code = short(draft_id)
    rows = _clarify_rows(draft_id, ambiguities)
    first: list[Button] = [Button("✅ Записать", callback("dr", "post", code))]
    if can_pick_category:
        first.append(Button("🗂 Категория", callback("dr", "cats", code, "0")))
    rows.append(tuple(first))
    rows.append(
        (
            Button("✏️ Изменить", callback("dr", "edit", code)),
            Button("✕ Отменить", callback("dr", "cancel", code)),
        )
    )
    return tuple(rows)


async def draft_card(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, draft_id: uuid.UUID
) -> tuple[str | None, str, tuple[tuple[Button, ...], ...]]:
    """Открытый вопрос, описание записи и кнопки черновика."""
    from fintracker.application.catalog.categories import list_categories

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        draft, candidates = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        state = draft.state
        active = [row for row in candidates if row.state not in {"excluded", "cancelled"}]
        fields = [CandidateFields.from_payload(dict(row.fields)) for row in active]
        ambiguities = [item for row in active for item in (row.ambiguities or [])]
        names = {
            item.id: item.name for item in await list_categories(session, workspace_id=workspace_id)
        }
        open_question = None
        if state == "needs_clarification":
            from fintracker.db.models.platform import Clarification

            open_question = (
                await session.execute(
                    select(Clarification.question)
                    .where(
                        Clarification.workspace_id == workspace_id,
                        Clarification.draft_id == draft_id,
                        Clarification.state == "open",
                    )
                    .order_by(Clarification.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
    summary = draft_summary(fields, workspace.currency, names)
    can_pick = len(fields) == 1 and fields[0].kind == "expense" and not fields[0].parts
    buttons = draft_buttons(
        draft_id,
        can_pick_category=can_pick,
        ambiguities=ambiguities if open_question else (),
    )
    return open_question, summary, buttons


async def draft_reply(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    notice: str | None = None,
) -> list[Reply]:
    """Карточка черновика до сохранения (R07, AR-06)."""
    open_question, summary, buttons = await draft_card(
        settings, actor=actor, workspace=workspace, draft_id=draft_id
    )
    if open_question:
        text = f"{open_question}\n\n🧾 Что уже распознано\n{summary}"
    else:
        text = f"🧾 Проверьте запись перед сохранением\n\n{summary}"
    if notice:
        text = f"{notice}\n\n{text}"
    return [Reply(text=text, buttons=buttons)]


async def posted_transaction_of_draft(
    settings: Settings, *, actor: ActorContext, draft_id: uuid.UUID
) -> uuid.UUID | None:
    """Единственная проведённая операция этого черновика (R-03)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        _, candidates = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        posted = [row.posted_transaction_id for row in candidates if row.posted_transaction_id]
    return posted[0] if len(posted) == 1 else None


async def posted_draft_reply(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, draft_id: uuid.UUID
) -> list[Reply]:
    """Карточка уже проведённой записи этого сообщения (AUD-02).

    Повтор обработки того же события показывает прежний результат и не
    создаёт вторую финансовую запись.
    """
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        _, candidates = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        posted = [row.posted_transaction_id for row in candidates if row.posted_transaction_id]
    if not posted:
        return await draft_reply(settings, actor=actor, workspace=workspace, draft_id=draft_id)
    replies: list[Reply] = []
    for transaction_id in posted:
        replies.extend(
            await transaction_card_reply(
                settings, actor=actor, workspace=workspace, transaction_id=transaction_id
            )
        )
    return replies


async def apply_draft_edit(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    text: str,
    candidate_id: uuid.UUID | None = None,
    expected_version: int | None = None,
) -> list[Reply]:
    """Применить правку к тому же черновику, а не создавать второй (FR-20, G-16).

    Принимается одна сумма, дата («вчера», «20.09») или название категории.
    """
    from fintracker.application.catalog.categories import list_categories
    from fintracker.application.catalog.keywords import suggest_category
    from fintracker.application.catalog.normalize import normalize_name
    from fintracker.application.conversation.entry import CandidateFields, load_draft
    from fintracker.application.conversation.guards import single_amount
    from fintracker.domain.parsing.dates import resolve_date_expression

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        draft, candidates = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        if draft.state in {"posted", "cancelled", "expired"}:
            return await draft_reply(settings, actor=actor, workspace=workspace, draft_id=draft_id)
        editable = [row for row in candidates if row.state not in {"excluded", "cancelled"}]
        if candidate_id is not None:
            editable = [row for row in editable if row.id == candidate_id]
            if not editable or editable[0].version != expected_version:
                from fintracker.core.errors import ConflictError

                raise ConflictError("Запись уже изменена. Откройте черновик заново.")
        if len(editable) != 1:
            return [
                Reply(
                    text=(
                        "ℹ️ В сообщении несколько трат\n\nСохраните их, затем откройте "
                        "нужную в истории и исправьте её отдельно."
                    )
                )
            ]
        row = editable[0]
        fields = CandidateFields.from_payload(dict(row.fields))
        ambiguities = list(row.ambiguities or [])
        changed: str
        amount = single_amount(text)
        parsed_date = None if amount is not None else resolve_date_expression(text, reference=today)
        if amount is not None:
            fields.amount_minor = Money.from_decimal(
                amount.value, fields.currency or workspace.currency
            ).minor
            ambiguities = [item for item in ambiguities if item.get("field") != "amount"]
            new_amount = Money(fields.amount_minor, fields.currency or workspace.currency)
            changed = f"сумма — {new_amount.format()}"
        elif parsed_date is not None and len(text.split()) <= 3:
            fields.occurred_date = parsed_date.value
            fields.date_expression = parsed_date.expression
            ambiguities = [
                item for item in ambiguities if item.get("field") not in {"date", "occurred_date"}
            ]
            changed = f"дата — {views.format_date(parsed_date.value, with_year=True)}"
        else:
            catalog = [
                item
                for item in await list_categories(session, workspace_id=workspace_id)
                if not getattr(item, "archived", False)
            ]
            wanted = normalize_name(text)
            match = next((item for item in catalog if normalize_name(item.name) == wanted), None)
            if match is None and len(wanted) >= 3:
                starts = [item for item in catalog if normalize_name(item.name).startswith(wanted)]
                match = starts[0] if len(starts) == 1 else None
            if match is None:
                suggested = suggest_category(text, ((item.id, item.name) for item in catalog))
                match = next((item for item in catalog if item.id == suggested), None)
            if match is None:
                return [
                    Reply(
                        text=(
                            "⚠️ Не понял правку\n\nОтправьте сумму («600»), дату "
                            "(«вчера», «20.09») или название категории. Категорию удобнее "
                            "выбрать кнопкой."
                        ),
                        buttons=(
                            (
                                Button(
                                    "🗂 Выбрать категорию",
                                    callback("dr", "cats", short(draft_id), "0"),
                                ),
                            ),
                        ),
                        retry_input=True,
                    )
                ]
            fields.category_id = match.id
            changed = f"категория — {match.name}"
        fields.operation_details.pop("details_confirmed", None)
        row.fields = fields.to_payload()
        row.ambiguities = ambiguities
        row.state = (
            "ready"
            if not ambiguities and fields.amount_minor is not None
            else "needs_clarification"
        )
        row.version += 1
        draft.state = (
            "ready"
            if all(item.state in {"ready", "excluded", "cancelled"} for item in candidates)
            else "needs_clarification"
        )
        draft.version += 1
    return await draft_reply(
        settings,
        actor=actor,
        workspace=workspace,
        draft_id=draft_id,
        notice=f"✏️ Изменено: {changed}",
    )


async def draft_categories(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    page: int = 0,
) -> list[Reply]:
    """Выбор категории черновика кнопками вместо ввода точного названия."""
    from fintracker.application.catalog.categories import list_categories

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        catalog = [
            item
            for item in await list_categories(session, workspace_id=workspace_id)
            if not getattr(item, "archived", False)
        ]
    per_page = 8
    last_page = max(0, (len(catalog) - 1) // per_page)
    page = max(0, min(page, last_page))
    code = short(draft_id)
    chunk = catalog[page * per_page : (page + 1) * per_page]
    rows: list[tuple[Button, ...]] = []
    for index in range(0, len(chunk), 2):
        rows.append(
            tuple(
                Button(item.name[:28], callback("dr", "setcat", code, short(item.id)))
                for item in chunk[index : index + 2]
            )
        )
    navigation: list[Button] = []
    if page:
        navigation.append(Button("← Назад", callback("dr", "cats", code, str(page - 1))))
    if page < last_page:
        navigation.append(Button("Далее →", callback("dr", "cats", code, str(page + 1))))
    if navigation:
        rows.append(tuple(navigation))
    rows.append(
        (
            Button("Без категории", callback("dr", "setcat", code, "-")),
            Button("← К записи", callback("dr", "open", code)),
        )
    )
    text = "🗂 Выберите категорию\n\nЗапись сохранится только после нажатия «Записать»."
    if not catalog:
        text = "🗂 Категорий пока нет\n\nДобавьте их в разделе «Категории»."
    return [Reply(text=text, buttons=tuple(rows))]


async def set_draft_category(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    category_id: uuid.UUID | None,
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        draft, candidates = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        if draft.state in {"posted", "cancelled", "expired"}:
            raise NotFound("Черновик уже сохранён или отменён. Откройте запись в истории.")
        editable = [row for row in candidates if row.state not in {"excluded", "cancelled"}]
        if len(editable) != 1:
            raise ValidationFailed(
                "В этом черновике несколько трат: категорию меняйте после записи"
            )
        row = editable[0]
        fields = CandidateFields.from_payload(dict(row.fields))
        fields.category_id = category_id
        row.fields = fields.to_payload()
        row.version += 1
        draft.version += 1
    return await draft_reply(settings, actor=actor, workspace=workspace, draft_id=draft_id)
