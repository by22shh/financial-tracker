"""Независимый журнал изменений доступа (ADR-14, SEC-10).

Хранится вне контура отката основной базы. Содержит только технические ID,
поколения членств, роли, состояния и последовательность; имён и сумм нет.
Требуемые свойства адаптера: проверяемое условное создание записи,
согласованное чтение после записи, обнаружение последней подтверждённой версии.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from fintracker.config import SecurityLogSettings
from fintracker.core.errors import TemporarilyUnavailable


@dataclass(frozen=True, slots=True)
class AccessSnapshot:
    """Минимальная карта доступа бюджета."""

    workspace_id: str
    state: str
    admin_user_id: str | None
    acl_revision: int
    members: tuple[dict[str, str], ...]

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SecurityLogRecord:
    operation_id: str
    workspace_id: str
    phase: str  # prepared | committed
    kind: str
    expected_acl_revision: int
    proposed_acl_revision: int
    snapshot: AccessSnapshot
    previous_version_key: str | None
    digest: str
    written_at: str

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, sort_keys=True, ensure_ascii=False)


class SecurityLogConflict(Exception):
    """Повтор с другим содержимым не перезаписывает запись."""


class SecurityLogStorage(Protocol):
    async def put_if_absent(self, key: str, body: str) -> str | None:
        """Условное создание. Возвращает существующее тело либо None."""

    async def get(self, key: str) -> str | None: ...

    async def list_keys(self, prefix: str) -> list[str]: ...


class FilesystemSecurityLog:
    """Файловая реализация с атомарным созданием (O_EXCL).

    Подходит для локальной среды и проверок. Для production выбирается
    версионируемое object storage с отдельными правами и ключом шифрования;
    свойства адаптера проверяются до пилота (ADR-14, BL-04).
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "__")
        return self._root / safe

    async def put_if_absent(self, key: str, body: str) -> str | None:
        def _write() -> str | None:
            path = self._path(key)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return path.read_text(encoding="utf-8")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            return None

        return await asyncio.to_thread(_write)

    async def get(self, key: str) -> str | None:
        def _read() -> str | None:
            path = self._path(key)
            return path.read_text(encoding="utf-8") if path.exists() else None

        return await asyncio.to_thread(_read)

    async def list_keys(self, prefix: str) -> list[str]:
        def _list() -> list[str]:
            safe = prefix.replace("/", "__")
            return sorted(p.name.replace("__", "/") for p in self._root.glob(f"{safe}*"))

        return await asyncio.to_thread(_list)


class SecurityLog:
    """Операции журнала поверх выбранного хранилища."""

    def __init__(self, storage: SecurityLogStorage) -> None:
        self._storage = storage

    @staticmethod
    def _key(workspace_id: str, operation_id: str, phase: str) -> str:
        return f"ws/{workspace_id}/op/{operation_id}/{phase}.json"

    async def write_prepared(
        self,
        *,
        operation_id: uuid.UUID,
        workspace_id: uuid.UUID,
        kind: str,
        expected_acl_revision: int,
        proposed_acl_revision: int,
        snapshot: AccessSnapshot,
        previous_version_key: str | None,
        now: dt.datetime,
    ) -> SecurityLogRecord:
        record = SecurityLogRecord(
            operation_id=str(operation_id),
            workspace_id=str(workspace_id),
            phase="prepared",
            kind=kind,
            expected_acl_revision=expected_acl_revision,
            proposed_acl_revision=proposed_acl_revision,
            snapshot=snapshot,
            previous_version_key=previous_version_key,
            digest=snapshot.digest(),
            written_at=now.isoformat(),
        )
        key = self._key(str(workspace_id), str(operation_id), "prepared")
        existing = await self._storage.put_if_absent(key, record.to_json())
        if existing is not None:
            # Повтор той же записи идемпотентен; несовпадение — конфликт.
            stored = json.loads(existing)
            if stored.get("digest") != record.digest:
                raise SecurityLogConflict(f"Запись {key} уже существует с другим содержимым")
        return record

    async def write_committed(
        self,
        *,
        operation_id: uuid.UUID,
        workspace_id: uuid.UUID,
        kind: str,
        applied_acl_revision: int,
        snapshot: AccessSnapshot,
        now: dt.datetime,
    ) -> SecurityLogRecord:
        prepared_key = self._key(str(workspace_id), str(operation_id), "prepared")
        prepared = await self._storage.get(prepared_key)
        if prepared is None:
            raise SecurityLogConflict("Нет prepared записи для подтверждения")
        record = SecurityLogRecord(
            operation_id=str(operation_id),
            workspace_id=str(workspace_id),
            phase="committed",
            kind=kind,
            expected_acl_revision=json.loads(prepared)["expected_acl_revision"],
            proposed_acl_revision=applied_acl_revision,
            snapshot=snapshot,
            previous_version_key=prepared_key,
            digest=snapshot.digest(),
            written_at=now.isoformat(),
        )
        key = self._key(str(workspace_id), str(operation_id), "committed")
        existing = await self._storage.put_if_absent(key, record.to_json())
        if existing is not None:
            stored = json.loads(existing)
            if stored.get("digest") != record.digest:
                raise SecurityLogConflict(f"Запись {key} уже существует с другим содержимым")
        return record

    async def write_aborted(
        self,
        *,
        operation_id: uuid.UUID,
        workspace_id: uuid.UUID,
        kind: str,
        snapshot: AccessSnapshot,
        reason: str,
        now: dt.datetime,
    ) -> SecurityLogRecord:
        """Доказанный отказ: изменение не применено и не будет применено (R-11).

        Запись нужна, чтобы отклонённая операция не выглядела незавершённой
        после восстановления и не держала бюджет в карантине.
        """
        record = SecurityLogRecord(
            operation_id=str(operation_id),
            workspace_id=str(workspace_id),
            phase="aborted",
            kind=kind,
            expected_acl_revision=snapshot.acl_revision,
            proposed_acl_revision=snapshot.acl_revision,
            snapshot=snapshot,
            previous_version_key=self._key(str(workspace_id), str(operation_id), "prepared"),
            digest=snapshot.digest(),
            written_at=now.isoformat(),
        )
        key = self._key(str(workspace_id), str(operation_id), "aborted")
        existing = await self._storage.put_if_absent(key, record.to_json())
        if existing is not None:
            stored = json.loads(existing)
            if stored.get("digest") != record.digest:
                raise SecurityLogConflict(f"Запись {key} уже существует с другим содержимым")
        return record

    async def last_committed(self, workspace_id: uuid.UUID) -> SecurityLogRecord | None:
        """Последняя подтверждённая версия доступа.

        При отсутствии доказуемой последней версии restore не открывает бюджет
        (ADR-14): вызывающий код обязан отличать None от ошибки хранилища.
        """
        keys = await self._storage.list_keys(f"ws/{workspace_id}/op/")
        best: SecurityLogRecord | None = None
        for key in keys:
            if not key.endswith("committed.json"):
                continue
            body = await self._storage.get(key)
            if body is None:
                continue
            data = json.loads(body)
            record = SecurityLogRecord(
                operation_id=data["operation_id"],
                workspace_id=data["workspace_id"],
                phase=data["phase"],
                kind=data["kind"],
                expected_acl_revision=data["expected_acl_revision"],
                proposed_acl_revision=data["proposed_acl_revision"],
                snapshot=AccessSnapshot(
                    workspace_id=data["snapshot"]["workspace_id"],
                    state=data["snapshot"]["state"],
                    admin_user_id=data["snapshot"]["admin_user_id"],
                    acl_revision=data["snapshot"]["acl_revision"],
                    members=tuple(data["snapshot"]["members"]),
                ),
                previous_version_key=data["previous_version_key"],
                digest=data["digest"],
                written_at=data["written_at"],
            )
            if best is None or record.proposed_acl_revision > best.proposed_acl_revision:
                best = record
        return best

    async def pending_operations(self, workspace_id: uuid.UUID) -> list[str]:
        """prepared без доказанного commit — основание карантина (AR-31)."""
        keys = await self._storage.list_keys(f"ws/{workspace_id}/op/")
        prepared = {key.rsplit("/", 2)[-2] for key in keys if key.endswith("prepared.json")}
        committed = {key.rsplit("/", 2)[-2] for key in keys if key.endswith("committed.json")}
        # Доказанный отказ так же определён, как и подтверждение (R-11).
        aborted = {key.rsplit("/", 2)[-2] for key in keys if key.endswith("aborted.json")}
        return sorted(prepared - committed - aborted)


def build_security_log(settings: SecurityLogSettings) -> SecurityLog:
    if settings.backend == "filesystem":
        return SecurityLog(FilesystemSecurityLog(settings.root))
    raise TemporarilyUnavailable(
        "Backend журнала доступа 's3' требует настроенного хранилища (BL-04)"
    )
