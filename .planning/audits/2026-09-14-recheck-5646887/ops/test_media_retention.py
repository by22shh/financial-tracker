"""The added source-media copy must obey the existing raw-data expiry."""

import datetime as dt

from sqlalchemy import update

from fintracker.application.conversation.types import Attachment, IncomingMessage, MessageKind
from fintracker.application.intelligence.media_pipeline import _prepare_media
from fintracker.application.maintenance.retention import sweep_private_drafts
from fintracker.db.models.platform import Draft
from fintracker.db.session import RuntimeRole, session_scope
from tests.integration.test_deep_audit import prepared


async def test_media_caption_is_erased_at_raw_data_deadline(owner_session, test_settings):
    fixture = await prepared(owner_session)
    message = IncomingMessage(
        telegram_user_id=fixture.user.telegram_user_id,
        chat_id=fixture.user.telegram_user_id,
        workspace_id=fixture.workspace.id,
        kind=MessageKind.PHOTO,
        message_id=800,
        text="Synthetic private source caption, not a saved transaction note",
        attachments=(Attachment(file_id="synthetic-photo", kind="photo"),),
    )
    prepared_media = await _prepare_media(
        test_settings,
        actor=fixture.actor,
        workspace=fixture.workspace,
        message=message,
    )
    now = dt.datetime.now(dt.UTC)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(
            update(Draft)
            .where(Draft.id == prepared_media.draft_id)
            .values(
                delete_raw_after=now - dt.timedelta(seconds=1),
                expires_at=now - dt.timedelta(seconds=1),
            )
        )
    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        _, cleared = await sweep_private_drafts(session, now)
        assert cleared == 1
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        draft = await session.get(Draft, prepared_media.draft_id)
        assert draft.raw_text is None
        assert draft.source_media.get("caption") is None, draft.source_media
