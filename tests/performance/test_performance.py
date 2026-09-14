"""Измеренные показатели производительности (NFR-01, NFR-03, NFR-06, NFR-07, NFR-08, AR-34).

Проверки длительные и запускаются явно: `FINTRACKER_PERF=1 pytest -m slow`.
Числа записываются в .planning/evidence/performance.json как доказательство,
а не как оценка.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import pathlib
import statistics
import time
import uuid

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.journal import list_journal
from fintracker.application.analytics.reports import FilterSpec, spending_report
from fintracker.application.ingestion.accept_update import accept_telegram_update
from fintracker.config import Settings
from fintracker.db.models.access import Workspace
from fintracker.db.models.ledger import (
    Allocation,
    CashLeg,
    FinancialEffect,
    Transaction,
    TransactionRevision,
)
from fintracker.db.session import RuntimeRole, session_scope
from tests.conftest import requires_pg
from tests.integration.factories import TZ, Fixture, build_fixture

pytestmark = [
    pytest.mark.pg,
    pytest.mark.slow,
    requires_pg,
    pytest.mark.skipif(
        os.environ.get("FINTRACKER_PERF") != "1",
        reason="Измерение производительности запускается явно: FINTRACKER_PERF=1",
    ),
]

EVIDENCE = pathlib.Path(__file__).resolve().parents[2] / ".planning" / "evidence"
VOLUME = int(os.environ.get("FINTRACKER_PERF_VOLUME", "50000"))
DAY = dt.date(2026, 9, 12)


def _record(name: str, payload: dict[str, object]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / "performance.json"
    data: dict[str, object] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data[name] = {
        **payload,
        "measured_at": dt.datetime.now(dt.UTC).isoformat(),
        "volume": VOLUME,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


async def _bulk_transactions(session: AsyncSession, fixture: Fixture, count: int) -> None:
    """Быстро наполнить журнал операциями напрямую, минуя диалог."""
    categories = list(fixture.categories.values())
    account = next(iter(fixture.accounts.values()))
    batch = 2_000
    created = 0
    while created < count:
        size = min(batch, count - created)
        transactions = []
        revisions = []
        allocations = []
        legs = []
        effects = []
        for index in range(size):
            transaction_id = uuid.uuid4()
            effect_id = uuid.uuid4()
            amount = 10_000 + (created + index) % 90_000
            occurred = DAY - dt.timedelta(days=(created + index) % 28)
            transactions.append(
                {
                    "id": transaction_id,
                    "workspace_id": fixture.workspace.id,
                    "created_by": fixture.user.id,
                    "current_revision": 1,
                    "status": "posted",
                    "occurred_sort_date": occurred,
                    "origin": "import",
                }
            )
            revisions.append(
                {
                    "id": uuid.uuid4(),
                    "workspace_id": fixture.workspace.id,
                    "transaction_id": transaction_id,
                    "revision": 1,
                    "change_kind": "created",
                    "changed_by": fixture.user.id,
                    "transaction_type": "expense",
                    "amount_minor": amount,
                    "currency": "RUB",
                    "occurred_date": occurred,
                    "date_precision": "day",
                    "timezone": TZ,
                    "granularity": "individual",
                    "description": f"Операция {created + index}",
                }
            )
            allocations.append(
                {
                    "id": uuid.uuid4(),
                    "workspace_id": fixture.workspace.id,
                    "transaction_id": transaction_id,
                    "revision": 1,
                    "stable_line_id": uuid.uuid4(),
                    "economic_role": "expense",
                    "category_id": categories[(created + index) % len(categories)],
                    "amount_minor": amount,
                }
            )
            legs.append(
                {
                    "id": uuid.uuid4(),
                    "workspace_id": fixture.workspace.id,
                    "transaction_id": transaction_id,
                    "revision": 1,
                    "account_id": account,
                    "signed_minor": -amount,
                    "coverage_mode": "tracked",
                }
            )
            effects.append(
                {
                    "id": effect_id,
                    "workspace_id": fixture.workspace.id,
                    "transaction_id": transaction_id,
                    "source_revision": 1,
                    "is_active": True,
                }
            )
        await session.execute(insert(Transaction), transactions)
        await session.execute(insert(TransactionRevision), revisions)
        await session.execute(insert(Allocation), allocations)
        await session.execute(insert(CashLeg), legs)
        await session.execute(insert(FinancialEffect), effects)
        await session.flush()
        created += size
    # Статистика планировщика: в рабочей системе её поддерживает autovacuum,
    # в измерении она обновляется явно, иначе план строится вслепую (AR-34).
    from sqlalchemy import text as sql_text

    await session.execute(sql_text("ANALYZE transactions, transaction_revisions, allocations"))


async def test_nfr06_report_on_large_journal(owner_session: AsyncSession) -> None:
    """NFR-06, AR-34: числовой отчёт p95 <= 2 с на 50 000 операций."""
    fixture = await build_fixture(owner_session)
    started = time.perf_counter()
    await _bulk_transactions(owner_session, fixture, VOLUME)
    fill_seconds = time.perf_counter() - started

    total = (
        await owner_session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.workspace_id == fixture.workspace.id)
        )
    ).scalar_one()
    assert int(total) == VOLUME

    workspace = (
        await owner_session.execute(select(Workspace).where(Workspace.id == fixture.workspace.id))
    ).scalar_one()
    durations: list[float] = []
    for _ in range(20):
        start = time.perf_counter()
        report = await spending_report(
            owner_session,
            workspace=workspace,
            date_from=DAY - dt.timedelta(days=30),
            date_to_exclusive=DAY + dt.timedelta(days=1),
        )
        durations.append(time.perf_counter() - start)
        assert report.transaction_count == VOLUME

    p95 = _percentile(durations, 0.95)
    _record(
        "nfr06_report",
        {
            "p95_seconds": round(p95, 4),
            "median_seconds": round(statistics.median(durations), 4),
            "fill_seconds": round(fill_seconds, 2),
            "requirement": "p95 <= 2 s на 50 000 операций",
        },
    )
    assert p95 <= 2.0, f"p95 отчёта {p95:.3f} с превышает 2 с"


async def test_nfr06_filtered_journal_on_large_volume(owner_session: AsyncSession) -> None:
    """NFR-06, AR-34: расширенные фильтры журнала укладываются в предел."""
    fixture = await build_fixture(owner_session)
    await _bulk_transactions(owner_session, fixture, VOLUME)
    category_id = next(iter(fixture.categories.values()))

    durations: list[float] = []
    for _ in range(20):
        start = time.perf_counter()
        page = await list_journal(
            owner_session,
            workspace_id=fixture.workspace.id,
            filters=FilterSpec(
                category_ids=(category_id,),
                origins=("import",),
                min_amount_minor=10_000,
            ),
            date_from=DAY - dt.timedelta(days=30),
            date_to_exclusive=DAY + dt.timedelta(days=1),
            limit=8,
        )
        durations.append(time.perf_counter() - start)
        assert page.total > 0

    p95 = _percentile(durations, 0.95)
    _record(
        "nfr06_journal_filters",
        {"p95_seconds": round(p95, 4), "requirement": "p95 <= 2 s"},
    )
    assert p95 <= 2.0, f"p95 журнала {p95:.3f} с превышает 2 с"


async def test_nfr01_nfr07_intake_rate(clean_db: None, test_settings: Settings) -> None:
    """NFR-01, NFR-07, AR-34: приём p95 <= 1 с, нагрузка без потерь и дублей."""
    from fintracker.db.models.platform import InboundEvent

    def update(update_id: int, user_id: int) -> dict:
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1789000000,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Нагрузка"},
                "text": "продукты 500",
            },
        }

    update_id = 990_000

    async def timed(payload: dict[str, object]) -> float:
        """Задержка одного запроса, а не пакета: p95 считается по запросам."""
        started = time.perf_counter()
        result = await accept_telegram_update(test_settings, payload)
        assert not result.duplicate
        return time.perf_counter() - started

    # Профиль ТЗ §23: устойчивый поток 5 событий в секунду 10 минут и
    # 30-секундный всплеск 20 в секунду — 3 600 событий (NFR-01, NFR-07).
    steady_seconds = int(os.environ.get("FINTRACKER_PERF_STEADY", "600"))
    burst_seconds = int(os.environ.get("FINTRACKER_PERF_BURST", "30"))
    durations: list[float] = []
    for second in range(steady_seconds):
        started = time.perf_counter()
        batch = []
        for _ in range(5):
            update_id += 1
            batch.append(timed(update(update_id, 7_000 + second % 50)))
        durations.extend(await asyncio.gather(*batch))
        elapsed = time.perf_counter() - started
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)

    burst_durations: list[float] = []
    for second in range(burst_seconds):
        started = time.perf_counter()
        batch = []
        for _ in range(20):
            update_id += 1
            batch.append(timed(update(update_id, 7_500 + second % 10)))
        burst_durations.extend(await asyncio.gather(*batch))
        elapsed = time.perf_counter() - started
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)

    expected = steady_seconds * 5 + burst_seconds * 20
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        stored = (
            await session.execute(select(func.count()).select_from(InboundEvent))
        ).scalar_one()
    assert int(stored) == expected, "нет потерь и дублей при нагрузке"

    samples = durations + burst_durations
    p95 = _percentile(samples, 0.95)
    _record(
        "nfr01_intake",
        {
            "p95_seconds": round(p95, 4),
            "max_seconds": round(max(samples), 4),
            "steady_rate_per_second": 5,
            "steady_seconds": steady_seconds,
            "burst_rate_per_second": 20,
            "burst_seconds": burst_seconds,
            "events": expected,
            "requirement": "p95 <= 1 s до долговечного сохранения",
            "method": (
                "измерена задержка каждого запроса приёма; профиль ТЗ §23: "
                f"{steady_seconds} с по 5/с и {burst_seconds} с по 20/с"
            ),
        },
    )
    assert p95 <= 1.0, f"p95 приёма {p95:.3f} с превышает 1 с"


async def test_nfr08_concurrent_workspaces(clean_db: None, test_settings: Settings) -> None:
    """NFR-08, AR-34: 10 бюджетов по 5 участников работают одновременно."""
    from tests.acceptance.conftest import make_user
    from tests.acceptance.helpers import create_budget, issue_invite_code

    budgets = 10
    members_per_budget = 4

    async def prepare(index: int) -> tuple[str, int]:
        admin = make_user(test_settings, 950_000 + index * 10)
        await create_budget(admin, name=f"Бюджет {index}")
        return await issue_invite_code(admin), index

    # Одновременность ограничена пулом соединений: столько команд участников
    # выполняется в один момент и в работающей системе (ADR-10, NFR-08).
    concurrency = test_settings.db.api_pool_size + test_settings.db.api_max_overflow
    gate = asyncio.Semaphore(concurrency)

    async def member_session(code: str, index: int, member_index: int) -> None:
        member = make_user(test_settings, 950_000 + index * 10 + member_index + 1)
        async with gate:
            await member.send(f"/join {code}")
        async with gate:
            await member.send("продукты 300")
            if member.has_button("Записать"):
                await member.press(member.button_data("Записать"))

    # Бюджеты создаются последовательно: измеряется одновременная работа
    # участников, а не создание пространств (NFR-08, AR-34, G-30).
    codes = [await prepare(index) for index in range(budgets)]
    started = time.perf_counter()
    await asyncio.gather(
        *(
            member_session(code, index, member_index)
            for code, index in codes
            for member_index in range(members_per_budget)
        )
    )
    total_seconds = time.perf_counter() - started

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        workspaces = (
            await session.execute(select(func.count()).select_from(Workspace))
        ).scalar_one()
        transactions = (
            await session.execute(select(func.count()).select_from(Transaction))
        ).scalar_one()
    assert int(workspaces) == budgets
    assert int(transactions) == budgets * members_per_budget

    _record(
        "nfr08_concurrency",
        {
            "workspaces": budgets,
            "members_per_workspace": members_per_budget + 1,
            "total_seconds": round(total_seconds, 2),
            "requirement": "10 бюджетов по 5 участников",
            "concurrency_limit": concurrency,
            "method": (
                "все участники работают одновременно (asyncio.gather) с "
                "ограничением по пулу соединений; создание бюджетов в "
                "измерение не входит"
            ),
        },
    )


async def test_nfr02_nfr03_dialog_latency(clean_db: None, test_settings: Settings) -> None:
    """NFR-02, NFR-03: ответ <= 2 с после приёма, текст -> карточка p95 <= 5 с."""
    from tests.acceptance.conftest import make_user
    from tests.acceptance.helpers import create_budget

    user = make_user(test_settings, 960_001)
    await create_budget(user, limits="Продукты 20000")

    feedback: list[float] = []
    card: list[float] = []
    for index in range(20):
        start = time.perf_counter()
        await user.send(f"продукты {100 + index}")
        card.append(time.perf_counter() - start)
        start = time.perf_counter()
        if user.has_button("Записать"):
            await user.press(user.button_data("Записать"))
        feedback.append(time.perf_counter() - start)

    card_p95 = _percentile(card, 0.95)
    feedback_p95 = _percentile(feedback, 0.95)
    _record(
        "nfr02_nfr03_dialog",
        {
            "card_p95_seconds": round(card_p95, 4),
            "feedback_p95_seconds": round(feedback_p95, 4),
            "requirement": "NFR-02 <= 2 s, NFR-03 p95 <= 5 s (детерминированный путь)",
            "note": (
                "Измерено на детерминированном разборе без внешнего провайдера; "
                "задержка модели добавляется после подключения ключа (BL-01)."
            ),
        },
    )
    assert card_p95 <= 5.0, f"p95 карточки {card_p95:.3f} с превышает 5 с"
    assert feedback_p95 <= 2.0, f"p95 подтверждения {feedback_p95:.3f} с превышает 2 с"


def test_ar33_nfr10_restore_drill(pg_database: None) -> None:
    """AR-33, NFR-10: учение восстановления с измеренным RTO и честным RPO."""
    import json as json_module
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [str(root / ".venv" / "bin" / "python"), str(root / ".planning/tools/restore_drill.py")],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    if result.returncode == 2:
        pytest.skip(f"Среда учения недоступна: {result.stdout.strip()[-200:]}")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]

    payload = json_module.loads((EVIDENCE / "restore_drill.json").read_text(encoding="utf-8"))
    assert payload["invariant_failures"] == {}, (
        "финансовые инварианты нарушены после восстановления"
    )
    assert payload["row_counts_match"], "состав восстановленных данных не совпал"
    assert payload["snapshot_is_consistent"], "восстановлен неполный или лишний снимок"
    assert payload["rto_seconds_measured"] <= payload["rto_limit_seconds"]
    # Учение без архива WAL не доказывает RPO: отчёт обязан это заявлять.
    assert payload["rpo_demonstrated"] is False
    assert payload["risk_tail"], "хвост риска должен быть указан явно"
