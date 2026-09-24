"""Диалог мастера создания бюджета и вход по коду (FR-84, FR-78)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from fintracker.application.conversation import onboarding_limits
from fintracker.application.conversation.keyboards import Button, callback, start_menu
from fintracker.application.conversation.types import IncomingMessage, Reply
from fintracker.application.identity.invites import accept_invite, preview_invite
from fintracker.application.onboarding.wizard import (
    STEP_ORDER,
    DraftCategory,
    WizardState,
    WizardStep,
    check_funding,
    get_or_create_draft,
    preview_periods,
    publish_workspace,
    save_draft,
)
from fintracker.config import Settings
from fintracker.core.calendar import (
    CalendarError,
    PeriodPolicy,
    RepeatMode,
    infer_policy_options,
    validate_timezone,
)
from fintracker.core.errors import DomainError, ValidationFailed
from fintracker.core.money import Money, MoneyError, normalize_currency
from fintracker.db.models.access import BudgetSetupDraft, User
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.parsing.amounts import parse_amounts
from fintracker.domain.parsing.dates import resolve_date_expression

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_CURRENCY = "RUB"

# Общий шаблон категорий предлагается, но не навязывается (FR-84).
COMMON_CATEGORY_TEMPLATE: tuple[str, ...] = (
    "Продукты питания",
    "Рестораны",
    "Транспорт",
    "Жильё",
    "Связь",
    "Здоровье",
    "Одежда",
    "Досуг",
    "Подписки",
    "Другое",
)


def _income_period_label(state: WizardState) -> tuple[str, bool]:
    """Return the visible income basis and whether it is a monthly amount."""
    monthly = state.repeat_mode is RepeatMode.CALENDAR_MONTHS and state.repeat_interval == 1
    if monthly:
        return "за месяц", True
    if state.start_date is not None and state.end_inclusive is not None:
        return (
            "за первый период "
            f"{state.start_date.strftime('%d.%m.%Y')} — "
            f"{state.end_inclusive.strftime('%d.%m.%Y')}",
            False,
        )
    return "за первый период", False


async def has_active_wizard(settings: Settings, *, user_id: uuid.UUID) -> bool:
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        row = (
            await session.execute(
                select(BudgetSetupDraft.id).where(
                    BudgetSetupDraft.owner_user_id == user_id,
                    BudgetSetupDraft.state == "draft",
                    BudgetSetupDraft.step != WizardStep.DONE.value,
                )
            )
        ).scalar_one_or_none()
        return row is not None


async def wizard_help(settings: Settings, *, user_id: uuid.UUID) -> list[Reply] | None:
    """Show instructions and valid input for the participant's current step."""
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        draft = (
            await session.execute(
                select(BudgetSetupDraft)
                .where(
                    BudgetSetupDraft.owner_user_id == user_id,
                    BudgetSetupDraft.state == "draft",
                )
                .order_by(BudgetSetupDraft.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if draft is None or draft.step == WizardStep.DONE.value:
            return None
        step = WizardStep(draft.step)
        state = WizardState.from_payload(dict(draft.payload))
    replies = _prompt_for(step, state)
    return [
        Reply(
            text="❔ Помощь по текущему шагу\n\n"
            + reply.text
            + "\n\n/cancel — отменить настройку.",
            buttons=reply.buttons,
            retry_input=reply.retry_input,
        )
        for reply in replies
    ]


async def cancel_wizard(settings: Settings, *, user_id: uuid.UUID) -> bool:
    """Stop setup without touching any published budget or its transactions."""
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        rows = (
            (
                await session.execute(
                    select(BudgetSetupDraft)
                    .where(
                        BudgetSetupDraft.owner_user_id == user_id,
                        BudgetSetupDraft.state == "draft",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for draft in rows:
            draft.state = "cancelled"
            draft.version += 1
        return bool(rows)


async def start_wizard(settings: Settings, *, user_id: uuid.UUID) -> list[Reply]:
    """Start a new setup, offering a choice if one already exists."""
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        existing = (
            await session.execute(
                select(BudgetSetupDraft)
                .where(
                    BudgetSetupDraft.owner_user_id == user_id,
                    BudgetSetupDraft.state == "draft",
                )
                .order_by(BudgetSetupDraft.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return [
                Reply(
                    text=(
                        "📒 У вас есть незавершённая настройка\n\n"
                        "Продолжите её или начните заново. При новом старте прежняя "
                        "настройка будет отменена. Опубликованные бюджеты не изменятся."
                    ),
                    buttons=(
                        (Button("▶️ Продолжить настройку", callback("wiz", "resume")),),
                        (
                            Button(
                                "🔄 Начать заново", callback("wiz", "restart", existing.id.hex[:16])
                            ),
                        ),
                    ),
                )
            ]
        draft, state = await get_or_create_draft(session, owner_user_id=user_id)
        step = WizardStep(draft.step)
    return _prompt_for(step, state)


async def start_join_flow(settings: Settings, *, user_id: uuid.UUID) -> list[Reply]:
    # Незавершённая настройка своего бюджета при этом сохраняется: код
    # распознаётся по виду и не попадает в шаг мастера.
    del settings, user_id
    return [
        Reply(
            text=(
                "🔑 Войти в общий бюджет\n\nОтправьте сюда код приглашения, который "
                "прислал администратор бюджета. Например: ABCD-EFGH-JKMN.\n\n"
                "Если вам прислали ссылку — просто откройте её."
            )
        )
    ]


async def submit_join_code(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    message: IncomingMessage,
    raw_code: str | None = None,
    code_digest: str | None = None,
) -> list[Reply]:
    """Показать бюджет до подтверждения, затем присоединить (FR-78).

    Отложенная обработка получает проверочное значение кода: открытый секрет
    не сохраняется в технических таблицах (SEC-04).
    """
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        session.expunge(user)
    try:
        preview = await preview_invite(
            settings, user=user, raw_code=raw_code, code_digest=code_digest
        )
    except DomainError as exc:
        return [
            Reply(
                text=(
                    f"⚠️ {exc.message}\n\nПроверьте код или попросите у администратора "
                    "новый — у приглашения есть срок действия."
                ),
                buttons=start_menu(returning=False),
            )
        ]

    if preview.already_member:
        return [
            Reply(
                text=f"ℹ️ Вы уже участник бюджета «{preview.workspace_name}».",
                buttons=(
                    (
                        Button(
                            "📒 Открыть бюджет",
                            callback("ws", "use", preview.workspace_id.hex[:16]),
                        ),
                    ),
                ),
            )
        ]

    result = await accept_invite(
        settings,
        user=user,
        raw_code=raw_code,
        code_digest=code_digest,
        correlation_id=message.correlation_id or uuid.uuid4().hex,
    )
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=result.workspace_id
    ) as session:
        from fintracker.application.identity.actor import set_active_workspace

        await set_active_workspace(session, user=user, workspace_id=result.workspace_id)
    from fintracker.application.identity.profile import ensure_member_profile

    await ensure_member_profile(
        settings, user_id=user_id, workspace_id=result.workspace_id, name=message.display_name
    )
    return [
        Reply(
            text=(
                f"🤝 Вы присоединились к бюджету «{result.workspace_name}»!\n\n"
                "Вам видна общая история, а ваши записи увидят остальные участники.\n\n"
                "Чтобы записать трату, просто напишите её: «кофе 250»."
            ),
            buttons=(
                (
                    Button("📒 Открыть бюджет", callback("menu", "budget")),
                    Button("➕ Добавить трату", callback("menu", "add")),
                ),
            ),
        )
    ]


# Повторение периода — часть шага «Период»: номер шага не перескакивает,
# если готовый цикл выбран кнопкой.
_VISIBLE_STEPS = tuple(step for step in STEP_ORDER if step is not WizardStep.PERIOD_REPEAT)


def _wizard_progress(step: WizardStep) -> str:
    if step not in STEP_ORDER:
        return ""
    shown = WizardStep.PERIOD_DATES if step is WizardStep.PERIOD_REPEAT else step
    return f"Шаг {_VISIBLE_STEPS.index(shown) + 1} из {len(_VISIBLE_STEPS)}"


def _days_word(count: int) -> str:
    from fintracker.application.conversation.views import plural

    return plural(count, "день", "дня", "дней")


def _categories_word(count: int) -> str:
    from fintracker.application.conversation.views import plural

    return plural(count, "категорию", "категории", "категорий")


def _local_today(state: WizardState) -> dt.date:
    """Сегодня в выбранном часовом поясе, а не по часам сервера."""
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(state.timezone or DEFAULT_TIMEZONE)).date()


def _date(value: dt.date) -> str:
    from fintracker.application.delivery.render import format_date

    return format_date(value, with_year=True)


def _prompt_for(step: WizardStep, state: WizardState) -> list[Reply]:
    """Render one consistent wizard screen with progress and safe navigation."""
    replies = _prompt_body(step, state)
    if step not in STEP_ORDER:
        return replies
    progress = _wizard_progress(step)
    result: list[Reply] = []
    for reply in replies:
        buttons = list(reply.buttons)
        if step is not WizardStep.NAME:
            buttons.append((Button("← Назад", callback("wiz", "back", step.value)),))
        result.append(
            Reply(
                text=f"{progress}\n\n{reply.text}",
                buttons=tuple(buttons),
                transaction_id=reply.transaction_id,
                immediate=reply.immediate,
                retry_input=reply.retry_input,
            )
        )
    return result


def _prompt_body(step: WizardStep, state: WizardState) -> list[Reply]:
    match step:
        case WizardStep.NAME:
            return [
                Reply(
                    text=(
                        "📒 Давайте создадим бюджет\n\nКак его назвать?\nНапример: «Наш "
                        "общий бюджет» или «Личный».\n\n✍️ Отправьте название одним "
                        "сообщением."
                    )
                )
            ]
        case WizardStep.CURRENCY:
            return [
                Reply(
                    text=(
                        "💱 Валюта бюджета\n\nВ какой валюте вести учёт? Выберите кнопкой "
                        "или отправьте код, например KZT. Позже валюту поменять нельзя.\n\n"
                        "⚡ Не хотите настраивать всё сейчас? «Быстрый старт» создаст бюджет "
                        "в рублях на календарный месяц с готовыми категориями — всё можно "
                        "поменять на последнем шаге."
                    ),
                    buttons=(
                        (
                            Button("₽ RUB", callback("wiz", "cur", "RUB")),
                            Button("$ USD", callback("wiz", "cur", "USD")),
                            Button("€ EUR", callback("wiz", "cur", "EUR")),
                        ),
                        (Button("⚡ Быстрый старт", callback("wiz", "quick")),),
                    ),
                )
            ]
        case WizardStep.TIMEZONE:
            return [
                Reply(
                    text=(
                        "🌍 Часовой пояс\n\nОт него зависят даты трат и время напоминаний. "
                        "Выберите ближайший город."
                    ),
                    buttons=(
                        (
                            Button("Москва · UTC+3", callback("wiz", "tz", "msk")),
                            Button("Екатеринбург · UTC+5", callback("wiz", "tz", "ekb")),
                        ),
                        (
                            Button("Новосибирск · UTC+7", callback("wiz", "tz", "nsk")),
                            Button("UTC", callback("wiz", "tz", "utc")),
                        ),
                        (Button("Другой часовой пояс", callback("wiz", "tz", "custom")),),
                    ),
                )
            ]
        case WizardStep.PERIOD_DATES:
            return [
                Reply(
                    text=(
                        "📅 Период бюджета\n\nНа какой срок планировать лимиты? Новый период "
                        "будет начинаться автоматически.\n\n«С 10-го по 9-е» удобно, если "
                        "зарплата приходит 10-го числа."
                    ),
                    buttons=(
                        (
                            Button("Календарный месяц", callback("wiz", "period", "month")),
                            Button("Неделя", callback("wiz", "period", "week")),
                        ),
                        (Button("С 10-го по 9-е", callback("wiz", "period", "10to9")),),
                        (Button("Свои даты", callback("wiz", "period", "custom")),),
                    ),
                )
            ]
        case WizardStep.PERIOD_REPEAT:
            options = _repeat_options(state)
            lines = [
                (
                    "🔁 Повторение бюджета\n\nКак повторять периоды? Ниже видно, когда "
                    "начнётся следующий.\n"
                )
            ]
            buttons: list[tuple[Button, ...]] = []
            for index, option in enumerate(options):
                preview = option.preview(2)
                lines.append(
                    f"{index + 1}. {option.describe()} → следующий "
                    f"{_date(preview[1].start)} — {_date(preview[1].end_inclusive)}"
                )
                buttons.append((Button(option.describe(), callback("wiz", "rep", str(index))),))
            return [Reply(text="\n".join(lines), buttons=tuple(buttons))]
        case WizardStep.INCOME:
            income_label, _ = _income_period_label(state)
            if state.income_precision in {"exact", "estimate"}:
                exact = state.income_precision == "exact"
                heading = "🎯 Выбран точный план" if exact else "≈ Выбрана примерная оценка"
                switch_label = "≈ Сделать примерным" if exact else "🎯 Сделать точным"
                switch_value = "estimate" if exact else "exact"
                currency = state.currency or DEFAULT_CURRENCY
                return [
                    Reply(
                        text=(
                            f"{heading}\n\n"
                            f"✍️ Отправьте сумму дохода {income_label} в {currency}.\n"
                            "Например: 120000\n\n"
                            "Это только план — с ним бот сравнит лимиты. Сами поступления "
                            "записываются сообщением, например «зарплата 120000»."
                        ),
                        buttons=(
                            (Button(switch_label, callback("wiz", "inc", switch_value)),),
                            (Button("Укажу позже →", callback("wiz", "inc", "later")),),
                        ),
                    )
                ]
            return [
                Reply(
                    text=(
                        "💰 Планируемый доход\n\n"
                        f"Сколько вы ожидаете получить {income_label}? Бот сравнит доход с "
                        "лимитами и предупредит, если планируете потратить больше.\n\n"
                        "Сначала выберите, точная это сумма или примерная. Можно пропустить."
                    ),
                    buttons=(
                        (
                            Button("🎯 Точный план", callback("wiz", "inc", "exact")),
                            Button("≈ Примерная оценка", callback("wiz", "inc", "estimate")),
                        ),
                        (Button("Укажу позже →", callback("wiz", "inc", "later")),),
                    ),
                )
            ]
        case WizardStep.CATEGORIES:
            return [
                Reply(
                    text=(
                        "🗂 Категории расходов\n\nНа что обычно уходят деньги? Перечислите"
                        " категории через запятую.\n\nНапример: Продукты, Кафе, "
                        "Транспорт, Жильё\n\nМожно взять готовый шаблон или начать с "
                        "нуля. Категории легко изменить позже."
                    ),
                    buttons=(
                        (
                            Button("🗂 Готовый шаблон", callback("wiz", "cats", "template")),
                            Button("➕ С нуля", callback("wiz", "cats", "empty")),
                        ),
                    ),
                )
            ]
        case WizardStep.LIMITS:
            return [onboarding_limits.prompt(state)]
        case WizardStep.COMMITMENTS:
            return [
                Reply(
                    text=(
                        "🗓 Регулярные платежи\n\nАренда, интернет, подписки — бот напомнит о "
                        "сроке и запишет оплату одним нажатием.\n\nПроще добавить их потом в "
                        "разделе «Платежи». Если хотите сейчас — отправьте по одному в строке:\n"
                        "Интернет = 900 = 20.09"
                    ),
                    buttons=(
                        (Button("Настроить позже →", callback("wiz", "skip", "commitments")),),
                    ),
                )
            ]
        case WizardStep.GOALS:
            return [
                Reply(
                    text=(
                        "🎯 На что будем копить?\n\nЦели удобнее добавить потом в разделе "
                        "«Цели». Если хотите сейчас — отправьте по одной в строке:\n"
                        "Отпуск = 100000"
                    ),
                    buttons=((Button("Настроить позже →", callback("wiz", "skip", "goals")),),),
                )
            ]
        case WizardStep.TEMPLATE:
            return [
                Reply(
                    text=(
                        "🔁 Повторять лимиты каждый период?\n\nВ новом периоде будут те же "
                        "лимиты — не придётся настраивать их заново. Траты и доходы, "
                        "конечно, начнутся с нуля."
                    ),
                    buttons=(
                        (
                            Button("🔁 Повторять", callback("wiz", "tpl", "on")),
                            Button("Не повторять", callback("wiz", "tpl", "off")),
                        ),
                    ),
                )
            ]
        case WizardStep.REVIEW:
            return [_review_reply(state)]
        case _:
            return [Reply(text="✅ Настройка завершена\n\nМожно переходить к учёту расходов.")]


def _repeat_options(state: WizardState) -> list[PeriodPolicy]:
    if state.start_date is None or state.end_inclusive is None or state.timezone is None:
        return []
    return infer_policy_options(state.start_date, state.end_inclusive, state.timezone)


def _review_reply(state: WizardState) -> Reply:
    """Предпросмотр и явный показ дефицита (FR-84, FR-62)."""
    currency = state.currency or DEFAULT_CURRENCY
    funding = check_funding(state)
    from fintracker.application.conversation.sections import timezone_label

    lines = [
        "📋 Проверьте настройки",
        "",
        f"Бюджет: {state.name}",
        f"Валюта: {currency}",
        f"Часовой пояс: {timezone_label(state.timezone or DEFAULT_TIMEZONE)}",
        "",
    ]
    if state.start_date and state.end_inclusive:
        days = (state.end_inclusive - state.start_date).days + 1
        lines.append(
            f"📅 Первый период: {_date(state.start_date)} — {_date(state.end_inclusive)} "
            f"({days} {_days_word(days)})"
        )
    upcoming = preview_periods(state)
    if upcoming:
        lines.append(
            "Дальше: "
            + ", ".join(
                f"{_date(item.start)} — {_date(item.end_inclusive)}" for item in upcoming[:2]
            )
        )
    lines.extend(["", "💰 План"])
    if funding.income_known and funding.income_minor is not None:
        lines.append(f"Доход: {Money(funding.income_minor, currency).format()}")
    else:
        lines.append("Доход: не указан")
    lines.append(
        f"Лимиты: {Money(funding.limits_total_minor, currency).format()} "
        f"на {len(state.categories)} {_categories_word(len(state.categories))}"
    )
    if funding.deficit_minor:
        lines.append(
            f"\n⚠️ Лимиты больше дохода на {Money(funding.deficit_minor, currency).format()}\n"
            "Уменьшите лимиты или подтвердите, что так и задумано."
        )
    lines.append(
        "🔁 Лимиты каждый период: " + ("повторять" if state.repeat_template else "не повторять")
    )
    lines.extend(["", "Всё верно? Нажмите «Создать бюджет» или исправьте нужный пункт."])

    buttons: list[tuple[Button, ...]] = [(Button("➕ Создать бюджет", callback("wiz", "publish")),)]
    if funding.deficit_minor and not state.deficit_accepted:
        buttons.insert(0, (Button("⚠️ Так и задумано", callback("wiz", "deficit")),))
    buttons.extend(
        [
            (
                Button("✏️ Название", callback("wiz", "edit", "name")),
                Button("💱 Валюта", callback("wiz", "edit", "currency")),
            ),
            (
                Button("🌍 Часовой пояс", callback("wiz", "edit", "timezone")),
                Button("📅 Период", callback("wiz", "edit", "period_dates")),
            ),
            (
                Button("💰 Доход", callback("wiz", "edit", "income")),
                Button("🗂 Категории", callback("wiz", "edit", "categories")),
            ),
            (Button("💳 Лимиты", callback("wiz", "edit", "limits")),),
        ]
    )
    return Reply(text="\n".join(lines), buttons=tuple(buttons))


# Брошенная настройка не перехватывает траты: через полчаса без ответа
# текст снова разбирается как обычно, а настройку можно продолжить из /start.
WIZARD_IDLE = dt.timedelta(minutes=30)
# На этих шагах ответ обычно выбирается кнопкой: фраза «кофе 250» здесь —
# трата в уже существующий бюджет, а не ответ мастеру.
_BUTTON_STEPS = frozenset(
    {
        WizardStep.CURRENCY,
        WizardStep.TIMEZONE,
        WizardStep.PERIOD_REPEAT,
        WizardStep.INCOME,
        WizardStep.TEMPLATE,
        WizardStep.REVIEW,
    }
)


async def continue_wizard_input(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    message: IncomingMessage,
    has_budget: bool = False,
) -> list[Reply] | None:
    """Обработать текстовый ответ на шаг мастера.

    Возвращает None, если текст не относится к мастеру: новая законченная
    фраза о покупке создаёт отдельный ввод и не затирает настройку (R07).
    """
    from fintracker.application.conversation.guards import looks_like_new_entry

    text = (message.text or "").strip()
    if not text:
        return None
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        draft, state = await get_or_create_draft(session, owner_user_id=user_id)
        step = WizardStep(draft.step)
        if step is WizardStep.DONE:
            return None
        if has_budget:
            idle = dt.datetime.now(dt.UTC) - draft.updated_at > WIZARD_IDLE
            income_amount = step is WizardStep.INCOME and state.income_precision is not None
            if idle or (step in _BUTTON_STEPS and not income_amount and looks_like_new_entry(text)):
                return None
        next_step, replies = _apply_input(step, state, text)
        if next_step is None:
            return replies
        await save_draft(session, draft, state, step=next_step)
    return replies or _prompt_for(next_step, state)


def _apply_input(
    step: WizardStep, state: WizardState, text: str
) -> tuple[WizardStep | None, list[Reply] | None]:
    match step:
        case WizardStep.NAME:
            state.name = text[:120]
            return _after_review_edit(state, WizardStep.NAME, WizardStep.CURRENCY), None
        case WizardStep.CURRENCY:
            try:
                state.currency = normalize_currency(text)
            except MoneyError:
                return None, [
                    Reply(text="⚠️ Не узнал валюту.\n\nУкажите трёхбуквенный код, например RUB.")
                ]
            return _after_review_edit(state, WizardStep.CURRENCY, WizardStep.TIMEZONE), None
        case WizardStep.TIMEZONE:
            try:
                state.timezone = validate_timezone(text)
            except CalendarError:
                return None, [
                    Reply(
                        text=(
                            "⚠️ Не узнал часовой пояс.\n\nВведите международное название, например "
                            "Asia/Novosibirsk."
                        )
                    )
                ]
            return _after_review_edit(state, WizardStep.TIMEZONE, WizardStep.PERIOD_DATES), None
        case WizardStep.PERIOD_DATES:
            parsed = _parse_period_dates(text)
            if parsed is None:
                return None, [
                    Reply(
                        text=(
                            "⚠️ Не удалось разобрать даты.\n\nФормат: ДД.ММ.ГГГГ — "
                            "ДД.ММ.ГГГГ.\n\nНапример: 10.09.2026 — 09.10.2026."
                        )
                    )
                ]
            start, end = parsed
            if end < start:
                return None, [Reply(text="ℹ️ Дата конца раньше даты начала.\n\nПовторите ввод.")]
            state.start_date, state.end_inclusive = start, end
            return WizardStep.PERIOD_REPEAT, None
        case WizardStep.PERIOD_REPEAT:
            return None, _prompt_for(WizardStep.PERIOD_REPEAT, state)
        case WizardStep.INCOME:
            amounts = parse_amounts(text)
            if not amounts:
                return None, [
                    Reply(text="✍️ Укажите сумму дохода числом либо нажмите «Укажу позже».")
                ]
            currency = state.currency or DEFAULT_CURRENCY
            income_minor = Money.from_decimal(Decimal(amounts[0].value), currency).minor
            _, monthly = _income_period_label(state)
            if monthly:
                state.income_monthly_minor = income_minor
                state.income_period_minor = None
            else:
                state.income_monthly_minor = None
                state.income_period_minor = income_minor
            state.income_precision = state.income_precision or "estimate"
            return _after_review_edit(state, WizardStep.INCOME, WizardStep.CATEGORIES), None
        case WizardStep.CATEGORIES:
            names = [part.strip() for part in text.split(",") if part.strip()]
            if not names:
                return None, [Reply(text="✍️ Перечислите категории через запятую.")]
            state.categories = [DraftCategory(name=name[:120]) for name in names]
            state.limit_category = None
            state.limits_page = 0
            state.deficit_accepted = False
            return WizardStep.LIMITS, None
        case WizardStep.LIMITS:
            if state.limit_category is not None:
                return WizardStep.LIMITS, [onboarding_limits.apply_amount(state, text)]
            if not any(
                category.name.casefold() in text.casefold() for category in state.categories
            ):
                return None, [
                    onboarding_limits.prompt(
                        state, notice="👆 Сначала выберите категорию кнопкой ниже."
                    )
                ]
            errors = _apply_limits(state, text)
            if errors:
                return None, [Reply(text="✍️ Проверьте ввод\n\n" + "\n\n".join(errors))]
            return _after_review_edit(state, WizardStep.LIMITS, WizardStep.COMMITMENTS), None
        case WizardStep.COMMITMENTS:
            # Введённые обязательства сохраняются и создаются вместе с
            # бюджетом: набранный текст не теряется (FR-84, G-14).
            entries, errors = _parse_planned(text)
            if errors:
                return None, [Reply(text="✍️ Проверьте ввод\n\n" + "\n\n".join(errors))]
            for entry in entries:
                due = entry.get("due")
                if due and state.start_date is not None:
                    parsed_due = resolve_date_expression(
                        str(due), reference=state.start_date, prefer_future=True
                    )
                    if parsed_due is None:
                        return None, [
                            Reply(
                                text=(
                                    f"⚠️ Не удалось разобрать дату платежа «{entry['name']}».\n\n"
                                    "Укажите существующую дату в формате ДД.ММ или ДД.ММ.ГГГГ."
                                ),
                            )
                        ]
            state.commitments = entries
            return WizardStep.GOALS, None
        case WizardStep.GOALS:
            entries, errors = _parse_planned(text)
            if errors:
                return None, [Reply(text="✍️ Проверьте ввод\n\n" + "\n\n".join(errors))]
            state.goals = entries
            return WizardStep.TEMPLATE, None
        case WizardStep.TEMPLATE:
            state.repeat_template = text.strip().lower() not in {"нет", "не повторять", "off"}
            return WizardStep.REVIEW, None
        case WizardStep.REVIEW:
            return None, [_review_reply(state)]
        case _:
            return None, None


def _after_review_edit(
    state: WizardState, completed: WizardStep, normal_next: WizardStep
) -> WizardStep:
    if state.return_to_review_after == completed.value:
        state.return_to_review_after = None
        return WizardStep.REVIEW
    return normal_next


def _parse_planned(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Разобрать строки «Название = сумма [= дата]» мастера (FR-84, G-14)."""
    from decimal import Decimal

    from fintracker.domain.parsing.amounts import parse_amounts

    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for raw in text.replace(";", "\n").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [item.strip() for item in line.split("=")]
        if not parts[0]:
            errors.append(f"Не понял строку «{line}»: нужно «Название = сумма».")
            continue
        amounts = parse_amounts(parts[1]) if len(parts) > 1 else []
        if not amounts:
            errors.append(f"Не понял сумму в строке «{line}».")
            continue
        entry: dict[str, Any] = {
            "name": parts[0][:120],
            "amount_decimal": str(Decimal(amounts[0].value)),
        }
        if len(parts) > 2 and parts[2]:
            entry["due"] = parts[2]
        entries.append(entry)
    return entries, errors


def _parse_period_dates(text: str) -> tuple[dt.date, dt.date] | None:
    normalized = text.replace("—", "-").replace("–", "-")
    parts = [part.strip() for part in normalized.split("-") if part.strip()]
    if len(parts) != 2:
        return None
    today = dt.date.today()
    start = resolve_date_expression(parts[0], reference=today)
    end = resolve_date_expression(parts[1], reference=today)
    if start is None or end is None:
        return None
    return start.value, end.value


def _split_name_and_amount(line: str, by_name: dict[str, DraftCategory]) -> tuple[str, str]:
    """Разделить «Категория 20000» на известное название и сумму (FR-84)."""
    words = line.split()
    for count in range(len(words) - 1, 0, -1):
        candidate = " ".join(words[:count])
        if candidate.casefold() in by_name:
            return candidate, " ".join(words[count:])
    return "", ""


def _apply_limits(state: WizardState, text: str) -> list[str]:
    errors: list[str] = []
    currency = state.currency or DEFAULT_CURRENCY
    by_name = {item.name.casefold(): item for item in state.categories}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line:
            name, _, value = line.partition("=")
        else:
            # Форма «Категория 20000» принимается наравне с «Категория = 20000»:
            # молча терять заданный лимит нельзя.
            name, value = _split_name_and_amount(line, by_name)
            if not name:
                errors.append(f"Строка «{line}» не разобрана: укажите «Категория = сумма»")
                continue
        target = by_name.get(name.strip().casefold())
        if target is None:
            errors.append(f"Категория «{name.strip()}» не найдена в списке")
            continue
        amounts = parse_amounts(value)
        if not amounts:
            errors.append(f"Не удалось разобрать лимит для «{name.strip()}»")
            continue
        try:
            target.limit_minor = Money.from_decimal(Decimal(amounts[0].value), currency).minor
        except MoneyError as exc:
            errors.append(f"{name.strip()}: {exc}")
    return errors


async def apply_wizard_choice(
    settings: Settings, *, user_id: uuid.UUID, action: str, value: str, message: IncomingMessage
) -> list[Reply]:
    """Обработать нажатие кнопки мастера."""
    if action == "start":
        return await start_wizard(settings, user_id=user_id)
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        draft = (
            await session.execute(
                select(BudgetSetupDraft)
                .where(
                    BudgetSetupDraft.owner_user_id == user_id,
                    BudgetSetupDraft.state == "draft",
                )
                .order_by(BudgetSetupDraft.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if draft is None:
            return [
                Reply(
                    text=(
                        "ℹ️ Эта настройка уже завершена или отменена.\n\nМожно создать новый бюджет."
                    ),
                    buttons=start_menu(returning=True),
                )
            ]
        if action == "restart":
            if value != draft.id.hex[:16]:
                return [Reply(text="🔄 Настройка уже изменилась. Откройте /start заново.")]
            draft.state = "cancelled"
            draft.version += 1
            await session.flush()
            draft, state = await get_or_create_draft(session, owner_user_id=user_id)
            return _prompt_for(WizardStep.NAME, state)
        state = WizardState.from_payload(dict(draft.payload))
        draft_id = draft.id
        step = WizardStep(draft.step)
        next_step: WizardStep | None = None
        expected_step = {
            "cur": WizardStep.CURRENCY,
            "tz": WizardStep.TIMEZONE,
            "period": WizardStep.PERIOD_DATES,
            "rep": WizardStep.PERIOD_REPEAT,
            "tpl": WizardStep.TEMPLATE,
            "deficit": WizardStep.REVIEW,
            "publish": WizardStep.REVIEW,
        }.get(action)
        if expected_step is not None and step is not expected_step:
            return _prompt_for(step, state)

        if action in onboarding_limits.ACTIONS:
            if step is not WizardStep.LIMITS:
                return _prompt_for(step, state)
            done = onboarding_limits.choose(state, action=action, value=value)
            next_step = (
                _after_review_edit(state, WizardStep.LIMITS, WizardStep.COMMITMENTS)
                if done
                else WizardStep.LIMITS
            )
            await save_draft(session, draft, state, step=next_step)
            return _prompt_for(next_step, state)

        match action:
            case "resume":
                return _prompt_for(step, state)
            case "quick":
                # Быстрый старт: разумные значения и сразу проверка настроек.
                if step not in {WizardStep.CURRENCY, WizardStep.TIMEZONE, WizardStep.PERIOD_DATES}:
                    return _prompt_for(step, state)
                state.currency = state.currency or DEFAULT_CURRENCY
                state.timezone = state.timezone or DEFAULT_TIMEZONE
                today = _local_today(state)
                state.start_date = today.replace(day=1)
                state.repeat_mode = RepeatMode.CALENDAR_MONTHS
                state.repeat_interval = 1
                state.end_inclusive = state.policy().period(0).end_inclusive
                if not state.categories:
                    state.categories = [
                        DraftCategory(name=name) for name in COMMON_CATEGORY_TEMPLATE
                    ]
                state.repeat_template = True
                state.limit_category = None
                next_step = WizardStep.REVIEW
            case "cur":
                state.currency = normalize_currency(value)
                next_step = _after_review_edit(state, WizardStep.CURRENCY, WizardStep.TIMEZONE)
            case "tz":
                timezones = {
                    "msk": "Europe/Moscow",
                    "ekb": "Asia/Yekaterinburg",
                    "nsk": "Asia/Novosibirsk",
                    "utc": "UTC",
                    "default": DEFAULT_TIMEZONE,
                }
                if value == "custom":
                    return [
                        Reply(
                            text=(
                                f"{_wizard_progress(WizardStep.TIMEZONE)}\n\n"
                                "🌍 Другой часовой пояс\n\nОтправьте международное "
                                "название, например Asia/Irkutsk или Europe/Moscow.\n\n"
                                "/cancel — отменить настройку."
                            ),
                            buttons=((Button("← Назад", callback("wiz", "back", step.value)),),),
                        )
                    ]
                state.timezone = timezones.get(value, DEFAULT_TIMEZONE)
                next_step = _after_review_edit(state, WizardStep.TIMEZONE, WizardStep.PERIOD_DATES)
            case "period":
                if value == "custom":
                    return [
                        Reply(
                            text=(
                                f"{_wizard_progress(WizardStep.PERIOD_DATES)}\n\n"
                                "📅 Свой период\n\nОтправьте начало и конец одним сообщением:\n"
                                "10.09.2026 — 09.10.2026\n\nОбе даты входят в период."
                            ),
                            buttons=((Button("← Назад", callback("wiz", "back", step.value)),),),
                        )
                    ]
                today = _local_today(state)
                if value == "month":
                    start = today.replace(day=1)
                    if today.month == 12:
                        boundary = dt.date(today.year + 1, 1, 1)
                    else:
                        boundary = dt.date(today.year, today.month + 1, 1)
                    state.start_date = start
                    state.end_inclusive = boundary - dt.timedelta(days=1)
                    state.repeat_mode = RepeatMode.CALENDAR_MONTHS
                    state.repeat_interval = 1
                elif value == "week":
                    start = today - dt.timedelta(days=today.weekday())
                    state.start_date = start
                    state.end_inclusive = start + dt.timedelta(days=6)
                    state.repeat_mode = RepeatMode.FIXED_DAYS
                    state.repeat_interval = 7
                elif value == "10to9":
                    if today.day >= 10:
                        start = today.replace(day=10)
                    else:
                        previous = today.replace(day=1) - dt.timedelta(days=1)
                        start = previous.replace(day=10)
                    state.start_date = start
                    state.repeat_mode = RepeatMode.CALENDAR_MONTHS
                    state.repeat_interval = 1
                    state.end_inclusive = state.policy().period(0).end_inclusive
                else:
                    return _prompt_for(step, state)
                state.income_monthly_minor = None
                state.income_period_minor = None
                next_step = WizardStep.INCOME
            case "rep":
                options = _repeat_options(state)
                index = int(value) if value.isdigit() else 0
                if not options or index >= len(options):
                    return [Reply(text="✍️ Сначала выберите даты первого периода.")]
                chosen = options[index]
                state.repeat_mode = chosen.mode
                state.repeat_interval = chosen.interval
                # A previously entered amount belongs to the old cycle basis.
                # Never reinterpret it silently after the user changes the cycle.
                state.income_monthly_minor = None
                state.income_period_minor = None
                # Конец первого периода приводится в соответствие выбранному
                # правилу явно, без молчаливой подмены (FR-90, A205).
                state.end_inclusive = chosen.period(0).end_inclusive
                next_step = WizardStep.INCOME
            case "inc":
                # Старые кнопки не возвращают уже пройденный мастер к доходу.
                if step is not WizardStep.INCOME or value not in {"exact", "estimate", "later"}:
                    return _prompt_for(step, state)
                if value == "later":
                    state.income_precision = None
                    state.income_monthly_minor = None
                    state.income_period_minor = None
                    next_step = _after_review_edit(state, WizardStep.INCOME, WizardStep.CATEGORIES)
                else:
                    state.income_precision = value
                    next_step = WizardStep.INCOME
            case "cats":
                if step is not WizardStep.CATEGORIES:
                    return _prompt_for(step, state)
                state.limit_category = None
                state.limits_page = 0
                if value == "template":
                    state.categories = [
                        DraftCategory(name=name) for name in COMMON_CATEGORY_TEMPLATE
                    ]
                    next_step = WizardStep.LIMITS
                else:
                    state.categories = []
                    next_step = WizardStep.CATEGORIES
            case "skip":
                if value != step.value:
                    return _prompt_for(step, state)
                state.limit_category = None
                order = list(STEP_ORDER)
                next_step = order[min(order.index(step) + 1, len(order) - 1)]
            case "tpl":
                state.repeat_template = value == "on"
                next_step = WizardStep.REVIEW
            case "deficit":
                state.deficit_accepted = True
                state.deficit_reason = "Принято осознанно при создании бюджета"
                next_step = WizardStep.REVIEW
            case "back":
                if value and value != step.value:
                    return _prompt_for(step, state)
                state.limit_category = None
                order = list(STEP_ORDER)
                next_step = order[max(order.index(step) - 1, 0)]
            case "edit":
                if step is not WizardStep.REVIEW:
                    return _prompt_for(step, state)
                edit_steps = {
                    "name": WizardStep.NAME,
                    "currency": WizardStep.CURRENCY,
                    "timezone": WizardStep.TIMEZONE,
                    "period_dates": WizardStep.PERIOD_DATES,
                    "income": WizardStep.INCOME,
                    "categories": WizardStep.CATEGORIES,
                    "limits": WizardStep.LIMITS,
                }
                target = edit_steps.get(value)
                if target is None:
                    return _prompt_for(step, state)
                if target is WizardStep.PERIOD_DATES:
                    state.return_to_review_after = WizardStep.INCOME.value
                elif target is WizardStep.CATEGORIES:
                    state.return_to_review_after = WizardStep.LIMITS.value
                else:
                    state.return_to_review_after = target.value
                next_step = target
            case "publish":
                await save_draft(session, draft, state, step=WizardStep.REVIEW)
            case _:
                return [Reply(text="ℹ️ Неизвестное действие мастера.")]

        if next_step is not None:
            await save_draft(session, draft, state, step=next_step)

    if action == "publish":
        return await _publish(settings, user_id=user_id, draft_id=draft_id, message=message)
    assert next_step is not None
    return _prompt_for(next_step, state)


async def _publish(
    settings: Settings, *, user_id: uuid.UUID, draft_id: uuid.UUID, message: IncomingMessage
) -> list[Reply]:
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        session.expunge(user)
    try:
        workspace_id = await publish_workspace(
            settings,
            user=user,
            draft_id=draft_id,
            correlation_id=message.correlation_id or uuid.uuid4().hex,
        )
    except ValidationFailed as exc:
        return [Reply(text=f"⚠️ Не удалось создать бюджет: {exc.message}")]

    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        from fintracker.application.planning.periods import list_periods
        from fintracker.db.models.access import Workspace

        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        periods = await list_periods(session, workspace_id=workspace_id, limit=2)
    current = periods[0] if periods else None
    from fintracker.application.identity.profile import ensure_member_profile

    await ensure_member_profile(
        settings, user_id=user_id, workspace_id=workspace_id, name=message.display_name
    )
    lines = [f"🎉 Бюджет «{workspace.name}» создан!", "", "Вы — администратор бюджета."]
    if current:
        lines.append(
            f"Текущий период: {_date(current.start_date)} — "
            f"{_date(current.end_exclusive - dt.timedelta(days=1))}"
        )
        lines.append(f"Следующий период начнётся {_date(current.end_exclusive)}")
    lines.extend(
        [
            "",
            "✍️ Чтобы записать трату, просто напишите её: «кофе 250» или «вчера такси 450».",
            "",
            "Вести бюджет вместе? Нажмите «Пригласить» и перешлите код.",
        ]
    )
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("➕ Добавить трату", callback("menu", "add")),
                    Button("🔗 Пригласить", callback("inv", "new")),
                ),
                (Button("🏠 Меню", callback("menu", "main")),),
            ),
        )
    ]
