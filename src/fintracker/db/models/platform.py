"""Приём, очередь, outbox, доставки и идемпотентность (DATA_CONTRACT §2.6, ADR-05)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fintracker.db.base import Base, now_server, pk_uuid, version_column


class InboundEvent(Base):
    """Долговечный приём обновления до ответа 2xx (TECH-01, TECH-02)."""

    __tablename__ = "inbound_events"

    id: Mapped[uuid.UUID] = pk_uuid()
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    edit_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    media_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Тип чата фиксируется при приёме: приватный ответ не уходит в группу
    # и решение не зависит от чтения защищённого payload (SEC-05).
    chat_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Контекст закрепляется при приёме и не меняется поздним переключением (FR-79).
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    membership_generation: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'received'")
    )
    received_at: Mapped[dt.datetime] = now_server()
    telegram_date: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("bot_id", "update_id"),
        CheckConstraint(
            "state IN ('received','routed','processed','ignored','failed')", name="state_allowed"
        ),
        Index("ix_inbound_events_ws", "workspace_id", "received_at"),
        Index("ix_inbound_events_message", "bot_id", "chat_id", "message_id"),
    )


class InboundPayload(Base):
    """Защищённое содержимое входящего события отдельно от очереди (ADR-06).

    Секрет приглашения сюда не попадает: он заменяется digest на приёме.
    """

    __tablename__ = "inbound_payloads"

    inbound_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("inbound_events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    delete_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (Index("ix_inbound_payloads_retention", "delete_after"),)


class LogicalMessage(Base):
    """Логическое сообщение с учётом редактирования и альбома (TECH-02)."""

    __tablename__ = "logical_messages"

    id: Mapped[uuid.UUID] = pk_uuid()
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    edit_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    media_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("bot_id", "chat_id", "message_id", "edit_version"),
        Index("ix_logical_messages_group", "bot_id", "chat_id", "media_group_id"),
    )


class MessagePart(Base):
    __tablename__ = "message_parts"

    id: Mapped[uuid.UUID] = pk_uuid()
    logical_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("logical_messages.id", ondelete="CASCADE"), nullable=False
    )
    inbound_event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    part_index: Mapped[int] = mapped_column(Integer, nullable=False)
    part_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("logical_message_id", "part_index"),
        UniqueConstraint("logical_message_id", "inbound_event_id"),
    )


class PendingAction(Base):
    """Ожидаемый ввод участника после нажатия кнопки (FR-21, FR-45, G-13…G-16).

    Кнопка, обещающая продолжение диалога, сохраняет здесь свой контекст:
    следующее сообщение участника относится к обещанному действию, а не
    разбирается как новая трата.
    """

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = pk_uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = now_server()
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_pending_actions_user"),
        Index("ix_pending_actions_expiry", "expires_at"),
    )


class HistoryQueryState(Base):
    """Server-side continuation for a journal query that cannot fit Telegram's 64 bytes."""

    __tablename__ = "history_query_states"

    id: Mapped[uuid.UUID] = pk_uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token: Mapped[str] = mapped_column(String(16), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", "token"),
        Index("ix_history_query_states_expiry", "expires_at"),
    )


class Draft(Base):
    """Черновик ввода (FR-20). Личный до проведения."""

    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_membership_generation: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    logical_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Постоянный ключ исходного сообщения: одинаков для всех его редакций и
    # для любого повтора обработки одного входа (R-01, R-03).
    source_message_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'received'")
    )
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Исходный материал черновика: ссылки на файлы и разобранный документ.
    # Нужен, чтобы повтор разбора и подтверждение оплаты работали с тем же
    # материалом, а не начинали заново (FR-20, G-16).
    source_media: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = version_column()
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delete_raw_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "state IN ('received','processing','needs_clarification','ready','posted',"
            "'failed_retryable','cancelled','expired')",
            name="state_allowed",
        ),
        CheckConstraint(
            "source_kind IN ('text','voice','photo','document','form','album')",
            name="source_kind_allowed",
        ),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_drafts_owner", "workspace_id", "owner_user_id", "state"),
        Index("ix_drafts_expiry", "expires_at", postgresql_where=text("state <> 'posted'")),
        Index(
            "uq_drafts_source_message",
            "workspace_id",
            "owner_user_id",
            "source_message_key",
            unique=True,
            postgresql_where=text("source_message_key IS NOT NULL AND state <> 'cancelled'"),
        ),
    )


class AuthorReply(Base):
    """Подготовленный ответ автору до его доставки (FR-53, ADR-06, R-04).

    Текст ответа содержит суммы и статьи бюджета, поэтому хранится в строке,
    изолированной по бюджету и владельцу, а не в глобальной таблице задач.
    Задача доставки ссылается только на идентификатор.
    """

    __tablename__ = "author_replies"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    inbound_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    messages: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    # Связь отправленных карточек с операциями: ответ на конкретную карточку
    # адресует именно её операцию (FR-33, G-06).
    card_links: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    created_at: Mapped[dt.datetime] = now_server()
    delete_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("state IN ('pending','sent','cancelled')", name="state_allowed"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_author_replies_event", "workspace_id", "inbound_event_id"),
        Index("ix_author_replies_cards", "card_links", postgresql_using="gin"),
        Index("ix_author_replies_retention", "delete_after"),
    )


class Candidate(Base):
    """Предполагаемое денежное событие внутри черновика (ADR-08)."""

    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_key: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'draft'"))
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ambiguities: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    manual_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    posted_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "state IN ('draft','ready','needs_clarification','posted','excluded','cancelled')",
            name="state_allowed",
        ),
        UniqueConstraint("workspace_id", "draft_id", "candidate_key"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "draft_id"],
            ["drafts.workspace_id", "drafts.id"],
            name="fk_candidates_draft",
            ondelete="CASCADE",
        ),
        Index("ix_candidates_draft", "workspace_id", "draft_id", "state"),
    )


class Clarification(Base):
    """Уточняющий вопрос, связанный с полем конкретного кандидата (R07)."""

    __tablename__ = "clarifications"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    options: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    asked_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()
    answered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('open','answered','cancelled','expired')", name="state_allowed"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_clarifications_open", "workspace_id", "draft_id", "state"),
    )


class ParseAttempt(Base):
    """Попытка разбора провайдером (ADR-17): фиксируется весь профиль."""

    __tablename__ = "parse_attempts"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    base_draft_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(60), nullable=False)
    returned_model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False)
    service_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    result_status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_kind: Mapped[str | None] = mapped_column(String(60), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "result_status IN ('success','invalid_schema','refused','timeout','error',"
            "'cancelled','stale')",
            name="result_status_allowed",
        ),
        Index("ix_parse_attempts_draft", "workspace_id", "draft_id", "created_at"),
    )


class CommandResult(Base):
    """Идемпотентность команд (DATA_CONTRACT §2.6)."""

    __tablename__ = "command_results"

    id: Mapped[uuid.UUID] = pk_uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    command: Mapped[str] = mapped_column(String(60), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    canonical_body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    entity_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workspace_id",
            "command",
            "idempotency_key",
            postgresql_nulls_not_distinct=True,
        ),
    )


class Job(Base):
    """Долговечная фоновая задача с арендой и fencing (ADR-05)."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = pk_uuid()
    queue_class: Mapped[str] = mapped_column(String(24), nullable=False)
    job_type: Mapped[str] = mapped_column(String(60), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Логический ключ запуска: повтор одной логической задачи не создаёт вторую.
    logical_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    available_at: Mapped[dt.datetime] = now_server()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','running','retry_wait','succeeded','failed','cancelled')",
            name="state_allowed",
        ),
        CheckConstraint(
            "queue_class IN ('interactive','calendar','review','integration','maintenance')",
            name="queue_class_allowed",
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        Index(
            "ix_jobs_ready",
            "queue_class",
            "available_at",
            "id",
            postgresql_where=text("state IN ('queued','retry_wait')"),
        ),
        Index(
            "ix_jobs_running_lease",
            "lease_until",
            postgresql_where=text("state = 'running'"),
        ),
        Index("ix_jobs_workspace", "workspace_id", "state"),
    )


class OutboxEvent(Base):
    """Доменное событие, сохранённое в транзакции предметного изменения (TECH-05)."""

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    aggregate_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    audience: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'members'")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("workspace_id", "event_seq"),
        CheckConstraint(
            "audience IN ('members','author','admin','terminal','none')", name="audience_allowed"
        ),
        Index("ix_outbox_events_ws", "workspace_id", "event_seq"),
    )


class ConsumerReceipt(Base):
    """Отметка обработки события конкретным потребителем (ADR-05)."""

    __tablename__ = "consumer_receipts"

    id: Mapped[uuid.UUID] = pk_uuid()
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outbox_events.id", ondelete="CASCADE"), nullable=False
    )
    consumer_name: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'done'"))
    processed_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (UniqueConstraint("event_id", "consumer_name"),)


class NotificationDelivery(Base):
    """Персональная доставка (FR-86). Ключ включает поколение членства."""

    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = pk_uuid()
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_generation: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'telegram'")
    )
    delivery_class: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'pending'"))
    available_at: Mapped[dt.datetime] = now_server()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("event_id", "recipient_user_id", "membership_generation", "channel"),
        CheckConstraint(
            "state IN ('pending','sending','sent','failed','cancelled','suppressed','unknown')",
            name="state_allowed",
        ),
        CheckConstraint(
            "delivery_class IN ('author_card','shared_change','threshold','review',"
            "'reminder','terminal','operational')",
            name="delivery_class_allowed",
        ),
        Index(
            "ix_notification_deliveries_ready",
            "state",
            "available_at",
            "id",
            postgresql_where=text("state IN ('pending','failed')"),
        ),
        Index("ix_notification_deliveries_recipient", "recipient_user_id", "created_at"),
    )


class ThresholdEvent(Base):
    """Пороговое событие статьи в периоде (FR-52)."""

    __tablename__ = "threshold_events"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stable_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    threshold_type: Mapped[str] = mapped_column(String(24), nullable=False)
    triggered_at: Mapped[dt.datetime] = now_server()
    fact_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("workspace_id", "period_id", "stable_line_id", "threshold_type"),
        CheckConstraint(
            "threshold_type IN ('approach_80','approach_90','exhausted_100','overspent',"
            "'forecast_risk')",
            name="threshold_type_allowed",
        ),
    )


class RecipientDayQuota(Base):
    """Дневной предел проактивных сообщений по всем бюджетам (FR-53, LIM-06)."""

    __tablename__ = "recipient_day_quotas"

    id: Mapped[uuid.UUID] = pk_uuid()
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    local_day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    used_slots: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    limit_slots: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("recipient_user_id", "local_day"),
        CheckConstraint("used_slots >= 0", name="used_non_negative"),
    )


class AICostReservation(Base):
    """Атомарное резервирование стоимости запроса (ADR-12, H12)."""

    __tablename__ = "ai_cost_reservations"

    id: Mapped[uuid.UUID] = pk_uuid()
    service_scope: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'global'")
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    quota_month: Mapped[str] = mapped_column(String(7), nullable=False)
    request_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    actual_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'reserved'")
    )
    created_at: Mapped[dt.datetime] = now_server()
    settled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("reserved_amount >= 0", name="reserved_non_negative"),
        CheckConstraint(
            "state IN ('reserved','settled','released','unknown')", name="state_allowed"
        ),
        Index("ix_ai_cost_reservations_month", "quota_month", "state"),
        Index("ix_ai_cost_reservations_ws", "workspace_id", "quota_month"),
    )


class AIQuotaCounter(Base):
    """Агрегат месячной квоты; блокируется при резервировании (ADR-12)."""

    __tablename__ = "ai_quota_counters"

    id: Mapped[uuid.UUID] = pk_uuid()
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    quota_month: Mapped[str] = mapped_column(String(7), nullable=False)
    reserved_total: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, server_default=text("0")
    )
    settled_total: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, server_default=text("0")
    )
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    in_flight: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint(
            "scope",
            "workspace_id",
            "quota_month",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("scope IN ('global','workspace')", name="scope_allowed"),
        CheckConstraint("in_flight >= 0", name="in_flight_non_negative"),
    )


class Attachment(Base):
    """Приватное вложение (ADR-11). Публичного URL не существует."""

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'owner'")
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'staging'"))
    telegram_file_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    delete_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("visibility IN ('owner','workspace')", name="visibility_allowed"),
        CheckConstraint("state IN ('staging','ready','deleting','deleted')", name="state_allowed"),
        CheckConstraint(
            "kind IN ('photo','document','voice','audio','export')", name="kind_allowed"
        ),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        Index(
            "ix_attachments_retention",
            "delete_after",
            "id",
            postgresql_where=text("state IN ('ready','deleting')"),
        ),
        Index("ix_attachments_owner", "owner_user_id", "state"),
    )


class ExportFile(Base):
    __tablename__ = "export_files"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fmt: Mapped[str] = mapped_column(String(16), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    data_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    delete_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("fmt IN ('xlsx','csv')", name="format_allowed"),
        CheckConstraint("state IN ('pending','ready','failed','deleted')", name="state_allowed"),
        Index("ix_export_files_retention", "delete_after", "id"),
    )
