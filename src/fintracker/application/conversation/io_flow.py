"""Импорт и экспорт из чата (FR-63–FR-67, CMD-28, CMD-29).

Полный пользовательский путь: меню → загрузка таблицы → предпросмотр →
подтверждение → применение; выгрузка выдаётся файлом получателю с повторной
проверкой членства в момент выдачи (SEC-05).
"""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import IncomingMessage, Reply
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import DomainError, NotFound, ValidationFailed
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Workspace
from fintracker.db.models.integrations import ImportBatch
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

logger = get_logger("conversation.io")

SUPPORTED_TABLE_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "text/csv",
    }
)


async def export_menu(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Экспорт доступен независимо от AI (FR-67, A103)."""
    return [
        Reply(
            text=(
                "Импорт и экспорт\n"
                "• Экспорт XLSX содержит листы «Операции», «Распределения», "
                "«Категории», «Бюджеты», «Цели» и «Описание полей».\n"
                "• Экспорт CSV — нормализованный журнал в UTF-8 с явными датами.\n"
                "• Импорт разбирает снимок таблицы и показывает предпросмотр до "
                "применения: в рабочие итоги ничего не попадает без подтверждения."
            ),
            buttons=(
                (
                    Button("Экспорт XLSX", callback("exp", "xlsx")),
                    Button("Экспорт CSV", callback("exp", "csv")),
                ),
                (
                    Button("Импорт таблицы", callback("imp", "start")),
                    Button("← Меню", callback("menu", "main")),
                ),
            ),
        )
    ]


async def export_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    fmt: str,
    chat_id: int | None,
) -> list[Reply]:
    """Собрать снимок и выдать файл участнику (FR-67, CMD-29, NFR-12)."""
    from fintracker.application.integrations.exporter import build_snapshot, to_csv, to_xlsx
    from fintracker.infra.telegram.sender import build_sender

    if fmt not in {"xlsx", "csv"}:
        raise ValidationFailed("Поддерживаются форматы xlsx и csv")
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        # Доступ подтверждается повторно в момент выдачи файла (SEC-05).
        await uow.check_actor(actor)
        snapshot = await build_snapshot(session, workspace=workspace)

    content = to_xlsx(snapshot) if fmt == "xlsx" else to_csv(snapshot)
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    filename = f"fintracker-{today.isoformat()}.{fmt}"

    if chat_id is None:
        return [Reply(text="Не удалось определить чат для выдачи файла.")]
    sender = build_sender(settings)
    result = await sender.send_document(
        chat_id=chat_id,
        filename=filename,
        content=content,
        caption=f"Выгрузка «{workspace.name}» на {today.isoformat()}",
    )
    if not result.ok:
        return [
            Reply(
                text=(
                    "Не удалось отправить файл выгрузки. Повторите позже — "
                    "данные бюджета не изменились."
                ),
                buttons=((Button("← Импорт и экспорт", callback("menu", "io")),),),
            )
        ]
    return [
        Reply(
            text=(
                f"Файл {filename} отправлен. Он содержит {len(snapshot.rows)} операций "
                "и версию данных для сверки."
            ),
            buttons=((Button("← Импорт и экспорт", callback("menu", "io")),),),
        )
    ]


async def import_start(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Объяснить путь импорта и запросить файл (CMD-28)."""
    return [
        Reply(
            text=(
                "Пришлите файл таблицы (XLSX) сообщением в этот чат.\n"
                "Я разберу снимок и покажу предпросмотр: сколько строк, какие "
                "периоды и категории. В рабочие итоги ничего не попадёт до "
                "вашего подтверждения."
            ),
            buttons=((Button("← Импорт и экспорт", callback("menu", "io")),),),
        )
    ]


async def handle_table_document(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    message: IncomingMessage,
) -> list[Reply]:
    """Разобрать присланную таблицу и показать предпросмотр (FR-63, CMD-28)."""
    import tempfile
    from pathlib import Path

    from fintracker.application.integrations.importer import build_preview
    from fintracker.application.integrations.sheet_parser import parse_workbook
    from fintracker.infra.telegram.files import download_attachment

    attachment = message.attachments[0] if message.attachments else None
    if attachment is None:
        return [Reply(text="Не удалось получить файл.")]
    if attachment.size_bytes and attachment.size_bytes > settings.limits.max_attachment_bytes:
        return [Reply(text="Файл больше допустимого размера. Разделите таблицу на части.")]

    try:
        content = await download_attachment(settings, file_id=attachment.file_id)
    except DomainError as exc:
        return [Reply(text=f"Не удалось загрузить файл: {exc.message}")]

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / (attachment.file_id + ".xlsx")
        path.write_bytes(content)
        try:
            workbook = parse_workbook(path, year=today.year, taken_at=dt.datetime.now(dt.UTC))
        except Exception as exc:  # разбор чужого файла не должен ронять обработчик
            logger.info("import_parse_failed", error=type(exc).__name__)
            return [
                Reply(
                    text=(
                        "Не удалось разобрать таблицу. Проверьте, что это снимок "
                        "бюджета в формате XLSX, и пришлите файл снова."
                    )
                )
            ]

        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
            await uow.lock_workspace(workspace_id, actor=actor)
            preview = await build_preview(
                session, actor=actor, workbook=workbook, currency=workspace.currency
            )
            batch_id = preview.batch_id
            rows = len(preview.rows)
            blocked = preview.blocked
            blocked_reason = preview.blocked_reason
            new_categories = preview.new_categories
            difference = preview.reconciliation.difference_minor

    lines = [
        f"Предпросмотр импорта: строк {rows}.",
        f"Расхождение сверки: {difference} минимальных единиц.",
    ]
    if new_categories:
        lines.append("Новые статьи: " + ", ".join(new_categories[:8]))
    if blocked:
        lines.append(f"Применение недоступно: {blocked_reason}")
        return [Reply(text="\n".join(lines))]
    lines.append("Подтвердите применение — до этого рабочие итоги не меняются.")
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("Применить импорт", callback("imp", "commit", short(batch_id))),
                    Button("Отменить", callback("imp", "cancel", short(batch_id))),
                ),
            ),
        )
    ]


async def import_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    """Кнопки импорта: подтверждение и отмена пакета (CMD-28)."""
    from fintracker.application.integrations.importer import commit_import

    if action == "start":
        return await import_start(settings, actor=actor, workspace=workspace)
    if not rest:
        return [Reply(text="Кнопка устарела. Откройте «Импорт и экспорт» заново.")]

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        batches = (
            (
                await session.execute(
                    select(ImportBatch).where(ImportBatch.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        target: uuid.UUID | None = next(
            (row.id for row in batches if short(row.id) == rest[0]), None
        )
        if target is None:
            raise NotFound("Пакет импорта недоступен")

    if action == "cancel":
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            batch = (
                await session.execute(select(ImportBatch).where(ImportBatch.id == target))
            ).scalar_one()
            batch.state = "cancelled"
            batch.version += 1
        return [
            Reply(
                text="Импорт отменён: рабочие итоги не изменились.",
                buttons=((Button("← Импорт и экспорт", callback("menu", "io")),),),
            )
        ]

    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        try:
            report = await commit_import(
                session,
                uow,
                actor=actor,
                batch_id=target,
                currency=workspace.currency,
                timezone=workspace.timezone,
                max_rows=settings.limits.max_import_rows,
            )
        except DomainError as exc:
            return [Reply(text=exc.message)]
    return [
        Reply(
            text=(
                "Импорт применён.\n"
                f"Сумма источника: {report.total_source_minor} минимальных единиц, "
                f"перенесено: {report.total_import_minor}."
            ),
            buttons=(
                (
                    Button("Бюджет", callback("menu", "budget")),
                    Button("История", callback("menu", "history")),
                ),
            ),
        )
    ]
