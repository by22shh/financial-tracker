"""Базовые типы и соглашения схемы (DATA_CONTRACT §1)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, MetaData, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012
        uuid.UUID: Uuid(as_uuid=True),
        dt.datetime: DateTime(timezone=True),
        dt.date: Date(),
        int: BigInteger(),
        str: String(),
        dict[str, Any]: JSONB(),
        list[Any]: JSONB(),
    }


def pk_uuid() -> Any:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def now_server() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


def money_column(*, nullable: bool = False, positive: bool = False, name: str = "") -> Any:
    """Денежный столбец: целые minor units (FR-26)."""
    args: list[Any] = []
    if positive and name:
        args.append(CheckConstraint(f"{name} > 0", name=f"{name}_positive"))
    return mapped_column(BigInteger, *args, nullable=nullable)


def version_column() -> Any:
    """Счётчик версий для оптимистичной конкуренции (DATA_CONTRACT §1)."""
    return mapped_column(BigInteger, nullable=False, server_default=text("1"))
