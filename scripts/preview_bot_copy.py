"""Generate a local Telegram-style gallery from real message renderers, without sending messages."""

from __future__ import annotations

import ast
import datetime as dt
import html
import uuid
from dataclasses import replace
from pathlib import Path

from fintracker.application.conversation import keyboards, onboarding_limits, views
from fintracker.application.conversation.context import HELP_TEXT
from fintracker.application.conversation.manual_form import FORM_HELP
from fintracker.application.conversation.onboarding_flow import _prompt_for
from fintracker.application.conversation.types import Reply
from fintracker.application.delivery.thresholds import ThresholdOutcome
from fintracker.application.onboarding.wizard import DraftCategory, WizardState, WizardStep
from fintracker.application.planning.plan import LimitState, LineStatus, PeriodStatus

ROOT = Path(__file__).resolve().parents[1]


def examples() -> list[tuple[str, Reply]]:
    tree = ast.parse((ROOT / "src/fintracker/application/conversation/service.py").read_text())
    welcome = next(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.startswith("👋 Добро пожаловать")
    )
    samples = [("Первый запуск", Reply(welcome, keyboards.start_menu(returning=False)))]
    state = WizardState(
        name="Наш бюджет",
        currency="RUB",
        timezone="Asia/Novosibirsk",
        start_date=dt.date(2026, 9, 10),
        end_inclusive=dt.date(2026, 10, 9),
        categories=[DraftCategory("Продукты", 3000000), DraftCategory("Кафе", 1000000)],
        income_period_minor=10000000,
        income_precision="exact",
    )
    for step in WizardStep:
        if step is WizardStep.INCOME:
            for precision in (None, "exact", "estimate"):
                income_state = replace(state, income_precision=precision)
                samples.append(
                    (f"Доход · {precision or 'выбор'}", _prompt_for(step, income_state)[0])
                )
        elif step is not WizardStep.DONE:
            samples.append((f"Настройка · {step.value}", _prompt_for(step, state)[0]))
            if step is WizardStep.LIMITS:
                selected = replace(
                    state, limit_category=onboarding_limits.category_key(0, state.categories[0])
                )
                samples.append(("Лимит · ввод суммы", onboarding_limits.prompt(selected)))
    line = LineStatus(
        stable_line_id=uuid.UUID(int=1),
        category_id=uuid.UUID(int=2),
        category_name="Кафе",
        beneficiary_id=None,
        beneficiary_name=None,
        assigned_limit_minor=1000000,
        rollover_minor=0,
        effective_limit_minor=1000000,
        fact_minor=800000,
        remaining_minor=200000,
        commitments_minor=0,
        available_minor=200000,
        overdue_commitments_minor=0,
        limit_state=LimitState.POSITIVE,
        usage_percent=80,
        is_protected=False,
        currency="RUB",
    )
    period = PeriodStatus(
        period_id=uuid.UUID(int=3),
        start_date=state.start_date,
        end_inclusive=state.end_inclusive,
        currency="RUB",
        plan_status="approved",
        plan_origin="template",
        budget_version_id=None,
        lines=(line,),
        total_fact_minor=800000,
        total_limit_minor=1000000,
        overall_limit_minor=None,
        uncategorized_fact_minor=0,
        pending_drafts=1,
        pending_confident_minor=25000,
        completeness="incomplete",
    )
    samples.append(
        (
            "Обзор",
            Reply(
                views.budget_overview(period, workspace_name="Наш бюджет"), keyboards.main_menu()
            ),
        )
    )
    samples.append(
        (
            "Карточка операции",
            Reply(
                views.transaction_card(
                    workspace_name="Наш бюджет",
                    amount_minor=125000,
                    currency="RUB",
                    category_path="Кафе",
                    beneficiary="Мы",
                    spender="Анна",
                    account="Карта",
                    occurred_date=dt.date(2026, 9, 20),
                    period=(state.start_date, state.end_inclusive),
                    line=line,
                    author_name="Никита",
                    note="Ужин после кино",
                ),
                keyboards.transaction_card(uuid.UUID(int=4)),
            ),
        )
    )
    samples.append(("Категории", Reply(views.category_lines(period)[0])))
    for kind, fact in [("approach_80", 800000), ("exhausted_100", 1000000), ("overspent", 1125000)]:
        message = ThresholdOutcome(uuid.UUID(int=1), kind, "Кафе", fact, 1000000, "RUB").message(
            "Наш бюджет"
        )
        samples.append((f"Уведомление · {kind}", Reply(message)))
    for section in ["history", "goals", "payments"]:
        samples.append((f"Пустой раздел · {section}", Reply(views.empty_state(section))))
    samples.append(("Ручной ввод", Reply(FORM_HELP)))
    samples.append(("Помощь", Reply(HELP_TEXT, keyboards.main_menu())))
    return samples


def main() -> None:
    cards = []
    for title, reply in examples():
        keyboard = "".join(
            '<div class="row">'
            + "".join(
                '<span class="button">' + html.escape(button.text) + "</span>" for button in row
            )
            + "</div>"
            for row in reply.buttons
        )
        cards.append(
            "<article><h2>"
            + html.escape(title)
            + '</h2><div class="chat"><div class="bubble">'
            + html.escape(reply.text)
            + '<small>10:42</small></div><div class="keyboard">'
            + keyboard
            + "</div></div></article>"
        )
    content = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Бюджет — сообщения</title>
<style>
*{box-sizing:border-box}
body{margin:0;
background:#f4f6f6;
color:#172c2d;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}

header{padding:36px 32px 18px;
max-width:1200px;
margin:auto}
h1{font-size:28px;
margin:0 0 8px}
header p{color:#5b7375;
margin:0}

main{display:grid;
grid-template-columns:repeat(auto-fit,minmax(310px,370px));
gap:28px;
justify-content:center;
padding:20px 24px 60px;
align-items:start}

article{min-width:0}
h2{font-size:12px;
text-transform:uppercase;
letter-spacing:.08em;
color:#5b7375;
margin:0 0 10px}

.chat{background:#dce9e4;
padding:16px 12px;
border-radius:18px;
min-height:120px;
border:1px solid #cddcd5}

.bubble{background:white;
padding:14px 15px 8px;
border-radius:14px 14px 14px 4px;
white-space:pre-wrap;
overflow-wrap:anywhere;
font-size:15px;
line-height:1.48;
box-shadow:0 1px 2px #17312512}

small{display:block;
text-align:right;
color:#87a09d;
font-size:10px;
margin-top:7px}
.keyboard{margin-top:5px}
.row{display:flex;
gap:4px;
margin-top:4px}
.button{flex:1;
min-width:0;
text-align:center;
background:#ffffffae;
color:#276970;
border-radius:7px;
padding:10px 6px;
font-size:12px;
font-weight:600;
overflow-wrap:anywhere}

@media(max-width:400px){main{padding:16px 10px}
header{padding:24px 16px}
h1{font-size:24px}
}

</style><header><h1>Бюджет · сообщения бота</h1>
<p>Реальные шаблоны интерфейса. Все имена и суммы — демонстрационные.<br>
Предпросмотр ширины телефона; внешний вид Telegram зависит от темы и клиента.</p></header><main>"""
    content += "".join(cards) + "</main></html>"
    target = ROOT / "docs/BOT_MESSAGES_PREVIEW.html"
    target.write_text(content)
    print(f"{len(cards)} message previews: {target}")


if __name__ == "__main__":
    main()
