"""Выбор категории кнопкой и ввод её лимита в мастере настройки."""

from __future__ import annotations

import hashlib
import re

from fintracker.application.conversation.keyboards import Button, callback
from fintracker.application.conversation.types import Reply
from fintracker.application.onboarding.wizard import DraftCategory, WizardState
from fintracker.core.money import Money, MoneyError
from fintracker.domain.parsing.amounts import detect_currency, parse_amounts

PAGE_SIZE = 6
ACTIONS = frozenset({"lim", "lclear", "llist", "lpage", "ldone"})


def category_key(index: int, category: DraftCategory) -> str:
    # Название защищает от старой кнопки после изменения списка категорий.
    digest = hashlib.sha256(category.name.encode()).hexdigest()[:12]
    return f"{index}-{digest}"


def find_category(state: WizardState, key: str | None) -> tuple[int, DraftCategory] | None:
    return next(
        ((i, item) for i, item in enumerate(state.categories) if category_key(i, item) == key),
        None,
    )


def limit_label(category: DraftCategory, currency: str) -> str:
    if category.limit_minor is None:
        return "без лимита"
    return Money(category.limit_minor, currency).format()


def prompt(state: WizardState, *, notice: str | None = None) -> Reply:
    currency = state.currency or "RUB"
    selected = find_category(state, state.limit_category)
    buttons: tuple[tuple[Button, ...], ...]
    if selected is not None:
        index, category = selected
        text = (
            f"💰 Лимит: {category.name}\n\n"
            f"Сейчас: {limit_label(category, currency)}\n\n"
            f"✍️ Отправьте сумму на один период бюджета в {currency}.\n"
            "Например: 15000\n\n"
            "0 — траты не запланированы. «Без лимита» — без ограничения суммы."
        )
        buttons = (
            (Button("♾ Без лимита", callback("wiz", "lclear", category_key(index, category))),),
            (Button("← К категориям", callback("wiz", "llist")),),
        )
    else:
        count = len(state.categories)
        last_page = max(0, (count - 1) // PAGE_SIZE)
        page = max(0, min(state.limits_page, last_page))
        total = sum(item.limit_minor or 0 for item in state.categories)
        assigned = sum(item.limit_minor is not None for item in state.categories)
        text = (
            "💰 Лимиты по категориям\n\n"
            "Нажмите на категорию и отправьте сумму. После сохранения вернёмся к списку.\n\n"
            f"Настроено: {assigned} из {count}\n"
            f"Сумма заданных лимитов: {Money(total, currency).format()}\n\n"
            "Лимиты действуют на один период бюджета. Категории без лимита не ограничены."
        )
        rows: list[tuple[Button, ...]] = []
        for index in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, count)):
            category = state.categories[index]
            icon = "✅" if category.limit_minor is not None else "▫️"
            name = category.name if len(category.name) <= 22 else category.name[:21] + "…"
            rows.append(
                (
                    Button(
                        f"{icon} {name} · {limit_label(category, currency)}",
                        callback("wiz", "lim", category_key(index, category)),
                    ),
                )
            )
        if last_page:
            text += f"\n\nСтраница {page + 1} из {last_page + 1}"
            paging = []
            if page:
                paging.append(Button("← Назад", callback("wiz", "lpage", str(page - 1))))
            if page < last_page:
                paging.append(Button("Далее →", callback("wiz", "lpage", str(page + 1))))
            rows.append(tuple(paging))
        if not count:
            text = "🗂 Категорий пока нет\n\nВернитесь к категориям или продолжите без лимитов."
            rows.append((Button("← Добавить категории", callback("wiz", "back")),))
        rows.append((Button("Готово →", callback("wiz", "ldone")),))
        buttons = tuple(rows)
    if notice:
        text = f"{notice}\n\n{text}"
    return Reply(text=text, buttons=buttons)


def choose(state: WizardState, *, action: str, value: str) -> bool:
    """Изменить выбор. True означает завершение шага; суммы не создают расходы."""
    if action == "ldone":
        state.limit_category = None
        return True
    if action == "llist":
        state.limit_category = None
    elif action == "lpage":
        if value.isdigit():
            state.limits_page = min(int(value), max(0, (len(state.categories) - 1) // PAGE_SIZE))
            state.limit_category = None
    elif action == "lim":
        selected = find_category(state, value)
        if selected is not None:
            state.limit_category = value
            state.limits_page = selected[0] // PAGE_SIZE
    elif action == "lclear" and value == state.limit_category:
        selected = find_category(state, value)
        if selected is not None:
            selected[1].limit_minor = None
            state.limit_category = None
            state.deficit_accepted = False
    return False


def apply_amount(state: WizardState, text: str) -> Reply:
    selected = find_category(state, state.limit_category)
    if selected is None:
        state.limit_category = None
        return prompt(state, notice="🔄 Список изменился. Выберите категорию заново.")
    amounts = parse_amounts(text)
    currency = state.currency or "RUB"
    # Принимается ровно одно число, а не фраза о покупке или диапазон сумм.
    if len(amounts) != 1:
        return prompt(state, notice="✍️ Отправьте одну сумму числом, например 15000.")
    amount = amounts[0]
    raw = amount.raw.strip()
    entered = text.strip()
    suffix = entered[len(raw) :].strip()
    valid_suffix = not suffix or (
        re.fullmatch(r"[A-Za-zА-Яа-яЁё]+|[₽$€₸₺₼֏₾£]", suffix)
        and detect_currency(suffix) is not None
    )
    if not entered.startswith(raw) or not valid_suffix:
        return prompt(state, notice="✍️ Отправьте одну сумму числом, например 15000.")
    if amount.value < 0 or amount.is_ambiguous:
        return prompt(
            state, notice="✍️ Нужна однозначная сумма от нуля. Например: 15000 или 1500,50."
        )
    if amount.currency is not None and amount.currency != currency:
        return prompt(state, notice=f"✍️ Укажите сумму в валюте бюджета: {currency}.")
    try:
        money = Money.from_decimal(amount.value, currency)
        total = sum(item.limit_minor or 0 for item in state.categories)
        Money(total - (selected[1].limit_minor or 0) + money.minor, currency)
    except (MoneyError, ArithmeticError):
        return prompt(state, notice="✍️ Проверьте сумму: она слишком велика или записана неточно.")
    category = selected[1]
    category.limit_minor = money.minor
    state.limit_category = None
    state.deficit_accepted = False
    return prompt(state, notice=f"✅ {category.name}: лимит {money.format()} сохранён.")
