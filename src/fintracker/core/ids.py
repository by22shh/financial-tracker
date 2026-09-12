"""Идентификаторы и коды приглашений (DATA_CONTRACT §1, FR-77, SEC-04)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from typing import Final

# Однозначный алфавит без 0/O, 1/I/L (FR-77).
INVITE_ALPHABET: Final[str] = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
INVITE_LENGTH: Final[int] = 12
_INVITE_GROUP: Final[int] = 4


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def new_generation() -> uuid.UUID:
    """Поколение членства — случайный UUID, не счётчик (ADR-14)."""
    return uuid.uuid4()


def generate_invite_code() -> str:
    """Криптографически случайный код приглашения."""
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))


def format_invite_code(code: str) -> str:
    """Показ группами по четыре символа."""
    return "-".join(code[i : i + _INVITE_GROUP] for i in range(0, len(code), _INVITE_GROUP))


def normalize_invite_code(raw: str) -> str:
    """Нормализация ввода: регистр, пробелы, дефисы (FR-77).

    Похожие символы приводятся к алфавиту, чтобы опечатка регистра или O/0
    не отнимала попытку.
    """
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    # O/0 -> Q и I/1/L -> J выбраны как ближайшие по начертанию буквы алфавита.
    lookalikes: dict[str, str | int | None] = {"O": "Q", "0": "Q", "I": "J", "1": "J", "L": "J"}
    return cleaned.translate(str.maketrans(lookalikes))


def invite_digest(code: str, key: str, *, key_version: int = 1) -> str:
    """Проверочное значение кода: HMAC-SHA-256 с серверным ключом (ADR-06).

    Открытый секрет в базе не хранится; версия ключа позволяет ротацию.
    """
    material = f"v{key_version}:{code}".encode()
    return hmac.new(key.encode(), material, hashlib.sha256).hexdigest()


def short_id(value: uuid.UUID) -> str:
    """Короткий видимый идентификатор бюджета для различения названий (FR-79)."""
    return value.hex[:8].upper()


def canonical_hash(payload: str) -> str:
    """Хэш канонического содержимого команды для идемпотентности."""
    return hashlib.sha256(payload.encode()).hexdigest()
