"""Лимиты мастера: сумма, ноль и отсутствие лимита имеют разный смысл."""

import pytest

from fintracker.application.conversation import onboarding_limits as limits
from fintracker.application.onboarding.wizard import DraftCategory, WizardState


def state_with_selection() -> WizardState:
    state = WizardState(currency="RUB", categories=[DraftCategory("Продукты", 50000)])
    limits.choose(state, action="lim", value=limits.category_key(0, state.categories[0]))
    return state


@pytest.mark.parametrize("value", ["15000", "15 000", "15000 ₽", "15000 RUB", "15к"])
def test_amount_formats_save_only_selected_category(value: str) -> None:
    state = state_with_selection()
    state.categories.append(DraftCategory("Транспорт", 10000))
    reply = limits.apply_amount(state, value)
    assert state.categories[0].limit_minor == 1500000
    assert state.categories[1].limit_minor == 10000
    assert state.limit_category is None
    assert "сохранён" in reply.text


@pytest.mark.parametrize(
    "value", ["-500", "−500", "100 или 200", "1.500", "купил 500", "150 EUR", "не 500", "9" * 80]
)
def test_invalid_amount_keeps_selection_and_previous_limit(value: str) -> None:
    state = state_with_selection()
    key = state.limit_category
    reply = limits.apply_amount(state, value)
    assert reply.text.startswith("✍️")
    assert state.categories[0].limit_minor == 50000
    assert state.limit_category == key


def test_zero_and_unlimited_survive_serialization() -> None:
    state = state_with_selection()
    limits.apply_amount(state, "0")
    state = WizardState.from_payload(state.to_payload())
    assert state.categories[0].limit_minor == 0
    limits.choose(state, action="lim", value=limits.category_key(0, state.categories[0]))
    state = WizardState.from_payload(state.to_payload())
    limits.choose(state, action="lclear", value=state.limit_category or "")
    assert state.categories[0].limit_minor is None
    assert state.limit_category is None
    assert WizardState.from_payload({}).limits_page == 0


def test_stale_category_button_cannot_change_a_replaced_category() -> None:
    state = state_with_selection()
    old_key = state.limit_category or ""
    state.categories[0] = DraftCategory("Жильё", 4000000)
    limits.choose(state, action="lim", value=old_key)
    limits.apply_amount(state, "10")
    assert state.categories[0].limit_minor == 4000000
    assert state.limit_category is None


def test_paging_and_long_labels_keep_telegram_callback_limits() -> None:
    state = WizardState(
        currency="RUB",
        categories=[DraftCategory("Категория " + str(i) + "ю" * 110) for i in range(15)],
    )
    limits.choose(state, action="lpage", value="1")
    reply = limits.prompt(state)
    category_buttons = [b for row in reply.buttons for b in row if b.data.startswith("wiz:lim:")]
    assert len(category_buttons) == limits.PAGE_SIZE
    assert category_buttons[0].data == "wiz:lim:" + limits.category_key(6, state.categories[6])
    assert "Страница 2 из 3" in reply.text
    assert all(len(b.data.encode()) <= 64 for row in reply.buttons for b in row)
    assert len(reply.text) < 4096
