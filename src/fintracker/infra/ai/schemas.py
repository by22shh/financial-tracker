"""Строгие схемы результата AI (AI-03, ADR-08).

Дополнительные поля запрещены; тип намерения берётся из перечисления;
ID категорий допускаются только из переданного справочника; длины строк и
число кандидатов ограничены. Невалидный JSON не исполняется как команда.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EXTRACTION_SCHEMA_VERSION = "1.0"
RECOMMENDATION_SCHEMA_VERSION = "1.0"

MAX_CANDIDATES = 10
MAX_STRING = 300
MAX_NOTE = 2000

_DECIMAL_PATTERN = r"^-?\d{1,15}(\.\d{1,4})?$"


class Evidence(BaseModel):
    """Опора модели на исходный текст — для показа «Почему»."""

    model_config = ConfigDict(extra="forbid")

    amount: Annotated[str, Field(max_length=MAX_STRING)] | None = None
    date: Annotated[str, Field(max_length=MAX_STRING)] | None = None
    category: Annotated[str, Field(max_length=MAX_STRING)] | None = None
    beneficiary: Annotated[str, Field(max_length=MAX_STRING)] | None = None
    spender: Annotated[str, Field(max_length=MAX_STRING)] | None = None
    merchant: Annotated[str, Field(max_length=MAX_STRING)] | None = None


class Ambiguity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal[
        "amount", "currency", "date", "category", "beneficiary", "spender", "account", "note"
    ]
    reason: Annotated[str, Field(max_length=MAX_STRING)]
    options: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=6)


class ExtractionCandidate(BaseModel):
    """Один предполагаемый расход или доход."""

    model_config = ConfigDict(extra="forbid")

    candidate_key: Annotated[str, Field(max_length=16, pattern=r"^c\d{1,3}$")]
    kind: Literal["expense", "income", "transfer", "refund", "unknown"]
    # Сумма приходит десятичной строкой: float в денежном пути запрещён (FR-26).
    amount_decimal: Annotated[str, Field(pattern=_DECIMAL_PATTERN)] | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None = None
    currency_origin: Literal["message", "workspace_default", "unknown"] = "unknown"
    date_expression: Annotated[str, Field(max_length=120)] | None = None
    description: Annotated[str, Field(max_length=MAX_STRING)] | None = None
    merchant: Annotated[str, Field(max_length=MAX_STRING)] | None = None
    category_id: Annotated[str, Field(max_length=64)] | None = None
    beneficiary_id: Annotated[str, Field(max_length=64)] | None = None
    spender_person_id: Annotated[str, Field(max_length=64)] | None = None
    account_id: Annotated[str, Field(max_length=64)] | None = None
    note: Annotated[str, Field(max_length=MAX_NOTE)] | None = None
    evidence: Evidence = Field(default_factory=Evidence)
    ambiguities: list[Ambiguity] = Field(default_factory=list, max_length=8)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    def amount_as_decimal(self) -> Decimal | None:
        return Decimal(self.amount_decimal) if self.amount_decimal else None


class ExtractionResponse(BaseModel):
    """Ответ извлечения (AI-01, раздел 18.1 ТЗ)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    intent: Literal[
        "record_transaction",
        "question",
        "hypothetical",
        "negated",
        "future_plan",
        "change_limit",
        "create_category",
        "correct_transaction",
        "cancel_transaction",
        "add_note",
        "reminder",
        "unknown",
    ]
    candidates: list[ExtractionCandidate] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    question: Annotated[str, Field(max_length=MAX_STRING)] | None = None


class ReceiptLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(max_length=MAX_STRING)]
    amount_decimal: Annotated[str, Field(pattern=_DECIMAL_PATTERN)]
    quantity: Annotated[str, Field(max_length=24)] | None = None
    unit_price_decimal: Annotated[str, Field(pattern=_DECIMAL_PATTERN)] | None = None
    category_id: Annotated[str, Field(max_length=64)] | None = None
    readable: bool = True


class ReceiptResponse(BaseModel):
    """Разбор изображения чека (FR-14–FR-17)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    document_kind: Literal[
        "receipt", "invoice", "cart", "order_confirmation", "bank_screenshot", "other"
    ]
    payment_confirmed: bool = False
    merchant: Annotated[str, Field(max_length=MAX_STRING)] | None = None
    date_expression: Annotated[str, Field(max_length=120)] | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None = None
    total_decimal: Annotated[str, Field(pattern=_DECIMAL_PATTERN)] | None = None
    discount_decimal: Annotated[str, Field(pattern=_DECIMAL_PATTERN)] | None = None
    tip_decimal: Annotated[str, Field(pattern=_DECIMAL_PATTERN)] | None = None
    lines: list[ReceiptLine] = Field(default_factory=list, max_length=100)
    unreadable_lines: int = Field(default=0, ge=0, le=500)
    fiscal_id: Annotated[str, Field(max_length=120)] | None = None
    is_refund: bool = False
    ambiguities: list[Ambiguity] = Field(default_factory=list, max_length=8)


class RecommendationCard(BaseModel):
    """Карточка рекомендации с обязательным основанием (FR-75, AI-08)."""

    model_config = ConfigDict(extra="forbid")

    direction: Literal[
        "flexible_spend",
        "repeated_overspend",
        "known_recurring",
        "irregular_payments",
        "savings_goals",
        "budget_imbalance",
    ]
    observation: Annotated[str, Field(max_length=1000)]
    action_kind: Literal[
        "reduce_flexible",
        "adjust_plan",
        "check_tariff",
        "start_fund",
        "adjust_contribution",
        "reallocate_limit",
        "review_completeness",
    ]
    # Каждое числовое утверждение связывается с метрикой снимка (AI-08).
    metric_refs: list[Annotated[str, Field(max_length=80)]] = Field(
        default_factory=list, max_length=10
    )
    estimated_effect_decimal: Annotated[str, Field(pattern=_DECIMAL_PATTERN)] | None = None
    effect_formula: Annotated[str, Field(max_length=500)] | None = None
    effect_unavailable_reason: Annotated[str, Field(max_length=300)] | None = None
    conditions: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=6
    )
    alternative_group: Annotated[str, Field(max_length=60)] | None = None
    stable_line_id: Annotated[str, Field(max_length=64)] | None = None
    priority: int = Field(default=100, ge=1, le=1000)


class RecommendationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    summary: Annotated[str, Field(max_length=1000)]
    abstained_reason: Annotated[str, Field(max_length=300)] | None = None
    cards: list[RecommendationCard] = Field(default_factory=list, max_length=5)


class AnalyticsPlan(BaseModel):
    """Разрешённый план запроса аналитики (AI-06).

    Произвольный SQL, доступ к чужому пространству и сетевой доступ модели
    не предоставляются.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    tool: Literal[
        "get_spending",
        "get_budget_status",
        "list_transactions",
        "get_upcoming_payments",
        "get_goal_status",
        "simulate_purchase",
    ]
    period: Literal["current", "previous", "custom", "calendar_month"] = "current"
    date_from: Annotated[str, Field(max_length=10)] | None = None
    date_to: Annotated[str, Field(max_length=10)] | None = None
    category_ids: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list, max_length=20
    )
    beneficiary_ids: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list, max_length=20
    )
    spender_person_ids: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list, max_length=20
    )
    actor_user_id: Annotated[str, Field(max_length=64)] | None = None
    note_query: Annotated[str, Field(max_length=120)] | None = None
    group_by: Literal["category", "beneficiary", "spender", "day", "none"] = "none"
    clarification: Annotated[str, Field(max_length=300)] | None = None


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema для строгого структурированного вывода провайдера."""
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    return schema
