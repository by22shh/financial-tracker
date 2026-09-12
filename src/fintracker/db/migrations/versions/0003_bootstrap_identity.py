"""Узкая SECURITY DEFINER функция разрешения собственной личности

Revision ID: 0003_identity
Revises: 0002_rls

ADR-06 разрешает отдельные SECURITY DEFINER функции для узкой подтверждённой
необходимости. Здесь она одна: по проверенному Telegram ID найти или создать
СОБСТВЕННУЮ строку пользователя. До этого шага контекст app.user_id ещё
неизвестен, поэтому обычная политика «свой пользователь» его закрывает.

Функция возвращает только один идентификатор вызывающего пользователя,
не раскрывает чужие строки, имеет фиксированный search_path, а EXECUTE
отозван у PUBLIC и выдан только runtime ролям.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES

revision: str = "0003_identity"
down_revision: str | None = "0002_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = """
CREATE OR REPLACE FUNCTION resolve_self_user(
    p_telegram_user_id BIGINT,
    p_locale TEXT DEFAULT 'ru'
) RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    found_id UUID;
BEGIN
    IF p_telegram_user_id IS NULL OR p_telegram_user_id <= 0 THEN
        RAISE EXCEPTION 'Некорректный Telegram ID' USING ERRCODE = 'check_violation';
    END IF;

    SELECT u.id INTO found_id FROM users u WHERE u.telegram_user_id = p_telegram_user_id;
    IF found_id IS NOT NULL THEN
        RETURN found_id;
    END IF;

    INSERT INTO users (id, telegram_user_id, locale)
    VALUES (gen_random_uuid(), p_telegram_user_id, COALESCE(p_locale, 'ru'))
    ON CONFLICT (telegram_user_id) DO NOTHING
    RETURNING id INTO found_id;

    IF found_id IS NULL THEN
        -- Конкурентная вставка: берём существующую строку.
        SELECT u.id INTO found_id FROM users u WHERE u.telegram_user_id = p_telegram_user_id;
    END IF;

    RETURN found_id;
END;
$$;
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_self_user(BIGINT, TEXT) FROM PUBLIC")
    for role in RUNTIME_ROLES:
        op.execute(f"GRANT EXECUTE ON FUNCTION resolve_self_user(BIGINT, TEXT) TO {role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_self_user(BIGINT, TEXT)")
