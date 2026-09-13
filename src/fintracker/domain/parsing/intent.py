"""Детерминированное распознавание намерения без AI (FR-12, NFR-14).

Кнопки и формы управления работают независимо от доступности AI; модель лишь
помогает понять свободную формулировку. Здесь распознаются классы намерений,
которые нельзя проводить как расход: вопросы, гипотезы, отрицания, планы.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Intent(StrEnum):
    RECORD_TRANSACTION = "record_transaction"
    QUESTION = "question"
    HYPOTHETICAL = "hypothetical"
    NEGATED = "negated"
    FUTURE_PLAN = "future_plan"
    CHANGE_LIMIT = "change_limit"
    CREATE_CATEGORY = "create_category"
    CORRECT_TRANSACTION = "correct_transaction"
    CANCEL_TRANSACTION = "cancel_transaction"
    ADD_NOTE = "add_note"
    REMINDER = "reminder"
    UNKNOWN = "unknown"


# Порядок важен: гипотеза и отрицание приоритетнее обычной записи (A09, A10).
_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (
        Intent.HYPOTHETICAL,
        re.compile(
            r"\bесли\b.{0,60}\b(потрач|куплю|купить|отдам|заплач)|"
            r"\bмогу ли я\b|\bсмогу ли\b|\bчто останется\b|\bхватит ли\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.NEGATED,
        re.compile(
            r"\bпередума|\bне куп|\bне стал\b|\bотменил[аи]? покупк|\bне брал[аи]?\b|"
            r"\bхотел[аи]? купить\b.{0,40}\bно\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.CORRECT_TRANSACTION,
        re.compile(
            r"\bисправ|\bпоправ|\bздесь было\b|\bа не\b\s*\d|\bперенеси\b|"
            r"\bэто было вчера\b|\bзамени\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.CANCEL_TRANSACTION,
        re.compile(
            r"\bудали эту трату\b|\bудали трату\b|\bэто дубл|\bвнёс по ошибке\b|"
            r"\bвнес по ошибке\b|\bотмени запись\b|\bотмени трату\b",
            re.IGNORECASE,
        ),
    ),
    (
        Intent.ADD_NOTE,
        re.compile(r"\bдобавь комментарий\b|\bкомментарий:\s", re.IGNORECASE),
    ),
    (
        Intent.CHANGE_LIMIT,
        re.compile(r"\b(поставь|установи|измени|подними|снизь)\s+лимит\b", re.IGNORECASE),
    ),
    (
        Intent.CREATE_CATEGORY,
        re.compile(r"\b(создай|добавь|заведи)\s+категор", re.IGNORECASE),
    ),
    (
        Intent.REMINDER,
        re.compile(r"\bнапомни\b", re.IGNORECASE),
    ),
    (
        Intent.QUESTION,
        re.compile(r"^\s*(сколько|что|как|почему|когда|покажи|сравни|где)\b|\?\s*$", re.IGNORECASE),
    ),
    (
        Intent.FUTURE_PLAN,
        re.compile(
            r"\b(завтра|послезавтра|на следующей неделе)\b.{0,40}\b(куплю|оплачу|плачу)\b",
            re.IGNORECASE,
        ),
    ),
)


# Глаголы покупки без суммы: сообщение относится к трате, но сумму нужно
# запросить, а не выдумать (A06, AI-05).
_PURCHASE_VERBS = re.compile(
    r"(?<![а-яё])(купил|купила|куплен|потратил|потратила|оплатил|оплатила|"
    r"заказал|заказала|заправил|заправился|заправилась|взял|взяла|сходил|сходила)",
    re.IGNORECASE,
)


def _note_belongs_to_new_record(text: str, match: re.Match[str]) -> bool:
    """Пояснение относится к трате из этого же сообщения, а не к прошлой записи."""
    if match.group().strip().lower().startswith("добавь"):
        return False
    return bool(re.search(r"\d", text[: match.start()]))


@dataclass(frozen=True, slots=True)
class IntentGuess:
    intent: Intent
    matched: str | None


def classify_intent(text: str) -> IntentGuess:
    """Определить намерение по явным признакам текста.

    Возвращает ``RECORD_TRANSACTION`` только когда ни один защитный образец
    не сработал: число в сообщении само по себе не создаёт покупку (FR-12).
    """
    stripped = text.strip()
    if not stripped:
        return IntentGuess(Intent.UNKNOWN, None)
    for intent, pattern in _PATTERNS:
        match = pattern.search(stripped)
        if not match:
            continue
        if intent is Intent.ADD_NOTE and _note_belongs_to_new_record(stripped, match):
            # «Кофе 250. Комментарий: …» — одна новая трата с пояснением, а не
            # заметка к уже записанной операции (A188).
            break
        return IntentGuess(intent, match.group())
    if re.search(r"\d", stripped):
        return IntentGuess(Intent.RECORD_TRANSACTION, None)
    verb = _PURCHASE_VERBS.search(stripped)
    if verb:
        # Сообщение о покупке без суммы: сумма запрашивается отдельно.
        return IntentGuess(Intent.RECORD_TRANSACTION, verb.group())
    return IntentGuess(Intent.UNKNOWN, None)


# Русские основы слов: граница слова после основы не ставится, иначе
# «зарплата» не совпадёт с основой «зарплат».
_INCOME_WORDS = re.compile(
    r"(?<![а-яё])(зарплат|аванс|премия|прем[иь]|получил[аи]? деньги|доход|поступил|"
    r"кэшбэк|кешбэк)",
    re.IGNORECASE,
)
_REFUND_WORDS = re.compile(r"(?<![а-яё])(вернул[аи]?|возврат|возвращен)", re.IGNORECASE)
_TRANSFER_WORDS = re.compile(
    r"(?<![а-яё])(перевёл|перевел|перевод|снял[аи]? наличн|снятие наличн|положил[аи]? на)",
    re.IGNORECASE,
)


def guess_transaction_kind(text: str) -> str:
    """Грубая подсказка типа операции для детерминированного пути."""
    if _REFUND_WORDS.search(text):
        return "refund"
    if _TRANSFER_WORDS.search(text):
        return "transfer"
    if _INCOME_WORDS.search(text):
        return "income"
    return "expense"
