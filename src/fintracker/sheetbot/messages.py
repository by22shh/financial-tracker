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
    "👇 Сводки, категории и помощь — на кнопках под полем сообщения."
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
    "«Категории» — все категории последнего листа: потрачено и план.\n"
    "Неделя — последние 7 дней, включая сегодня, в пределах рабочего листа.\n"
    "«Новый период» — создать следующий лист по шаблону.\n"
    "«Отменить ввод» — выйти из уточнения или исправления."
)

GROUP_EMOJIS = {
    "AI + окружение для работы": "🤖",
    "DDX Зал Абонемент Тренер": "🏋️",
    "Врачи": "🩺",
    "Вредные привычки": "🚬",
    "Дом": "🏡",
    "Досуг": "🎟️",
    "Другое": "📎",
    "Квартира": "🏠",
    "Квартира Ставрополь": "🏘️",
    "Косметика": "💄",
    "Лекарства": "💊",
    "Машина": "🚗",
    "Накопления": "💰",
    "Ниджат DDX Зал Абонемент": "🏋️",
    "Одежда": "👕",
    "Подарки": "🎁",
    "Подписки": "🔁",
    "Продукты питания": "🛒",
    "Рестораны": "🍽️",
    "Родители": "👪",
    "Связь (телефон, интернет)": "📱",
    "Софа DDX Зал Абонемент": "🏋️",
    "Софа Epoque Пилатес Абонемент": "🧘",
    "Софа Репетитор Турецкий": "📖",
    "Спортивное питание": "🥤",
    "Транспорт": "🚕",
    "Уход за собой": "🧴",
    "Шанелька": "🐾",
}


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
        ]
        if category_status is not None:
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
                lines.append("📊 Трата сохранена, но итог и план сейчас не загрузились")
        lines.append(f"📅 {expense.date:%d.%m.%Y}")
        blocks.append("\n".join(lines))
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
    groups: dict[str, list[tuple[str | None, int]]] = {}
    for row in data["categories"]:
        parent, separator, child = row["label"].partition(" / ")
        groups.setdefault(parent, []).append((child if separator else None, row["amount_minor"]))
    ranked = sorted(groups.items(), key=lambda group: -sum(amount for _, amount in group[1]))
    heading = {
        "today": "Расходы за день",
        "yesterday": "Расходы за день",
        "week": "Расходы за неделю",
        "period": "Расходы за весь период",
    }[scope]
    start = date.fromisoformat(data["from"]).strftime("%d.%m.%Y")
    end = date.fromisoformat(data["to"]).strftime("%d.%m.%Y")
    period = f"📅 {start} — {end}" if scope in {"week", "period"} else f"📅 {start}"
    blocks = [
        f"📊 <b>{heading}</b>\n{period}",
        f"💸 <b>Всего: {escape(money(data['total_minor'], currency))}</b>",
    ]
    categories = []
    for parent, entries in ranked[:15]:
        entries.sort(key=lambda entry: -entry[1])
        group_total = sum(amount for _, amount in entries)
        heading = f"{GROUP_EMOJIS.get(parent, '📁')} <b>{escape(parent[:80])}</b>"
        if len(entries) == 1 and entries[0][0] is None:
            categories.append(f"{heading} — <b>{escape(money(group_total, currency))}</b>")
            continue
        if len(entries) > 1:
            heading += f" · <b>{escape(money(group_total, currency))}</b>"
        children = []
        for index, (child, amount) in enumerate(entries[:4]):
            branch = "└" if index == len(entries) - 1 else "├"
            name = child or "Без подкатегории"
            children.append(
                f"{branch} {escape(name[:80])} — <b>{escape(money(amount, currency))}</b>"
            )
        if len(entries) > 4:
            rest = sum(amount for _, amount in entries[4:])
            children.append(f"└ Остальные подкатегории — <b>{escape(money(rest, currency))}</b>")
        categories.append(heading + "\n" + "\n".join(children))
    if len(ranked) > 15:
        rest = sum(amount for _, entries in ranked[15:] for _, amount in entries)
        categories.append(f"📁 <b>Остальные категории</b> — <b>{escape(money(rest, currency))}</b>")
    if categories:
        blocks.append("📂 <b>По категориям</b>\n" + "\n\n".join(categories))
    return formatted("\n\n".join(blocks))


def category_overview_message(
    catalog: Catalog, statuses: list[CategoryStatus], currency: str, *, page: int = 0
) -> Reply:
    by_id = {item.id: item for item in statuses}
    groups: dict[str, list[tuple[str | None, CategoryStatus]]] = {}
    for category in catalog.categories:
        parent, separator, child = category.label.partition(" / ")
        groups.setdefault(parent, []).append((child if separator else None, by_id[category.id]))

    start, end = min(catalog.dates), max(catalog.dates)
    spent = sum(item.spent_minor for item in statuses)
    planned = sum(item.plan_minor or 0 for item in statuses)
    missing_plans = sum(item.plan_minor is None for item in statuses)
    plan_label = "План" if not missing_plans else "Указано в планах"
    header = (
        "📋 <b>Категории и планы</b>\n"
        f"📅 {start:%d.%m.%Y} — {end:%d.%m.%Y}\n\n"
        f"💸 Потрачено: <b>{escape(money(spent, currency))}</b>\n"
        f"🎯 {plan_label}: <b>{escape(money(planned, currency))}</b>"
    )

    def values(item: CategoryStatus) -> str:
        spent = f"<b>{escape(money(item.spent_minor, currency))}</b>"
        if item.plan_minor is None:
            return f"{spent} (план не задан)"
        return f"{spent} из {escape(money(item.plan_minor, currency))}"

    group_blocks = []
    for parent, entries in groups.items():
        title = f"{GROUP_EMOJIS.get(parent, '📁')} <b>{escape(parent[:80])}</b>"
        if len(entries) == 1 and entries[0][0] is None:
            group_blocks.append(f"{title} — {values(entries[0][1])}")
            continue
        if len(entries) > 1:
            group_spent = sum(item.spent_minor for _, item in entries)
            title += f" — <b>{escape(money(group_spent, currency))}</b>"
        lines = [title]
        for index, (subcategory, item) in enumerate(entries):
            branch = "└" if index == len(entries) - 1 else "├"
            lines.append(
                f"{branch} {escape((subcategory or 'Без подкатегории')[:80])} — {values(item)}"
            )
        group_blocks.append("\n".join(lines))

    pages: list[list[str]] = []
    current: list[str] = []
    for block in group_blocks:
        candidate = header + "\n\n" + "\n\n".join([*current, block])
        if current and len(candidate) > 3500:
            pages.append(current)
            current = [block]
        else:
            current.append(block)
    if current:
        pages.append(current)
    if not pages:
        return formatted(header)

    page = max(0, min(page, len(pages) - 1))
    marker = f"\nСтраница {page + 1} из {len(pages)}" if len(pages) > 1 else ""
    reply = formatted(header + marker + "\n\n" + "\n\n".join(pages[page]))
    buttons = []
    if page:
        buttons.append(("⬅️ Назад", f"categories:{page - 1}"))
    if page + 1 < len(pages):
        buttons.append(("Далее ➡️", f"categories:{page + 1}"))
    return with_buttons(reply, [buttons]) if buttons else reply
