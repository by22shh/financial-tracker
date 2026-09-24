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


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 запись, 2 записи, 5 записей."""
    value = abs(count) % 100
    if 11 <= value <= 14:
        return many
    last = value % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def role_label(role: str) -> str:
    return {"admin": "администратор", "member": "участник"}.get(role, "участник")


def period_state_label(state: str) -> str:
    return {"open": "текущий", "ended": "завершён", "planned": "запланирован"}.get(state, "создан")


def allocation_role_label(role: str) -> str:
    return {
        "expense": "расход",
        "receivable_increase": "вам должны",
        "receivable_decrease": "возврат долга вам",
        "liability_decrease": "погашение вашего долга",
        "expense_refund": "возврат покупки",
        "receivable_reversal": "уменьшение долга перед вами",
        "income": "доход",
        "external_funding": "внешнее пополнение",
        "interest_expense": "проценты по долгу",
        "principal_repayment": "погашение основного долга",
        "goal_allocation": "на цель",
        "unclassified": "нужно уточнить",
    }.get(role, "другая операция")


def transaction_type_label(transaction_type: str) -> str:
    """Понятное название денежной операции без опоры на цвет или знак суммы."""
    return {
        "expense": "Расход",
        "income": "Доход",
        "refund": "Возврат",
        "transfer": "Перевод",
        "mixed_payment": "Покупка",
        "external_funding": "Пополнение",
        "loan_received": "Получение займа",
        "loan_principal_payment": "Платёж по займу",
        "receivable_settlement": "Возврат долга",
        "adjustment": "Корректировка",
        "legacy_unclassified_flow": "Операция",
    }.get(transaction_type, "Операция")


def _transaction_state_phrase(transaction_type: str, state: str) -> str:
    forms = {
        "expense": ("Расход", "записан", "отменён"),
        "income": ("Доход", "записан", "отменён"),
        "refund": ("Возврат", "записан", "отменён"),
        "transfer": ("Перевод", "записан", "отменён"),
        "mixed_payment": ("Покупка", "записана", "отменена"),
        "external_funding": ("Пополнение", "записано", "отменено"),
        "loan_received": ("Получение займа", "записано", "отменено"),
        "loan_principal_payment": ("Платёж по займу", "записан", "отменён"),
        "receivable_settlement": ("Возврат долга", "записан", "отменён"),
        "adjustment": ("Корректировка", "записана", "отменена"),
    }
    kind, posted, voided = forms.get(transaction_type, ("Операция", "записана", "отменена"))
    return f"{kind} {voided if state == 'voided' else posted}"


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
    transaction_type: str = "expense",
    confirmation: bool = False,
    detailed: bool = False,
) -> str:
    """Краткое подтверждение или нейтральная карточка существующей операции."""
    kind = transaction_type_label(transaction_type)
    if is_voided:
        title = (
            f"↩️ {_transaction_state_phrase(transaction_type, 'voided')}"
            f" · {money(amount_minor, currency)}"
        )
    elif confirmation:
        title = (
            f"✅ {_transaction_state_phrase(transaction_type, 'posted')}"
            f" · {money(amount_minor, currency)}"
        )
    else:
        title = f"🧾 {kind} · {money(amount_minor, currency)}"
    lines = [title, ""]
    if transaction_type == "transfer" and account:
        lines.append(f"Счета: {account}")
    elif transaction_type not in {"income", "transfer"}:
        lines.append(f"Категория: {category_path}")
    lines.append(f"Дата: {format_date(occurred_date, with_year=True)}")
    if author_name and not detailed:
        # В общем бюджете видно, кто добавил чужую запись (FR-04).
        lines.append(f"Добавлено: {author_name}")
    lines.extend(["", f"📒 {workspace_name}"])
    if detailed:
        lines.append(f"Тип операции: {kind}")
    context: list[str] = []
    if beneficiary:
        context.append(f"Для кого: {beneficiary}")
    if spender:
        context.append(f"Кто потратил: {spender}")
    if account and transaction_type != "transfer":
        context.append(f"Счёт: {account}")
    if detailed:
        lines.extend(context)
        if period:
            lines.append(f"Период: {format_range(period[0], period[1])}")
        if author_name:
            lines.append(f"Добавлено: {author_name}")
    if line is not None:
        lines.extend(["", f"💰 По категории: {line.status_text}"])
        if line.limit_state is LimitState.POSITIVE and line.effective_limit_minor:
            lines.append(
                f"Потрачено: {money(line.fact_minor, currency)} "
                f"из {money(line.effective_limit_minor, currency)}"
            )
    if note:
        preview = note.strip()
        lines.extend(["", f"💬 Комментарий: {preview}"])
    return "\n".join(lines)


def budget_overview(status: PeriodStatus, *, workspace_name: str) -> str:
    """Обзор бюджета (раздел 6.3 ТЗ).

    «После известных платежей» намеренно не называется балансом счёта.
    """
    currency = status.currency
    lines = [
        f"📒 {workspace_name}",
        f"Период: {format_range(status.start_date, status.end_inclusive)}",
        "",
        f"💸 Учтённые расходы: {money(status.total_fact_minor, currency)}",
    ]

    if status.total_limit_minor is None:
        lines.append("План расходов: не задан")
    else:
        lines.append(f"План расходов: {money(status.total_limit_minor, currency)}")
        remaining = status.total_limit_minor - status.total_fact_minor
        if remaining < 0:
            lines.append(f"⚠️ Сверх плана: {money(-remaining, currency)}")
        else:
            lines.append(f"Осталось по плану: {money(remaining, currency)}")
        commitments = sum(line.commitments_minor for line in status.lines)
        if commitments:
            lines.extend(["", f"🗓 Предстоящие платежи: {money(commitments, currency)}"])
            lines.append(f"Останется после них: {money(remaining - commitments, currency)}")
            lines.append("Это остаток плана, а не деньги на счёте.")

    if status.plan_status != "approved":
        label = {
            "draft": "План на период ещё не утверждён",
            "needs_review": "План на период стоит проверить",
        }.get(status.plan_status, "План на период стоит проверить")
        lines.extend(["", f"✍️ {label}"])
    elif status.plan_origin == "template":
        lines.extend(["", "🔁 Лимиты повторены из прошлого периода"])

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
            parts.append(f"близко к лимиту — {len(risky)}")
        if over:
            parts.append(f"с перерасходом — {len(over)}")
        lines.extend(["", "⚠️ Категории: " + " · ".join(parts)])

    if status.uncategorized_fact_minor:
        lines.extend(["", f"🗂 Без категории: {money(status.uncategorized_fact_minor, currency)}"])
    if status.pending_drafts:
        pending = (
            f"Не сохранено: {status.pending_drafts} "
            f"{plural(status.pending_drafts, 'запись', 'записи', 'записей')}"
        )
        if status.pending_confident_minor:
            pending += (
                f"\nТраты в них на {money(status.pending_confident_minor, currency)} "
                "в расходы пока не входят"
            )
        lines.extend(["", f"✍️ {pending}"])
    if status.completeness == "incomplete":
        lines.extend(
            ["", "ℹ️ Учёт не отмечен полным. Когда внесёте все траты, нажмите «Проверить учёт»."]
        )
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
        return "Лимитов и трат в этом периоде пока нет.", False
    rows: list[str] = []
    for line in chunk:
        title = line.category_name
        if line.beneficiary_name:
            title = f"{title} · {line.beneficiary_name}"
        fact = money(line.fact_minor, status.currency)
        if line.effective_limit_minor is None:
            rows.append(f"🗂 {title}\nПотрачено: {fact}\n{line.status_text}")
        else:
            plan = money(line.effective_limit_minor, status.currency)
            icon = "⚠️" if line.fact_minor >= line.effective_limit_minor else "🗂"
            rows.append(f"{icon} {title}\n{fact} из {plan}\n{line.status_text}")
    has_more = start + PAGE_SIZE < len(ordered)
    return "\n\n".join(rows), has_more


def empty_state(section: str) -> str:
    """Пустые состояния предусмотрены явно (FR-08)."""
    return {
        "history": (
            "🧾 Здесь будет история операций\n\nПока сохранённых записей нет. "
            "Напишите первую трату, например «кофе 250», или нажмите «➕ Добавить трату»."
        ),
        "categories": (
            "🗂 Пока нет категорий\n\nНажмите «➕ Добавить категорию». Например: "
            "Продукты, Транспорт или Кафе."
        ),
        "goals": (
            "🎯 Первая цель — с чего начнём?\n\nОтпуск, подушка безопасности "
            "или новая техника? Нажмите «Добавить цель» и укажите нужную "
            "сумму."
        ),
        "budgets": (
            "📒 Пока нет бюджетов\n\nСоздайте свой или присоединитесь к общему по коду приглашения."
        ),
        "drafts": ("✅ Всё сохранено\n\nНесохранённых записей нет. Можно добавить новую трату."),
        "payments": (
            "🗓 Плановых платежей пока нет\n\nДобавьте аренду, интернет или подписку — "
            "бот напомнит о сроке, а оплату запишет одним нажатием.\n\nМожно и "
            "текстом: «напомни оплатить интернет 900 25 числа»."
        ),
    }.get(section, "Пока здесь пусто. Новые записи появятся в этом разделе.")


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
    transaction_type: str,
    account_flow: str | None = None,
) -> str:
    kind = transaction_type_label(transaction_type)
    marker = "Отменено · " if is_voided else ""
    note_marker = " 💬" if has_note else ""
    detail = _history_detail(transaction_type, category_path, account_flow)
    second = " · ".join(part for part in (detail, author) if part)
    return f"{marker}{kind} · {money(amount_minor, currency)} · {format_date(occurred_date)}" + (
        f"\n{second}{note_marker}" if second or note_marker else ""
    )


def _history_detail(transaction_type: str, category_path: str, account_flow: str | None) -> str:
    """Вторая строка записи: категория расхода или счета перевода."""
    if transaction_type == "transfer" and account_flow:
        return account_flow
    if transaction_type in {"income", "transfer", "external_funding", "loan_received"}:
        return ""
    return category_path


def history_button_label(
    *,
    amount_minor: int,
    currency: str,
    occurred_date: dt.date,
    transaction_type: str,
    category_path: str,
    account_flow: str | None = None,
) -> str:
    """Предметная подпись кнопки; callback остаётся коротким непрозрачным ID."""
    kind = transaction_type_label(transaction_type)
    detail = _history_detail(transaction_type, category_path, account_flow)
    # Длинное пользовательское название не должно превращать кнопку в абзац.
    if len(detail) > 22:
        detail = detail[:21].rstrip() + "…"
    parts = [f"{occurred_date:%d.%m}", money(amount_minor, currency)]
    if transaction_type != "expense" or not detail:
        parts.append(kind)
    if detail:
        parts.append(detail)
    return " · ".join(parts)


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
    "history_button_label",
    "history_line",
    "link_type_label",
    "money",
    "plural",
    "transaction_card",
    "transaction_type_label",
]
