"""Журнал с фильтрами и сортировкой в диалоге (FR-07).

Состояние фильтра переносится в данных кнопки: журнал не зависит от
незаписанной памяти сессии и одинаково открывается у любого участника.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo

from fintracker.application.analytics.journal import list_journal
from fintracker.application.analytics.reports import FilterSpec
from fintracker.application.conversation import views
from fintracker.application.conversation.context import author_names, category_paths
from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.db.models.access import Workspace
from fintracker.db.session import RuntimeRole, session_scope

PAGE_SIZE = 8

# Флаги фильтра: одна буква на условие, чтобы уложиться в лимит кнопки (LIM-10).
FLAG_LABELS = {
    "a": "Мои записи",
    "s": "Потратил я",
    "n": "С комментарием",
    "v": "С отменёнными",
    "p": "Текущий период",
    "i": "Из импорта",
    "r": "Только возвраты",
}


@dataclass(frozen=True, slots=True)
class JournalView:
    """Разобранное состояние экрана журнала."""

    flags: str
    sort: str
    offset: int
    category: str
    # Поиск по комментарию переносится между страницами: переход «Ещё →» не
    # должен превращаться в журнал без фильтра (FR-07, G-19).
    note_query: str = ""

    @classmethod
    def parse(cls, rest: list[str]) -> JournalView:
        flags = rest[0] if rest and rest[0] != "-" else ""
        sort = rest[1] if len(rest) > 1 and rest[1] in {"o", "d"} else "o"
        offset = int(rest[2]) if len(rest) > 2 and rest[2].isdigit() else 0
        category = rest[3] if len(rest) > 3 and rest[3] != "-" else ""
        note = rest[4] if len(rest) > 4 and rest[4] != "-" else ""
        return cls(flags=flags, sort=sort, offset=offset, category=category, note_query=note)

    def parts(self, *, offset: int | None = None) -> tuple[str, ...]:
        # Данные кнопки ограничены Telegram, но обычный пользовательский поиск
        # должен переживать paging/sort. Двоеточие убирается как разделитель.
        note = self.note_query.replace(":", " ")[:40].strip() if self.note_query else ""
        note = note or "-"
        return (
            self.flags or "-",
            self.sort,
            str(self.offset if offset is None else offset),
            self.category or "-",
            note,
        )

    def toggled(self, flag: str) -> JournalView:
        flags = self.flags.replace(flag, "") if flag in self.flags else self.flags + flag
        return JournalView(
            flags="".join(sorted(flags)),
            sort=self.sort,
            offset=0,
            category=self.category,
            note_query=self.note_query,
        )

    def described(self) -> str:
        active = [FLAG_LABELS[flag] for flag in self.flags if flag in FLAG_LABELS]
        order = "по дате операции" if self.sort == "o" else "по времени добавления"
        if not active:
            return f"Фильтры: без ограничений · {order}"
        return f"Фильтры: {', '.join(active)} · {order}"


async def journal_view(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    view: JournalView,
    note_query: str | None = None,
) -> list[Reply]:
    """Показать страницу журнала с учётом фильтров (FR-07)."""
    # Запрос приходит либо из команды, либо из состояния страницы (G-19).
    note_query = note_query or view.note_query or None
    if note_query and not view.note_query:
        view = replace(view, note_query=note_query)
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()

    date_from: dt.date | None = None
    date_to_exclusive: dt.date | None = None
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        if "p" in view.flags:
            from fintracker.application.planning.periods import period_for_date

            period = await period_for_date(session, workspace_id=workspace_id, day=today)
            date_from = period.start_date
            date_to_exclusive = period.end_exclusive

        category_ids: tuple[uuid.UUID, ...] = ()
        paths = await category_paths(session, workspace_id=workspace_id)
        if view.category:
            category_ids = tuple(
                category_id for category_id in paths if short(category_id) == view.category
            )

        filters = FilterSpec(
            category_ids=category_ids,
            actor_user_ids=(actor.user_id,) if "a" in view.flags else (),
            spender_person_ids=(
                (actor.person_id,) if "s" in view.flags and actor.person_id else ()
            ),
            transaction_types=("refund",) if "r" in view.flags else (),
            origins=("import",) if "i" in view.flags else (),
            has_note=True if "n" in view.flags else None,
            include_voided="v" in view.flags,
            note_query=note_query,
        )
        page = await list_journal(
            session,
            workspace_id=workspace_id,
            filters=filters,
            date_from=date_from,
            date_to_exclusive=date_to_exclusive,
            sort="added" if view.sort == "d" else "occurred",
            limit=PAGE_SIZE,
            offset=view.offset,
        )
        authors = await author_names(session, workspace_id=workspace_id)

    if not page.entries:
        return [
            Reply(
                text=(
                    f"{view.described()}\nПодходящих записей нет. "
                    "Снимите часть условий или измените период."
                ),
                buttons=(
                    (Button("Фильтры", callback("hist", "filters", *view.parts())),),
                    (Button("← Меню", callback("menu", "main")),),
                ),
            )
        ]

    lines = [view.described()]
    if note_query:
        lines.append(f"Поиск по комментарию: «{note_query}»")
    shown_to = view.offset + len(page.entries)
    lines.append(f"Записи {view.offset + 1}–{shown_to} из {page.total}:")
    for entry in page.entries:
        if len(entry.category_ids) > 1:
            path = f"{len(entry.category_ids)} статей"
        elif entry.category_ids and entry.category_ids[0]:
            path = paths.get(entry.category_ids[0], "Без категории")
        else:
            path = "Без категории"
        lines.append(
            views.history_line(
                transaction_id=entry.transaction_id,
                amount_minor=entry.amount_minor,
                currency=entry.currency,
                occurred_date=entry.occurred_date,
                category_path=path,
                author=authors.get(entry.author_user_id),
                is_voided=entry.status == "voided",
                has_note=bool(entry.note),
            )
        )

    rows: list[tuple[Button, ...]] = [
        tuple(
            Button(
                f"Запись {index + 1}",
                callback("tx", "open", entry.transaction_id.hex[:16]),
            )
            for index, entry in enumerate(page.entries[:4])
        )
    ]
    paging: list[Button] = []
    if view.offset:
        paging.append(
            Button(
                "← Назад",
                callback("hist", "page", *view.parts(offset=max(0, view.offset - PAGE_SIZE))),
            )
        )
    if shown_to < page.total:
        paging.append(
            Button(
                "Ещё →",
                callback("hist", "page", *view.parts(offset=view.offset + PAGE_SIZE)),
            )
        )
    if paging:
        rows.append(tuple(paging))
    rows.append(
        (
            Button("Фильтры", callback("hist", "filters", *view.parts())),
            Button(
                "Последние добавленные" if view.sort == "o" else "По дате операции",
                callback("hist", "sort", *view.parts(offset=0)),
            ),
        )
    )
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def filters_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, view: JournalView
) -> list[Reply]:
    """Экран выбора условий журнала (FR-07)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        paths = await category_paths(session, workspace_id=workspace_id)

    lines = [
        view.described(),
        "Отметьте условия: повторное нажатие снимает их.",
        "Поиск по комментарию: отправьте «/history слово».",
    ]
    rows: list[tuple[Button, ...]] = []
    flags = list(FLAG_LABELS)
    for index in range(0, len(flags), 2):
        rows.append(
            tuple(
                Button(
                    ("✓ " if flag in view.flags else "") + FLAG_LABELS[flag],
                    callback("hist", "flag", flag, *view.parts()),
                )
                for flag in flags[index : index + 2]
            )
        )
    if view.category:
        rows.append((Button("Снять статью", callback("hist", "cat", "-", *view.parts())),))
    else:
        for category_id, path in list(paths.items())[:4]:
            rows.append(
                (
                    Button(
                        f"Статья: {path}"[:40],
                        callback("hist", "cat", short(category_id), *view.parts()),
                    ),
                )
            )
    rows.append(
        (
            Button("Показать", callback("hist", "page", *view.parts(offset=0))),
            Button("Сбросить", callback("hist", "reset")),
        )
    )
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def history_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    """Обработать кнопку журнала."""
    match action:
        case "reset":
            return await journal_view(
                settings,
                actor=actor,
                workspace=workspace,
                view=JournalView(flags="", sort="o", offset=0, category=""),
            )
        case "filters":
            return await filters_view(
                settings, actor=actor, workspace=workspace, view=JournalView.parse(rest)
            )
        case "page":
            return await journal_view(
                settings, actor=actor, workspace=workspace, view=JournalView.parse(rest)
            )
        case "sort":
            current = JournalView.parse(rest)
            flipped = JournalView(
                flags=current.flags,
                sort="d" if current.sort == "o" else "o",
                offset=0,
                category=current.category,
                note_query=current.note_query,
            )
            return await journal_view(settings, actor=actor, workspace=workspace, view=flipped)
        case "flag" if rest:
            flag = rest[0]
            if flag not in FLAG_LABELS:
                return [Reply(text="Кнопка устарела. Откройте журнал заново.")]
            return await filters_view(
                settings,
                actor=actor,
                workspace=workspace,
                view=JournalView.parse(rest[1:]).toggled(flag),
            )
        case "cat" if rest:
            current = JournalView.parse(rest[1:])
            chosen = "" if rest[0] == "-" else rest[0]
            return await filters_view(
                settings,
                actor=actor,
                workspace=workspace,
                view=JournalView(
                    flags=current.flags,
                    sort=current.sort,
                    offset=0,
                    category=chosen,
                    note_query=current.note_query,
                ),
            )
        case _:
            return [Reply(text="Кнопка устарела. Откройте журнал заново.")]
