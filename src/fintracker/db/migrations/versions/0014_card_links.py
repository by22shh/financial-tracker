"""Связь исходящей карточки автора с операцией

Revision ID: 0014_cards
Revises: 0013_analysis

Ответ участнику на его запись — карточка конкретной операции. Без сохранённой
связи «отправленное сообщение → операция» комментарий, отправленный ответом на
старую карточку, применялся к последней трате (G-06). Связь хранится вместе с
самим ответом: та же изоляция по бюджету и владельцу и тот же срок хранения.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_cards"
down_revision: str | None = "0013_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE author_replies ADD COLUMN card_links JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("CREATE INDEX ix_author_replies_cards ON author_replies USING gin (card_links)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_author_replies_cards")
    op.execute("ALTER TABLE author_replies DROP COLUMN IF EXISTS card_links")
