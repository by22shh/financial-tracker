"""Голос и чеки: от вложения до карточки операции (FR-13–FR-18).

Голос сначала проходит ASR, затем текст обрабатывается тем же профилем Luna
Medium. Изображение разбирается визуально; суммы сводятся сервером.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.conversation import sections
from fintracker.application.conversation.entry import (
    CandidateFields,
    DraftAlreadyExists,
    ExtractionResult,
    create_draft_with_candidates,
    find_message_draft,
)
from fintracker.application.conversation.keyboards import Button, callback, confirm_candidate
from fintracker.application.conversation.types import Attachment as MediaAttachment
from fintracker.application.conversation.types import IncomingMessage, MessageKind, Reply
from fintracker.application.intelligence.extraction import (
    WorkspaceCatalog,
    extract_receipt,
    extract_with_model,
    load_catalog,
)
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import ProviderUnavailable, ValidationFailed, VersionConflict
from fintracker.core.logging import get_logger
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.platform import PendingAction
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.domain.ledger.receipt import (
    ReceiptLineInput,
    check_receipt,
    parse_decimal,
    reconcile_to_total,
)
from fintracker.domain.media.image import InvalidImage, inspect_image
from fintracker.domain.parsing.dates import resolve_date_expression
from fintracker.domain.parsing.intent import Intent
from fintracker.infra.asr.provider import build_asr
from fintracker.infra.telegram.files import download_attachment

logger = get_logger("intelligence.media")


@dataclass(frozen=True)
class _MediaPreparation:
    draft_id: uuid.UUID
    version: int
    catalog: WorkspaceCatalog


async def _prepare_media(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    message: IncomingMessage,
    fingerprint: str | None = None,
) -> _MediaPreparation | list[Reply]:
    workspace_id = actor.require_workspace()
    media = {
        "kind": "voice" if message.kind is MessageKind.VOICE else "photo",
        "file_ids": [item.file_id for item in message.attachments],
        "mime_types": [item.mime_type for item in message.attachments],
        "caption": message.text,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
    }
    existing_id: uuid.UUID | None = None
    existing_state: str | None = None
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        await UnitOfWork(session, actor.correlation_id).lock_workspace(workspace_id, actor=actor)
        draft = None
        if message.source_key is not None:
            draft = await find_message_draft(
                session,
                workspace_id=workspace_id,
                owner_user_id=actor.user_id,
                source_message_key=message.source_key,
            )
        if draft is None:
            try:
                draft, _ = await create_draft_with_candidates(
                    session,
                    settings=settings,
                    actor=actor,
                    source_kind="voice" if message.kind is MessageKind.VOICE else "photo",
                    raw_text=message.text,
                    extraction=ExtractionResult(intent=Intent.UNKNOWN, candidates=[]),
                    source_fingerprint=fingerprint,
                    source_message_key=message.source_key,
                    logical_message_id=message.inbound_event_id,
                )
                draft.state = "processing"
                draft.source_media = media
            except DraftAlreadyExists as exc:
                existing_id, existing_state = exc.draft_id, exc.state
        if draft is not None:
            if draft.state not in {"processing", "failed_retryable", "received"}:
                existing_id, existing_state = draft.id, draft.state
            else:
                draft.state = "processing"
                draft.source_media = {**dict(draft.source_media or {}), **media}
                draft.version += 1
                pending = (
                    await session.execute(
                        select(PendingAction)
                        .where(PendingAction.user_id == actor.user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    pending is not None
                    and pending.workspace_id == workspace_id
                    and pending.kind == "occurrence_settle"
                    and not pending.payload.get("draft_id")
                ):
                    pending.payload = {**dict(pending.payload), "draft_id": str(draft.id)}
                catalog = await load_catalog(
                    session,
                    workspace_id=workspace_id,
                    currency=workspace.currency,
                    timezone=workspace.timezone,
                )
                return _MediaPreparation(draft.id, draft.version, catalog)
    assert existing_id is not None
    if existing_state == "posted":
        return await sections.posted_draft_reply(
            settings, actor=actor, workspace=workspace, draft_id=existing_id
        )
    return await sections.draft_reply(
        settings, actor=actor, workspace=workspace, draft_id=existing_id
    )


async def process_media_draft(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply]:
    """Сохранить материал и попытаться разобрать его."""
    if message.kind is MessageKind.VOICE:
        return await _process_voice(settings, actor=actor, workspace=workspace, message=message)
    return await _process_image(settings, actor=actor, workspace=workspace, message=message)


async def _process_voice(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply]:
    """Голос: ASR → текст → тот же профиль Luna (ADR-17, FR-13)."""
    workspace_id = actor.require_workspace()
    attachment = message.attachments[0] if message.attachments else None
    if attachment is None:
        return [Reply(text="Не удалось получить голосовое сообщение.")]

    local_date = message.received_at.astimezone(ZoneInfo(workspace.timezone)).date()
    prepared = await _prepare_media(settings, actor=actor, workspace=workspace, message=message)
    if isinstance(prepared, list):
        return prepared
    draft_id, draft_version, catalog = prepared.draft_id, prepared.version, prepared.catalog

    try:
        audio = await download_attachment(settings, file_id=attachment.file_id)
    except ProviderUnavailable as exc:
        return [Reply(text=f"Не удалось загрузить запись: {exc.message}")]

    asr = build_asr(settings.asr)
    try:
        transcript = await asr.transcribe(
            audio=audio,
            mime_type=attachment.mime_type or "audio/ogg",
            duration_seconds=float(attachment.duration_seconds or 0),
        )
    except (ProviderUnavailable, ValidationFailed) as exc:
        await _mark_draft(
            settings, actor, draft_id, draft_version, "failed_retryable", str(exc.message)
        )
        return [
            Reply(
                text=(
                    "Не удалось распознать речь. Черновик сохранён — повторите запись "
                    "или введите сумму текстом."
                ),
                buttons=(
                    (
                        Button("Повторить разбор", callback("dr", "retry", draft_id.hex[:16])),
                        Button("Ручной ввод", callback("menu", "add")),
                    ),
                ),
            )
        ]

    if not transcript.speech_detected or not transcript.text.strip():
        # Ни отсутствие речи, ни недоступность ASR не считаются нулевым расходом.
        await _mark_draft(
            settings, actor, draft_id, draft_version, "needs_clarification", "no_speech"
        )
        return [
            Reply(
                text=(
                    "В записи не распознана речь. Черновик сохранён — повторите "
                    "или введите сумму текстом."
                ),
                buttons=((Button("Ручной ввод", callback("menu", "add")),),),
            )
        ]

    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        from sqlalchemy import select

        from fintracker.db.models.platform import Draft

        await UnitOfWork(session, actor.correlation_id).lock_workspace(workspace_id, actor=actor)
        row = (
            await session.execute(
                select(Draft).where(Draft.workspace_id == workspace_id, Draft.id == draft_id)
            )
        ).scalar_one()
        if row.version != draft_version or row.state != "processing":
            raise VersionConflict("Разбор сообщения уже обновлён")
        # Расшифровка доступна из карточки для исправления (FR-13).
        row.transcript = transcript.text
        row.version += 1
        draft_version = row.version

    extraction = await extract_with_model(
        settings,
        actor=actor,
        draft_id=draft_id,
        draft_version=draft_version,
        text=transcript.text,
        catalog=catalog,
        reference_date=local_date,
    )
    return await _finalize_extraction(
        settings,
        actor=actor,
        workspace=workspace,
        draft_id=draft_id,
        expected_version=draft_version,
        extraction=extraction,
        source_note=f"Распознано: «{transcript.text}»",
        voice_amount_check=True,
    )


async def _process_image(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply]:
    """Чек или платёжный документ (FR-14–FR-17)."""
    attachment = message.attachments[0] if message.attachments else None
    if attachment is None:
        return [Reply(text="Не удалось получить изображение.")]

    local_date = message.received_at.astimezone(ZoneInfo(workspace.timezone)).date()
    try:
        image = await download_attachment(settings, file_id=attachment.file_id)
    except ProviderUnavailable as exc:
        return [Reply(text=f"Не удалось загрузить изображение: {exc.message}")]

    # Проверяются настоящие байты, а не заявленные Telegram тип и размеры:
    # текст под видом JPEG не должен дойти до платной модели (SEC-07, G-26).
    try:
        facts = inspect_image(
            image,
            max_bytes=settings.limits.max_attachment_bytes,
            max_pixels=settings.limits.max_image_pixels,
            max_side=settings.limits.max_image_side,
            declared_media_type=attachment.mime_type,
        )
    except InvalidImage as exc:
        logger.info("image_rejected", reason=str(exc), file_id=attachment.file_id)
        return [
            Reply(
                text=(
                    f"Файл не удалось прочитать как изображение: {exc}. "
                    "Пришлите фото чека ещё раз или введите сумму текстом."
                )
            )
        ]

    if not facts.declared_type_matches:
        # Провайдеру уходит определённый по содержимому тип, а не заявленный.
        logger.info(
            "image_type_mismatch",
            declared=attachment.mime_type,
            detected=facts.media_type,
        )
    fingerprint = hashlib.sha256(image).hexdigest()
    data_url = f"data:{facts.media_type};base64,{base64.b64encode(image).decode()}"

    # Альбом из нескольких снимков одного чека разбирается одним пакетом (A28).
    extra_urls: list[str] = []
    for item in message.attachments[1:]:
        try:
            extra = await download_attachment(settings, file_id=item.file_id)
        except ProviderUnavailable:
            continue
        try:
            extra_facts = inspect_image(
                extra,
                max_bytes=settings.limits.max_attachment_bytes,
                max_pixels=settings.limits.max_image_pixels,
                max_side=settings.limits.max_image_side,
                declared_media_type=item.mime_type,
            )
        except InvalidImage as exc:
            logger.info("image_rejected", reason=str(exc), file_id=item.file_id)
            continue
        fingerprint = hashlib.sha256(
            (fingerprint + hashlib.sha256(extra).hexdigest()).encode()
        ).hexdigest()
        extra_urls.append(
            f"data:{extra_facts.media_type};base64,{base64.b64encode(extra).decode()}"
        )

    prepared = await _prepare_media(
        settings, actor=actor, workspace=workspace, message=message, fingerprint=fingerprint
    )
    if isinstance(prepared, list):
        return prepared
    draft_id, draft_version, catalog = prepared.draft_id, prepared.version, prepared.catalog

    try:
        receipt = await extract_receipt(
            settings,
            actor=actor,
            draft_id=draft_id,
            draft_version=draft_version,
            image_data_url=data_url,
            caption=message.text,
            catalog=catalog,
            reference_date=local_date,
            source_fingerprint=fingerprint,
            extra_image_urls=tuple(extra_urls),
        )
    except (ProviderUnavailable, ValidationFailed) as exc:
        await _mark_draft(settings, actor, draft_id, draft_version, "failed_retryable", exc.message)
        return [
            Reply(
                text=(
                    "Не удалось разобрать изображение. Черновик сохранён — повторите "
                    "или введите сумму текстом."
                ),
                buttons=(
                    (
                        Button("Повторить разбор", callback("dr", "retry", draft_id.hex[:16])),
                        Button("Ручной ввод", callback("menu", "add")),
                    ),
                ),
            )
        ]

    if receipt.document_kind in {"invoice", "cart", "order_confirmation"} or (
        not receipt.payment_confirmed and receipt.document_kind != "receipt"
    ):
        # Счёт, корзина и подтверждение заказа не подтверждают расход (FR-17).
        await _mark_draft(
            settings, actor, draft_id, draft_version, "needs_clarification", "not_paid"
        )
        # Разобранный документ сохраняется: «Да, оплачено» использует его, а не
        # повторный платный вызов (FR-17, G-16).
        await _store_media(settings, actor, draft_id, {"receipt": receipt.model_dump(mode="json")})
        kind_label = {
            "invoice": "счёт на оплату",
            "cart": "корзина",
            "order_confirmation": "подтверждение заказа",
            "bank_screenshot": "банковский экран",
            "other": "документ",
            "receipt": "чек",
        }[receipt.document_kind]
        return [
            Reply(
                text=(
                    f"Это похоже на {kind_label}, а не на подтверждённую оплату.\n"
                    "Платёж уже совершён?"
                ),
                buttons=(
                    (
                        Button("Да, оплачено", callback("dr", "paid", draft_id.hex[:16])),
                        Button("Нет", callback("dr", "cancel", draft_id.hex[:16])),
                    ),
                ),
            )
        ]

    return await _receipt_to_draft(
        settings,
        actor=actor,
        workspace=workspace,
        message=message,
        receipt=receipt,
        catalog=catalog,
        draft_id=draft_id,
        draft_version=draft_version,
        local_date=local_date,
    )


async def _receipt_to_draft(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    message: IncomingMessage,
    receipt: Any,
    catalog: Any,
    draft_id: uuid.UUID,
    draft_version: int,
    local_date: Any,
) -> list[Reply]:
    """Превратить разобранный чек в черновик с карточкой (FR-14, G-16)."""
    total_decimal = parse_decimal(receipt.total_decimal)
    if total_decimal is None or total_decimal <= 0:
        await _mark_draft(
            settings, actor, draft_id, draft_version, "needs_clarification", "no_total"
        )
        return [
            Reply(
                text="Не удалось прочитать итог чека. Введите сумму текстом.",
                buttons=((Button("Ручной ввод", callback("menu", "add")),),),
            )
        ]

    currency = receipt.currency or workspace.currency
    if currency != workspace.currency:
        # Неподдерживаемый валютный чек требует фактически списанной суммы (R10).
        await _mark_draft(
            settings, actor, draft_id, draft_version, "needs_clarification", "currency"
        )
        return [
            Reply(
                text=(
                    f"Чек в валюте {currency}, а бюджет ведётся в {workspace.currency}.\n"
                    "Укажите фактически списанную сумму в валюте бюджета — курс "
                    "не подставляется автоматически."
                )
            )
        ]

    total = Money.from_decimal(total_decimal, currency)
    lines = [
        ReceiptLineInput(
            label=line.label,
            amount=Money.from_decimal(Decimal(line.amount_decimal), currency),
            category_id=line.category_id if line.category_id in catalog.category_ids else None,
            quantity=line.quantity,
            unit_price=line.unit_price_decimal,
            readable=line.readable,
        )
        for line in receipt.lines
    ]
    check = check_receipt(
        total=total,
        lines=lines,
        discount=(
            Money.from_decimal(parse_decimal(receipt.discount_decimal) or Decimal(0), currency)
        ),
        tip=Money.from_decimal(parse_decimal(receipt.tip_decimal) or Decimal(0), currency),
        unreadable_lines=receipt.unreadable_lines,
    )

    occurred = local_date
    if receipt.date_expression:
        parsed = resolve_date_expression(receipt.date_expression, reference=local_date)
        if parsed is not None and not parsed.is_future:
            occurred = parsed.value

    fields_list: list[CandidateFields] = []
    if check.can_autopost and check.detailed and len(check.distributed) > 1:
        # Один чек — одна покупка: позиции становятся распределениями, а не
        # самостоятельными операциями (FR-14, G-17).
        distributed = reconcile_to_total(check)
        fields_list.append(
            CandidateFields(
                amount_minor=total.minor,
                currency=currency,
                kind="refund" if receipt.is_refund else "expense",
                occurred_date=occurred,
                description=receipt.merchant or "Чек",
                merchant=receipt.merchant,
                note=message.text,
                parts=[
                    {
                        "amount_minor": line.amount.minor,
                        "category_id": line.category_id,
                        "label": line.label,
                    }
                    for line in distributed
                ],
                evidence={"lines": str(len(distributed)), "total": receipt.total_decimal or ""},
            )
        )
    else:
        fields = CandidateFields(
            amount_minor=total.minor,
            currency=currency,
            kind="refund" if receipt.is_refund else "expense",
            occurred_date=occurred,
            description=receipt.merchant or "Чек",
            merchant=receipt.merchant,
            note=message.text,
            evidence={"total": receipt.total_decimal or ""},
        )
        if check.reason:
            fields.ambiguities.append({"field": "amount", "reason": "receipt_mismatch"})
        fields_list.append(fields)

    extraction = ExtractionResult(
        intent=Intent.RECORD_TRANSACTION,
        candidates=fields_list,
        question=check.reason,
    )
    summary_lines = [f"Чек на {total.format()}"]
    if receipt.merchant:
        summary_lines.append(f"Продавец: {receipt.merchant}")
    parts = list(fields_list[0].parts) if fields_list else []
    if len(parts) > 1:
        summary_lines.append(f"Распределение по {len(parts)} статьям")
        summary_lines.extend(
            f"• {part.get('label') or 'Позиция'}: "
            f"{Money(int(part['amount_minor']), currency).format()}"
            for part in parts
        )
    if check.reason:
        summary_lines.append(check.reason)
    # Чеки в P0 всегда подтверждаются (FR-19).
    return await _finalize_extraction(
        settings,
        actor=actor,
        workspace=workspace,
        draft_id=draft_id,
        expected_version=draft_version,
        extraction=extraction,
        source_note="\n".join(summary_lines),
    )


async def _finalize_extraction(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    expected_version: int,
    extraction: ExtractionResult,
    source_note: str,
    voice_amount_check: bool = False,
) -> list[Reply]:
    """Сохранить кандидатов и показать карточку подтверждения."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        from sqlalchemy import select

        from fintracker.db.models.platform import Candidate, Draft

        await UnitOfWork(session, actor.correlation_id).lock_workspace(workspace_id, actor=actor)
        draft = (
            await session.execute(
                select(Draft).where(Draft.workspace_id == workspace_id, Draft.id == draft_id)
            )
        ).scalar_one()
        if draft.version != expected_version or draft.state != "processing":
            raise VersionConflict("Разбор сообщения уже обновлён")
        # Перед сохранением новых кандидатов убираем непроведённые прежние,
        # чтобы поздний результат не создавал вторую запись (AR-05).
        existing = (
            (
                await session.execute(
                    select(Candidate).where(
                        Candidate.workspace_id == workspace_id,
                        Candidate.draft_id == draft_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing:
            if row.state != "posted":
                await session.delete(row)
        await session.flush()
        for index, fields in enumerate(extraction.candidates, start=1):
            session.add(
                Candidate(
                    workspace_id=workspace_id,
                    draft_id=draft_id,
                    candidate_key=f"c{index}",
                    state="needs_clarification" if fields.ambiguities else "ready",
                    fields=fields.to_payload(),
                    ambiguities=fields.ambiguities,
                )
            )
        draft.state = "needs_clarification" if extraction.question else "ready"
        draft.version += 1
        candidate_fields = list(extraction.candidates)

    summary = sections.draft_summary(candidate_fields, workspace.currency)
    header = extraction.question or "Проверьте запись перед сохранением:"
    text = f"{source_note}\n\n{header}\n{summary}"
    if voice_amount_check:
        text += "\n\nРасшифровку можно исправить перед записью."
    return [Reply(text=text, buttons=confirm_candidate(draft_id))]


async def _mark_draft(
    settings: Settings,
    actor: ActorContext,
    draft_id: uuid.UUID,
    expected_version: int,
    state: str,
    reason: str | None,
) -> None:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        from sqlalchemy import update

        from fintracker.db.models.platform import Draft

        await UnitOfWork(session, actor.correlation_id).lock_workspace(workspace_id, actor=actor)
        result = await session.execute(
            update(Draft)
            .where(
                Draft.workspace_id == workspace_id,
                Draft.id == draft_id,
                Draft.version == expected_version,
                Draft.state == "processing",
            )
            .values(state=state, failure_reason=(reason or "")[:200], version=Draft.version + 1)
            .returning(Draft.id)
        )
        if result.scalar_one_or_none() is None:
            raise VersionConflict("Разбор сообщения уже обновлён")


async def _store_media(
    settings: Settings, actor: ActorContext, draft_id: uuid.UUID, extra: dict[str, Any]
) -> None:
    """Дополнить сохранённый исходный материал черновика (G-16)."""
    from fintracker.db.models.platform import Draft

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        draft = (
            await session.execute(
                select(Draft).where(Draft.workspace_id == workspace_id, Draft.id == draft_id)
            )
        ).scalar_one_or_none()
        if draft is None:
            return
        draft.source_media = {**dict(draft.source_media or {}), **extra}
        draft.version += 1


async def _saved_media(
    settings: Settings, actor: ActorContext, draft_id: uuid.UUID
) -> dict[str, Any]:
    from fintracker.db.models.platform import Draft

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        draft = (
            await session.execute(
                select(Draft).where(Draft.workspace_id == workspace_id, Draft.id == draft_id)
            )
        ).scalar_one_or_none()
        return dict(draft.source_media or {}) if draft is not None else {}


def _message_from_media(media: dict[str, Any], *, workspace_id: uuid.UUID) -> IncomingMessage:
    """Восстановить исходное сообщение из сохранённого материала (G-16)."""
    file_ids = [str(item) for item in (media.get("file_ids") or [])]
    mime_types = list(media.get("mime_types") or [])
    kind = MessageKind.VOICE if media.get("kind") == "voice" else MessageKind.PHOTO
    attachments = tuple(
        MediaAttachment(
            file_id=file_id,
            kind="voice" if kind is MessageKind.VOICE else "photo",
            mime_type=mime_types[index] if index < len(mime_types) else None,
        )
        for index, file_id in enumerate(file_ids)
    )
    return IncomingMessage(
        telegram_user_id=0,
        chat_id=int(media.get("chat_id") or 0),
        kind=kind,
        text=media.get("caption"),
        message_id=media.get("message_id"),
        attachments=attachments,
        workspace_id=workspace_id,
    )


async def retry_media_draft(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, draft_id: uuid.UUID
) -> list[Reply]:
    """Повторить разбор сохранённого материала того же черновика (FR-20, G-16)."""
    media = await _saved_media(settings, actor, draft_id)
    if not media.get("file_ids"):
        return [
            Reply(
                text=(
                    "Исходный файл больше не сохранён: пришлите его ещё раз "
                    "или введите сумму текстом."
                )
            )
        ]
    message = _message_from_media(media, workspace_id=actor.require_workspace())
    return await process_media_draft(settings, actor=actor, workspace=workspace, message=message)


async def confirm_invoice_paid(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, draft_id: uuid.UUID
) -> list[Reply]:
    """Подтвердить, что документ уже оплачен (FR-17, G-16).

    Повторного платного вызова не происходит: используется уже разобранный
    документ, а решение о записи остаётся за участником.
    """
    from fintracker.application.conversation.entry import load_draft
    from fintracker.infra.ai.schemas import ReceiptResponse

    media = await _saved_media(settings, actor, draft_id)
    raw = media.get("receipt")
    if not raw:
        return [
            Reply(
                text=(
                    "Разобранный документ не сохранён: пришлите чек ещё раз "
                    "или введите сумму текстом."
                )
            )
        ]
    receipt = ReceiptResponse.model_validate(raw)
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        draft, _ = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        if draft.state not in {"needs_clarification", "failed_retryable", "processing"}:
            return await sections.draft_reply(
                settings, actor=actor, workspace=workspace, draft_id=draft_id
            )
        # Подтверждение оплаты возвращает черновик в разбор с той же версией.
        draft.state = "processing"
        draft.version += 1
        draft_version = draft.version
        local_date = draft.created_at.astimezone(ZoneInfo(workspace.timezone)).date()
        catalog = await load_catalog(
            session,
            workspace_id=workspace_id,
            currency=workspace.currency,
            timezone=workspace.timezone,
        )
    message = _message_from_media(media, workspace_id=workspace_id)
    return await _receipt_to_draft(
        settings,
        actor=actor,
        workspace=workspace,
        message=message,
        receipt=receipt,
        catalog=catalog,
        draft_id=draft_id,
        draft_version=draft_version,
        local_date=local_date,
    )
