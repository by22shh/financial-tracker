"""Analysis interruption, fencing and durable single publication on PostgreSQL."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from fintracker.application.intelligence import analysis
from fintracker.application.intelligence.schedule import handle_run_analysis
from fintracker.application.platform import queue
from fintracker.core.errors import ProviderUnavailable, TemporarilyUnavailable
from fintracker.core.fencing import execution_fence
from fintracker.db.models.access import Workspace
from fintracker.db.models.intelligence import AnalysisRun
from fintracker.db.models.platform import AICostReservation, AIQuotaCounter, Job, OutboxEvent
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from tests.conftest import requires_pg
from tests.integration.test_recommendations import TODAY, _complete_fixture, recommendation_json

pytestmark = [pytest.mark.pg, requires_pg]


@pytest.fixture
def configured(test_settings):
    settings = test_settings.model_copy(deep=True)
    settings.ai.enabled = True
    yield settings
    set_provider_override(None)


def arguments(fixture, key="recovery:test"):
    return {
        "workspace_id": fixture.workspace.id,
        "run_kind": "weekly_review",
        "logical_key": key,
        "today": TODAY,
    }


async def completion_count(settings):
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        return await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "AnalysisCompleted")
        )


async def analysis_job(settings, fixture):
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(
            session,
            job_type="run_analysis",
            logical_key="recovery:job",
            queue_class="review",
            workspace_id=fixture.workspace.id,
            payload={"analysis_key": "recovery:test", "analysis_date": TODAY.isoformat()},
        )
    return (await queue.claim_jobs(settings, queue_classes=("review",)))[0]


async def test_cancelled_analysis_resumes_with_new_cost_reservation(owner_session, configured):
    fixture = await _complete_fixture(owner_session)

    class Interrupted(ScriptedAIProvider):
        async def structured(self, **kwargs):
            raise asyncio.CancelledError()

    set_provider_override(Interrupted(responses=[]))
    with pytest.raises(asyncio.CancelledError):
        await analysis.run_analysis(configured, **arguments(fixture))
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        run = (await session.scalars(select(AnalysisRun))).one()
        old_attempt = run.attempt_id
        assert run.status == "pending"
        reservation = (await session.scalars(select(AICostReservation))).one()
        assert reservation.state == "unknown"
        counters = (await session.scalars(select(AIQuotaCounter))).all()
        assert all(counter.in_flight == 0 and counter.reserved_total > 0 for counter in counters)
    provider = ScriptedAIProvider(responses=[recommendation_json(cards=[])])
    set_provider_override(provider)
    outcome = await analysis.run_analysis(configured, **arguments(fixture))
    assert outcome.status == "succeeded" and len(provider.calls) == 1
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        run = (await session.scalars(select(AnalysisRun))).one()
        assert run.attempt_id != old_attempt
        reservations = (await session.scalars(select(AICostReservation))).all()
        assert len(reservations) == 2 and len({row.request_key for row in reservations}) == 2
    assert await completion_count(configured) == 1


async def test_live_parallel_analysis_does_not_start_second_request(owner_session, configured):
    fixture = await _complete_fixture(owner_session)
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(ScriptedAIProvider):
        async def structured(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().structured(**kwargs)

    provider = Waiting(responses=[recommendation_json(cards=[])])
    set_provider_override(provider)
    first = asyncio.create_task(analysis.run_analysis(configured, **arguments(fixture)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(TemporarilyUnavailable):
            await analysis.run_analysis(configured, **arguments(fixture))
        release.set()
        assert (await first).status == "succeeded"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    assert len(provider.calls) == 1
    assert await completion_count(configured) == 1


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("invalidate", ["lease", "quarantine", "security_fence"])
async def test_result_and_fallback_recheck_authority(
    owner_session, configured, fallback, invalidate
):
    fixture = await _complete_fixture(owner_session)
    job = await analysis_job(configured, fixture)

    class Invalidating(ScriptedAIProvider):
        async def structured(self, **kwargs):
            async with session_scope(configured, RuntimeRole.OWNER) as session:
                if invalidate == "lease":
                    await session.execute(
                        update(Job).where(Job.id == job.id).values(lease_token=uuid.uuid4())
                    )
                else:
                    values = (
                        {"quarantined": True}
                        if invalidate == "quarantine"
                        else {"security_fence": uuid.uuid4()}
                    )
                    await session.execute(
                        update(Workspace)
                        .where(Workspace.id == fixture.workspace.id)
                        .values(**values)
                    )
            if fallback:
                raise ProviderUnavailable("Fixture unavailable")
            return await super().structured(**kwargs)

    set_provider_override(Invalidating(responses=[recommendation_json(cards=[])]))
    with pytest.raises(TemporarilyUnavailable):
        await handle_run_analysis(configured, job)
    assert await completion_count(configured) == 0
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        run = (await session.scalars(select(AnalysisRun))).one()
        assert run.status == "running" and run.published_at is None and not run.summary


async def test_takeover_of_crashed_attempt_rejects_late_old_result(owner_session, configured):
    fixture = await _complete_fixture(owner_session)
    job = await analysis_job(configured, fixture)
    async with execution_fence(queue.lease_fence(job), job_id=job.id, lease_token=job.lease_token):
        old, _ = await analysis._prepare_analysis(configured, **arguments(fixture))
    assert old is not None
    # No cancellation cleanup: this represents a worker killed after preparing.
    reservation = await analysis.quota.reserve(
        configured,
        request_key=f"analysis:{old.run_id}:{old.attempt_id}",
        upper_bound=Decimal("0.01"),
        workspace_id=fixture.workspace.id,
        purpose="recommendation",
    )
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        await session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(lease_until=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
    new_job = (await queue.claim_jobs(configured, queue_classes=("review",)))[0]
    provider = ScriptedAIProvider(
        responses=[recommendation_json(cards=[]), recommendation_json(cards=[])]
    )
    set_provider_override(provider)
    await handle_run_analysis(configured, new_job)
    result = await analysis._generate_cards(configured, old)
    # Even with no context fence, persisted attempt identity rejects the late result.
    with pytest.raises(TemporarilyUnavailable):
        await analysis._store_analysis(configured, old, result, correlation_id="late")
    assert await completion_count(configured) == 1
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        row = await session.scalar(
            select(AICostReservation).where(
                AICostReservation.request_key == reservation.request_key
            )
        )
        assert row.state == "unknown"


@pytest.mark.parametrize("ai_enabled", [True, False])
async def test_publication_is_single_and_retry_reads_persisted_summary(
    owner_session, configured, ai_enabled
):
    fixture = await _complete_fixture(owner_session)
    configured.ai.enabled = ai_enabled
    job = await analysis_job(configured, fixture)
    provider = ScriptedAIProvider(responses=[recommendation_json(cards=[])])
    set_provider_override(provider)
    await handle_run_analysis(configured, job)
    await handle_run_analysis(configured, job)
    outcome = await analysis.run_analysis(configured, **arguments(fixture))
    assert outcome.summary
    assert await completion_count(configured) == 1
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        jobs = (await session.scalars(select(Job).where(Job.job_type == "expand_outbox"))).all()
        assert len(jobs) == 1
    assert len(provider.calls) == (1 if ai_enabled else 0)


async def test_expired_direct_attempt_is_resumed(owner_session, configured):
    fixture = await _complete_fixture(owner_session)
    preparation, _ = await analysis._prepare_analysis(configured, **arguments(fixture))
    assert preparation is not None
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        await session.execute(
            update(AnalysisRun)
            .where(AnalysisRun.id == preparation.run_id)
            .values(attempt_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
    set_provider_override(ScriptedAIProvider(responses=[recommendation_json(cards=[])]))
    result = await analysis.run_analysis(configured, **arguments(fixture))
    assert result.run_id == preparation.run_id and result.status == "succeeded"
    assert await completion_count(configured) == 1


async def test_failed_expansion_enqueue_rolls_back_completion(
    owner_session, configured, monkeypatch
):
    fixture = await _complete_fixture(owner_session)
    provider = ScriptedAIProvider(
        responses=[recommendation_json(cards=[]), recommendation_json(cards=[])]
    )
    set_provider_override(provider)
    enqueue = queue.enqueue

    async def fail_expansion(*args, **kwargs):
        if kwargs.get("job_type") == "expand_outbox":
            raise RuntimeError("Crash before committing expansion task")
        return await enqueue(*args, **kwargs)

    monkeypatch.setattr(queue, "enqueue", fail_expansion)
    with pytest.raises(RuntimeError, match="Crash before"):
        await analysis.run_analysis(configured, **arguments(fixture))
    assert await completion_count(configured) == 0
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        run = (await session.scalars(select(AnalysisRun))).one()
        assert run.status == "pending" and run.published_at is None and not run.summary
    monkeypatch.setattr(queue, "enqueue", enqueue)
    outcome = await analysis.run_analysis(configured, **arguments(fixture))
    assert outcome.status == "succeeded" and await completion_count(configured) == 1


async def test_prepare_rejects_expired_job_before_request(owner_session, configured):
    fixture = await _complete_fixture(owner_session)
    job = await analysis_job(configured, fixture)
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        await session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(lease_until=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
    provider = ScriptedAIProvider(responses=[recommendation_json(cards=[])])
    set_provider_override(provider)
    with pytest.raises(TemporarilyUnavailable):
        await handle_run_analysis(configured, job)
    assert not provider.calls
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        assert await session.scalar(select(func.count()).select_from(AnalysisRun)) == 0


async def test_crashed_reservation_releases_slot_when_retry_disables_ai(owner_session, configured):
    fixture = await _complete_fixture(owner_session)
    old, _ = await analysis._prepare_analysis(configured, **arguments(fixture))
    assert old is not None
    await analysis.quota.reserve(
        configured,
        request_key=f"analysis:{old.run_id}:{old.attempt_id}",
        upper_bound=Decimal("0.01"),
        workspace_id=fixture.workspace.id,
        purpose="recommendation",
    )
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        await session.execute(
            update(AnalysisRun)
            .where(AnalysisRun.id == old.run_id)
            .values(attempt_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
    configured.ai.enabled = False
    result = await analysis.run_analysis(configured, **arguments(fixture))
    assert result.status == "fallback" and result.summary
    async with session_scope(configured, RuntimeRole.OWNER) as session:
        assert (await session.scalars(select(AICostReservation))).one().state == "unknown"
        counters = (await session.scalars(select(AIQuotaCounter))).all()
        assert all(row.in_flight == 0 and row.reserved_total > 0 for row in counters)


async def test_unpublished_legacy_summary_is_recovered_once(owner_session, configured):
    fixture = await _complete_fixture(owner_session)
    snapshot, _ = await analysis.build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    owner_session.add(
        AnalysisRun(
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="recovery:test",
            snapshot_id=snapshot.id,
            status="succeeded",
            summary="",
            finished_at=dt.datetime.now(dt.UTC),
        )
    )
    await owner_session.commit()
    provider = ScriptedAIProvider(responses=[])
    set_provider_override(provider)
    result = await analysis.run_analysis(configured, **arguments(fixture))
    repeated = await analysis.run_analysis(configured, **arguments(fixture))
    assert result.summary == repeated.summary and "Период:" in result.summary
    assert result.status == "fallback" and not provider.calls
    assert await completion_count(configured) == 1
