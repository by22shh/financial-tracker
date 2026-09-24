"""Independent required-behavior assertions; no live external calls."""

import datetime as dt
import json

import httpx
from sqlalchemy import select

from fintracker.application.conversation.media import handle_media
from fintracker.application.conversation.types import Attachment, IncomingMessage, MessageKind
from fintracker.application.intelligence import media_pipeline
from fintracker.db.models.platform import NotificationDelivery, OutboxEvent
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.ai.openai_client import (
    OpenAIResponsesProvider,
    ScriptedAIProvider,
    set_provider_override,
)
from fintracker.infra.ai.schemas import ExtractionResponse, json_schema_for
from fintracker.infra.storage import build_storage
from fintracker.runtime.health import check_readiness, collect_metrics
from tests.images import png_bytes
from tests.integration.test_deep_audit import prepared


async def test_strict_response_allowed_null_is_accepted(test_settings, monkeypatch):
    # Every property is present. The sole change from a valid model dump is a
    # null array, expressly allowed by the actual outgoing JSON Schema.
    payload = ExtractionResponse(schema_version="1.0", intent="question").model_dump()
    payload["candidates"] = None
    schema = json_schema_for(ExtractionResponse)
    assert "null" in schema["properties"]["candidates"]["type"]
    calls = []

    async def post(client, url, **kwargs):
        calls.append(kwargs["json"])
        assert (
            "null" in kwargs["json"]["text"]["format"]["schema"]["properties"]["candidates"]["type"]
        )
        return httpx.Response(
            200,
            json={
                "id": "synthetic-schema-response",
                "output_text": json.dumps(payload),
                "usage": {"input_tokens": 10, "output_tokens": 10},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await OpenAIResponsesProvider(test_settings.ai).structured(
        instructions="Return the permitted result",
        input_items=[],
        response_model=ExtractionResponse,
        prompt_version="probe",
        schema_name="probe",
    )
    assert result.parsed.candidates == []
    assert len(calls) == 1


async def test_truncated_png_rejected_before_vision(owner_session, test_settings, monkeypatch):
    fixture = await prepared(owner_session)
    settings = test_settings.model_copy(deep=True)
    settings.ai.enabled = True
    truncated = png_bytes()[:24]  # Signature and dimensions only; no pixels/CRC/IEND.

    async def download(settings, *, file_id):
        return truncated

    monkeypatch.setattr(media_pipeline, "download_attachment", download)
    provider = ScriptedAIProvider(
        responses=[
            json.dumps(
                {
                    "schema_version": "1.0",
                    "document_kind": "receipt",
                    "payment_confirmed": True,
                    "total_decimal": "450.00",
                    "currency": "RUB",
                    "lines": [],
                    "unreadable_lines": 0,
                }
            )
        ]
    )
    set_provider_override(provider)
    try:
        replies = await handle_media(
            settings,
            IncomingMessage(
                telegram_user_id=fixture.user.telegram_user_id,
                chat_id=fixture.user.telegram_user_id,
                workspace_id=fixture.workspace.id,
                kind=MessageKind.DOCUMENT,
                message_id=555,
                attachments=(
                    Attachment(
                        file_id="truncated-png",
                        kind="document",
                        mime_type="image/png",
                        size_bytes=len(truncated),
                        file_name="receipt.png",
                    ),
                ),
            ),
            user_id=fixture.user.id,
        )
        assert not provider.calls, f"Truncated 24-byte PNG reached vision: {replies}"
    finally:
        set_provider_override(None)


async def test_existing_readonly_root_is_not_ready(clean_db, test_settings, tmp_path):
    settings = test_settings.model_copy(deep=True)
    root = tmp_path / "readonly-objects"
    root.mkdir()
    root.chmod(0o500)
    settings.storage.root = root
    try:
        # Establish real failure under the current non-root OS user.
        try:
            await build_storage(settings.storage).put("probe", b"probe")
        except PermissionError:
            pass
        else:
            raise AssertionError("Invalid probe environment: readonly write unexpectedly succeeded")
        report = await check_readiness(settings)
        assert report.storage_writable is False and report.ready is False, report.to_payload()
    finally:
        root.chmod(0o700)


async def test_metrics_include_tenant_deliveries(owner_session, test_settings):
    fixture = await prepared(owner_session)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        event = (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.workspace_id == fixture.workspace.id,
                )
                .limit(1)
            )
        ).one()
        delivery = NotificationDelivery(
            event_id=event.id,
            workspace_id=fixture.workspace.id,
            recipient_user_id=fixture.user.id,
            membership_generation=fixture.actor.membership_generation,
            channel="telegram",
            delivery_class="shared_change",
            state="pending",
            available_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=60),
        )
        session.add(delivery)
    report = await collect_metrics(test_settings)
    assert report.delivery_unfinished == 1, report.to_payload()
    assert report.delivery_oldest_seconds >= 50, report.to_payload()
