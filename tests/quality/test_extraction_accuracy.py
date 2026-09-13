"""Измеренная точность разбора на размеченном наборе (AI-04, AI-05, FR-19).

Самооценка модели не используется как вероятность правильности: решение об
автозаписи опирается на проверенные классы входа, детерминированные
ограничения и измеренную здесь точность по полям.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.conversation.entry import extract_from_text
from tests.conftest import requires_pg
from tests.integration.factories import Fixture, build_fixture

pytestmark = [pytest.mark.pg, requires_pg]

EVIDENCE = pathlib.Path(__file__).resolve().parents[2] / ".planning" / "evidence"
REFERENCE = dt.date(2026, 9, 12)

# Пороги измеренной точности по полям. Цель — высокая точность записанной
# части, а не максимальная доля автозаписей (AI-04).
THRESHOLDS = {
    "amount": 0.95,
    "currency": 0.95,
    "date": 0.95,
    "kind": 0.95,
    "category": 0.80,
}


@dataclass(frozen=True, slots=True)
class Sample:
    """Размеченный пример: текст и ожидаемые поля."""

    text: str
    amount_minor: int | None
    currency: str | None
    occurred_date: dt.date | None
    kind: str
    category: str | None
    should_record: bool = True


CORPUS: tuple[Sample, ...] = (
    Sample("кофе 250", 25_000, "RUB", REFERENCE, "expense", "Рестораны"),
    Sample("продукты 1500", 150_000, "RUB", REFERENCE, "expense", "Продукты"),
    Sample(
        "вчера такси 430", 43_000, "RUB", REFERENCE - dt.timedelta(days=1), "expense", "Транспорт"
    ),
    Sample("рестораны 2400", 240_000, "RUB", REFERENCE, "expense", "Рестораны"),
    Sample("продукты 1 200,50", 120_050, "RUB", REFERENCE, "expense", "Продукты"),
    Sample("два кофе по 250", 50_000, "RUB", REFERENCE, "expense", "Рестораны"),
    Sample("транспорт 90", 9_000, "RUB", REFERENCE, "expense", "Транспорт"),
    Sample("зарплата 100000", 10_000_000, "RUB", REFERENCE, "income", None),
    Sample("аванс 40000", 4_000_000, "RUB", REFERENCE, "income", None),
    Sample("продукты 500 рублей", 50_000, "RUB", REFERENCE, "expense", "Продукты"),
    Sample("кофе 3.5 USD", 350, "USD", REFERENCE, "expense", "Рестораны"),
    Sample(
        "позавчера продукты 700",
        70_000,
        "RUB",
        REFERENCE - dt.timedelta(days=2),
        "expense",
        "Продукты",
    ),
    Sample("рестораны 1000 и транспорт 200", 100_000, "RUB", REFERENCE, "expense", "Рестораны"),
    Sample("продукты 0", 0, "RUB", REFERENCE, "expense", "Продукты"),
    Sample("такси 430", 43_000, "RUB", REFERENCE, "expense", "Транспорт"),
)

# Входы, которые не должны становиться записью (AI-05, FR-12).
GUARDED: tuple[str, ...] = (
    "Сколько я потратил на продукты?",
    "Если потрачу 5000, что останется?",
    "Передумал, не купил кофе за 250",
    "Завтра куплю продукты на 3000",
    "Напомни оплатить интернет 900",
)


# Синонимы, которые участник задаёт в своём бюджете: без них «кофе» не имеет
# основания для статьи, и выдумывать её нельзя (FR-23, AI-05).
ALIASES: tuple[tuple[str, str], ...] = (
    ("кофе", "Рестораны"),
    ("такси", "Транспорт"),
)


async def _seed_aliases(session: AsyncSession, fixture: Fixture) -> None:
    from fintracker.db.models.catalog import CategoryAlias

    for alias, category in ALIASES:
        session.add(
            CategoryAlias(
                workspace_id=fixture.workspace.id,
                category_id=fixture.categories[category],
                alias=alias,
                normalized_alias=alias,
                created_by=fixture.user.id,
            )
        )
    await session.flush()


async def _measure(session: AsyncSession, fixture: Fixture) -> dict[str, float]:
    names = {value: key for key, value in fixture.categories.items()}
    hits = dict.fromkeys(THRESHOLDS, 0)
    totals = dict.fromkeys(THRESHOLDS, 0)

    for sample in CORPUS:
        result = await extract_from_text(
            session,
            settings=None,
            actor=fixture.actor,
            text=sample.text,
            workspace_currency="RUB",
            reference_date=REFERENCE,
            assume_self_spender=False,
        )
        if not result.candidates:
            for field_name in totals:
                totals[field_name] += 1
            continue
        candidate = result.candidates[0]

        totals["amount"] += 1
        if candidate.amount_minor == sample.amount_minor:
            hits["amount"] += 1
        totals["currency"] += 1
        if (candidate.currency or "RUB") == sample.currency:
            hits["currency"] += 1
        totals["date"] += 1
        if candidate.occurred_date == sample.occurred_date:
            hits["date"] += 1
        totals["kind"] += 1
        if candidate.kind == sample.kind:
            hits["kind"] += 1
        if sample.category is not None:
            totals["category"] += 1
            resolved = names.get(candidate.category_id) if candidate.category_id else None
            if resolved == sample.category:
                hits["category"] += 1

    return {name: (hits[name] / totals[name] if totals[name] else 0.0) for name in THRESHOLDS}


async def test_ai04_measured_accuracy_by_field(owner_session: AsyncSession) -> None:
    """AI-04: точность измеряется отдельно по сумме, валюте, дате, типу и статье."""
    fixture = await build_fixture(owner_session)
    await _seed_aliases(owner_session, fixture)
    accuracy = await _measure(owner_session, fixture)

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "extraction_accuracy.json").write_text(
        json.dumps(
            {
                "measured_at": dt.datetime.now(dt.UTC).isoformat(),
                "corpus_size": len(CORPUS),
                "guarded_inputs": len(GUARDED),
                "accuracy": {name: round(value, 4) for name, value in accuracy.items()},
                "thresholds": THRESHOLDS,
                "method": (
                    "Детерминированный разбор без модели; самооценка модели "
                    "как вероятность правильности не используется (AI-04)."
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    below = {name: round(value, 3) for name, value in accuracy.items() if value < THRESHOLDS[name]}
    assert not below, f"Точность ниже порога: {below}"


async def test_ai04_guarded_inputs_are_not_recorded(owner_session: AsyncSession) -> None:
    """AI-04, AI-05, FR-12: вопрос, гипотеза и план не записываются молча."""
    from fintracker.domain.parsing.intent import Intent

    fixture = await build_fixture(owner_session)
    for text in GUARDED:
        result = await extract_from_text(
            owner_session,
            settings=None,
            actor=fixture.actor,
            text=text,
            workspace_currency="RUB",
            reference_date=REFERENCE,
            assume_self_spender=False,
        )
        guarded = result.intent is not Intent.RECORD_TRANSACTION
        # Будущая покупка допускается как кандидат, но только с вопросом:
        # автоматической записи не происходит (A55, AI-05).
        asked = result.question is not None or any(
            candidate.ambiguities for candidate in result.candidates
        )
        assert guarded or asked, f"«{text}» не должно записываться без уточнения"


def test_ai04_autopost_ignores_model_self_confidence() -> None:
    """AI-04, FR-19: автозапись не зависит от самооценки модели."""
    import uuid

    from fintracker.application.conversation.entry import CandidateFields
    from fintracker.application.conversation.service import _autopost_allowed

    confident = CandidateFields(
        amount_minor=50_000,
        currency="RUB",
        kind="expense",
        occurred_date=REFERENCE,
        category_id=uuid.uuid4(),
        evidence={"confidence": "0.99"},
    )
    # Без включённой участником автозаписи самооценка ничего не меняет.
    assert not _autopost_allowed([confident], autopost=False, large_threshold=None)
    assert _autopost_allowed([confident], autopost=True, large_threshold=None)
    # Настроенный порог крупной суммы важнее любой уверенности модели.
    assert not _autopost_allowed([confident], autopost=True, large_threshold=50_000)

    ambiguous = CandidateFields(
        amount_minor=50_000,
        currency="RUB",
        kind="transfer",
        occurred_date=REFERENCE,
        category_id=uuid.uuid4(),
        evidence={"confidence": "0.99"},
    )
    assert not _autopost_allowed([ambiguous], autopost=True, large_threshold=None)
