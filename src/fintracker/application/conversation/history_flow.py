"""Журнал с фильтрами и сортировкой в диалоге (FR-07).

Состояние фильтра переносится в данных кнопки: журнал не зависит от
незаписанной памяти сессии и одинаково открывается у любого участника.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.analytics.journal import list_journal
from fintracker.application.analytics.reports import FilterSpec
from fintracker.application.conversation import views
from fintracker.application.conversation.context import author_names, category_paths
from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.db.models.access import Workspace
from fintracker.db.models.platform import HistoryQueryState
from fintracker.db.session import RuntimeRole, session_scope

PAGE_SIZE = 8
CATEGORY_PAGE_SIZE = 6

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
        if self.category:
            active.append("выбрана категория")
        if self.note_query:
            active.append("поиск по тексту")
        order = "по дате операции" if self.sort == "o" else "по времени добавления"
        if not active:
            return f"Фильтры: без ограничений · {order}"
        return f"Фильтры: {', '.join(active)} · {order}"


def _parts_label(count: int) -> str:
    return f"{count} {views.plural(count, 'категория', 'категории', 'категорий')}"


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
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()

    date_from: dt.date | None = None
    date_to_exclusive: dt.date | None = None
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        if note_query and note_query.startswith("~"):
            stored = (
                await session.execute(
                    select(HistoryQueryState.query).where(
                        HistoryQueryState.user_id == actor.user_id,
                        HistoryQueryState.workspace_id == workspace_id,
                        HistoryQueryState.token == note_query[1:],
                        HistoryQueryState.expires_at > dt.datetime.now(dt.UTC),
                    )
                )
            ).scalar_one_or_none()
            note_query = stored
        elif note_query and not view.note_query:
            # The callback needs a compact opaque continuation, while the full
            # text remains available for paging, sorting and category filters.
            token = uuid.uuid4().hex[:12]
            session.add(
                HistoryQueryState(
                    user_id=actor.user_id,
                    workspace_id=workspace_id,
                    token=token,
                    query=note_query,
                    expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
                )
            )
            view = replace(view, note_query=f"~{token}")
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
                    f"🔎 Подходящих записей нет\n\n{view.described()}\n\n"
                    "Снимите часть условий или попробуйте другое слово для поиска."
                ),
                buttons=(
                    (Button("🔎 Фильтры", callback("hist", "filters", *view.parts())),),
                    (Button("← Меню", callback("menu", "main")),),
                ),
            )
        ]

    lines = ["🧾 История операций", "", view.described()]
    if note_query:
        lines.append(f"Поиск: «{note_query}»")
    shown_to = view.offset + len(page.entries)
    lines.extend(["", f"Записи {view.offset + 1}–{shown_to} из {page.total}:"])
    for entry in page.entries:
        if len(entry.category_ids) > 1:
            path = _parts_label(len(entry.category_ids))
        elif entry.category_ids and entry.category_ids[0]:
            path = paths.get(entry.category_ids[0], "Без категории")
        else:
            path = "Без категории"
        lines.append("")
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
                transaction_type=entry.transaction_type,
                account_flow=(
                    " → ".join(entry.account_flow) if entry.account_flow is not None else None
                ),
            )
        )

    entry_buttons = [
        Button(
            views.history_button_label(
                amount_minor=entry.amount_minor,
                currency=entry.currency,
                occurred_date=entry.occurred_date,
                transaction_type=entry.transaction_type,
                category_path=(
                    _parts_label(len(entry.category_ids))
                    if len(entry.category_ids) > 1
                    else (
                        paths.get(entry.category_ids[0], "Без категории")
                        if entry.category_ids and entry.category_ids[0]
                        else "Без категории"
                    )
                ),
                account_flow=(
                    " → ".join(entry.account_flow) if entry.account_flow is not None else None
                ),
            ),
            callback("tx", "open", entry.transaction_id.hex[:16]),
        )
        for entry in page.entries
    ]
    # По одной записи в строке: на телефоне подпись не обрезается до «12.09 · Рас…».
    rows: list[tuple[Button, ...]] = [(button,) for button in entry_buttons]
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
            Button("🔎 Фильтры", callback("hist", "filters", *view.parts())),
            Button(
                "Последние добавленные" if view.sort == "o" else "По дате операции",
                callback("hist", "sort", *view.parts(offset=0)),
            ),
        )
    )
    rows.append((Button("← Меню", callback("menu", "main")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def filters_view(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    view: JournalView,
    category_page: int = 0,
) -> list[Reply]:
    """Экран выбора условий журнала (FR-07)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        paths = await category_paths(session, workspace_id=workspace_id)

    categories = sorted(paths.items(), key=lambda item: (item[1].casefold(), item[0].hex))
    last_page = max(0, (len(categories) - 1) // CATEGORY_PAGE_SIZE)
    category_page = max(0, min(category_page, last_page))

    lines = [
        "🔎 Фильтры истории",
        "",
        view.described(),
        "",
        "Выберите нужные условия. Повторное нажатие уберёт условие.",
        "",
        "🔎 Чтобы найти запись по описанию или комментарию, отправьте:\n/history кофе",
    ]
    rows: list[tuple[Button, ...]] = []
    flags = list(FLAG_LABELS)
    for index in range(0, len(flags), 2):
        rows.append(
            tuple(
                Button(
                    ("✓ " if flag in view.flags else "") + FLAG_LABELS[flag],
                    callback("hist", "flag", flag, *view.parts(), str(category_page)),
                )
                for flag in flags[index : index + 2]
            )
        )
    if view.category:
        selected = next((path for cid, path in categories if short(cid) == view.category), None)
        if selected:
            lines.extend(["", f"Выбрана категория: {selected}"])
        rows.append(
            (
                Button(
                    "Снять категорию",
                    callback(
                        "hist", "cat", "-", *replace(view, category="").parts(), str(category_page)
                    ),
                ),
            )
        )
    start = category_page * CATEGORY_PAGE_SIZE
    for category_id, path in categories[start : start + CATEGORY_PAGE_SIZE]:
        rows.append(
            (
                Button(
                    ("✓ " if short(category_id) == view.category else "")
                    + f"Категория: {path}"[:40],
                    # The selected category replaces the old value; do not encode both
                    # UUID prefixes alongside the stored note-query token (Telegram 64B).
                    callback(
                        "hist",
                        "cat",
                        short(category_id),
                        *replace(view, category="").parts(),
                        str(category_page),
                    ),
                ),
            )
        )
    if last_page:
        lines.extend(["", f"Категории · страница {category_page + 1} из {last_page + 1}"])
        navigation = []
        if category_page:
            navigation.append(
                Button(
                    "← Категории",
                    callback("hist", "cats", str(category_page - 1), *view.parts()),
                )
            )
        if category_page < last_page:
            navigation.append(
                Button(
                    "Категории →",
                    callback("hist", "cats", str(category_page + 1), *view.parts()),
                )
            )
        rows.append(tuple(navigation))
    rows.append(
        (
            Button("🔎 Показать", callback("hist", "page", *view.parts(offset=0))),
            Button("↩️ Сбросить", callback("hist", "reset")),
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
        case "cats" if rest:
            return await filters_view(
                settings,
                actor=actor,
                workspace=workspace,
                view=JournalView.parse(rest[1:]),
                category_page=int(rest[0]) if rest[0].isdigit() else 0,
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
                return [Reply(text="🔄 Кнопка устарела.\n\nОткройте журнал заново.")]
            return await filters_view(
                settings,
                actor=actor,
                workspace=workspace,
                view=JournalView.parse(rest[1:]).toggled(flag),
                category_page=int(rest[6]) if len(rest) > 6 and rest[6].isdigit() else 0,
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
                category_page=int(rest[6]) if len(rest) > 6 and rest[6].isdigit() else 0,
            )
        case _:
            return [Reply(text="🔄 Кнопка устарела.\n\nОткройте журнал заново.")]
