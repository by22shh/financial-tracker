"""Liveness и readiness (ADR-13, OPS-02).

Liveness показывает состояние процесса; readiness — возможность долговечно
принять работу и совместимость схемы. Недоступность AI не выводит ручной API
из readiness (NFR-14).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from fintracker.config import Settings
from fintracker.db.session import RuntimeRole, get_sessionmaker


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    database: bool
    schema_current: bool
    schema_revision: str | None
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
            # Внешние интеграции показываются справочно и не блокируют readiness.
            "integrations": {
                "ai": self.ai_configured,
                "asr": self.asr_configured,
                "telegram": self.telegram_configured,
            },
            "detail": self.detail,
        }


def expected_schema_revision() -> str:
    """Ревизия схемы, требуемая этим кодом (OPS-04)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config("alembic.ini")
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

    return ReadinessReport(
        ready=database_ok and schema_current,
        database=database_ok,
        schema_current=schema_current,
        schema_revision=schema_revision,
        ai_configured=settings.ai.enabled,
        asr_configured=settings.asr.available,
        telegram_configured=settings.telegram.configured,
        detail=detail,
    )
