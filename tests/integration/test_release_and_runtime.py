"""Выпуск, совместимость и готовность процессов (ADR-01, ADR-15, OPS-01, OPS-02, OPS-04)."""

from __future__ import annotations

import asyncio
import pathlib
import subprocess

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.config import Settings
from fintracker.db.session import RuntimeRole, get_sessionmaker
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_adr01_single_artifact_runs_all_roles() -> None:
    """ADR-01: api, worker и scheduler запускаются из одного артефакта."""
    from fintracker.runtime.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])

    parser_output = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "fintracker"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert parser_output.returncode == 0
    for command in ("api", "worker", "scheduler", "check"):
        assert command in parser_output.stdout


async def test_ops02_readiness_is_separate_from_liveness(
    clean_db: None, test_settings: Settings
) -> None:
    """OPS-02: liveness отделён от readiness, недоступность AI её не блокирует."""
    from httpx import ASGITransport, AsyncClient

    from fintracker.api.app import create_app
    from fintracker.runtime.health import check_readiness

    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.status_code == 200, ready.text

    report = await check_readiness(test_settings, RuntimeRole.API)
    payload = report.to_payload()
    assert payload["integrations"]["ai"] is False, "AI не настроен в тестовой среде"
    assert report.ready is True, "недоступность AI не блокирует readiness"
    assert payload["schema_revision"], "версия схемы показана в readiness"


async def test_adr15_ops01_migrations_run_as_separate_step(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """ADR-15, OPS-01: миграции выполняются отдельным заданием до нового кода."""
    current = (
        await owner_session.execute(text("SELECT version_num FROM alembic_version"))
    ).scalar_one()
    assert current, "версия схемы зафиксирована в базе"

    versions = sorted(
        path.name
        for path in (ROOT / "src/fintracker/db/migrations/versions").glob("*.py")
        if not path.name.startswith("__")
    )
    assert len(versions) >= 2, "история миграций сохранена отдельными ревизиями"

    # Отдельное задание существует и не совмещено с запуском приложения.
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "migrate:" in makefile
    assert "alembic upgrade head" in makefile


async def test_ops04_job_payload_carries_schema_version(
    clean_db: None, test_settings: Settings
) -> None:
    """OPS-04: полезная нагрузка задачи несёт версию схемы для совместимости."""
    from fintracker.application.platform import queue
    from fintracker.db.models.platform import Job
    from fintracker.db.session import session_scope

    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(
            session,
            job_type="retention_sweep",
            logical_key="ops04:schema",
            queue_class="maintenance",
            payload={"schema_version": 1},
            correlation_id="ops04",
        )
    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        row = (
            await session.execute(select(Job).where(Job.logical_key == "ops04:schema"))
        ).scalar_one()
    assert row.payload["schema_version"] == 1
    assert row.payload_version >= 1, "версия формата payload зафиксирована"


def test_adr16_architecture_change_has_measured_basis() -> None:
    """ADR-16: изменение архитектуры опирается на измеренное ограничение."""
    import json

    migration = (
        ROOT / "src/fintracker/db/migrations/versions/0007_money_join_indexes.py"
    ).read_text(encoding="utf-8")
    assert "Измеренное ограничение" in migration
    assert "NFR-06" in migration and "AR-34" in migration

    evidence = ROOT / ".planning/evidence/performance.json"
    if not evidence.exists():
        pytest.skip("Измерение производительности ещё не выполнялось")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    report = payload["nfr06_report"]
    assert report["volume"] >= 50_000
    assert report["p95_seconds"] <= 2.0, "после изменения требование выполняется"


def test_ops03_secrets_are_outside_repository() -> None:
    """OPS-03: раздельные среды, секреты не хранятся в репозитории."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FINTRACKER_ENV" in example
    for line in example.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        upper = key.upper()
        if upper.endswith(("_VERSION", "_ID", "_URL", "_MODE")):
            continue
        if any(word in upper for word in ("TOKEN", "KEY", "PASSWORD", "SECRET")):
            cleaned = value.strip().strip('"')
            placeholder = not cleaned or cleaned.startswith("<") or cleaned.startswith("change-me")
            assert placeholder, f"в примере окружения не должно быть значения секрета: {key}"
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=False
    ).stdout.splitlines()
    assert ".env" not in tracked
    assert not any(name.startswith(".research-private") for name in tracked)


async def test_ops05_downgrade_and_upgrade_are_reversible(
    clean_db: None, test_settings: Settings
) -> None:
    """OPS-05: документированный откат приложения проверен на последней миграции."""
    import os

    env = dict(os.environ)
    env["FINTRACKER_DB__OWNER_DSN"] = test_settings.db.owner_dsn

    down = await asyncio.to_thread(
        subprocess.run,
        [str(ROOT / ".venv/bin/alembic"), "downgrade", "-1"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    assert down.returncode == 0, down.stderr[-2000:]

    up = await asyncio.to_thread(
        subprocess.run,
        [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    assert up.returncode == 0, up.stderr[-2000:]

    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    from fintracker.runtime.health import expected_schema_revision

    assert revision == expected_schema_revision()


def test_ops04_expected_revision_resolves_outside_repository() -> None:
    """OPS-04: требуемая ревизия схемы известна и без исходного дерева.

    В образе приложения нет ни `alembic.ini`, ни каталога `src`: readiness
    обязана определять требуемую ревизию по установленному пакету, иначе она
    всегда отрицательна при работающей базе.
    """
    import os
    import tempfile

    from fintracker.runtime.health import expected_schema_revision, migrations_path

    assert pathlib.Path(migrations_path(), "versions").is_dir()
    previous = os.getcwd()
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            revision = expected_schema_revision()
        finally:
            os.chdir(previous)
    assert revision, "требуемая ревизия схемы не определена вне каталога проекта"


async def test_ops02_readiness_requires_writable_backends(
    clean_db: None, test_settings: Settings, tmp_path: pathlib.Path
) -> None:
    """OPS-02, G-28: готовность учитывает доступность каталогов объектов.

    В образе с настройками по умолчанию каталоги могли быть недоступны на
    запись, а readiness этого не замечала: создание бюджета и выгрузка падали
    при `ready: true`.
    """
    from fintracker.runtime.health import check_readiness

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    broken = test_settings.model_copy(deep=True)
    broken.storage.root = blocked / "objects"
    try:
        report = await check_readiness(broken)
    finally:
        blocked.chmod(0o700)
    assert report.storage_writable is False
    assert report.ready is False
    assert report.detail and "хранилище объектов" in report.detail

    healthy = await check_readiness(test_settings)
    assert healthy.storage_writable is True
