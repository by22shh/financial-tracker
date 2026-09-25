"""Telegram copy and HTML formatting. External text is always escaped."""

from datetime import date
from html import escape
from typing import Any

from fintracker.sheetbot.models import Catalog, CategoryStatus, Expense, Reply, ReportScope

WELCOME = (
    "👋 <b>Привет! Я помогу записывать расходы.</b>\n\n"
    "Напишите, на что потратили деньги, или отправьте голосовое. "
    "Я определю категорию и сохраню сумму в таблице.\n\n"
    "✍️ <b>Например</b>\n"
    "«Продукты 1 250 рублей»\n"
    "«Вчера доставка 890 рублей»\n\n"
    "📊 <b>Всё — в последний лист</b>\n"
    "Выбирать ничего не нужно. Когда появится новая вкладка, "
    "следующие расходы пойдут в неё.\n\n"
    "<b>Начнём?</b> Пришлите первую трату.\n\n"
    "👇 Сводки и помощь — на кнопках под полем сообщения."
)

HELP = (
    "📝 <b>Как записать расход</b>\n\n"
    "<b>Текстом</b> — напишите покупку и сумму:\n"
    "«Кофе 250 рублей, продукты 1 200 рублей».\n\n"
    "<b>Голосом</b> — расскажите о тратах обычными словами.\n\n"
    "📊 Запись всегда идёт в последний лист таблицы. "
    "Дату можно указать словами: «сегодня» или «вчера».\n\n"
    "Если чего-то не хватает, я задам вопрос. "
    "Просто ответьте следующим сообщением.\n\n"
    "✏️ Под квитанцией можно изменить или отменить отдельную трату.\n"
    "Исправленные категории запоминаю для следующих покупок.\n\n"
    "📊 Спросите: «Сколько потратил сегодня?» или «Расходы на продукты за период».\n\n"
    "👇 <b>Меню под полем сообщения</b>\n"
    "«Сегодня», «За неделю», «За весь период» — сводки расходов.\n"
    "Неделя — последние 7 дней, включая сегодня, в пределах рабочего листа.\n"
    "«Новый период» — создать следующий лист по шаблону.\n"
    "«Отменить ввод» — выйти из уточнения или исправления."
)


def formatted(text: str) -> Reply:
    """Use only for trusted HTML assembled in this module."""
    return Reply(text=text, parse_mode="HTML")


def notice(heading: str, body: str) -> Reply:
    return formatted(f"<b>{escape(heading)}</b>\n\n{escape(body[:1800])}")


def clarification(question: str) -> Reply:
    return formatted(
        "💬 <b>Нужно уточнение</b>\n\n"
        f"{escape(question[:1800])}\n\n"
        "Ответьте следующим сообщением.\n"
        "Чтобы выйти без записи, нажмите «✖️ Отменить ввод» в меню."
    )


def receipt(
    catalog: Catalog,
    expenses: list[Expense],
    currency: str,
    category_status: list[CategoryStatus] | None = None,
) -> Reply:
    labels = {item.id: item.label for item in catalog.categories}
    statuses = {item.id: item for item in category_status or []}
    blocks = ["✅ <b>Записано</b>"]
    for expense in expenses:
        label = labels[expense.category_id]
        if len(label) > 65:
            label = label[:64] + "…"
        lines = [
            f"💸 <b>{escape(money(expense.amount_minor, currency))}</b>",
            f"📂 {escape(label)}",
            f"📅 {expense.date:%d.%m.%Y}",
        ]
        status = statuses.get(expense.category_id)
        if status:
            lines.append(
                f"📊 Потрачено по категории: {escape(money(status.spent_minor, currency))}"
            )
            lines.append(
                "🎯 План: "
                + (
                    escape(money(status.plan_minor, currency))
                    if status.plan_minor is not None
                    else "не указан"
                )
            )
        else:
            lines.append("📊 Итог и план временно недоступны")
        blocks.append("\n".join(lines))
    blocks.append(f"📊 Лист «{escape(catalog.title[:100])}»")
    return formatted("\n\n".join(blocks))


def with_buttons(reply: Reply, rows: list[list[tuple[str, str]]]) -> Reply:
    from fintracker.sheetbot.models import Button

    reply.buttons = [[Button(text=text, data=data) for text, data in row] for row in rows]
    return reply


def voice_preview(reply: Reply, transcript: str | None) -> Reply:
    if transcript:
        preview = transcript[:240] + ("…" if len(transcript) > 240 else "")
        reply.text += f"\n\n🎙 <b>Я услышал:</b> {escape(preview)}"
    return reply


def money(minor: int, currency: str) -> str:
    value = f"{minor / 100:,.2f}".replace(",", " ").replace(".", ",")
    if value.endswith(",00"):
        value = value[:-3]
    return value + " " + {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency[:12])


def summary_message(data: dict[str, Any], currency: str, *, scope: ReportScope) -> Reply:
    rows = sorted(data["categories"], key=lambda row: row["amount_minor"], reverse=True)
    heading = {
        "today": "Расходы за день",
        "yesterday": "Расходы за день",
        "week": "Расходы за неделю",
        "period": "Расходы за весь период",
    }[scope]
    start = date.fromisoformat(data["from"]).strftime("%d.%m.%Y")
    end = date.fromisoformat(data["to"]).strftime("%d.%m.%Y")
    lines = [
        f"📊 <b>{heading}</b>",
        f"📅 {start} — {end}" if scope in {"week", "period"} else f"📅 {start}",
        f"Всего: <b>{escape(money(data['total_minor'], currency))}</b>",
    ]
    for row in rows[:15]:
        lines.append(
            f"• {escape(row['label'][:80])}: {escape(money(row['amount_minor'], currency))}"
        )
    if len(rows) > 15:
        rest = sum(row["amount_minor"] for row in rows[15:])
        lines.append(f"• Остальные категории: {escape(money(rest, currency))}")
    return formatted("\n\n".join(lines))
