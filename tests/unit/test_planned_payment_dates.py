"""TG-03: upcoming payments must not inherit historical expense date inference."""

import datetime as dt

import pytest

from fintracker.domain.parsing.dates import resolve_date_expression


@pytest.mark.parametrize(
    ("text", "reference", "expected"),
    [
        ("25.09", dt.date(2026, 9, 10), dt.date(2026, 9, 25)),
        ("25 сентября", dt.date(2026, 9, 20), dt.date(2026, 9, 25)),
        ("05.01", dt.date(2026, 12, 20), dt.date(2027, 1, 5)),
        ("20.09", dt.date(2026, 9, 20), dt.date(2026, 9, 20)),
        ("29.02", dt.date(2026, 9, 20), dt.date(2028, 2, 29)),
        ("25.09.2025", dt.date(2026, 9, 20), dt.date(2025, 9, 25)),
        ("завтра", dt.date(2026, 9, 20), dt.date(2026, 9, 21)),
    ],
)
def test_planned_date_uses_next_occurrence(text, reference, expected):
    parsed = resolve_date_expression(text, reference=reference, prefer_future=True)
    assert parsed is not None
    assert parsed.value == expected


def test_expense_date_inference_unchanged():
    parsed = resolve_date_expression("25.09", reference=dt.date(2026, 9, 10))
    assert parsed is not None
    assert parsed.value == dt.date(2025, 9, 25)
    assert (
        resolve_date_expression("31.02", reference=dt.date(2026, 9, 10), prefer_future=True) is None
    )
