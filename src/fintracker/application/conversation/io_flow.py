"""Импорт и экспорт из чата (FR-63–FR-67, CMD-28, CMD-29)."""

from __future__ import annotations

from fintracker.application.conversation.keyboards import Button, callback
from fintracker.application.conversation.types import Reply
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.db.models.access import Workspace


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
