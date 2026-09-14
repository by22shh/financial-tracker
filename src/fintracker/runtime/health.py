"""Liveness и readiness (ADR-13, OPS-02).

Liveness показывает состояние процесса; readiness — возможность долговечно
принять работу и совместимость схемы. Недоступность AI не выводит ручной API
из readiness (NFR-14).
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from fintracker.config import Settings
from fintracker.db.session import RuntimeRole, get_sessionmaker, session_scope


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    database: bool
    schema_current: bool
    schema_revision: str | None
    storage_writable: bool
    ai_configured: bool
    asr_configured: bool
    telegram_configured: bool
    detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "database": self.database,
            "schema_current": self.schema_current,
            "schema_revision": self.schema_revision,
            "storage_writable": self.storage_writable,
            # Внешние интеграции показываются справочно и не блокируют readiness.
            "integrations": {
                "ai": self.ai_configured,
                "asr": self.asr_configured,
                "telegram": self.telegram_configured,
            },
            "detail": self.detail,
        }


def migrations_path() -> str:
    """Каталог миграций внутри установленного пакета (OPS-04).

    Каталог берётся от модуля, а не от текущего рабочего каталога: в образе
    приложения исходного дерева нет, и readiness иначе не знала бы требуемую
    ревизию схемы.
    """
    import fintracker.db as db_package

    return str(pathlib.Path(db_package.__file__).resolve().parent / "migrations")


def expected_schema_revision() -> str:
    """Ревизия схемы, требуемая этим кодом (OPS-04)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", migrations_path())
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    return head or ""


async def check_readiness(
    settings: Settings, role: RuntimeRole = RuntimeRole.API
) -> ReadinessReport:
    database_ok = False
    schema_revision: str | None = None
    detail: str | None = None
    try:
        factory = get_sessionmaker(settings, role)
        async with factory() as session, session.begin():
            await session.execute(text("SELECT 1"))
            database_ok = True
            schema_revision = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except Exception as exc:
        detail = f"база недоступна: {type(exc).__name__}"

    try:
        expected = expected_schema_revision()
    except Exception:
        expected = ""
    schema_current = bool(expected) and schema_revision == expected
    if database_ok and not schema_current:
        detail = detail or (
            f"схема {schema_revision or 'не применена'} не совпадает с требуемой {expected}"
        )

    # Без записываемых каталогов объектов и журнала доступа создание бюджета и
    # выгрузка недоступны, хотя база отвечает (ADR-11, ADR-14, OPS-02, G-28).
    storage_ok, storage_detail = _writable_backends(settings)
    if storage_detail and not detail:
        detail = storage_detail

    return ReadinessReport(
        ready=database_ok and schema_current and storage_ok,
        database=database_ok,
        schema_current=schema_current,
        schema_revision=schema_revision,
        storage_writable=storage_ok,
        ai_configured=settings.ai.enabled,
        asr_configured=settings.asr.available,
        telegram_configured=settings.telegram.configured,
        detail=detail,
    )


def _writable_backends(settings: Settings) -> tuple[bool, str | None]:
    """Доступны ли на запись хранилище объектов и журнал доступа (OPS-02)."""
    from fintracker.infra.security_log import build_security_log
    from fintracker.infra.storage import build_storage

    for name, factory in (
        ("хранилище объектов", lambda: build_storage(settings.storage)),
        ("журнал доступа", lambda: build_security_log(settings.security_log)),
    ):
        try:
            factory()
        except Exception as exc:
            return False, f"{name} недоступно: {type(exc).__name__}"
    return True, None


@dataclass(frozen=True, slots=True)
class OperationalMetrics:
    """Наблюдаемые показатели работы (NFR-13, OPS-02).

    Собираются из собственного состояния: возраст очереди, незавершённые
    доставки, неисполненные попытки AI и давность обслуживания. Оповещение по
    порогам выполняет система мониторинга выбранной среды (BL-04).
    """

    queue_oldest_seconds: float
    queue_failed: int
    delivery_oldest_seconds: float
    delivery_unfinished: int
    ai_reservations_open: int
    retention_last_seconds: float | None
    schema_revision: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "queue_oldest_seconds": round(self.queue_oldest_seconds, 3),
            "queue_failed": self.queue_failed,
            "delivery_oldest_seconds": round(self.delivery_oldest_seconds, 3),
            "delivery_unfinished": self.delivery_unfinished,
            "ai_reservations_open": self.ai_reservations_open,
            "retention_last_seconds": (
                round(self.retention_last_seconds, 3)
                if self.retention_last_seconds is not None
                else None
            ),
            "schema_revision": self.schema_revision,
        }


async def collect_metrics(settings: Settings) -> OperationalMetrics:
    """Снять операционные показатели одной короткой транзакцией (NFR-13)."""
    from sqlalchemy import func, select

    from fintracker.db.models.platform import AICostReservation, Job, NotificationDelivery

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        now = (await session.execute(text("SELECT now()"))).scalar_one()
        queue_oldest = (
            await session.execute(
                select(func.min(Job.available_at)).where(
                    Job.state.in_(("queued", "retry_wait")), Job.available_at <= now
                )
            )
        ).scalar_one_or_none()
        queue_failed = int(
            (
                await session.execute(
                    select(func.count()).select_from(Job).where(Job.state == "failed")
                )
            ).scalar_one()
        )
        delivery_oldest = (
            await session.execute(
                select(func.min(NotificationDelivery.available_at)).where(
                    NotificationDelivery.state.in_(("pending", "failed")),
                    NotificationDelivery.available_at <= now,
                )
            )
        ).scalar_one_or_none()
        delivery_unfinished = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(NotificationDelivery)
                    .where(NotificationDelivery.state.in_(("pending", "failed")))
                )
            ).scalar_one()
        )
        reservations = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AICostReservation)
                    .where(AICostReservation.state == "reserved")
                )
            ).scalar_one()
        )
        retention_last = (
            await session.execute(
                select(func.max(Job.updated_at)).where(
                    Job.job_type == "retention_sweep", Job.state == "succeeded"
                )
            )
        ).scalar_one_or_none()
        schema_revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()

    def age(value: Any) -> float:
        return (now - value).total_seconds() if value is not None else 0.0

    return OperationalMetrics(
        queue_oldest_seconds=age(queue_oldest),
        queue_failed=queue_failed,
        delivery_oldest_seconds=age(delivery_oldest),
        delivery_unfinished=delivery_unfinished,
        ai_reservations_open=reservations,
        retention_last_seconds=age(retention_last) if retention_last is not None else None,
        schema_revision=schema_revision,
    )
