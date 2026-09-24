from __future__ import annotations

import pytest

from fintracker.application.ingestion.process_event import _send_reply_item
from fintracker.infra.telegram.sender import RecordingSender, SendResult, render_safe_html


def test_safe_html_adds_hierarchy_and_escapes_dynamic_text() -> None:
    rendered = render_safe_html(
        "📒 Бюджет <семьи> & друзей\n\n"
        "12 500 ₽ — Продукты & дом\n"
        "Период: 10 сентября — 9 октября\n"
        "Комментарий: <script>не брать</script>"
    )

    assert rendered.startswith("<b>📒 Бюджет &lt;семьи&gt; &amp; друзей</b>")
    assert "<b>Период:</b> 10 сентября — 9 октября" in rendered
    assert "<b>12 500 ₽ — Продукты &amp; дом</b>" in rendered
    assert "<b>Комментарий:</b> &lt;script&gt;не брать&lt;/script&gt;" in rendered
    assert "<script>" not in rendered


@pytest.mark.asyncio
async def test_callback_reply_edits_existing_card() -> None:
    sender = RecordingSender()

    result = await _send_reply_item(
        sender,
        chat_id=101,
        item={"text": "📒 Бюджет", "buttons": [], "edit_message_id": 55},
    )

    assert result.ok
    assert sender.edited == [{"chat_id": 101, "message_id": 55, "text": "📒 Бюджет", "buttons": []}]
    assert sender.sent == []


@pytest.mark.asyncio
async def test_failed_edit_falls_back_to_new_message() -> None:
    sender = RecordingSender(fail_edits=True)

    result = await _send_reply_item(
        sender,
        chat_id=102,
        item={"text": "📊 Аналитика", "buttons": None, "edit_message_id": 56},
    )

    assert result.ok
    assert sender.edited == []
    assert sender.sent == [{"chat_id": 102, "text": "📊 Аналитика", "buttons": None}]


@pytest.mark.asyncio
async def test_legacy_sender_without_edit_method_still_works() -> None:
    class LegacySender:
        def __init__(self) -> None:
            self.sent = 0

        async def send_message(self, **kwargs: object) -> SendResult:
            self.sent += 1
            return SendResult(ok=True, message_id=77)

    sender = LegacySender()
    result = await _send_reply_item(
        sender,
        chat_id=103,
        item={"text": "⚙️ Настройки", "buttons": None, "edit_message_id": 57},
    )

    assert result.ok
    assert sender.sent == 1
