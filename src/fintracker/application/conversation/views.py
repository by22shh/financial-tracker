"""Тексты карточек и обзоров (раздел 6 ТЗ, FR-06–FR-09)."""

from __future__ import annotations

import datetime as dt
import uuid

from fintracker.application.delivery.render import format_date, format_range
from fintracker.application.planning.plan import LimitState, LineStatus, PeriodStatus
from fintracker.core.money import Money

# В чате выводится до восьми элементов за страницу (FR-06).
PAGE_SIZE = 8


def money(minor: int, currency: str) -> str:
    return Money(minor, currency).format()


def transaction_card(
    *,
    workspace_name: str,
    amount_minor: int,
    currency: str,
    category_path: str,
    beneficiary: str | None,
    spender: str | None,
    account: str | None,
    occurred_date: dt.date,
    period: tuple[dt.date, dt.date] | None,
    line: LineStatus | None,
    author_name: str | None,
    note: str | None,
    is_voided: bool = False,
) -> str:
    """Карточка записанной операции (раздел 6.2 ТЗ)."""
    lines = [f"Бюджет: {workspace_name}"]
    verb = "Отменена запись" if is_voided else "Записано"
    lines.append(f"{verb} {money(amount_minor, currency)} — {category_path}")
    context: list[str] = []
    if beneficiary:
        context.append(f"Для кого: {beneficiary}")
    if spender:
        context.append(f"Кто потратил: {spender}")
    if account:
        context.append(f"Счёт: {account}")
    context.append(format_date(occurred_date, with_year=True))
    lines.append(" · ".join(context))
    if period:
        lines.append(f"Период: {format_range(period[0], period[1])}")
    if author_name:
        lines.append(f"Запись добавил: {author_name}")
    if line is not None:
        lines.append(f"По статье: {line.status_text}")
        if line.limit_state is LimitState.POSITIVE and line.effective_limit_minor:
            lines.append(
                f"Остаток по статье: {money(line.remaining_minor or 0, currency)} "
                f"из {money(line.effective_limit_minor, currency)}"
            )
    if note:
        preview = note.strip().splitlines()[0]
        if len(preview) > 120:
            preview = preview[:117] + "…"
        lines.append(f"Комментарий: {preview}")
    return "\n".join(lines)


def budget_overview(status: PeriodStatus, *, workspace_name: str) -> str:
    """Обзор бюджета (раздел 6.3 ТЗ).

    «После известных платежей» намеренно не называется балансом счёта.
    """
    currency = status.currency
    lines = [workspace_name, format_range(status.start_date, status.end_inclusive)]
    lines.append(f"Учтённые расходы: {money(status.total_fact_minor, currency)}")

    if status.total_limit_minor is None:
        lines.append("План расходов: не задан")
    else:
        lines.append(f"План расходов: {money(status.total_limit_minor, currency)}")
        remaining = status.total_limit_minor - status.total_fact_minor
        lines.append(f"Осталось по плану: {money(remaining, currency)}")
        commitments = sum(line.commitments_minor for line in status.lines)
        if commitments:
            lines.append(f"Из них ожидаемые платежи: {money(commitments, currency)}")
            lines.append(f"После известных платежей: {money(remaining - commitments, currency)}")

    if status.plan_status != "approved":
        label = {
            "draft": "План не утверждён",
            "needs_review": "План требует проверки",
        }.get(status.plan_status, status.plan_status)
        lines.append(label)
    elif status.plan_origin == "template":
        lines.append("План перенесён из утверждённого шаблона")

    risky = [
        line
        for line in status.lines
        if line.limit_state is LimitState.POSITIVE
        and line.usage_percent is not None
        and 80 <= line.usage_percent < 100
    ]
    over = [
        line
        for line in status.lines
        if line.effective_limit_minor is not None and line.fact_minor > line.effective_limit_minor
    ]
    if risky or over:
        parts = []
        if risky:
            parts.append(f"{len(risky)} близки к лимиту")
        if over:
            parts.append(f"{len(over)} превышены")
        lines.append(" · ".join(parts))

    if status.uncategorized_fact_minor:
        lines.append(f"Без категории: {money(status.uncategorized_fact_minor, currency)}")
    if status.pending_drafts:
        pending = f"Есть {status.pending_drafts} операций на уточнение"
        if status.pending_confident_minor:
            pending += f" на {money(status.pending_confident_minor, currency)}"
        lines.append(pending)
    if status.completeness == "incomplete":
        lines.append("Полнота учёта: не подтверждена")
    return "\n".join(lines)


def category_lines(status: PeriodStatus, *, page: int = 0) -> tuple[str, bool]:
    """Список статей: сначала превышенные и рискованные (FR-06)."""

    def sort_key(line: LineStatus) -> tuple[int, float]:
        if line.effective_limit_minor is not None and line.fact_minor > line.effective_limit_minor:
            return (0, -line.fact_minor)
        if line.usage_percent is not None and line.usage_percent >= 80:
            return (1, -line.usage_percent)
        return (2, -line.fact_minor)

    ordered = sorted(status.lines, key=sort_key)
    start = page * PAGE_SIZE
    chunk = ordered[start : start + PAGE_SIZE]
    if not chunk:
        return "В этом периоде пока нет статей с планом и расходами.", False
    rows: list[str] = []
    for line in chunk:
        title = line.category_name
        if line.beneficiary_name:
            title = f"{title} · {line.beneficiary_name}"
        fact = money(line.fact_minor, status.currency)
        if line.effective_limit_minor is None:
            rows.append(f"{title}: факт {fact} · {line.status_text}")
        else:
            plan = money(line.effective_limit_minor, status.currency)
            rows.append(f"{title}: {fact} из {plan} · {line.status_text}")
    has_more = start + PAGE_SIZE < len(ordered)
    return "\n".join(rows), has_more


def empty_state(section: str) -> str:
    """Пустые состояния предусмотрены явно (FR-08)."""
    return {
        "history": "В этом бюджете пока нет записанных операций.",
        "categories": "Категорий пока нет. Добавьте первую через «Категории → Добавить».",
        "goals": "Целей накоплений пока нет.",
        "budgets": "У вас пока нет бюджетов. Создайте свой или войдите по коду.",
        "drafts": "Незавершённых записей нет.",
        "payments": "Плановых платежей пока нет.",
    }.get(section, "Пока пусто.")


def history_line(
    *,
    transaction_id: uuid.UUID,
    amount_minor: int,
    currency: str,
    occurred_date: dt.date,
    category_path: str,
    author: str | None,
    is_voided: bool,
    has_note: bool,
) -> str:
    marker = "✗ " if is_voided else ""
    note_marker = " 💬" if has_note else ""
    tail = f" · {author}" if author else ""
    return (
        f"{marker}{format_date(occurred_date)} — {money(amount_minor, currency)} · "
        f"{category_path}{tail}{note_marker}"
    )


def change_kind_label(kind: str) -> str:
    """Человеческое название изменения ревизии (FR-07)."""
    return {
        "created": "создана",
        "amended": "исправлена",
        "voided": "отменена",
        "restored": "восстановлена",
        "note_changed": "изменён комментарий",
        "context_changed": "изменён контекст",
        "import_revision": "обновлена импортом",
    }.get(kind, kind)


def link_type_label(link_type: str) -> str:
    """Человеческое название связи между записями (FR-07)."""
    return {
        "refund_of": "возврат",
        "transfer_pair": "перевод",
        "settles_receivable": "погашение долга",
        "replaces_aggregate": "замена агрегата",
        "duplicate_of": "дубликат",
        "settles_occurrence": "оплата обязательства",
    }.get(link_type, link_type)


__all__ = [
    "PAGE_SIZE",
    "budget_overview",
    "category_lines",
    "change_kind_label",
    "empty_state",
    "format_date",
    "format_range",
    "history_line",
    "link_type_label",
    "money",
    "transaction_card",
]
