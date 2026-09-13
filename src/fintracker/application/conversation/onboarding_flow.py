"""Диалог мастера создания бюджета и вход по коду (FR-84, FR-78)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select

from fintracker.application.conversation.keyboards import Button, callback
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
    infer_policy_options,
    validate_timezone,
)
from fintracker.core.errors import DomainError, ValidationFailed
from fintracker.core.money import Money, MoneyError, normalize_currency
from fintracker.db.models.access import BudgetSetupDraft, User
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.parsing.amounts import parse_amounts
from fintracker.domain.parsing.dates import resolve_date_expression

DEFAULT_TIMEZONE = "Asia/Novosibirsk"
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


async def start_wizard(settings: Settings, *, user_id: uuid.UUID) -> list[Reply]:
    """Начать или продолжить настройку бюджета."""
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        draft, state = await get_or_create_draft(session, owner_user_id=user_id)
        step = WizardStep(draft.step)
    return _prompt_for(step, state)


async def start_join_flow(settings: Settings, *, user_id: uuid.UUID) -> list[Reply]:
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        draft, state = await get_or_create_draft(session, owner_user_id=user_id)
        await save_draft(session, draft, state, step=WizardStep.NAME)
        draft.state = "cancelled"
    return [
        Reply(
            text=(
                "Введите код приглашения от администратора бюджета.\n"
                "Код состоит из 12 символов и может быть записан группами по четыре."
            )
        )
    ]


async def submit_join_code(
    settings: Settings, *, user_id: uuid.UUID, raw_code: str, message: IncomingMessage
) -> list[Reply]:
    """Показать бюджет до подтверждения, затем присоединить (FR-78)."""
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        session.expunge(user)
    try:
        preview = await preview_invite(settings, raw_code=raw_code, user=user)
    except DomainError as exc:
        return [Reply(text=exc.message)]

    if preview.already_member:
        return [
            Reply(
                text=f"Вы уже участник бюджета «{preview.workspace_name}».",
                buttons=(
                    (
                        Button(
                            "Открыть бюджет",
                            callback("ws", "use", preview.workspace_id.hex[:16]),
                        ),
                    ),
                ),
            )
        ]

    result = await accept_invite(
        settings,
        raw_code=raw_code,
        user=user,
        correlation_id=message.correlation_id or uuid.uuid4().hex,
    )
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=result.workspace_id
    ) as session:
        from fintracker.application.identity.actor import set_active_workspace

        await set_active_workspace(session, user=user, workspace_id=result.workspace_id)
    return [
        Reply(
            text=(
                f"Вы присоединились к бюджету «{result.workspace_name}».\n"
                "Ваши записи в этом бюджете будут видны всем участникам, "
                "а вам доступна вся общая история."
            ),
            buttons=(
                (
                    Button("Открыть бюджет", callback("menu", "budget")),
                    Button("Добавить трату", callback("menu", "add")),
                ),
            ),
        )
    ]


def _prompt_for(step: WizardStep, state: WizardState) -> list[Reply]:
    match step:
        case WizardStep.NAME:
            return [
                Reply(
                    text=(
                        "Создаём бюджет. Как его назвать?\n"
                        "Например: «Наш общий бюджет» или «Личный»."
                    )
                )
            ]
        case WizardStep.CURRENCY:
            return [
                Reply(
                    text=(
                        "В какой валюте вести учёт? Укажите код, например RUB.\n"
                        "Валюту заполненного бюджета менять нельзя, поэтому её нужно "
                        "подтвердить сейчас."
                    ),
                    buttons=(
                        (
                            Button("RUB", callback("wiz", "cur", "RUB")),
                            Button("USD", callback("wiz", "cur", "USD")),
                            Button("EUR", callback("wiz", "cur", "EUR")),
                        ),
                    ),
                )
            ]
        case WizardStep.TIMEZONE:
            return [
                Reply(
                    text=(
                        "Какой часовой пояс использовать для дат?\n"
                        f"Предлагается {DEFAULT_TIMEZONE}. Можно ввести другой в формате "
                        "IANA, например Europe/Moscow."
                    ),
                    buttons=((Button(DEFAULT_TIMEZONE, callback("wiz", "tz", "default")),),),
                )
            ]
        case WizardStep.PERIOD_DATES:
            return [
                Reply(
                    text=(
                        "Выберите даты первого периода в формате ДД.ММ.ГГГГ — ДД.ММ.ГГГГ.\n"
                        "Например: 10.09.2026 — 09.10.2026. Обе даты включаются в период."
                    )
                )
            ]
        case WizardStep.PERIOD_REPEAT:
            options = _repeat_options(state)
            lines = ["Как повторять период?"]
            buttons: list[tuple[Button, ...]] = []
            for index, option in enumerate(options):
                preview = option.preview(2)
                lines.append(
                    f"{index + 1}. {option.describe()} → далее "
                    f"{preview[1].start.isoformat()} — {preview[1].end_inclusive.isoformat()}"
                )
                buttons.append((Button(option.describe(), callback("wiz", "rep", str(index))),))
            return [Reply(text="\n".join(lines), buttons=tuple(buttons))]
        case WizardStep.INCOME:
            return [
                Reply(
                    text=(
                        "Какой доход планируется? Укажите сумму и выберите тип оценки.\n"
                        "План дохода не является фактом поступления и не увеличивает счёт."
                    ),
                    buttons=(
                        (
                            Button("Точный план", callback("wiz", "inc", "exact")),
                            Button("Примерная оценка", callback("wiz", "inc", "estimate")),
                        ),
                        (Button("Укажу позже", callback("wiz", "inc", "later")),),
                    ),
                )
            ]
        case WizardStep.CATEGORIES:
            return [
                Reply(
                    text=(
                        "Категории: перечислите свои через запятую либо возьмите общий "
                        "шаблон.\nИмена чужих бюджетов не навязываются."
                    ),
                    buttons=(
                        (
                            Button("Общий шаблон", callback("wiz", "cats", "template")),
                            Button("С нуля", callback("wiz", "cats", "empty")),
                        ),
                    ),
                )
            ]
        case WizardStep.LIMITS:
            names = ", ".join(item.name for item in state.categories) or "нет категорий"
            return [
                Reply(
                    text=(
                        f"Задайте лимиты строками «Категория = сумма».\nКатегории: {names}.\n"
                        "Нулевой лимит отличается от незаданного: пропустите строку, "
                        "если лимит пока не нужен."
                    ),
                    buttons=((Button("Пропустить", callback("wiz", "skip", "limits")),),),
                )
            ]
        case WizardStep.COMMITMENTS:
            return [
                Reply(
                    text=(
                        "Плановые траты: «Название = сумма = ДД.ММ».\n"
                        "Это будущие обязательства, а не совершённые покупки."
                    ),
                    buttons=((Button("Пропустить", callback("wiz", "skip", "commitments")),),),
                )
            ]
        case WizardStep.GOALS:
            return [
                Reply(
                    text=(
                        "Цели накоплений: «Название = целевая сумма».\n"
                        "Выделение денег на цель не является покупкой."
                    ),
                    buttons=((Button("Пропустить", callback("wiz", "skip", "goals")),),),
                )
            ]
        case WizardStep.TEMPLATE:
            return [
                Reply(
                    text=(
                        "Повторять утверждённый шаблон плана в следующих периодах?\n"
                        "Копируются лимиты и правила, но не расходы, факт дохода и "
                        "прогресс целей."
                    ),
                    buttons=(
                        (
                            Button("Повторять", callback("wiz", "tpl", "on")),
                            Button("Не повторять", callback("wiz", "tpl", "off")),
                        ),
                    ),
                )
            ]
        case WizardStep.REVIEW:
            return [_review_reply(state)]
        case _:
            return [Reply(text="Настройка завершена.")]


def _repeat_options(state: WizardState) -> list[PeriodPolicy]:
    if state.start_date is None or state.end_inclusive is None or state.timezone is None:
        return []
    return infer_policy_options(state.start_date, state.end_inclusive, state.timezone)


def _review_reply(state: WizardState) -> Reply:
    """Предпросмотр и явный показ дефицита (FR-84, FR-62)."""
    currency = state.currency or DEFAULT_CURRENCY
    funding = check_funding(state)
    lines = [
        f"Бюджет: {state.name}",
        f"Валюта: {currency} · Пояс: {state.timezone}",
    ]
    if state.start_date and state.end_inclusive:
        lines.append(
            f"Период: {state.start_date.isoformat()} — {state.end_inclusive.isoformat()} "
            f"({(state.end_inclusive - state.start_date).days + 1} дн.)"
        )
    upcoming = preview_periods(state)
    if upcoming:
        lines.append("Далее:")
        lines.extend(
            f"       {item.start.isoformat()} — {item.end_inclusive.isoformat()}"
            for item in upcoming
        )
    if funding.income_known and funding.income_minor is not None:
        lines.append(f"План дохода на период: {Money(funding.income_minor, currency).format()}")
    else:
        lines.append("План дохода на период: не задан (финансирование непроверено)")
    lines.append(f"Сумма лимитов: {Money(funding.limits_total_minor, currency).format()}")
    if funding.deficit_minor:
        lines.append(
            f"Дефицит: {Money(funding.deficit_minor, currency).format()} — "
            "нужно подтвердить осознанно или уменьшить лимиты"
        )
    lines.append(
        "Повторять шаблон плана: " + ("включено" if state.repeat_template else "выключено")
    )
    lines.append(f"Категорий: {len(state.categories)}")

    buttons: list[tuple[Button, ...]] = [(Button("Создать бюджет", callback("wiz", "publish")),)]
    if funding.deficit_minor and not state.deficit_accepted:
        buttons.insert(0, (Button("Принять дефицит осознанно", callback("wiz", "deficit")),))
    buttons.append((Button("Изменить", callback("wiz", "back")),))
    return Reply(text="\n".join(lines), buttons=tuple(buttons))


async def continue_wizard_input(
    settings: Settings, *, user_id: uuid.UUID, message: IncomingMessage
) -> list[Reply] | None:
    """Обработать текстовый ответ на шаг мастера.

    Возвращает None, если текст не относится к мастеру: новая законченная
    фраза о покупке создаёт отдельный ввод и не затирает настройку (R07).
    """
    text = (message.text or "").strip()
    if not text:
        return None
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        draft, state = await get_or_create_draft(session, owner_user_id=user_id)
        step = WizardStep(draft.step)
        if step is WizardStep.DONE:
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
            return WizardStep.CURRENCY, None
        case WizardStep.CURRENCY:
            try:
                state.currency = normalize_currency(text)
            except MoneyError:
                return None, [
                    Reply(text="Не узнал валюту. Укажите трёхбуквенный код, например RUB.")
                ]
            return WizardStep.TIMEZONE, None
        case WizardStep.TIMEZONE:
            try:
                state.timezone = validate_timezone(text)
            except CalendarError:
                return None, [
                    Reply(
                        text=(
                            "Не узнал часовой пояс. Введите в формате IANA, "
                            "например Asia/Novosibirsk."
                        )
                    )
                ]
            return WizardStep.PERIOD_DATES, None
        case WizardStep.PERIOD_DATES:
            parsed = _parse_period_dates(text)
            if parsed is None:
                return None, [
                    Reply(
                        text=(
                            "Не удалось разобрать даты. Формат: ДД.ММ.ГГГГ — ДД.ММ.ГГГГ.\n"
                            "Например: 10.09.2026 — 09.10.2026."
                        )
                    )
                ]
            start, end = parsed
            if end < start:
                return None, [Reply(text="Дата конца раньше даты начала. Повторите ввод.")]
            state.start_date, state.end_inclusive = start, end
            return WizardStep.PERIOD_REPEAT, None
        case WizardStep.PERIOD_REPEAT:
            return None, _prompt_for(WizardStep.PERIOD_REPEAT, state)
        case WizardStep.INCOME:
            amounts = parse_amounts(text)
            if not amounts:
                return None, [Reply(text="Укажите сумму дохода числом либо нажмите «Укажу позже».")]
            currency = state.currency or DEFAULT_CURRENCY
            state.income_monthly_minor = Money.from_decimal(
                Decimal(amounts[0].value), currency
            ).minor
            state.income_precision = state.income_precision or "estimate"
            return WizardStep.CATEGORIES, None
        case WizardStep.CATEGORIES:
            names = [part.strip() for part in text.split(",") if part.strip()]
            if not names:
                return None, [Reply(text="Перечислите категории через запятую.")]
            state.categories = [DraftCategory(name=name[:120]) for name in names]
            return WizardStep.LIMITS, None
        case WizardStep.LIMITS:
            errors = _apply_limits(state, text)
            if errors:
                return None, [Reply(text="\n".join(errors))]
            return WizardStep.COMMITMENTS, None
        case WizardStep.COMMITMENTS | WizardStep.GOALS:
            # Плановые траты и цели заполняются после создания бюджета,
            # чтобы не смешивать их с фактическими покупками (FR-84).
            nxt = WizardStep.GOALS if step is WizardStep.COMMITMENTS else WizardStep.TEMPLATE
            return nxt, None
        case WizardStep.TEMPLATE:
            state.repeat_template = text.strip().lower() not in {"нет", "не повторять", "off"}
            return WizardStep.REVIEW, None
        case WizardStep.REVIEW:
            return None, [_review_reply(state)]
        case _:
            return None, None


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
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        draft, state = await get_or_create_draft(session, owner_user_id=user_id)
        draft_id = draft.id
        step = WizardStep(draft.step)
        next_step: WizardStep | None = None

        match action:
            case "start":
                return _prompt_for(step, state)
            case "cur":
                state.currency = normalize_currency(value)
                next_step = WizardStep.TIMEZONE
            case "tz":
                state.timezone = DEFAULT_TIMEZONE
                next_step = WizardStep.PERIOD_DATES
            case "rep":
                options = _repeat_options(state)
                index = int(value) if value.isdigit() else 0
                if not options or index >= len(options):
                    return [Reply(text="Сначала выберите даты первого периода.")]
                chosen = options[index]
                state.repeat_mode = chosen.mode
                state.repeat_interval = chosen.interval
                # Конец первого периода приводится в соответствие выбранному
                # правилу явно, без молчаливой подмены (FR-90, A205).
                state.end_inclusive = chosen.period(0).end_inclusive
                next_step = WizardStep.INCOME
            case "inc":
                if value == "later":
                    state.income_precision = None
                    next_step = WizardStep.CATEGORIES
                else:
                    state.income_precision = value
                    next_step = WizardStep.INCOME
            case "cats":
                if value == "template":
                    state.categories = [
                        DraftCategory(name=name) for name in COMMON_CATEGORY_TEMPLATE
                    ]
                    next_step = WizardStep.LIMITS
                else:
                    state.categories = []
                    next_step = WizardStep.LIMITS
            case "skip":
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
                order = list(STEP_ORDER)
                next_step = order[max(order.index(step) - 1, 0)]
            case "publish":
                await save_draft(session, draft, state, step=WizardStep.REVIEW)
            case _:
                return [Reply(text="Неизвестное действие мастера.")]

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
        return [Reply(text=f"Не удалось создать бюджет: {exc.message}")]

    from fintracker.core.ids import short_id

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
    lines = [
        f"Бюджет «{workspace.name}» создан.",
        f"Постоянный ID: {short_id(workspace_id)}",
    ]
    if current:
        lines.append(
            f"Текущий период: {current.start_date.isoformat()} — "
            f"{(current.end_exclusive - dt.timedelta(days=1)).isoformat()}"
        )
        lines.append(f"Следующий период начнётся {current.end_exclusive.isoformat()}")
    lines.append("Ваша роль: администратор")
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("Добавить трату", callback("menu", "add")),
                    Button("Пригласить", callback("inv", "new")),
                ),
                (Button("Открыть бюджет", callback("menu", "budget")),),
            ),
        )
    ]
