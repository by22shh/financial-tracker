"""Разбор свободного текста (FR-10, FR-12, A01–A12, A50, A55)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from fintracker.core.errors import ValidationFailed
from fintracker.domain.parsing.amounts import detect_currency, parse_amounts
from fintracker.domain.parsing.arithmetic import evaluate, looks_like_expression
from fintracker.domain.parsing.dates import resolve_date_expression
from fintracker.domain.parsing.intent import Intent, classify_intent, guess_transaction_kind

REFERENCE = dt.date(2026, 9, 10)


def single(text: str) -> Decimal:
    amounts = parse_amounts(text)
    assert len(amounts) == 1, f"ожидалась одна сумма, найдено {len(amounts)}"
    return amounts[0].value


def test_a01_simple_amount() -> None:
    assert single("кофе 250") == Decimal(250)


def test_a07_two_items_by_price() -> None:
    """A07: «Два кофе по 250» означает 500, количество 2."""
    amounts = parse_amounts("Два кофе по 250")
    assert len(amounts) == 1
    assert amounts[0].value == Decimal(500)
    assert amounts[0].quantity == 2


def test_a08_thousand_suffix() -> None:
    """A08: «1,5к бензин» — 1500 с точным десятичным преобразованием."""
    assert single("1,5к бензин") == Decimal(1500)
    assert single("полторы тысячи такси") == Decimal(1500)


def test_thousands_separated_by_spaces() -> None:
    assert single("ресторан 1 200") == Decimal(1200)
    assert single("ресторан 1 200") == Decimal(1200)


def test_ambiguous_dot_thousand_is_flagged() -> None:
    """FR-10: «1.500» допускает несколько интерпретаций и уточняется."""
    amounts = parse_amounts("1.500 продукты")
    assert amounts[0].is_ambiguous
    assert set(amounts[0].ambiguous_options) == {Decimal(1500), Decimal("1.500")}


def test_decimal_comma_is_not_ambiguous() -> None:
    amounts = parse_amounts("кофе 250,50")
    assert amounts[0].value == Decimal("250.50")
    assert not amounts[0].is_ambiguous


def test_a14_currency_is_not_guessed_from_letters() -> None:
    """A14: буква «р» внутри слова не задаёт валюту — иначе пропадёт уточнение."""
    assert detect_currency("вчера заправился на 3200") is None
    assert detect_currency("такси 500") is None
    assert detect_currency("продукты 1200") is None
    assert detect_currency("такси 500 руб") == "RUB"
    assert detect_currency("ресторан 1 200 ₽") == "RUB"
    assert detect_currency("обед 30 USD") == "USD"


def test_multiple_amounts_detected() -> None:
    """A04: «Вчера бензин 3000, сегодня продукты 1800» даёт два кандидата."""
    amounts = parse_amounts("Вчера бензин 3000, сегодня продукты 1800")
    assert [amount.value for amount in amounts] == [Decimal(3000), Decimal(1800)]


def test_a06_no_amount_found() -> None:
    """A06: без суммы не выдумывается значение."""
    assert parse_amounts("Купил продукты") == []


def test_arithmetic_is_limited_and_exact() -> None:
    """FR-10: ограниченный парсер вместо eval; точные десятичные значения."""
    assert evaluate("(438+741)/2") == Decimal("589.5")
    assert evaluate("120 + 80") == Decimal(200)
    assert looks_like_expression("438+741")
    assert not looks_like_expression("кофе 250")


def test_arithmetic_rejects_code_and_overlong_input() -> None:
    with pytest.raises(ValidationFailed):
        evaluate("__import__('os').system('ls')")
    with pytest.raises(ValidationFailed):
        evaluate("1+" * 40 + "1")
    with pytest.raises(ValidationFailed):
        evaluate("10/0")


def test_a50_yesterday_is_relative_to_source_event() -> None:
    """A50: «вчера» от даты исходного сообщения, а не от даты обработки."""
    parsed = resolve_date_expression("вчера бензин 3000", reference=REFERENCE)
    assert parsed is not None
    assert parsed.value == dt.date(2026, 9, 9)
    # Повторная обработка на следующий день не сдвигает дату.
    assert resolve_date_expression("вчера", reference=REFERENCE) == parsed


def test_relative_and_explicit_dates() -> None:
    assert resolve_date_expression("позавчера", reference=REFERENCE).value == dt.date(2026, 9, 8)
    assert resolve_date_expression("9 сентября", reference=REFERENCE).value == dt.date(2026, 9, 9)
    assert resolve_date_expression("08.09", reference=REFERENCE).value == dt.date(2026, 9, 8)
    assert resolve_date_expression("3 дня назад", reference=REFERENCE).value == dt.date(2026, 9, 7)


def test_future_date_is_marked() -> None:
    """A55: будущая дата обозначается и не становится понесённым расходом."""
    parsed = resolve_date_expression("завтра", reference=REFERENCE)
    assert parsed is not None and parsed.is_future


def test_invalid_date_returns_none() -> None:
    assert resolve_date_expression("30 февраля", reference=REFERENCE) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("кофе 250", Intent.RECORD_TRANSACTION),
        ("Если завтра потрачу 3000 на ресторан, сколько останется?", Intent.HYPOTHETICAL),
        ("Хотел купить за 5000, но передумал", Intent.NEGATED),
        ("Сколько я потратил за месяц?", Intent.QUESTION),
        ("Поставь лимит на рестораны 8000", Intent.CHANGE_LIMIT),
        ("Создай категорию Путешествия", Intent.CREATE_CATEGORY),
        ("Напомни оплатить интернет", Intent.REMINDER),
        ("Здесь было 800, а не 1800", Intent.CORRECT_TRANSACTION),
        ("Удали эту трату, внёс по ошибке", Intent.CANCEL_TRANSACTION),
        ("Добавь комментарий: купили перед поездкой", Intent.ADD_NOTE),
    ],
)
def test_intent_guards(text: str, expected: Intent) -> None:
    """A09–A12: вопрос, гипотеза и отрицание не проводятся как покупка."""
    assert classify_intent(text).intent is expected


def test_intent_without_digits_is_unknown() -> None:
    assert classify_intent("привет").intent is Intent.UNKNOWN


def test_purchase_verb_without_amount_is_a_transaction() -> None:
    """A06: «Купил продукты» относится к трате, сумма запрашивается отдельно."""
    assert classify_intent("Купил продукты").intent is Intent.RECORD_TRANSACTION
    assert classify_intent("Заправился на заправке").intent is Intent.RECORD_TRANSACTION
    # Отрицание остаётся приоритетнее глагола покупки.
    assert classify_intent("Хотел купить, но передумал").intent is Intent.NEGATED


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("зарплата 100000", "income"),
        ("аванс 40000", "income"),
        ("вернули 900 за обувь", "refund"),
        ("перевёл 10000 на накопительный", "transfer"),
        ("снял наличные 5000", "transfer"),
        ("кофе 250", "expense"),
    ],
)
def test_transaction_kind_hint(text: str, kind: str) -> None:
    """A13: «зарплата 100000» — доход, а не отрицательный расход."""
    assert guess_transaction_kind(text) == kind
