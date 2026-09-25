"""Telegram copy and HTML formatting. External text is always escaped."""

from html import escape
from typing import Any

from fintracker.sheetbot.models import Catalog, Expense, Reply

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
    "<b>Начнём?</b> Пришлите первую трату."
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
    "/today — расходы за сегодня\n"
    "/summary — сводка периода\n"
    "/period — новый период по шаблону\n"
    "/cancel — отменить текущий ввод\n"
    "/help — открыть подсказку"
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
        "/cancel — отменить этот расход"
    )


def receipt(catalog: Catalog, expenses: list[Expense], currency: str) -> Reply:
    labels = {item.id: item.label for item in catalog.categories}
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    blocks = ["✅ <b>Записано</b>"]
    for expense in expenses:
        amount = f"{expense.amount_minor // 100:,}".replace(",", " ")
        if expense.amount_minor % 100:
            amount += f",{expense.amount_minor % 100:02d}"
        label = labels[expense.category_id]
        if len(label) > 65:
            label = label[:64] + "…"
        blocks.append(
            f"<b>{amount} {escape(symbol[:12])}</b>\n{escape(label)}\n{expense.date:%d.%m.%Y}"
        )
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


def summary_message(data: dict[str, Any], currency: str) -> Reply:
    rows = sorted(data["categories"], key=lambda row: row["amount_minor"], reverse=True)
    lines = [
        "📊 <b>Ваши расходы</b>",
        f"{escape(data['from'])} — {escape(data['to'])}",
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
