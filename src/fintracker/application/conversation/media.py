"""Приём голоса и изображений (FR-13–FR-18, LIM-03, LIM-09).

Ограничения размера, типа и разрешения проверяются до дорогой обработки и
до обращения к платному сервису (SEC-07, NFR-11).
Голос сначала проходит отдельный ASR, затем текст обрабатывается тем же
профилем Luna Medium. Пока конкретная ASR модель не выбрана (BL-02),
материал сохраняется черновиком, а пользователю предлагается ввод текстом.
"""

from __future__ import annotations

import uuid

from fintracker.application.conversation.context import (
    active_context,
    load_actor,
    no_budget_reply,
)
from fintracker.application.conversation.keyboards import Button, callback
from fintracker.application.conversation.types import IncomingMessage, MessageKind, Reply
from fintracker.config import Settings
from fintracker.core.logging import get_logger

logger = get_logger("conversation.media")

SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


async def handle_media(
    settings: Settings, message: IncomingMessage, *, user_id: uuid.UUID
) -> list[Reply]:
    """Проверить ограничения до дорогой обработки и создать черновик."""
    workspace_id = await active_context(settings, user_id=user_id, message=message)
    if workspace_id is None:
        return no_budget_reply()

    limits = settings.limits
    for attachment in message.attachments:
        if attachment.size_bytes and attachment.size_bytes > limits.max_attachment_bytes:
            # Безопасный отказ до декодирования сверх лимита (A33).
            return [
                Reply(
                    text=(
                        "Файл больше допустимых 15 MB. Пришлите изображение меньшего "
                        "размера или введите сумму текстом."
                    )
                )
            ]
        if attachment.width and attachment.height:
            if attachment.width * attachment.height > limits.max_image_pixels:
                return [Reply(text="Изображение слишком большое для безопасной обработки.")]
            if max(attachment.width, attachment.height) > limits.max_image_side:
                return [Reply(text="Слишком большая сторона изображения.")]
        if (
            attachment.mime_type
            and attachment.kind == "document"
            and attachment.mime_type not in SUPPORTED_IMAGE_TYPES
        ):
            return [
                Reply(
                    text=(
                        f"Формат {attachment.mime_type} пока не поддерживается. "
                        "Поддерживаются JPEG, PNG и WebP. Исходное сообщение сохранено."
                    )
                )
            ]
        if (
            message.kind is MessageKind.VOICE
            and attachment.duration_seconds
            and attachment.duration_seconds > settings.asr.max_audio_seconds
        ):
            # Отказ до отправки в платный сервис (A21).
            minutes = settings.asr.max_audio_seconds // 60
            return [
                Reply(
                    text=(
                        f"Запись длиннее {minutes} минут не обрабатывается. "
                        "Запишите короче или введите сумму текстом."
                    )
                )
            ]

    actor, workspace = await load_actor(
        settings,
        user_id=user_id,
        workspace_id=workspace_id,
        correlation_id=message.correlation_id,
    )

    if message.kind is MessageKind.VOICE and not settings.asr.available:
        # Недоступность ASR не считается нулевым расходом (FR-13).
        return [
            Reply(
                text=(
                    "Распознавание речи пока не подключено: не выбрана модель "
                    "транскрипции.\nГолос сохранён — введите сумму текстом или "
                    "используйте /add."
                ),
                buttons=((Button("Ручной ввод", callback("menu", "add")),),),
            )
        ]

    if not settings.ai.enabled:
        kind_label = "Фото чека" if message.kind is MessageKind.PHOTO else "Файл"
        return [
            Reply(
                text=(
                    f"{kind_label} сохранён как черновик: разбор изображений требует "
                    "подключённого ключа AI.\nВведите сумму текстом или через /add — "
                    "учёт продолжает работать."
                ),
                buttons=((Button("Ручной ввод", callback("menu", "add")),),),
            )
        ]

    from fintracker.application.intelligence.media_pipeline import process_media_draft

    return await process_media_draft(settings, actor=actor, workspace=workspace, message=message)
