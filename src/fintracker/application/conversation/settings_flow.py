"""Личные настройки, правила и общие настройки бюджета (CMD-26, CMD-27, CMD-30, CMD-31).

Личная настройка меняет только доставку и ввод самого участника: она не
скрывает его расходы из общей аналитики и не меняет правила другим (FR-54).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select

from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.application.conversation.views import money
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import NotFound
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Membership, Workspace
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

logger = get_logger("conversation.settings")

# Пресеты вместо свободного ввода: выбор всегда виден до сохранения (FR-19).
THRESHOLD_PRESETS_MINOR = (300_000, 500_000, 1_000_000)
QUIET_PRESETS = ((22, 9), (23, 8), (0, 0))
TIMEZONE_PRESETS = {
    "kgd": ("Калининград · UTC+2", "Europe/Kaliningrad"),
    "msk": ("Москва · UTC+3", "Europe/Moscow"),
    "ekb": ("Екатеринбург · UTC+5", "Asia/Yekaterinburg"),
    "nsk": ("Новосибирск · UTC+7", "Asia/Novosibirsk"),
    "vvo": ("Владивосток · UTC+10", "Asia/Vladivostok"),
}

FAMILY_LABELS = {
    "shared_change": "Изменения участников",
    "threshold": "Предупреждения о лимитах",
    "review": "Обзоры и анализ",
    "reminder": "Напоминания о платежах",
    "author_card": "Подтверждения моих операций",
}
MODE_LABELS = {"immediate": "сразу", "digest": "сводкой", "off": "выключено"}
MODE_CODES = {"i": "immediate", "d": "digest", "o": "off"}
FAMILY_CODES = {
    "shared_change": "sc",
    "threshold": "th",
    "review": "rv",
    "reminder": "rm",
    "author_card": "ac",
}
CODE_TO_FAMILY = {code: family for family, code in FAMILY_CODES.items()}


def _timezone_display(timezone: str, *, workspace_timezone: str) -> str:
    for label, stored_timezone in TIMEZONE_PRESETS.values():
        if stored_timezone == timezone:
            return label
    if timezone == workspace_timezone:
        return "как в бюджете"
    return "другой сохранённый пояс"


def _notification_summary(modes: dict[str, int]) -> str:
    total = sum(modes.values())
    if modes.get("immediate", 0) == total:
        return "Все уведомления приходят сразу"
    if modes.get("off", 0) == total:
        return "Все уведомления выключены"
    parts = []
    if modes.get("immediate"):
        parts.append(f"сразу — {modes['immediate']}")
    if modes.get("digest"):
        parts.append(f"сводкой — {modes['digest']}")
    if modes.get("off"):
        parts.append(f"выключено — {modes['off']}")
    return f"Из {total} типов: " + ", ".join(parts)


async def personal_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Главная страница личных настроек (CMD-26)."""
    from fintracker.application.identity.preferences import get_preferences

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        prefs = await get_preferences(session, user_id=actor.user_id, workspace_id=workspace_id)
        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == actor.user_id,
                )
            )
        ).scalar_one()
        autopost = membership.autopost_enabled
        assume_self = membership.assume_self_spender
        threshold = membership.large_amount_threshold_minor

    modes = {mode: list(prefs.families.values()).count(mode) for mode in MODE_LABELS}
    quiet = (
        "выключены"
        if prefs.quiet_hours_start == prefs.quiet_hours_end
        else f"{prefs.quiet_hours_start:02d}:00–{prefs.quiet_hours_end:02d}:00"
    )
    payer = "вы" if assume_self else "уточнять при необходимости"
    lines = [
        "⚙️ Личные настройки",
        "",
        "Здесь меняются только ваши уведомления и способ добавления операций.",
        "Другие участники сохраняют свои настройки.",
        "",
        "🔔 Уведомления",
        _notification_summary(modes),
        f"Тихие часы: {quiet}",
        "",
        "✍️ Ввод операций",
        f"Автоматическое сохранение: {'включено' if autopost else 'выключено'}",
        f"Кого считать плательщиком: {payer}",
        "Проверять крупные суммы: "
        + (f"от {money(threshold, workspace.currency)}" if threshold else "порог не задан"),
    ]
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("🔔 Уведомления", callback("set", "notify")),
                    Button("✍️ Ввод операций", callback("set", "input")),
                ),
                (
                    Button("✏️ Моё имя", callback("set", "name")),
                    Button("🧠 Правила", callback("set", "guide")),
                ),
                (Button("👤 Аккаунт", callback("set", "account")),),
                (Button("← Настройки бюджета", callback("menu", "settings")),),
            ),
        )
    ]


async def notifications_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Сводка уведомлений без изменения значений при открытии."""
    from fintracker.application.identity.preferences import get_preferences

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        prefs = await get_preferences(session, user_id=actor.user_id, workspace_id=workspace_id)
    timezone = prefs.timezone or workspace.timezone
    quiet = (
        "выключены"
        if prefs.quiet_hours_start == prefs.quiet_hours_end
        else f"{prefs.quiet_hours_start:02d}:00–{prefs.quiet_hours_end:02d}:00"
    )
    lines = ["🔔 Уведомления", "", "Нажмите на строку, чтобы увидеть варианты."]
    lines.extend(
        f"• {FAMILY_LABELS[family]}: {MODE_LABELS[prefs.families.get(family, 'immediate')]}"
        for family in FAMILY_LABELS
    )
    lines.extend(
        (
            "",
            f"🌙 Тихие часы: {quiet}",
            "🌍 Часовой пояс: "
            + _timezone_display(timezone, workspace_timezone=workspace.timezone),
        )
    )
    rows: list[tuple[Button, ...]] = [
        (
            Button(
                f"{FAMILY_LABELS[family]} · {MODE_LABELS[prefs.families.get(family, 'immediate')]}",
                callback("set", "family", FAMILY_CODES[family]),
            ),
        )
        for family in FAMILY_LABELS
    ]
    rows.extend(
        (
            (Button("🌙 Тихие часы", callback("set", "quiet")),),
            (Button("🌍 Часовой пояс", callback("set", "timezone")),),
            (Button("← Личные настройки", callback("set", "personal")),),
        )
    )
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def notification_family_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, family_code: str
) -> list[Reply]:
    """Явный выбор способа получения одного вида уведомлений."""
    from fintracker.application.identity.preferences import get_preferences

    family = CODE_TO_FAMILY.get(family_code)
    if family is None:
        return [_stale_button_reply()]
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        prefs = await get_preferences(session, user_id=actor.user_id, workspace_id=workspace_id)
    current = prefs.families.get(family, "immediate")
    explanation = {
        "immediate": "Бот сообщит сразу после события.",
        "digest": "Сообщения будут собраны в ближайшую сводку.",
        "off": "Такие уведомления приходить не будут.",
    }
    rows = tuple(
        (
            Button(
                f"{'✓ ' if current == mode else ''}{label}",
                callback("set", "fset", family_code, code, str(prefs.version)),
            ),
        )
        for code, mode in MODE_CODES.items()
        for label in (MODE_LABELS[mode].capitalize(),)
    )
    return [
        Reply(
            text=(
                f"🔔 {FAMILY_LABELS[family]}\n\n"
                f"Сейчас: {MODE_LABELS[current]}.\n{explanation[current]}\n\n"
                "Выберите подходящий вариант:"
            ),
            buttons=(*rows, (Button("← Уведомления", callback("set", "notify")),)),
        )
    ]


async def quiet_hours_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.identity.preferences import get_preferences

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        prefs = await get_preferences(session, user_id=actor.user_id, workspace_id=workspace_id)
    current = (prefs.quiet_hours_start, prefs.quiet_hours_end)
    rows = tuple(
        (
            Button(
                f"{'✓ ' if current == preset else ''}"
                + ("Выключить" if start == end else f"{start:02d}:00–{end:02d}:00"),
                callback("set", "qset", f"{start}-{end}", str(prefs.version)),
            ),
        )
        for preset in QUIET_PRESETS
        for start, end in (preset,)
    )
    current_text = (
        "выключены" if current[0] == current[1] else f"{current[0]:02d}:00–{current[1]:02d}:00"
    )
    return [
        Reply(
            text=(
                "🌙 Тихие часы\n\n"
                f"Сейчас: {current_text}.\n"
                "В это время обычные уведомления подождут до утра."
            ),
            buttons=(*rows, (Button("← Уведомления", callback("set", "notify")),)),
        )
    ]


async def timezone_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.identity.preferences import get_preferences

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        prefs = await get_preferences(session, user_id=actor.user_id, workspace_id=workspace_id)
    current = prefs.timezone or workspace.timezone
    rows = tuple(
        (
            Button(
                f"{'✓ ' if current == timezone else ''}{label}",
                callback("set", "tzset", code, str(prefs.version)),
            ),
        )
        for code, (label, timezone) in TIMEZONE_PRESETS.items()
    )
    return [
        Reply(
            text=(
                "🌍 Часовой пояс уведомлений\n\n"
                "Сейчас: "
                f"{_timezone_display(current, workspace_timezone=workspace.timezone)}.\n"
                "Он определяет, когда начинаются и заканчиваются тихие часы."
            ),
            buttons=(*rows, (Button("← Уведомления", callback("set", "notify")),)),
        )
    ]


async def input_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == actor.user_id,
                )
            )
        ).scalar_one()
    threshold = membership.large_amount_threshold_minor
    payer = "вы" if membership.assume_self_spender else "уточнять при необходимости"
    return [
        Reply(
            text=(
                "✍️ Ввод операций\n\n"
                "Автоматическое сохранение распознанных расходов: "
                f"{'включено' if membership.autopost_enabled else 'выключено'}\n"
                f"Кого считать плательщиком: {payer}\n"
                "Проверять крупные суммы: "
                + (f"от {money(threshold, workspace.currency)}" if threshold else "порог не задан")
                + "\n\nНажмите на настройку, чтобы увидеть варианты."
            ),
            buttons=(
                (Button("Автоматическое сохранение", callback("set", "auto")),),
                (Button("Кого считать плательщиком", callback("set", "payer")),),
                (Button("Проверка крупных сумм", callback("set", "threshold")),),
                (Button("← Личные настройки", callback("set", "personal")),),
            ),
        )
    ]


async def settings_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    """Обработать кнопку раздела настроек."""
    from fintracker.application.identity.preferences import (
        get_preferences,
        set_input_preferences,
        set_notification_family,
        set_quiet_hours,
    )

    workspace_id = actor.require_workspace()
    match action:
        case "personal":
            return await personal_view(settings, actor=actor, workspace=workspace)
        case "notify":
            return await notifications_view(settings, actor=actor, workspace=workspace)
        case "family" if rest:
            return await notification_family_view(
                settings, actor=actor, workspace=workspace, family_code=rest[0]
            )
        case "fset" if len(rest) == 3:
            family = CODE_TO_FAMILY.get(rest[0])
            mode = MODE_CODES.get(rest[1])
            if family is None or mode is None:
                return [_stale_button_reply()]
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                await set_notification_family(
                    session,
                    user_id=actor.user_id,
                    workspace_id=workspace_id,
                    family=family,
                    mode=mode,
                    expected_version=int(rest[2]),
                )
            return await notification_family_view(
                settings, actor=actor, workspace=workspace, family_code=rest[0]
            )
        case "quiet":
            return await quiet_hours_view(settings, actor=actor, workspace=workspace)
        case "qset" if len(rest) == 2:
            start_text, _, end_text = rest[0].partition("-")
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                prefs = await get_preferences(
                    session, user_id=actor.user_id, workspace_id=workspace_id
                )
                await set_quiet_hours(
                    session,
                    user_id=actor.user_id,
                    workspace_id=workspace_id,
                    start_hour=int(start_text),
                    end_hour=int(end_text),
                    timezone=prefs.timezone or workspace.timezone,
                    expected_version=int(rest[1]),
                )
            return await quiet_hours_view(settings, actor=actor, workspace=workspace)
        case "timezone":
            return await timezone_view(settings, actor=actor, workspace=workspace)
        case "tzset" if len(rest) == 2:
            preset = TIMEZONE_PRESETS.get(rest[0])
            if preset is None:
                return [_stale_button_reply()]
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                prefs = await get_preferences(
                    session, user_id=actor.user_id, workspace_id=workspace_id
                )
                await set_quiet_hours(
                    session,
                    user_id=actor.user_id,
                    workspace_id=workspace_id,
                    start_hour=prefs.quiet_hours_start,
                    end_hour=prefs.quiet_hours_end,
                    timezone=preset[1],
                    expected_version=int(rest[1]),
                )
            return await timezone_view(settings, actor=actor, workspace=workspace)
        case "input":
            return await input_view(settings, actor=actor, workspace=workspace)
        case "auto":
            return await boolean_input_view(
                settings, actor=actor, workspace=workspace, setting="auto"
            )
        case "payer":
            return await boolean_input_view(
                settings, actor=actor, workspace=workspace, setting="payer"
            )
        case "aset" | "pset" if len(rest) == 2:
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                selected = rest[0] == "1"
                if action == "aset":
                    await set_input_preferences(
                        session,
                        actor=actor,
                        autopost=selected,
                        expected_version=int(rest[1]),
                    )
                else:
                    await set_input_preferences(
                        session,
                        actor=actor,
                        assume_self_spender=selected,
                        expected_version=int(rest[1]),
                    )
            return await boolean_input_view(
                settings,
                actor=actor,
                workspace=workspace,
                setting="auto" if action == "aset" else "payer",
            )
        case "threshold":
            return await threshold_view(settings, actor=actor, workspace=workspace)
        case "tset" if len(rest) == 2:
            selected_threshold = int(rest[0])
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                await set_input_preferences(
                    session,
                    actor=actor,
                    large_amount_threshold_minor=(
                        selected_threshold if selected_threshold > 0 else None
                    ),
                    clear_large_amount_threshold=selected_threshold == 0,
                    expected_version=int(rest[1]),
                )
            return await threshold_view(settings, actor=actor, workspace=workspace)
        case "guide":
            return await rules_guide_view(settings, actor=actor, workspace=workspace)
        case "rules":
            return await rules_view(settings, actor=actor, workspace=workspace)
        case "rdel" if rest:
            return await _archive_rule(settings, actor=actor, workspace=workspace, prefix=rest[0])
        case "account":
            return account_view()
        case "acc" | "accdel":
            return await account_deletion_view(settings, actor=actor)
        case "accgo":
            return await confirm_account_deletion(settings, actor=actor)
        case "period":
            return await period_settings_view(settings, actor=actor, workspace=workspace)
        case "prd" if rest:
            return await period_change_preview(
                settings, actor=actor, workspace=workspace, preset=rest[0]
            )
        case "prdok" if rest:
            return await apply_period_change(
                settings, actor=actor, workspace=workspace, preset=rest[0]
            )
        case "name":
            from fintracker.application.conversation.pending import set_pending

            await set_pending(
                settings,
                user_id=actor.user_id,
                workspace_id=workspace_id,
                kind="profile_name",
                payload={},
            )
            return [
                Reply(
                    text=(
                        "✏️ Ваше имя в бюджете\n\nКак вас подписывать в истории и "
                        "уведомлениях? Отправьте имя одним сообщением, например «Аня»."
                    ),
                    buttons=((Button("✕ Отмена", callback("noop", "nochange")),),),
                )
            ]
        case "bname":
            from fintracker.application.conversation.pending import set_pending

            if not actor.is_admin:
                return [Reply(text="ℹ️ Переименовать бюджет может только администратор.")]
            await set_pending(
                settings,
                user_id=actor.user_id,
                workspace_id=workspace_id,
                kind="budget_rename",
                payload={},
            )
            return [
                Reply(
                    text=(
                        f"✏️ Новое название бюджета\n\nСейчас: {workspace.name}\n\n"
                        "Отправьте новое название одним сообщением."
                    ),
                    buttons=((Button("✕ Отмена", callback("noop", "nochange")),),),
                )
            ]
        case _:
            return [_stale_button_reply()]


def _stale_button_reply() -> Reply:
    return Reply(text="🔄 Эта кнопка устарела.\n\nОткройте личные настройки заново.")


async def _membership_for_settings(settings: Settings, *, actor: ActorContext) -> Membership:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        return (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == actor.user_id,
                )
            )
        ).scalar_one()


async def boolean_input_view(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    setting: str,
) -> list[Reply]:
    membership = await _membership_for_settings(settings, actor=actor)
    if setting == "auto":
        current = membership.autopost_enabled
        title = "Автоматическое сохранение"
        description = (
            "Бот может сразу сохранять однозначно распознанные расходы. "
            "Если данных недостаточно или сумма крупная, он всё равно попросит подтверждение."
        )
        action = "aset"
        labels = ("Выключить", "Включить")
    else:
        current = membership.assume_self_spender
        title = "Кого считать плательщиком"
        description = (
            "Если выбрать «Всегда я», бот будет считать плательщиком вас, "
            "когда в сообщении не указано другое имя."
        )
        action = "pset"
        labels = ("Уточнять при необходимости", "Всегда я")
    rows = tuple(
        (
            Button(
                f"{'✓ ' if current is selected else ''}{label}",
                callback("set", action, "1" if selected else "0", str(membership.version)),
            ),
        )
        for selected, label in zip((False, True), labels, strict=True)
    )
    return [
        Reply(
            text=f"✍️ {title}\n\n{description}\n\nСейчас: {labels[1 if current else 0]}.",
            buttons=(*rows, (Button("← Ввод операций", callback("set", "input")),)),
        )
    ]


async def threshold_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    membership = await _membership_for_settings(settings, actor=actor)
    current = membership.large_amount_threshold_minor
    choices: tuple[int | None, ...] = (None, *THRESHOLD_PRESETS_MINOR)
    rows = tuple(
        (
            Button(
                f"{'✓ ' if current == value else ''}"
                + ("Порог не задан" if value is None else f"От {money(value, workspace.currency)}"),
                callback(
                    "set", "tset", "0" if value is None else str(value), str(membership.version)
                ),
            ),
        )
        for value in choices
    )
    current_text = money(current, workspace.currency) if current else "не задан"
    return [
        Reply(
            text=(
                "💰 Проверка крупных сумм\n\n"
                "Расход от выбранной суммы бот не сохранит без вашего подтверждения.\n\n"
                f"Сейчас: {current_text}."
            ),
            buttons=(*rows, (Button("← Ввод операций", callback("set", "input")),)),
        )
    ]


async def rules_guide_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.catalog.rules import list_rules

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rules = await list_rules(
            session, workspace_id=workspace_id, membership_id=actor.membership_id
        )
    return [
        Reply(
            text=(
                "🧠 Правила и подсказки\n\n"
                "Когда вы исправляете категорию, бот может запомнить ваш выбор "
                "для будущих расходов.\n\n"
                f"Сохранённых правил: {len(rules)}. "
                "Они не меняют уже записанные операции."
            ),
            buttons=(
                (Button("Посмотреть правила", callback("set", "rules")),),
                (Button("← Личные настройки", callback("set", "personal")),),
            ),
        )
    ]


def account_view() -> list[Reply]:
    return [
        Reply(
            text=(
                "👤 Аккаунт\n\n"
                "Здесь можно удалить аккаунт и прекратить доступ ко всем бюджетам. "
                "Если вы управляете бюджетом, сначала потребуется передать управление.\n\n"
                "Совместные операции останутся у других участников без вашего имени."
            ),
            buttons=(
                (Button("🗑 Перейти к удалению", callback("set", "accdel")),),
                (Button("← Личные настройки", callback("set", "personal")),),
            ),
        )
    ]


async def rules_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Список правил классификации с указанием области (CMD-27)."""
    from fintracker.application.catalog.rules import list_rules

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rules = await list_rules(
            session, workspace_id=workspace_id, membership_id=actor.membership_id
        )
    if not rules:
        return [
            Reply(
                text=(
                    "🧠 Пока нет правил\n\nПосле исправления категории бот предложит "
                    "запомнить ваш выбор. Правило будет применяться к новым тратам."
                ),
                buttons=((Button("← Правила и подсказки", callback("set", "guide")),),),
            )
        ]
    lines = ["🧠 Правила категорий\n"]
    rows: list[tuple[Button, ...]] = []
    for rule in rules[:10]:
        scope = "только для вас" if rule.scope == "member" else "для всех участников"
        lines.append(f"• «{rule.keyword}» → {rule.category_name} ({scope})")
        rows.append(
            (Button(f"Убрать «{rule.keyword}»"[:40], callback("set", "rdel", short(rule.id))),)
        )
    lines.append("Правила применяются только к новым записям.")
    rows.append((Button("← Правила и подсказки", callback("set", "guide")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def _archive_rule(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, prefix: str
) -> list[Reply]:
    from fintracker.application.catalog.rules import archive_rule, list_rules

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rules = await list_rules(
            session, workspace_id=workspace_id, membership_id=actor.membership_id
        )
        target: uuid.UUID | None = next(
            (rule.id for rule in rules if short(rule.id) == prefix), None
        )
        if target is None:
            raise NotFound("Правило недоступно")
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await archive_rule(session, uow, actor=actor, rule_id=target)
    return await rules_view(settings, actor=actor, workspace=workspace)


async def period_settings_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Период и повторение: действующее правило и ближайшие периоды (FR-90)."""
    from fintracker.application.conversation import views
    from fintracker.application.conversation.context import current_status
    from fintracker.application.planning.periods import (
        latest_policy,
        policy_from_row,
        upcoming_periods,
    )

    workspace_id = actor.require_workspace()
    status = await current_status(settings, actor=actor, workspace=workspace)
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        policy = policy_from_row(await latest_policy(session, workspace_id))
        upcoming = await upcoming_periods(
            session,
            workspace_id=workspace_id,
            from_date=status.end_inclusive + dt.timedelta(days=1),
            count=3,
        )
    lines = [
        "📅 Период и повторение",
        "",
        f"Повторять: {policy.describe()}",
        f"Сейчас: {views.format_range(status.start_date, status.end_inclusive)}",
        "",
        "Дальше:",
    ]
    lines.extend(f"• {views.format_range(item.start, item.end_inclusive)}" for item in upcoming)
    rows: list[tuple[Button, ...]] = []
    if actor.is_admin:
        lines.extend(
            [
                "",
                "Новое правило начнёт действовать после уже открытых периодов: прошлые "
                "расходы и текущий план не пересчитываются.",
            ]
        )
        rows.append(
            (
                Button("Календарный месяц", callback("set", "prd", "month")),
                Button("С 10-го числа", callback("set", "prd", "10to9")),
            )
        )
        rows.append((Button("Неделя", callback("set", "prd", "week")),))
    else:
        lines.extend(["", "Изменить период может администратор бюджета."])
    rows.append((Button("← Настройки", callback("menu", "settings")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def period_change_preview(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, preset: str
) -> list[Reply]:
    from fintracker.application.conversation import views
    from fintracker.application.planning.periods import (
        PERIOD_PRESETS,
        _preset_anchor,
        next_policy_boundary,
    )

    if not actor.is_admin:
        return [Reply(text="ℹ️ Изменить период может только администратор бюджета.")]
    if preset not in PERIOD_PRESETS:
        return [_stale_button_reply()]
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        boundary = await next_policy_boundary(session, workspace_id=workspace_id)
    anchor, _, _ = _preset_anchor(preset, boundary)
    note = (
        ""
        if anchor == boundary
        else "\n\nДо первой новой границы будет короткий переходный период."
    )
    return [
        Reply(
            text=(
                f"📅 Новое правило: {PERIOD_PRESETS[preset]}\n\n"
                f"Начнёт действовать с {views.format_date(boundary, with_year=True)}. "
                "Уже открытые периоды и записи не изменятся."
                f"{note}\n\nИзменение увидят все участники."
            ),
            buttons=(
                (
                    Button("✅ Применить", callback("set", "prdok", preset)),
                    Button("Отмена", callback("set", "period")),
                ),
            ),
        )
    ]


async def apply_period_change(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, preset: str
) -> list[Reply]:
    from fintracker.application.planning.periods import PERIOD_PRESETS, change_period_policy

    if not actor.is_admin:
        return [Reply(text="ℹ️ Изменить период может только администратор бюджета.")]
    if preset not in PERIOD_PRESETS:
        return [_stale_button_reply()]
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        await change_period_policy(
            session, workspace_id=workspace_id, preset=preset, created_by=actor.user_id
        )
        await uow.bump_revisions(workspace_id, calendar=True)
    replies = await period_settings_view(settings, actor=actor, workspace=workspace)
    first = replies[0]
    return [
        Reply(text=f"✅ Правило периода изменено\n\n{first.text}", buttons=first.buttons),
        *replies[1:],
    ]


async def apply_profile_name(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, name: str
) -> list[Reply]:
    from fintracker.application.identity.profile import rename_member_profile
    from fintracker.core.errors import ValidationFailed

    try:
        display = await rename_member_profile(
            settings,
            user_id=actor.user_id,
            workspace_id=actor.require_workspace(),
            name=name,
        )
    except ValidationFailed as exc:
        return [Reply(text=f"⚠️ {exc.message}", retry_input=True)]
    return [
        Reply(
            text=f"✅ Теперь в бюджете «{workspace.name}» вы — {display}.",
            buttons=((Button("👥 Участники", callback("menu", "members")),),),
        )
    ]


async def apply_budget_rename(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, name: str
) -> list[Reply]:
    from fintracker.application.catalog.normalize import clean_display_name

    if not actor.is_admin:
        return [Reply(text="ℹ️ Переименовать бюджет может только администратор.")]
    display = clean_display_name(name)[:120]
    if not display:
        return [
            Reply(
                text="✍️ Название не может быть пустым. Отправьте новое название.", retry_input=True
            )
        ]
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        from sqlalchemy import update

        await session.execute(
            update(Workspace).where(Workspace.id == workspace_id).values(name=display)
        )
        await uow.bump_revisions(workspace_id, catalog=True)
    return [
        Reply(
            text=f"✅ Бюджет переименован: «{display}».",
            buttons=((Button("⚙️ Настройки", callback("menu", "settings")),),),
        )
    ]


async def account_deletion_view(settings: Settings, *, actor: ActorContext) -> list[Reply]:
    """Предпросмотр удаления аккаунта (CMD-31)."""
    from fintracker.application.identity.preferences import preview_account_deletion

    preview = await preview_account_deletion(settings, user_id=actor.user_id)
    lines = ["Удаление аккаунта"]
    if preview.admin_workspaces:
        lines.append("Сначала передайте администрирование или удалите эти бюджеты:")
        lines.extend(f"• {name}" for _, name in preview.admin_workspaces)
        lines.append("Пока это не сделано, аккаунт не удаляется.")
        buttons = ((Button("← Аккаунт", callback("set", "account")),),)
        return [Reply(text="\n".join(lines), buttons=buttons)]
    if preview.member_workspaces:
        lines.append("Будут прекращены членства:")
        lines.extend(f"• {name}" for _, name in preview.member_workspaces)
    lines.append(
        "Совместные записи остаются в бюджетах участников: они нужны для "
        "общего учёта. Ваше имя в них будет обезличено."
    )
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (Button("🗑 Подтвердить удаление", callback("set", "accgo")),),
                (Button("← Аккаунт", callback("set", "account")),),
            ),
        )
    ]


async def confirm_account_deletion(settings: Settings, *, actor: ActorContext) -> list[Reply]:
    """Выполнить удаление аккаунта (CMD-31)."""
    from fintracker.application.identity.preferences import delete_account

    result = await delete_account(
        settings, user_id=actor.user_id, correlation_id=actor.correlation_id
    )
    if result.member_workspaces or result.admin_workspaces:
        # Подтверждение полного удаления не выдаётся до завершения всех отзывов.
        return [Reply(text="ℹ️ Удаление начато, но часть членств ещё активна.\n\nПовторите позже.")]
    return [
        Reply(
            text=(
                "🗑 Аккаунт удалён: доступ к бюджетам прекращён.\n\nСовместные "
                "записи сохранены у остальных участников в обезличенном виде."
            )
        )
    ]
