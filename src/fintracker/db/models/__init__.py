"""Регистрация всех моделей схемы."""

from fintracker.db.base import Base
from fintracker.db.models import (  # noqa: F401
    access,
    catalog,
    commitments,
    integrations,
    intelligence,
    ledger,
    planning,
    platform,
)

__all__ = ["Base"]
