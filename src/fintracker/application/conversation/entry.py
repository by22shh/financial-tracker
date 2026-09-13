"""Свободный ввод: черновик, кандидаты, уточнения и проведение (FR-10–FR-25).

Детерминированный путь работает без AI: при недоступности модели сохраняется
входящий материал и доступен ручной ввод через форму (NFR-14).
Черновик живёт семь дней после последнего изменения (LIM-05).
Команда CMD-10: чтение, правка, подтверждение и отмена черновика автором.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.directory import resolve_person_alias
from fintracker.application.catalog.normalize import normalize_name
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.access import Beneficiary
from fintracker.db.models.catalog import Category
from fintracker.db.models.platform import Candidate, Clarification, Draft
from fintracker.db.uow import UnitOfWork
from fintracker.domain.ledger.model import (
    AllocationRole,
    AllocationSpec,
    CashLegSpec,
    CoverageMode,
    TransactionSpec,
    TransactionType,
)
from fintracker.domain.parsing.amounts import ParsedAmount, detect_currency, parse_amounts
from fintracker.domain.parsing.arithmetic import evaluate, looks_like_expression
from fintracker.domain.parsing.dates import resolve_date_expression
from fintracker.domain.parsing.intent import Intent, classify_intent, guess_transaction_kind

# Явно обозначенный комментарий в свободном вводе (FR-87).
_NOTE_MARKER = re.compile(
    r"(?:^|[.;,]\s*)(?:комментарий|заметка|примечание)\s*[:\-—]\s*(?P<note>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_SPENDER_PATTERNS = (
    re.compile(r"\b(?:купил[аи]?|оплатил[аи]?|потратил[аи]?)\s+(?P<name>[А-ЯЁ][а-яё]+)\b"),
    re.compile(r"\b(?P<name>[А-ЯЁ][а-яё]+)\s+(?:купил[аи]?|оплатил[аи]?|потратил[аи]?)\b"),
)
# «Софе такси» задаёт получателя, а не совершившего покупку (A190).
_BENEFICIARY_DATIVE = re.compile(r"\b(?P<name>[А-ЯЁ][а-яё]+(?:е|у|ю))\b")
_COMMON_BENEFICIARY_WORDS = re.compile(r"\b(нам|на нас(?: двоих)?|общее|общий)\b", re.IGNORECASE)


@dataclass(slots=True)
class CandidateFields:
    """Извлечённые поля одного предполагаемого события."""

    amount_minor: int | None = None
    currency: str | None = None
    kind: str = "expense"
    occurred_date: dt.date | None = None
    date_expression: str | None = None
    description: str | None = None
    merchant: str | None = None
    note: str | None = None
    category_id: uuid.UUID | None = None
    beneficiary_id: uuid.UUID | None = None
    spender_person_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    quantity: int | None = None
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "kind": self.kind,
            "occurred_date": self.occurred_date.isoformat() if self.occurred_date else None,
            "date_expression": self.date_expression,
            "description": self.description,
            "merchant": self.merchant,
            "note": self.note,
            "category_id": str(self.category_id) if self.category_id else None,
            "beneficiary_id": str(self.beneficiary_id) if self.beneficiary_id else None,
            "spender_person_id": (str(self.spender_person_id) if self.spender_person_id else None),
            "account_id": str(self.account_id) if self.account_id else None,
            "quantity": self.quantity,
            "evidence": self.evidence,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CandidateFields:
        def as_uuid(value: Any) -> uuid.UUID | None:
            return uuid.UUID(str(value)) if value else None

        return cls(
            amount_minor=payload.get("amount_minor"),
            currency=payload.get("currency"),
            kind=payload.get("kind", "expense"),
            occurred_date=(
                dt.date.fromisoformat(payload["occurred_date"])
                if payload.get("occurred_date")
                else None
            ),
            date_expression=payload.get("date_expression"),
            description=payload.get("description"),
            merchant=payload.get("merchant"),
            note=payload.get("note"),
            category_id=as_uuid(payload.get("category_id")),
            beneficiary_id=as_uuid(payload.get("beneficiary_id")),
            spender_person_id=as_uuid(payload.get("spender_person_id")),
            account_id=as_uuid(payload.get("account_id")),
            quantity=payload.get("quantity"),
            evidence=dict(payload.get("evidence") or {}),
        )

    @property
    def is_complete(self) -> bool:
        return (
            self.amount_minor is not None
            and self.currency is not None
            and self.occurred_date is not None
            and not self.ambiguities
        )


@dataclass(slots=True)
class ExtractionResult:
    intent: Intent
    candidates: list[CandidateFields]
    question: str | None = None
    common_note: str | None = None


def _extract_note(text: str) -> tuple[str, str | None]:
    match = _NOTE_MARKER.search(text)
    if not match:
        return text, None
    note = match.group("note").strip()
    remainder = text[: match.start()].strip(" .,;")
    return remainder or text, note or None


def _split_segments(text: str) -> list[str]:
    """Разделить сообщение на кандидатов по запятым и союзу «и».

    Запятая внутри числа является десятичным разделителем и границей
    кандидата не считается: «1 200,50» — одна сумма, а не две (FR-10).
    """
    parts = re.split(r"(?<!\d)[,;]\s*|[,;](?!\d)\s*|\s+и\s+(?=[А-Яа-яA-Za-z])", text)
    return [part.strip() for part in parts if part and part.strip()]


async def _match_category(
    session: AsyncSession, *, workspace_id: uuid.UUID, text: str, actor: ActorContext
) -> tuple[uuid.UUID | None, str | None]:
    """Классификация по приоритету FR-23 без обращения к модели.

    Порядок: подтверждённое персональное правило в этом бюджете → общее
    правило бюджета → точный алиас → совпадение названия категории.
    """
    from fintracker.application.catalog.rules import classify

    normalized = normalize_name(text)
    if not normalized:
        return None, None

    try:
        match = await classify(
            session,
            workspace_id=workspace_id,
            membership_id=actor.membership_id,
            text=text,
        )
    except ConflictError:
        # Конфликт одинаково приоритетных правил не разрешается случайно.
        return None, "rule_conflict"
    if match is not None:
        return match.category_id, match.basis

    categories = (
        await session.execute(
            select(Category.id, Category.normalized_name).where(
                Category.workspace_id == workspace_id, Category.archived_at.is_(None)
            )
        )
    ).all()
    best: tuple[uuid.UUID, int] | None = None
    for category_id, name in categories:
        if not name:
            continue
        stem = name[:5] if len(name) > 5 else name
        if re.search(rf"(?<![а-яёa-z]){re.escape(stem)}", normalized):
            score = len(stem)
            if best is None or score > best[1]:
                best = (category_id, score)
    if best is not None:
        return best[0], "name"
    return None, None


async def _resolve_people(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    text: str,
    actor: ActorContext,
    assume_self_spender: bool,
) -> tuple[uuid.UUID | None, uuid.UUID | None, list[dict[str, Any]]]:
    """Определить совершившего покупку и получателя (FR-88, A190, A191)."""
    ambiguities: list[dict[str, Any]] = []
    spender_id: uuid.UUID | None = None
    beneficiary_id: uuid.UUID | None = None

    for pattern in _SPENDER_PATTERNS:
        match = pattern.search(text)
        if match:
            name = match.group("name")
            try:
                resolved = await resolve_person_alias(
                    session, workspace_id=workspace_id, alias=name
                )
            except Exception:
                resolved = None
            if resolved is not None:
                spender_id = resolved
            else:
                ambiguities.append(
                    {"field": "spender_person_id", "reason": "unknown_person", "value": name}
                )
            break

    if _COMMON_BENEFICIARY_WORDS.search(text):
        beneficiary_id = (
            await session.execute(
                select(Beneficiary.id).where(
                    Beneficiary.workspace_id == workspace_id,
                    Beneficiary.kind == "common",
                    Beneficiary.archived_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    else:
        for match in _BENEFICIARY_DATIVE.finditer(text):
            candidate = match.group("name")
            stem = candidate[:-1]
            row = (
                await session.execute(
                    select(Beneficiary.id, Beneficiary.normalized_name).where(
                        Beneficiary.workspace_id == workspace_id,
                        Beneficiary.archived_at.is_(None),
                    )
                )
            ).all()
            for beneficiary_row_id, normalized in row:
                if normalized and normalized.startswith(normalize_name(stem)):
                    beneficiary_id = beneficiary_row_id
                    break
            if beneficiary_id is not None:
                break

    if spender_id is None and assume_self_spender and actor.person_id is not None:
        # Личное правило «обычно я записываю свои покупки» (FR-88).
        spender_id = actor.person_id
    # Без правила неизвестный человек остаётся «Не указан» и не блокирует запись.
    return spender_id, beneficiary_id, ambiguities


async def extract_from_text(
    session: AsyncSession,
    *,
    settings: Settings,
    actor: ActorContext,
    text: str,
    workspace_currency: str,
    reference_date: dt.date,
    assume_self_spender: bool = False,
) -> ExtractionResult:
    """Детерминированное извлечение полей из свободного текста."""
    workspace_id = actor.require_workspace()
    guess = classify_intent(text)
    if guess.intent in {
        Intent.QUESTION,
        Intent.HYPOTHETICAL,
        Intent.NEGATED,
        Intent.REMINDER,
        Intent.UNKNOWN,
        Intent.CHANGE_LIMIT,
        Intent.CREATE_CATEGORY,
        Intent.CORRECT_TRANSACTION,
        Intent.CANCEL_TRANSACTION,
        Intent.ADD_NOTE,
    }:
        return ExtractionResult(intent=guess.intent, candidates=[])

    body, note = _extract_note(text)
    segments = _split_segments(body)
    amounts_per_segment: list[tuple[str, list[ParsedAmount]]] = []
    for segment in segments:
        if looks_like_expression(segment):
            try:
                value = evaluate(re.sub(r"[^\d+\-*/().,\s]", " ", segment).strip())
            except ValidationFailed:
                value = None
            if value is not None and value > 0:
                amounts_per_segment.append(
                    (
                        segment,
                        [
                            ParsedAmount(
                                value=value,
                                raw=segment,
                                currency=detect_currency(segment),
                            )
                        ],
                    )
                )
                continue
        amounts_per_segment.append((segment, parse_amounts(segment)))

    total_amounts = sum(len(amounts) for _, amounts in amounts_per_segment)
    if total_amounts == 0:
        # Без суммы бот запрашивает сумму, не выдумывая значение (A06).
        fields = CandidateFields(
            description=body.strip()[:300] or None,
            note=note,
            occurred_date=reference_date,
        )
        fields.ambiguities.append({"field": "amount", "reason": "missing"})
        return ExtractionResult(
            intent=Intent.RECORD_TRANSACTION,
            candidates=[fields],
            question="Какая сумма у этой траты?",
        )

    candidates: list[CandidateFields] = []
    for segment, amounts in amounts_per_segment:
        if not amounts:
            continue
        amount = amounts[0]
        # Фрагменты, уже распознанные как суммы, не читаются как дата (AI-05).
        recognised = tuple(item.raw for item in amounts)
        parsed_date = resolve_date_expression(
            segment, reference=reference_date, ignore_raw=recognised
        )
        if parsed_date is None:
            parsed_date = resolve_date_expression(
                body, reference=reference_date, ignore_raw=recognised
            )
        occurred = parsed_date.value if parsed_date else reference_date
        currency = amount.currency or detect_currency(body) or workspace_currency
        fields = CandidateFields(
            amount_minor=Money.from_decimal(Decimal(amount.value), currency).minor,
            currency=currency,
            kind=guess_transaction_kind(segment)
            if len(amounts_per_segment) > 1
            else guess_transaction_kind(body),
            occurred_date=occurred,
            date_expression=parsed_date.expression if parsed_date else None,
            description=_describe(segment, amount.raw),
            quantity=amount.quantity,
            evidence={"amount": amount.raw, "segment": segment},
        )
        if amount.currency is None and detect_currency(body) is None:
            fields.evidence["currency_origin"] = "workspace_default"
        if amount.is_ambiguous:
            fields.ambiguities.append(
                {
                    "field": "amount",
                    "reason": "ambiguous",
                    "options": [str(option) for option in amount.ambiguous_options],
                }
            )
        if parsed_date is not None and parsed_date.is_future:
            fields.ambiguities.append({"field": "date", "reason": "future"})

        category_id, basis = await _match_category(
            session, workspace_id=workspace_id, text=segment or body, actor=actor
        )
        fields.category_id = category_id
        if basis:
            fields.evidence["category_basis"] = basis
        spender, beneficiary, person_ambiguities = await _resolve_people(
            session,
            workspace_id=workspace_id,
            text=segment if len(amounts_per_segment) > 1 else body,
            actor=actor,
            assume_self_spender=assume_self_spender,
        )
        fields.spender_person_id = spender
        fields.beneficiary_id = beneficiary
        fields.ambiguities.extend(person_ambiguities)
        candidates.append(fields)

    if note and len(candidates) == 1:
        candidates[0].note = note
    common_note = note if note and len(candidates) > 1 else None
    if common_note:
        # Общий комментарий применяется ко всему пакету только явно (FR-11).
        for candidate in candidates:
            candidate.ambiguities.append({"field": "note", "reason": "scope_unclear"})

    question = None
    if any(candidate.ambiguities for candidate in candidates):
        question = _first_question(candidates)
    return ExtractionResult(
        intent=Intent.RECORD_TRANSACTION,
        candidates=candidates,
        question=question,
        common_note=common_note,
    )


def _describe(segment: str, amount_raw: str) -> str | None:
    cleaned = segment.replace(amount_raw, " ").strip(" .,;-—")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned[:300] or None


def _first_question(candidates: list[CandidateFields]) -> str | None:
    """Один наиболее полезный вопрос, сохраняя распознанные поля (AI-05)."""
    for candidate in candidates:
        for ambiguity in candidate.ambiguities:
            match ambiguity.get("field"):
                case "amount" if ambiguity.get("reason") == "ambiguous":
                    options = ambiguity.get("options") or []
                    shown = " или ".join(str(option) for option in options)
                    return f"Уточните сумму: {shown}?"
                case "amount":
                    return "Какая сумма у этой траты?"
                case "date" if ambiguity.get("reason") == "future":
                    return "Это будущая покупка или уже совершённая?"
                case "spender_person_id":
                    return (
                        f"Кто такой «{ambiguity.get('value')}»? Добавить его в справочник "
                        "людей бюджета?"
                    )
                case "note":
                    return "Комментарий относится ко всем тратам или к одной?"
    return None


async def create_draft_with_candidates(
    session: AsyncSession,
    *,
    settings: Settings,
    actor: ActorContext,
    source_kind: str,
    raw_text: str | None,
    extraction: ExtractionResult,
    logical_message_id: uuid.UUID | None = None,
    source_fingerprint: str | None = None,
) -> tuple[Draft, list[Candidate]]:
    """Сохранить черновик и кандидатов с постоянными ID (ADR-08)."""
    workspace_id = actor.require_workspace()
    now = dt.datetime.now(dt.UTC)
    draft = Draft(
        workspace_id=workspace_id,
        owner_user_id=actor.user_id,
        owner_membership_generation=actor.membership_generation,
        logical_message_id=logical_message_id,
        source_kind=source_kind,
        state="needs_clarification" if extraction.question else "ready",
        raw_text=raw_text,
        source_fingerprint=source_fingerprint,
        expires_at=now + dt.timedelta(days=settings.limits.draft_ttl_days),
        delete_raw_after=now + dt.timedelta(days=7),
    )
    session.add(draft)
    await session.flush()

    rows: list[Candidate] = []
    for index, fields in enumerate(extraction.candidates, start=1):
        row = Candidate(
            workspace_id=workspace_id,
            draft_id=draft.id,
            candidate_key=f"c{index}",
            state="needs_clarification" if fields.ambiguities else "ready",
            fields=fields.to_payload(),
            ambiguities=fields.ambiguities,
        )
        session.add(row)
        rows.append(row)
    await session.flush()

    # Открытый вопрос сохраняется отдельной записью: свободный ответ не
    # попадает в чужой вопрос наугад (AR-06, R07).
    if extraction.question:
        for row, fields in zip(rows, extraction.candidates, strict=False):
            for ambiguity in fields.ambiguities:
                session.add(
                    Clarification(
                        workspace_id=workspace_id,
                        draft_id=draft.id,
                        candidate_id=row.id,
                        field=str(ambiguity.get("field") or "unknown"),
                        question=extraction.question[:500],
                        options=list(ambiguity.get("options") or []),
                        expected_version=row.version,
                        state="open",
                        expires_at=now + dt.timedelta(days=settings.limits.draft_ttl_days),
                    )
                )
        await session.flush()
    return draft, rows


def build_spec(
    fields: CandidateFields, *, timezone: str, workspace_currency: str
) -> TransactionSpec:
    """Собрать проверяемую спецификацию операции из полей кандидата."""
    if fields.amount_minor is None or fields.occurred_date is None:
        raise ValidationFailed("У кандидата нет суммы или даты")
    currency = fields.currency or workspace_currency
    amount = Money(fields.amount_minor, currency)
    kind = fields.kind

    if kind == "income":
        return TransactionSpec(
            transaction_type=TransactionType.INCOME,
            amount=amount,
            occurred_date=fields.occurred_date,
            timezone=timezone,
            description=fields.description,
            note=fields.note,
            spender_person_id=fields.spender_person_id,
            allocations=(
                AllocationSpec(
                    role=AllocationRole.INCOME,
                    amount=amount,
                    category_id=fields.category_id,
                    beneficiary_id=fields.beneficiary_id,
                ),
            ),
            cash_legs=(
                CashLegSpec(
                    signed=amount,
                    account_id=fields.account_id,
                    coverage=CoverageMode.REFERENCE if fields.account_id else CoverageMode.UNKNOWN,
                ),
            ),
        )

    return TransactionSpec(
        transaction_type=TransactionType.EXPENSE,
        amount=amount,
        occurred_date=fields.occurred_date,
        timezone=timezone,
        description=fields.description,
        merchant=fields.merchant,
        note=fields.note,
        spender_person_id=fields.spender_person_id,
        allocations=(
            AllocationSpec(
                role=AllocationRole.EXPENSE,
                amount=amount,
                category_id=fields.category_id,
                beneficiary_id=fields.beneficiary_id,
            ),
        ),
        cash_legs=(
            CashLegSpec(
                signed=-amount,
                account_id=fields.account_id,
                coverage=CoverageMode.REFERENCE if fields.account_id else CoverageMode.UNKNOWN,
            ),
        ),
    )


async def load_draft(
    session: AsyncSession, *, workspace_id: uuid.UUID, draft_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[Draft, list[Candidate]]:
    draft = (
        await session.execute(
            select(Draft).where(
                Draft.workspace_id == workspace_id,
                Draft.id == draft_id,
                Draft.owner_user_id == owner_id,
            )
        )
    ).scalar_one_or_none()
    if draft is None:
        raise NotFound("Черновик недоступен")
    candidates = (
        (
            await session.execute(
                select(Candidate)
                .where(Candidate.workspace_id == workspace_id, Candidate.draft_id == draft_id)
                .order_by(Candidate.candidate_key)
            )
        )
        .scalars()
        .all()
    )
    return draft, list(candidates)


async def post_draft(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    actor: ActorContext,
    draft: Draft,
    candidates: list[Candidate],
    timezone: str,
    workspace_currency: str,
    origin: str,
) -> list[uuid.UUID]:
    """Провести подтверждённый пакет одной транзакцией базы (FR-11).

    Частичный технический успех недопустим: либо проводятся все кандидаты,
    либо ни один.
    """
    from fintracker.application.ledger.service import post_transaction

    if draft.state == "posted":
        return [c.posted_transaction_id for c in candidates if c.posted_transaction_id]

    posted: list[uuid.UUID] = []
    for candidate in candidates:
        if candidate.state in {"excluded", "cancelled"}:
            continue
        fields = CandidateFields.from_payload(dict(candidate.fields))
        spec = build_spec(fields, timezone=timezone, workspace_currency=workspace_currency)
        result = await post_transaction(
            session,
            uow,
            actor=actor,
            spec=spec,
            origin=origin,
            source_candidate_id=candidate.id,
        )
        candidate.state = "posted"
        candidate.posted_transaction_id = result.transaction_id
        candidate.version += 1
        posted.append(result.transaction_id)
    draft.state = "posted"
    draft.version += 1
    await session.flush()
    return posted
