"""Приватное объектное хранилище (ADR-11).

Ключи случайные, без имени человека, магазина и суммы. Публичного чтения нет:
выдача идёт через авторизованный серверный endpoint с повторной проверкой
членства при каждом обращении (SEC-05).
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fintracker.config import StorageSettings
from fintracker.core.errors import TemporarilyUnavailable


def new_storage_key(prefix: str) -> str:
    """Случайный ключ без смысловой нагрузки."""
    return f"{prefix}/{secrets.token_urlsafe(24)}"


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    checksum_sha256: str


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes) -> StoredObject: ...

    async def get(self, key: str) -> bytes | None: ...

    async def delete(self, key: str) -> None: ...


class FilesystemStorage:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "__")
        return self._root / safe

    async def put(self, key: str, data: bytes) -> StoredObject:
        def _write() -> StoredObject:
            path = self._path(key)
            path.write_bytes(data)
            path.chmod(0o600)
            return StoredObject(
                key=key,
                size_bytes=len(data),
                checksum_sha256=hashlib.sha256(data).hexdigest(),
            )

        return await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes | None:
        def _read() -> bytes | None:
            path = self._path(key)
            return path.read_bytes() if path.exists() else None

        return await asyncio.to_thread(_read)

    async def delete(self, key: str) -> None:
        def _delete() -> None:
            path = self._path(key)
            path.unlink(missing_ok=True)

        await asyncio.to_thread(_delete)


def build_storage(settings: StorageSettings) -> ObjectStorage:
    if settings.backend == "filesystem":
        return FilesystemStorage(settings.root)
    raise TemporarilyUnavailable("Backend хранилища 's3' требует настроенных доступов (BL-04)")
