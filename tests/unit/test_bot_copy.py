"""Formatting must preserve financial meaning, user content and callback contracts."""

import datetime as dt
import uuid

import pytest

from fintracker.application.conversation import keyboards, views
from fintracker.application.conversation.onboarding_flow import _prompt_for
from fintracker.application.delivery.thresholds import ThresholdOutcome
from fintracker.application.onboarding.wizard import DraftCategory, WizardState, WizardStep


@pytest.mark.parametrize("step", list(WizardStep))
def test_wizard_copy_fits_mobile_messages_and_keeps_callbacks(step: WizardStep) -> None:
    state = WizardState(
        name="Наш бюджет",
        currency="RUB",
        timezone="Asia/Novosibirsk",
        start_date=dt.date(2026, 9, 10),
        end_inclusive=dt.date(2026, 10, 9),
        categories=[DraftCategory("Продукты")],
    )
    replies = _prompt_for(step, state)
    for reply in replies:
        assert 0 < len(reply.text) < 1500
        assert "\n\n" in reply.text
        assert "\\n" not in reply.text
        assert "ℹ️ 🌍" not in reply.text
        for row in reply.buttons:
            for button in row:
                assert "\n" not in button.text
                assert len(button.data.encode()) <= 64


def test_transaction_card_preserves_user_text_as_plain_text() -> None:
    workspace = "Наш <бюджет> & семья"
    note = "Кофе <b>не выделять</b> & десерт 🥐"
    text = views.transaction_card(
        workspace_name=workspace,
        amount_minor=125050,
        currency="RUB",
        category_path="Кафе & рестораны",
        beneficiary="Мы",
        spender="Анна",
        account="Карта",
        occurred_date=dt.date(2026, 9, 20),
        period=None,
        line=None,
        author_name="Никита",
        note=note,
        detailed=True,
    )
    assert workspace in text
    assert note in text
    assert views.money(125050, "RUB") in text
    assert "Для кого: Мы\nКто потратил: Анна\nСчёт: Карта" in text
    assert "\n\n💬 Комментарий:" in text


def test_visual_button_changes_keep_existing_callback_routes() -> None:
    # Добавление траты — первая кнопка меню: это основное действие бота.
    assert [[b.data for b in row] for row in keyboards.main_menu()] == [
        ["menu:add", "menu:budget"],
        ["menu:categories", "menu:history"],
        ["menu:analytics", "menu:goals"],
        ["menu:payments", "menu:more"],
    ]
    assert [b.data for row in keyboards.start_menu(returning=False) for b in row] == [
        "wiz:start",
        "join:start",
    ]


@pytest.mark.parametrize(
    ("kind", "fact", "label"),
    [
        ("approach_80", 80000, "80%"),
        ("exhausted_100", 100000, "исчерпан"),
        ("overspent", 125000, "Перерасход"),
    ],
)
def test_limit_notifications_keep_distinct_financial_meaning(
    kind: str, fact: int, label: str
) -> None:
    text = ThresholdOutcome(
        stable_line_id=uuid.uuid4(),
        threshold_type=kind,
        line_name="Кафе",
        fact_minor=fact,
        limit_minor=100000,
        currency="RUB",
    ).message("Личный")
    assert label in text
    assert views.money(fact, "RUB") in text
    assert views.money(100000, "RUB") in text
    assert "\n\n" in text
    if kind != "overspent":
        assert "перерасход" not in text.lower()


def test_internal_statuses_have_human_labels() -> None:
    assert views.role_label("admin") == "администратор"
    assert views.period_state_label("ended") == "завершён"
    assert views.allocation_role_label("receivable_reversal") == "уменьшение долга перед вами"
