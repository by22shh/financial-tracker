"""Личные настройки, правила и общие настройки бюджета (CMD-26, CMD-27, CMD-30, CMD-31).

Личная настройка меняет только доставку и ввод самого участника: она не
скрывает его расходы из общей аналитики и не меняет правила другим (FR-54).
"""

from __future__ import annotations

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

# Пресеты вместо свободного ввода: порог задаётся явно, а не оценивается (FR-19).
THRESHOLD_PRESETS_MINOR = (300_000, 500_000, 1_000_000)
QUIET_PRESETS = ((22, 9), (23, 8), (0, 0))

FAMILY_LABELS = {
    "shared_change": "Изменения участников",
    "threshold": "Пороги лимитов",
    "review": "Обзоры и анализ",
    "reminder": "Напоминания о платежах",
    "author_card": "Мои карточки записей",
}
MODE_CYCLE = {"immediate": "digest", "digest": "off", "off": "immediate"}
MODE_LABELS = {"immediate": "сразу", "digest": "сводкой", "off": "выключено"}
FAMILY_CODES = {
    "shared_change": "sc",
    "threshold": "th",
    "review": "rv",
    "reminder": "rm",
    "author_card": "ac",
}
CODE_TO_FAMILY = {code: family for family, code in FAMILY_CODES.items()}


async def personal_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Мои настройки: уведомления, тихие часы и режим ввода (CMD-26)."""
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

    lines = [prefs.describe(), "", "Ввод:"]
    lines.append(f"• Автозапись уверенных разборов: {'включена' if autopost else 'выключена'}")
    lines.append(f"• Считать покупателем себя: {'да' if assume_self else 'нет'}")
    lines.append(
        "• Порог подтверждения крупной суммы: "
        + (money(threshold, workspace.currency) if threshold else "не задан")
    )

    family_rows = tuple(
        (
            Button(
                f"{FAMILY_LABELS[family]}: {MODE_LABELS[prefs.families.get(family, 'immediate')]}",
                callback("set", "fam", FAMILY_CODES[family]),
            ),
        )
        for family in FAMILY_LABELS
    )
    rows: list[tuple[Button, ...]] = list(family_rows)
    rows.append(
        tuple(
            Button(
                "Тихие часы выкл." if start == end else f"Тихие часы {start}–{end}",
                callback("set", "quiet", f"{start}-{end}"),
            )
            for start, end in QUIET_PRESETS
        )
    )
    rows.append(
        (
            Button(
                "Автозапись: выключить" if autopost else "Автозапись: включить",
                callback("set", "auto"),
            ),
            Button(
                "Покупатель: не я" if assume_self else "Покупатель: я",
                callback("set", "selfbuyer"),
            ),
        )
    )
    rows.append(
        tuple(
            Button(
                f"Порог {money(value, workspace.currency)}",
                callback("set", "thr", str(value)),
            )
            for value in THRESHOLD_PRESETS_MINOR
        )
    )
    rows.append(
        (
            Button("Мои правила", callback("set", "rules")),
            Button("Удалить аккаунт", callback("set", "acc")),
        )
    )
    rows.append((Button("← Настройки", callback("menu", "settings")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


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
        case "fam" if rest:
            family = CODE_TO_FAMILY.get(rest[0])
            if family is None:
                return [Reply(text="Кнопка устарела. Откройте настройки заново.")]
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                current = await get_preferences(
                    session, user_id=actor.user_id, workspace_id=workspace_id
                )
                await set_notification_family(
                    session,
                    user_id=actor.user_id,
                    workspace_id=workspace_id,
                    family=family,
                    mode=MODE_CYCLE[current.families.get(family, "immediate")],
                )
            return await personal_view(settings, actor=actor, workspace=workspace)
        case "quiet" if rest:
            start_text, _, end_text = rest[0].partition("-")
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                await set_quiet_hours(
                    session,
                    user_id=actor.user_id,
                    workspace_id=workspace_id,
                    start_hour=int(start_text),
                    end_hour=int(end_text),
                    timezone=workspace.timezone,
                )
            return await personal_view(settings, actor=actor, workspace=workspace)
        case "auto" | "selfbuyer":
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
                if action == "auto":
                    await set_input_preferences(
                        session, actor=actor, autopost=not membership.autopost_enabled
                    )
                else:
                    await set_input_preferences(
                        session,
                        actor=actor,
                        assume_self_spender=not membership.assume_self_spender,
                    )
            return await personal_view(settings, actor=actor, workspace=workspace)
        case "thr" if rest:
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                await set_input_preferences(
                    session, actor=actor, large_amount_threshold_minor=int(rest[0])
                )
            return await personal_view(settings, actor=actor, workspace=workspace)
        case "rules":
            return await rules_view(settings, actor=actor, workspace=workspace)
        case "rdel" if rest:
            return await _archive_rule(settings, actor=actor, workspace=workspace, prefix=rest[0])
        case "acc":
            return await account_deletion_view(settings, actor=actor)
        case "accgo":
            return await confirm_account_deletion(settings, actor=actor)
        case "period":
            return await period_settings_view(settings, actor=actor, workspace=workspace)
        case _:
            return [Reply(text="Кнопка устарела. Откройте настройки заново.")]


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
                    "Правил пока нет.\nПосле исправления категории можно выбрать "
                    "«Всегда относить такие операции сюда»."
                ),
                buttons=((Button("← Мои настройки", callback("set", "personal")),),),
            )
        ]
    lines = ["Правила классификации:"]
    rows: list[tuple[Button, ...]] = []
    for rule in rules[:10]:
        scope = "личное" if rule.scope == "member" else "общее"
        lines.append(f"• «{rule.keyword}» → {rule.category_name} ({scope})")
        rows.append(
            (Button(f"Убрать «{rule.keyword}»"[:40], callback("set", "rdel", short(rule.id))),)
        )
    lines.append("Правила применяются только к новым записям.")
    rows.append((Button("← Мои настройки", callback("set", "personal")),))
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
    """Период и повторение: показ действующего правила и его источника (FR-90)."""
    from fintracker.application.planning.periods import latest_policy, policy_from_row

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        row = await latest_policy(session, workspace_id)
        policy = policy_from_row(row)
        upcoming = policy.preview(4)
    from fintracker.application.conversation import views

    lines = [
        f"Повторение периода: {policy.describe()}",
        f"Якорь: {row.anchor_date.isoformat()} (день {row.anchor_day})",
        "Ближайшие периоды:",
    ]
    lines.extend(f"• {views.format_range(item.start, item.end_inclusive)}" for item in upcoming)
    lines.append(
        "Изменение правила периода затрагивает всех участников и применяется "
        "со следующей границы: текущий период не пересчитывается задним числом."
    )
    return [
        Reply(
            text="\n".join(lines),
            buttons=((Button("← Настройки", callback("menu", "settings")),),),
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
        buttons = ((Button("← Мои настройки", callback("set", "personal")),),)
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
                (Button("Подтвердить удаление", callback("set", "accgo")),),
                (Button("← Мои настройки", callback("set", "personal")),),
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
        return [Reply(text="Удаление начато, но часть членств ещё активна. Повторите позже.")]
    return [
        Reply(
            text=(
                "Аккаунт удалён: доступ к бюджетам прекращён.\n"
                "Совместные записи сохранены у остальных участников в обезличенном виде."
            )
        )
    ]
