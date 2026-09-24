"""One-off repair of the synthetic TG-03 schedule; dry-run unless --apply.

Only the exact test schedule from the Telegram audit is eligible. Paid facts
and their links are retained. Run inside the app container after a backup.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import uuid

from sqlalchemy import select

from fintracker.application.commitments.schedules import (
    change_occurrence,
    create_schedule,
    materialize_occurrences,
)
from fintracker.application.identity.actor import resolve_actor
from fintracker.config import get_settings
from fintracker.core.money import Money
from fintracker.db.models.access import User
from fintracker.db.models.commitments import Occurrence, ScheduledItem, ScheduleVersion
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.domain.schedule import ScheduleKind, ScheduleRule

WORKSPACE = uuid.UUID("025fab8b-0470-4669-ac4f-0aa0b3a6a9a4")
OWNER = uuid.UUID("b77a9eba-0652-42c8-9d03-7f067bc575b6")
SCHEDULE = uuid.UUID("1a6054ba-9032-401f-9200-5028fa37b623")


async def run(*, apply: bool) -> None:
    settings = get_settings()
    async with session_scope(
        settings,
        RuntimeRole.API,
        user_id=OWNER,
        workspace_id=WORKSPACE,
    ) as session:
        user = (await session.execute(select(User).where(User.id == OWNER))).scalar_one()
        actor = await resolve_actor(
            session,
            user=user,
            workspace_id=WORKSPACE,
            require_admin=True,
            correlation_id="tg03-synthetic-repair-20260920",
        )
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(WORKSPACE, actor=actor)
        schedule = (
            await session.execute(
                select(ScheduledItem)
                .where(
                    ScheduledItem.workspace_id == WORKSPACE,
                    ScheduledItem.id == SCHEDULE,
                )
                .with_for_update()
            )
        ).scalar_one()
        version = (
            await session.execute(
                select(ScheduleVersion).where(
                    ScheduleVersion.workspace_id == WORKSPACE,
                    ScheduleVersion.schedule_id == SCHEDULE,
                    ScheduleVersion.version == schedule.current_version,
                )
            )
        ).scalar_one()
        if schedule.archived_at is not None:
            print(json.dumps({"status": "already_repaired"}))
            return
        if (
            schedule.name != "ТЕСТ Интернет"
            or version.expected_minor != 90000
            or version.anchor_date != dt.date(2025, 9, 25)
        ):
            raise RuntimeError("Test schedule no longer matches audit evidence; no repair applied")
        occurrences = (
            (
                await session.execute(
                    select(Occurrence)
                    .where(
                        Occurrence.workspace_id == WORKSPACE,
                        Occurrence.schedule_id == SCHEDULE,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if any(row.settled_minor and row.state != "settled" for row in occurrences):
            raise RuntimeError("Partially settled occurrence requires separate reconciliation")
        cancelled = [row for row in occurrences if row.state == "planned" and not row.settled_minor]
        result: dict[str, object] = {
            "mode": "apply" if apply else "dry-run",
            "cancel_unpaid": len(cancelled),
            "preserve_paid": sum(bool(row.settled_minor) for row in occurrences),
            "replacement_anchor": "2026-09-25",
        }
        if apply:
            schedule.archived_at = dt.datetime.now(dt.UTC)
            for row in cancelled:
                await change_occurrence(
                    session,
                    uow,
                    actor=actor,
                    occurrence_id=row.id,
                    action="cancel",
                    reason="TG-03: исправление ошибочной даты тестового расписания",
                )
            replacement = await create_schedule(
                session,
                uow,
                actor=actor,
                name=schedule.name,
                direction="payment",
                rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=dt.date(2026, 9, 25)),
                currency="RUB",
                expected=Money(90000, "RUB"),
                category_id=version.category_id,
            )
            await materialize_occurrences(
                session,
                workspace_id=WORKSPACE,
                until_date=dt.date(2026, 11, 9),
            )
            result["replacement_id"] = str(replacement.id)
        print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(apply=parser.parse_args().apply))
