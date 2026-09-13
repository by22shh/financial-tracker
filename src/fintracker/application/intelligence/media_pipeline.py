"""Голос и чеки: от вложения до карточки операции (FR-13–FR-18).

Голос сначала проходит ASR, затем текст обрабатывается тем же профилем Luna
Medium. Изображение разбирается визуально; суммы сводятся сервером.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from fintracker.application.conversation import sections
from fintracker.application.conversation.entry import (
    CandidateFields,
    ExtractionResult,
    create_draft_with_candidates,
)
from fintracker.application.conversation.keyboards import Button, callback, confirm_candidate
from fintracker.application.conversation.types import IncomingMessage, MessageKind, Reply
from fintracker.application.intelligence.extraction import (
    extract_receipt,
    extract_with_model,
    load_catalog,
)
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import ProviderUnavailable, ValidationFailed
from fintracker.core.logging import get_logger
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.ledger.receipt import (
    ReceiptLineInput,
    check_receipt,
    parse_decimal,
    reconcile_to_total,
)
from fintracker.domain.parsing.dates import resolve_date_expression
from fintracker.domain.parsing.intent import Intent
from fintracker.infra.asr.provider import build_asr
from fintracker.infra.telegram.files import download_attachment

logger = get_logger("intelligence.media")


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
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        catalog = await load_catalog(
            session,
            workspace_id=workspace_id,
            currency=workspace.currency,
            timezone=workspace.timezone,
        )
        draft, _ = await create_draft_with_candidates(
            session,
            settings=settings,
            actor=actor,
            source_kind="voice",
            raw_text=None,
            extraction=ExtractionResult(intent=Intent.UNKNOWN, candidates=[]),
        )
        draft.state = "processing"
        draft_id = draft.id
        draft_version = draft.version

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
        await _mark_draft(settings, workspace_id, draft_id, "failed_retryable", str(exc.message))
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
        await _mark_draft(settings, workspace_id, draft_id, "needs_clarification", "no_speech")
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

        row = (
            await session.execute(
                select(Draft).where(Draft.workspace_id == workspace_id, Draft.id == draft_id)
            )
        ).scalar_one()
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
        extraction=extraction,
        source_note=f"Распознано: «{transcript.text}»",
        voice_amount_check=True,
    )


async def _process_image(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply]:
    """Чек или платёжный документ (FR-14–FR-17)."""
    workspace_id = actor.require_workspace()
    attachment = message.attachments[0] if message.attachments else None
    if attachment is None:
        return [Reply(text="Не удалось получить изображение.")]

    local_date = message.received_at.astimezone(ZoneInfo(workspace.timezone)).date()
    try:
        image = await download_attachment(settings, file_id=attachment.file_id)
    except ProviderUnavailable as exc:
        return [Reply(text=f"Не удалось загрузить изображение: {exc.message}")]

    fingerprint = hashlib.sha256(image).hexdigest()
    data_url = (
        f"data:{attachment.mime_type or 'image/jpeg'};base64,{base64.b64encode(image).decode()}"
    )

    # Альбом из нескольких снимков одного чека разбирается одним пакетом (A28).
    extra_urls: list[str] = []
    for item in message.attachments[1:]:
        try:
            extra = await download_attachment(settings, file_id=item.file_id)
        except ProviderUnavailable:
            continue
        fingerprint = hashlib.sha256(
            (fingerprint + hashlib.sha256(extra).hexdigest()).encode()
        ).hexdigest()
        extra_urls.append(
            f"data:{item.mime_type or 'image/jpeg'};base64,{base64.b64encode(extra).decode()}"
        )

    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        catalog = await load_catalog(
            session,
            workspace_id=workspace_id,
            currency=workspace.currency,
            timezone=workspace.timezone,
        )
        draft, _ = await create_draft_with_candidates(
            session,
            settings=settings,
            actor=actor,
            source_kind="photo",
            raw_text=message.text,
            extraction=ExtractionResult(intent=Intent.UNKNOWN, candidates=[]),
            source_fingerprint=fingerprint,
        )
        draft.state = "processing"
        draft_id = draft.id
        draft_version = draft.version

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
        await _mark_draft(settings, workspace_id, draft_id, "failed_retryable", exc.message)
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
        await _mark_draft(settings, workspace_id, draft_id, "needs_clarification", "not_paid")
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

    total_decimal = parse_decimal(receipt.total_decimal)
    if total_decimal is None or total_decimal <= 0:
        await _mark_draft(settings, workspace_id, draft_id, "needs_clarification", "no_total")
        return [
            Reply(
                text="Не удалось прочитать итог чека. Введите сумму текстом.",
                buttons=((Button("Ручной ввод", callback("menu", "add")),),),
            )
        ]

    currency = receipt.currency or workspace.currency
    if currency != workspace.currency:
        # Неподдерживаемый валютный чек требует фактически списанной суммы (R10).
        await _mark_draft(settings, workspace_id, draft_id, "needs_clarification", "currency")
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
        distributed = reconcile_to_total(check)
        for index, line in enumerate(distributed, start=1):
            fields_list.append(
                CandidateFields(
                    amount_minor=line.amount.minor,
                    currency=currency,
                    kind="refund" if receipt.is_refund else "expense",
                    occurred_date=occurred,
                    description=line.label,
                    merchant=receipt.merchant,
                    category_id=uuid.UUID(line.category_id) if line.category_id else None,
                    note=message.text,
                    evidence={"line": line.label, "index": str(index)},
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
    if len(fields_list) > 1:
        summary_lines.append(f"Распределение по {len(fields_list)} статьям")
    if check.reason:
        summary_lines.append(check.reason)
    # Чеки в P0 всегда подтверждаются (FR-19).
    return await _finalize_extraction(
        settings,
        actor=actor,
        workspace=workspace,
        draft_id=draft_id,
        extraction=extraction,
        source_note="\n".join(summary_lines),
    )


async def _finalize_extraction(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
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

        draft = (
            await session.execute(
                select(Draft).where(Draft.workspace_id == workspace_id, Draft.id == draft_id)
            )
        ).scalar_one()
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
    workspace_id: uuid.UUID,
    draft_id: uuid.UUID,
    state: str,
    reason: str | None,
) -> None:
    async with session_scope(settings, RuntimeRole.API, workspace_id=workspace_id) as session:
        from sqlalchemy import update

        from fintracker.db.models.platform import Draft

        await session.execute(
            update(Draft)
            .where(Draft.workspace_id == workspace_id, Draft.id == draft_id)
            .values(state=state, failure_reason=(reason or "")[:200], version=Draft.version + 1)
        )
