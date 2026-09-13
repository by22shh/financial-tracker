"""Runtime access recovery and executable downgrade regressions (V-05/V-06)."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.api.app import create_app
from fintracker.application.identity.security_change import resume_or_quarantine
from fintracker.config import Settings
from fintracker.db.models.access import BudgetDeletionRecord, Membership, User, Workspace
from fintracker.db.session import RuntimeRole, dispose_engines, session_scope
from fintracker.infra.security_log import AccessSnapshot, FilesystemSecurityLog, SecurityLog
from fintracker.runtime.health import check_readiness
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture

pytestmark = [pytest.mark.pg, requires_pg]
ROOT = Path(__file__).resolve().parents[2]


async def test_api_start_without_migration_credentials(
    owner_session: AsyncSession, test_settings: Settings, tmp_path: Path
) -> None:
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    configured = test_settings.model_copy(deep=True)
    configured.security_log.root = tmp_path / "access-log"
    configured.db.owner_dsn = "postgresql+psycopg://unavailable:unused@127.0.0.1:1/unused"
    await dispose_engines()
    try:
        assert (await check_readiness(configured, RuntimeRole.API)).ready
        async with (
            LifespanManager(create_app(configured)),
            session_scope(
                configured, RuntimeRole.WORKER, workspace_id=fixture.workspace.id
            ) as session,
        ):
            assert (
                await session.execute(select(Workspace.id))
            ).scalar_one() == fixture.workspace.id
            assert not (
                await session.execute(
                    text(
                        "SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).scalar_one()
    finally:
        await dispose_engines()


@pytest.mark.parametrize("restored_state", ["active", "deleted"])
async def test_worker_replays_complete_acl_and_deletion_without_owner(
    owner_session: AsyncSession, test_settings: Settings, tmp_path: Path, restored_state: str
) -> None:
    fixture = await build_fixture(owner_session)
    fixture.workspace.state = restored_state
    new_admin = User(telegram_user_id=99001)
    extra_user = User(telegram_user_id=99002)
    owner_session.add_all([new_admin, extra_user])
    await owner_session.flush()
    owner_session.add(
        Membership(
            workspace_id=fixture.workspace.id,
            user_id=extra_user.id,
            role="member",
            status="active",
            generation=uuid.uuid4(),
        )
    )
    new_generation = uuid.uuid4()
    snapshot = AccessSnapshot(
        workspace_id=str(fixture.workspace.id),
        state="deleted",
        admin_user_id=str(new_admin.id),
        acl_revision=10,
        members=(
            {
                "user_id": str(fixture.user.id),
                "role": "member",
                "status": "removed",
                "generation": str(fixture.actor.membership_generation),
                "rejoin_blocked": "True",
            },
            {
                "user_id": str(new_admin.id),
                "role": "admin",
                "status": "active",
                "generation": str(new_generation),
                "rejoin_blocked": "False",
            },
        ),
    )
    await owner_session.commit()
    journal = SecurityLog(FilesystemSecurityLog(tmp_path / "journal"))
    operation_id = uuid.uuid4()
    await journal.write_prepared(
        operation_id=operation_id,
        workspace_id=fixture.workspace.id,
        kind="workspace_delete",
        expected_acl_revision=9,
        proposed_acl_revision=10,
        snapshot=snapshot,
        previous_version_key=None,
        now=dt.datetime.now(dt.UTC),
    )
    await journal.write_committed(
        operation_id=operation_id,
        workspace_id=fixture.workspace.id,
        kind="workspace_delete",
        applied_acl_revision=10,
        snapshot=snapshot,
        now=dt.datetime.now(dt.UTC),
    )
    configured = test_settings.model_copy(deep=True)
    configured.db.owner_dsn = "postgresql+psycopg://unavailable:unused@127.0.0.1:1/unused"
    await dispose_engines()
    try:
        await resume_or_quarantine(configured, fixture.workspace.id, security_log=journal)
        async with session_scope(
            configured, RuntimeRole.WORKER, workspace_id=fixture.workspace.id
        ) as session:
            workspace = (await session.execute(select(Workspace))).scalar_one()
            assert workspace.state == "deleted" and workspace.quarantined
            assert workspace.acl_revision == 10 and workspace.admin_user_id == new_admin.id
            assert workspace.deleted_at is not None
            deletion = (await session.execute(select(BudgetDeletionRecord))).scalar_one()
            assert deletion.state == "pending"
            members = {
                member.user_id: member
                for member in (await session.execute(select(Membership))).scalars()
            }
            assert members[new_admin.id].generation == new_generation
            assert members[new_admin.id].role == "admin"
            assert members[fixture.user.id].status == "removed"
            assert (
                members[extra_user.id].status == "removed" and members[extra_user.id].rejoin_blocked
            )
        # No selected workspace exposes no memberships or financial metadata.
        async with session_scope(configured, RuntimeRole.WORKER) as session:
            assert not (await session.execute(select(Workspace))).scalars().all()
            assert not (await session.execute(select(Membership))).scalars().all()
    finally:
        await dispose_engines()


async def test_downgrade_restores_executable_purge_at_both_previous_revisions(
    owner_session: AsyncSession, test_settings: Settings
) -> None:
    first = await build_fixture(owner_session, telegram_user_id=99101)
    second = await build_fixture(owner_session, telegram_user_id=99102)
    first.workspace.state = second.workspace.state = "deleted"
    await owner_session.commit()
    root = ROOT
    env = {**os.environ, "FINTRACKER_DB__OWNER_DSN": test_settings.db.owner_dsn}

    def migrate(direction: str, target: str) -> None:
        result = subprocess.run(
            [str(root / ".venv/bin/alembic"), direction, target],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    try:
        for revision, fixture, forbidden in (
            ("0010_input", first, "author_replies"),
            ("0009_maint", second, "attachments"),
        ):
            await asyncio.to_thread(migrate, "downgrade", revision)
            async with session_scope(test_settings, RuntimeRole.WORKER) as session:
                definition = (
                    await session.execute(
                        text(
                            "SELECT pg_get_functiondef('purge_workspace_data(uuid)'::regprocedure)"
                        )
                    )
                ).scalar_one()
                assert forbidden not in definition
                removed = (
                    await session.execute(
                        text("SELECT purge_workspace_data(:id)"), {"id": fixture.workspace.id}
                    )
                ).scalar_one()
                assert removed > 0
    finally:
        await asyncio.to_thread(migrate, "upgrade", "head")
