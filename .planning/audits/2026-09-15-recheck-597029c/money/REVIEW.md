---
phase: recheck-597029c-money
reviewed: 2026-09-14T17:28:27Z
depth: deep
files_reviewed: 13
files_reviewed_list:
  - src/fintracker/application/ledger/service.py
  - src/fintracker/application/planning/rollover.py
  - src/fintracker/application/analytics/reviews.py
  - src/fintracker/application/conversation/goals_flow.py
  - src/fintracker/application/ledger/operations.py
  - src/fintracker/application/commitments/goals.py
  - src/fintracker/application/commitments/schedules.py
  - src/fintracker/application/analytics/coverage.py
  - src/fintracker/application/planning/periods.py
  - src/fintracker/domain/ledger/model.py
  - src/fintracker/domain/parsing/amounts.py
  - src/fintracker/db/models/ledger.py
  - src/fintracker/db/models/commitments.py
findings:
  critical: 6
  warning: 0
  info: 0
  total: 6
status: issues_found
---

# Money lifecycle review of 597029c

## Narrative Findings (AI reviewer)

**Commit:** `597029c32a9339273d565a1d8f59116f1cb677bf`, base `5646887`.
Изучены три порученных изменённых файла целиком, связанные денежные сервисы,
ORM-контракты и вызовы. По дополнительному запросу проверен новый обработчик
резервов `apply_goal_amount`. Перечень выше включает эти перекрёстные проверки.

Все 11 прежних денежных диагностик проходят: исправлены проверенные ранее
restore/revise простого возмещения и оплаты, отмена исходной покупки без оплаты,
запрет уменьшения уже полученной доли, инвалидирование сверок и прогноз по
неполному периоду; предшествующий период с готовым следующим планом закрывается.
Однако 10 дополнительных сценариев выявляют 6 причин ошибок ниже. Все запуски
использовали отдельную настоящую PostgreSQL `fintracker_audit_597_money`, runtime
API/worker-роли, транзакции и реальные доменные сервисы. Нет сетевых заменителей.

Код продукта и штатные тесты не изменялись. Диагностики встроены в этот документ
и собраны pytest через collector; JUnit: `result.xml` в этом же каталоге.

### CR-01 — BLOCKER: восстановление совместной покупки теряет открытый долг

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:685-700`.
**Тип:** новая регрессия исправления M-02.
**Issue:** покупка 3000 ₽ со своей долей 1500 ₽, возмещений нет. После void
`outstanding_minor=0`, но `original_minor=150000`. Restore вычисляет полученное
возмещение как `original - outstanding`, принимает отменённую долю за полученные
1500 ₽ и оставляет требование `(0, settled)` вместо `(150000, open)`.
**Доказательство:** `test_restore_shared_origin_reopens_uncollected_debt`.
**Fix:** вычислять фактически полученные погашения по активным финансовым
эффектам; хранить отменённое исходное требование отдельно от погашенного.
Восстановление должно восстановить основание долга и вычесть только активные
погашения/возвраты. Проверить повторный void после restore.

### CR-02 — BLOCKER: удаление возмещаемой доли падает на SQL CHECK

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:694-701`.
**Связанный контракт:** `src/fintracker/db/models/ledger.py:467`.
**Тип:** новая регрессия: валидная корректировка теперь вызывает IntegrityError.
**Issue:** исправить ещё не возмещённую совместную покупку в обычную покупку
полностью за свой счёт. TransactionSpec проходит validate, но отсутствующая
receivable-строка превращается в `new_original=0`. Запись `original_minor=0`
нарушает `ck_receivables_original_positive`; пользовательская правка невозможна.
**Доказательство:** `test_remove_uncollected_share_is_valid_correction`.
**Fix:** моделировать прекращение требования отдельным статусом/связью с текущей
ревизией, сохраняя историю и положительное первоначальное основание. Не писать
ноль в поле с запрещающим его ограничением. Проверять зависимости до записи.

### CR-03 — BLOCKER: пересчёт требования забывает возврат возмещаемой доли

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:716-741,784-807`.
**Связанный producer:** `src/fintracker/application/ledger/operations.py:488-521`.
**Тип:** новые регрессии revise/void погашения и незакрытая отмена самого возврата.
**Issue:** при исходной доле 1500 ₽ магазин вернул 500 ₽, затем друг возместил
500 ₽; долг равен 500 ₽. Исправление одного комментария возмещения увеличивает
его до 1000 ₽. Отмена возмещения даёт 1500 ₽ вместо 1000 ₽. Отмена магазинного
возврата оставляет 500 ₽ вместо 1000 ₽. Helper выбирает только kind=settlement,
хотя producer сохраняет уменьшение требования продавцом как kind=reversal.
**Доказательство:** `test_refunded_share_survives_settlement_lifecycle`
с параметрами `note`, `void`, `refund_void`.
**Fix:** синхронизировать весь журнал требования: активное исходное основание,
погашения, возвраты доли и списания. При revise/void/restore обновлять записи и
пересчитывать остаток по всем активным эффектам, включая возврат как отдельный
тип уменьшения, а не возмещение контрагентом.

### CR-04 — BLOCKER: комментарий меняет частичную оплату на полную

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:834-841`.
**Контракт создания связи:** `src/fintracker/application/commitments/schedules.py:242-303`.
**Тип:** новая регрессия исправления M-03.
**Issue:** к обязательству на 1000 ₽ привязано 500 ₽ из расхода 1000 ₽.
Сервис допускает такую частичную связь и хранит собственную сумму, а также
необязательный stable_line_id. При изменении комментария helper заменяет сумму
каждой связи всей суммой операции: обязательство становится `(100000, settled)`
вместо `(50000, partially_settled)`. Для операции с несколькими связанными
частями такая логика также присваивает полный итог каждой части.
**Доказательство:** `test_note_edit_preserves_partial_occurrence_settlement`.
**Fix:** при правке метаданных переносить на новый эффект прежнюю сумму связи.
При денежной правке вычислять покрытие по конкретной связанной части/явному
распределению; неоднозначные уменьшения требуют уточнения до записи.

### CR-05 — BLOCKER: готовый план всё ещё отключает открытие и обзор периода

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/planning/rollover.py:289-298,364-396`.
**Тип:** незакрытый соседний путь M-06; закрытие предшественника исправлено.
**Issue:** текущий период и его план созданы заранее сервисом. Два запуска
`handle_open_next_period` закрывают предшественника, но так и не создают
`BudgetPeriodOpened` и задачу `plan_review` текущего периода: он не попадает в
pending, потому что план уже есть, а период ещё не заканчивается. Итог —
0 задач обзора и 0 событий открытия вместо одного каждого.
**Доказательство:** `test_existing_plan_still_schedules_period_opening`.
**Fix:** независимо и идемпотентно доводить до конца создание плана, закрытие,
событие открытия и постановку обзора; использовать явный признак отправленного
события/уникальный ключ, а не наличие BudgetVersion как признак всей инициализации.

### CR-06 — BLOCKER: резерв меняется по отрицательной, чужой или неоднозначной сумме

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/goals_flow.py:197-200`.
**Связанный parser:** `src/fintracker/domain/parsing/amounts.py:85-89,105-116`.
**Тип:** новая ветка UI обходит уже доступные признаки неоднозначности/валюты.
**Issue:** при резерве 5000 ₽ команда использования `-100` списывает 100 ₽,
`100 USD` тоже списывает 100 ₽, а `1.500` списывает 1500 ₽ без уточнения между
1,5 и 1500. Parser не захватывает знак, предоставляет explicit currency и
ambiguous_options; новый handler без проверки берёт первое value и принудительно
назначает валюту бюджета. Результат подтверждается успешным сообщением бота.
**Доказательство:** `test_goal_amount_needs_unambiguous_positive_currency`
для всех трёх входов; фактические резервы 4900/4900/3500 ₽.
**Fix:** для формы суммы проверять целиком введённое значение, положительность,
единственность, совпадение валюты и отсутствие ambiguous_options. Возвращать
уточнение без изменения резерва. Добавить положительность и совпадение валюты
также в `use_goal` и `release_goal`, как уже сделано в `allocate_to_goal`.

Проверка автоматического отката выделения цели вместе с переводом не включена
в находки: AR-18 требует выбранного пользователем действия с резервом, поэтому
автоматический откат нельзя считать однозначным ожидаемым поведением.

## Executable diagnostic cases

```python
from pathlib import Path
exec(Path('.planning/audits/2026-09-14-recheck-5646887/money/test_money_recheck.py').read_text().split('async def test_')[0], globals())
from fintracker.application.ledger.operations import post_refund
from fintracker.application.commitments.goals import create_goal, allocate_to_goal
from fintracker.application.conversation.goals_flow import apply_goal_amount
from fintracker.db.models.commitments import Goal
from fintracker.db.models.platform import Job, OutboxEvent
from fintracker.domain.ledger.model import AllocationRole, TransactionType


async def test_restore_shared_origin_reopens_uncollected_debt(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, receivable, _ = await mixed(session, uow, fixture, collect=False)
    async with command(test_settings, fixture) as (session, uow):
        cancelled = await void_transaction(session, uow, actor=fixture.actor,
            transaction_id=purchase.transaction_id, expected_version=purchase.entity_version)
    async with command(test_settings, fixture) as (session, uow):
        await restore_transaction(session, uow, actor=fixture.actor,
            transaction_id=purchase.transaction_id, expected_version=cancelled.entity_version)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        assert (row.outstanding_minor, row.status) == (150000, 'open'), (row.outstanding_minor, row.status)


async def test_remove_uncollected_share_is_valid_correction(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, receivable, _ = await mixed(session, uow, fixture, collect=False)
    async with command(test_settings, fixture) as (session, uow):
        _, _, spec = await load_current_spec(session, workspace_id=fixture.workspace.id,
            transaction_id=purchase.transaction_id)
        own = next(a for a in spec.allocations if a.role is AllocationRole.EXPENSE)
        amended = replace(spec, transaction_type=TransactionType.EXPENSE,
            allocations=(replace(own, amount=spec.amount),))
        amended.validate()
        await revise_transaction(session, uow, actor=fixture.actor,
            transaction_id=purchase.transaction_id, new_spec=amended,
            expected_version=purchase.entity_version)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        assert row.outstanding_minor == 0


@pytest.mark.parametrize('change', ['note', 'void', 'refund_void'])
async def test_refunded_share_survives_settlement_lifecycle(owner_session, test_settings, change):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, receivable, _ = await mixed(session, uow, fixture, collect=False)
        refund = await post_refund(session, uow, actor=fixture.actor,
            source_transaction_id=purchase.transaction_id,
            parts={receivable.origin_stable_line_id: rub(500)},
            occurred_date=DAY, timezone=TZ, account_id=fixture.accounts['Карта'])
        settlement = await settle_receivable(session, uow, actor=fixture.actor,
            receivable_id=receivable.id, amount=rub(500), occurred_date=DAY,
            timezone=TZ, account_id=fixture.accounts['Карта'])
    async with command(test_settings, fixture) as (session, uow):
        if change == 'note':
            _, _, spec = await load_current_spec(session, workspace_id=fixture.workspace.id,
                transaction_id=settlement.transaction_id)
            await revise_transaction(session, uow, actor=fixture.actor,
                transaction_id=settlement.transaction_id, new_spec=replace(spec, note='Комментарий'),
                expected_version=settlement.entity_version)
            expected = 50000
        else:
            target = refund if change == 'refund_void' else settlement
            await void_transaction(session, uow, actor=fixture.actor,
                transaction_id=target.transaction_id, expected_version=target.entity_version)
            expected = 100000
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        assert row.outstanding_minor == expected, (change, row.outstanding_minor, expected)


async def test_note_edit_preserves_partial_occurrence_settlement(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        await create_schedule(session, uow, actor=fixture.actor, name='Платёж', direction='payment',
            rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=DAY), currency='RUB',
            expected=rub(1000), category_id=fixture.categories['Продукты'])
        occurrence = (await materialize_occurrences(session,
            workspace_id=fixture.workspace.id, until_date=DAY))[0]
        spec = expense_spec(fixture, amount=rub(1000), category='Продукты', account='Карта')
        payment = await post_transaction(session, uow, actor=fixture.actor, spec=spec, origin='form')
        await settle_occurrence(session, uow, actor=fixture.actor, occurrence_id=occurrence.id,
            transaction_id=payment.transaction_id, effect_id=payment.effect_id, amount=rub(500))
    async with command(test_settings, fixture) as (session, uow):
        _, _, spec = await load_current_spec(session, workspace_id=fixture.workspace.id,
            transaction_id=payment.transaction_id)
        await revise_transaction(session, uow, actor=fixture.actor,
            transaction_id=payment.transaction_id, new_spec=replace(spec, note='Комментарий'),
            expected_version=payment.entity_version)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Occurrence, occurrence.id)
        assert (row.settled_minor, row.state) == (50000, 'partially_settled'), (row.settled_minor, row.state)


@pytest.mark.parametrize('input_text', ['-100', '100 USD', '1.500'])
async def test_goal_amount_needs_unambiguous_positive_currency(owner_session, test_settings, input_text):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        goal = await create_goal(session, uow, actor=fixture.actor, name='Цель', currency='RUB',
            target=rub(10000))
        await allocate_to_goal(session, uow, actor=fixture.actor, goal_id=goal.id, amount=rub(5000))
    replies = await apply_goal_amount(test_settings, actor=fixture.actor, workspace=fixture.workspace,
        goal_id=goal.id, operation='use', text=input_text)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Goal, goal.id)
        assert row.allocated_minor == 500000, (input_text, row.allocated_minor, [r.text for r in replies])


async def test_existing_plan_still_schedules_period_opening(owner_session, test_settings):
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10),
        limits={'Продукты': 100000})
    await _add_template(owner_session, fixture, limits={'Продукты': 100000})
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        period = await period_for_date(session, workspace_id=fixture.workspace.id, day=DAY)
        await apply_plan_for_period(session, uow, workspace_id=fixture.workspace.id, period=period)
    job = LeasedJob(id=uuid.uuid4(), job_type='open_next_period', queue_class='calendar',
        workspace_id=fixture.workspace.id, subject_id=None, payload={'local_date':'2026-09-12'},
        payload_version=1, attempts=1, max_attempts=6, lease_token=uuid.uuid4(),
        lease_until=dt.datetime.now(dt.UTC)+dt.timedelta(minutes=5), deadline_at=None,
        correlation_id='audit-money-597', logical_key='audit-money-597-open')
    await handle_open_next_period(test_settings, job)
    await handle_open_next_period(test_settings, job)
    async with command(test_settings, fixture) as (session, _):
        jobs = (await session.execute(select(Job).where(Job.workspace_id == fixture.workspace.id,
            Job.job_type == 'plan_review'))).scalars().all()
        events = (await session.execute(select(OutboxEvent).where(
            OutboxEvent.workspace_id == fixture.workspace.id,
            OutboxEvent.event_type == 'BudgetPeriodOpened',
            OutboxEvent.aggregate_id == period.id))).scalars().all()
        assert (len(jobs), len(events)) == (1, 1), (len(jobs), len(events))
```

## Reproduction command

Run from the repository root. This collector compiles the first Python code
block as a pytest module, retaining its real Markdown line numbers. It uses
the same PostgreSQL fixtures as the product suite on the dedicated audit DB.

```bash
FINTRACKER_TEST_DB=fintracker_audit_597_money PYTHONPATH=src:. .venv/bin/python -c 'import pathlib, types, pytest
p = pathlib.Path(".planning/audits/2026-09-15-recheck-597029c/money/REVIEW.md").resolve()
class Cases(pytest.Module):
    def _getobj(self):
        m = types.ModuleType("money_audit_597")
        m.__file__ = str(p)
        prefix, suffix = p.read_text().split("```python\n",1)
        code = "\n" * (prefix.count("\n") + 1) + suffix.split("\n```",1)[0]
        exec(compile(code, str(p), "exec"), m.__dict__)
        return m
class Collector:
    def pytest_collect_file(self, file_path, parent):
        if file_path == p:
            return Cases.from_parent(parent, path=file_path)
raise SystemExit(pytest.main(["-q", "-p", "tests.conftest", str(p), "--tb=short", "--junitxml=.planning/audits/2026-09-15-recheck-597029c/money/result.xml"], plugins=[Collector()]))
'
```

Final diagnostic result: **10 failed, 0 passed, 0 errors, 0 skipped**. These are
assertions for required behavior and a valid correction rejected by PostgreSQL,
not intentionally inverted assertions that would pass while bugs persist.
