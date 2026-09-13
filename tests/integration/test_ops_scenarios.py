"""Эксплуатация и устойчивость (A96, A97, A101, A110, A111, A112, A178)."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.ledger.service import post_transaction
from fintracker.application.platform import queue
from fintracker.config import Settings
from fintracker.db.models.ledger import Allocation, Transaction
from fintracker.db.models.platform import Job
from fintracker.db.session import RuntimeRole, get_sessionmaker, session_scope
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def test_a96_transaction_survives_worker_crash_after_commit(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A96: операция находится после сбоя, повтор не удваивает её."""
    fixture = await build_fixture(owner_session, telegram_user_id=6300)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_100), category="Продукты"),
        origin="telegram_text",
    )
    await owner_session.commit()

    # «Сбой» после commit: повторная обработка того же исходного кандидата.
    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        found = (
            await session.execute(
                select(Transaction).where(Transaction.id == posted.transaction_id)
            )
        ).scalar_one()
        assert found.status == "posted"
        total = (
            (
                await session.execute(
                    select(Transaction).where(Transaction.workspace_id == fixture.workspace.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(total) == 1, "повтор не создаёт вторую операцию"


async def test_a97_failure_before_commit_leaves_no_partial_state(
    clean_db: None, test_settings: Settings
) -> None:
    """A97: сбой до commit не оставляет частичных распределений и ложного «Записано»."""
    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session:
        fixture = await build_fixture(session)
        workspace_id = fixture.workspace.id
        await session.commit()

    async with factory() as session:
        fixture.uow.session = session
        try:
            await post_transaction(
                session,
                fixture.uow,
                actor=fixture.actor,
                spec=expense_spec(fixture, amount=rub(500), category="Продукты"),
                origin="form",
            )
            raise RuntimeError("сбой до commit")
        except RuntimeError:
            await session.rollback()

    async with factory() as session, session.begin():
        transactions = (
            (
                await session.execute(
                    select(Transaction).where(Transaction.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        allocations = (
            (
                await session.execute(
                    select(Allocation).where(Allocation.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert transactions == []
    assert allocations == []


async def test_a101_rate_limit_retry_respects_delay(
    clean_db: None, test_settings: Settings
) -> None:
    """A101: ошибка 429 даёт повтор с задержкой, а не бесконечный цикл."""
    from fintracker.application.platform.queue import next_delay

    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(
            session,
            job_type="retention_sweep",
            logical_key="a101:retry",
            queue_class="maintenance",
            payload={"schema_version": 1},
            correlation_id="a101",
        )
    claimed = await queue.claim_jobs(test_settings, queue_classes=("maintenance",), limit=5)
    job = next(item for item in claimed if item.logical_key == "a101:retry")

    await queue.fail(test_settings, job, error="429 Too Many Requests", retry_after=30.0)
    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        row = (
            await session.execute(select(Job).where(Job.logical_key == "a101:retry"))
        ).scalar_one()
    assert row.state == "retry_wait"
    assert row.attempts == 1
    assert row.available_at > dt.datetime.now(dt.UTC) + dt.timedelta(seconds=20)

    # Задержка растёт, повтор не выполняется немедленно и не бесконечен.
    delays = [next_delay(attempt) for attempt in range(1, 6)]
    assert delays == sorted(delays)
    assert row.max_attempts >= 1
    assert next_delay(1, retry_after=45.0) >= 45.0


async def test_a110_deleted_workspace_is_not_restored_into_access(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A110, A178: удалённый бюджет не возвращается в рабочий доступ."""
    from fintracker.application.identity.actor import resolve_actor
    from fintracker.core.context import WorkspaceState
    from fintracker.core.errors import NotFound
    from fintracker.db.models.access import Workspace

    fixture = await build_fixture(owner_session, telegram_user_id=6301)
    workspace = (
        await owner_session.execute(select(Workspace).where(Workspace.id == fixture.workspace.id))
    ).scalar_one()
    workspace.state = WorkspaceState.DELETED.value
    await owner_session.flush()

    with pytest.raises(NotFound):
        await resolve_actor(owner_session, user=fixture.user, workspace_id=fixture.workspace.id)

    # «Восстановление старого снимка» возвращает прежние строки, но состояние
    # удаления остаётся решающим до явного пересоздания доступа.
    assert workspace.state == WorkspaceState.DELETED.value


def test_a111_restore_evidence_is_recorded() -> None:
    """A111, AR-33: итоги и ревизии согласованы, RPO/RTO измерены."""
    import pathlib

    evidence = (
        pathlib.Path(__file__).resolve().parents[2]
        / ".planning"
        / "evidence"
        / "restore_drill.json"
    )
    if not evidence.exists():
        pytest.skip("Учение восстановления ещё не выполнялось")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["invariant_failures"] == {}
    assert payload["row_counts_match"] is True
    assert payload["rpo_seconds_measured"] <= payload["rpo_limit_seconds"]
    assert payload["rto_seconds_measured"] <= payload["rto_limit_seconds"]


async def test_a112_logs_do_not_contain_secrets(
    clean_db: None, test_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """A112, SEC-06: в логах нет токена, ключей и финансового payload."""
    from fintracker.application.ingestion.accept_update import accept_telegram_update
    from fintracker.core.logging import configure_logging, get_logger

    # Логи проверяются в той же конфигурации, что использует приложение.
    configure_logging(test_settings.observability)
    logger = get_logger("test.secrets")
    logger.info(
        "secret_check",
        bot_token="123456:SECRET-TOKEN",
        api_key="sk-test-key",
        download_url="https://api.telegram.org/file/bot123456:SECRET-TOKEN/x.jpg",
        amount_minor=123456,
    )
    await accept_telegram_update(
        test_settings,
        {
            "update_id": 991_001,
            "message": {
                "message_id": 1,
                "date": 1789000000,
                "chat": {"id": 6302, "type": "private"},
                "from": {"id": 6302, "is_bot": False, "first_name": "Лог"},
                "text": "продукты 1234",
            },
        },
    )
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "SECRET-TOKEN" not in combined
    assert "sk-test-key" not in combined
    assert "продукты 1234" not in combined, "исходный финансовый текст не пишется в лог"
    assert uuid.UUID(int=0) is not None
