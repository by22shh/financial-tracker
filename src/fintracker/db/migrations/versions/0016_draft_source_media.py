"""Черновик хранит свой исходный материал

Revision ID: 0016_media
Revises: 0015_pending

Кнопки «Повторить разбор» и «Да, оплачено» обещают продолжение работы с тем же
материалом, но черновик не хранил ни ссылок на файлы, ни разобранного документа:
повтор начинался с нуля, а подтверждение оплаты не создавало запись (G-16).

Хранится только техническая ссылка на файл Telegram и уже разобранная структура
документа; срок жизни совпадает со сроком черновика (RET-03, RET-08).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_media"
down_revision: str | None = "0015_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE drafts ADD COLUMN source_media JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE drafts DROP COLUMN IF EXISTS source_media")
