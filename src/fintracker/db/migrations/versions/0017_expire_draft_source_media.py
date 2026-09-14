"""Очистка сырого source_media в черновиках

Revision ID: 0017_media_retention
Revises: 0016_media

Новая колонка drafts.source_media хранит исходную подпись и детальный разбор
медиа. Эти копии подчиняются тому же сроку хранения сырого материала, что
raw_text/transcript (RET-03, RET-08).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_media_retention"
down_revision: str | None = "0016_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE = """
CREATE OR REPLACE FUNCTION maintenance_expire_drafts(p_now TIMESTAMPTZ)
RETURNS TABLE (expired BIGINT, cleared BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    expired_count BIGINT := 0;
    cleared_count BIGINT := 0;
BEGIN
    WITH updated AS (
        UPDATE drafts
           SET state = 'expired', version = version + 1
         WHERE expires_at <= p_now
           AND state IN ('received','processing','needs_clarification','ready')
        RETURNING 1
    )
    SELECT count(*) INTO expired_count FROM updated;

    WITH cleaned AS (
        UPDATE drafts
           SET raw_text = NULL,
               transcript = NULL,
               source_media = source_media - 'caption' - 'ocr_text' - 'parsed_document' - 'receipt_text',
               version = version + 1
         WHERE delete_raw_after <= p_now
           AND (
               raw_text IS NOT NULL
               OR transcript IS NOT NULL
               OR source_media ?| ARRAY['caption','ocr_text','parsed_document','receipt_text']
           )
        RETURNING 1
    )
    SELECT count(*) INTO cleared_count FROM cleaned;

    RETURN QUERY SELECT expired_count, cleared_count;
END;
$$;
"""


DOWNGRADE = """
CREATE OR REPLACE FUNCTION maintenance_expire_drafts(p_now TIMESTAMPTZ)
RETURNS TABLE (expired BIGINT, cleared BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    expired_count BIGINT := 0;
    cleared_count BIGINT := 0;
BEGIN
    WITH updated AS (
        UPDATE drafts
           SET state = 'expired', version = version + 1
         WHERE expires_at <= p_now
           AND state IN ('received','processing','needs_clarification','ready')
        RETURNING 1
    )
    SELECT count(*) INTO expired_count FROM updated;

    WITH cleaned AS (
        UPDATE drafts
           SET raw_text = NULL, transcript = NULL, version = version + 1
         WHERE delete_raw_after <= p_now
           AND (raw_text IS NOT NULL OR transcript IS NOT NULL)
        RETURNING 1
    )
    SELECT count(*) INTO cleared_count FROM cleaned;

    RETURN QUERY SELECT expired_count, cleared_count;
END;
$$;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
