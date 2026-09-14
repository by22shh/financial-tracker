---
phase: readiness-money
reviewed: 2026-09-13T19:04:42Z
depth: deep
commit: 7a56769e2a7dc465a4985ce2755d398d08b0f3de
files_reviewed: 11
files_reviewed_list:
  - src/fintracker/application/ledger/operations.py
  - src/fintracker/application/ledger/service.py
  - src/fintracker/application/planning/periods.py
  - src/fintracker/application/planning/plan.py
  - src/fintracker/application/planning/rollover.py
  - src/fintracker/application/analytics/coverage.py
  - src/fintracker/application/analytics/journal.py
  - src/fintracker/application/analytics/reports.py
  - src/fintracker/application/analytics/reviews.py
  - src/fintracker/domain/ledger/model.py
  - src/fintracker/core/calendar.py
findings:
  critical: 8
  warning: 0
  info: 0
  total: 8
status: issues_found
---

# Money and calendar readiness review

## Narrative Findings (AI reviewer)

Eight targeted defect probes failed their required assertions: **8 failed, 0 errors, 0 skipped**, 10.12 seconds. These are eight failure cases, with overlap between the linked-effect lifecycle cases. All are BLOCKER for claiming the associated P0 requirements complete; none is classified P0 incident severity. CR-01 and CR-02 reproduce actual conversation entry points. CR-03 through CR-08 reproduce lower service behavior with the runtime API role, workspace lock, RLS and real commit; their prerequisite creation commands currently have no production callers. Do not represent those six as demonstrated end-to-end Telegram failures.

The review read the eleven principal files above and traced relevant calls in conversation context, analytics, corrections, entry and payments, commitment schedules/goals, onboarding, UnitOfWork, and database constraints. Requirements and previous implementation audit/recheck/fixes were used as context. The previous R-05 refund restore/capacity fix is present; CR-06 concerns changing the source purchase's individual returned allocation, a different path.

Evidence: [pytest output](run-20260913T190442Z/pytest.log), [JUnit](run-20260913T190442Z/junit.xml), [snapshot and source hashes](run-20260913T190442Z/scope.json), [probe source](test_money_readiness.py), [isolated runner](run_probes.py). The runner copied tracked files, excluding private/ignored data and prior audit artifacts, and used a unique database `fintracker_money_*`. OWNER was used for fixtures/schema only; tested commands used API with actor/workspace context. The real calendar handler used WORKER. No live Telegram/AI calls, source changes, full-suite run or commits were performed.

## Critical Issues

### CR-01: A normal status read permanently bypasses period initialization

**Classification:** BLOCKER. **Priority:** P1. **Reachability:** production conversation path.

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/planning/rollover.py:279-281`

**Related caller:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/context.py:100-101`; `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/planning/periods.py:218`.

**Requirements:** FR-92, FR-93; dependent FR-39/FR-52 period rollover and review scheduling.

**Scenario:** An August 10–September 9 budget has an approved recurring template. Before the delayed `open_next_period` job runs, the user requests a current status (the shared caller used by `/budget`, reports and post-save status). `period_for_date` inserts the September 10–October 9 row without applying the plan or closing August. The normal worker is then run.

**Observed:** The current period still has no working plan; the previous period remains `open`. Probe output is `(False, 'open')` versus expected `(True, 'ended')`. The worker returns immediately because no row was newly inserted by its own call. The same return also skips rollover, review enqueue and period events; that consequence is traced from code rather than separately asserted in this probe. Running the worker later does not repair this September period.

**Fix:** Make complete period initialization one idempotent operation shared by read/write and worker paths. Under the workspace lock, repair every materialized but uninitialized period in order, apply its accepted template, close the predecessor, persist rollovers and unique events/jobs. Do not use “created during this invocation” as the completion marker.

**Reproduction:** `test_read_opened_period_still_gets_plan_and_closure`.

### CR-02: Forecast treats elapsed calendar days as confirmed observed history

**Classification:** BLOCKER. **Priority:** P1. **Reachability:** production report view.

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/conversation/analytics_flow.py:47-54`

**Related calculation:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/analytics/reports.py:522-537`; the weekly risk caller similarly derives days from the period start in `application/analytics/reviews.py:232-236`.

**Requirements:** FR-41, FORM-03; corresponding early-risk FR-42.

**Scenario:** September 1–30 period, one 1,000 ₽ expense on September 12, default `incomplete` coverage and no confirmed observation-day coverage. Request the real `report_view` on September 14 local time.

**Observed:** The report outputs flexible forecast **1,142.85 ₽** and period forecast **2,142.85 ₽**. It divides one recorded purchase over fourteen elapsed days, treating the other unconfirmed dates as zero. “Полнота учёта не подтверждена” is only a disclaimer and does not prevent the numerical forecast.

**Expected:** Fact, plan and known commitments, with no pace forecast until at least seven confirmed observed days. FR-41 explicitly disallows substituting incomplete history with zero-filled days.

**Fix:** Derive eligible days and corresponding flexible amounts from confirmed coverage, including its scope and revision. Gate both report and weekly-risk pace on seven eligible days. An elapsed-day count or disclaimer must not satisfy the observation requirement.

**Reproduction:** `test_incomplete_calendar_days_are_not_observed_forecast_days`.

### CR-03: Voiding a receivable settlement leaves the debt marked fully paid

**Classification:** BLOCKER. **Priority:** P1 for completing the money-service contract. **Reachability:** service-level reproduction; creation integration absent.

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:692-704`

**Related state writer:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/operations.py:287-309`.

**Requirements:** FR-30, FR-34, A41/A42; DATA_CONTRACT §2.4–2.5 effect/receivable invariants.

**Scenario:** Pay 3,000 ₽, own expense 1,500 ₽, other person's receivable 1,500 ₽. Receive the full reimbursement into the tracked account, then void that reimbursement with its current entity version.

**Observed:** Account balance correctly returns to **−3,000 ₽**, but receivable remains **0 ₽ / settled**. Actual tuple `(0, 'settled', -300000)` differs from expected `(150000, 'open', -300000)`. The cancelled settlement link changes status, but neither its `ReceivableEntry` nor `outstanding_minor` is reversed.

**Fix:** Reverse the receivable component of an effect atomically with account entries; update outstanding amount, status and version. Apply the same component lifecycle to revisions/restores, with capacity checks. Test the round trip on each effect component.

**Reproduction:** `test_void_settlement_reopens_receivable`.

### CR-04: Original shared purchase can be voided after its debt was collected

**Classification:** BLOCKER. **Priority:** P1 for completing the money-service contract. **Reachability:** service-level reproduction; creation integration absent.

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:674-677`

**Related guard:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:506-510`.

**Requirements:** FR-33, FR-34, FR-30; the explicit prohibition on removing a receivable origin after money has been collected, TZ §9.

**Scenario:** Create the same 3,000 ₽ shared purchase, collect the 1,500 ₽ receivable, then cancel the original purchase alone.

**Observed:** Cancellation commits instead of raising the required dependency conflict. `_guard_linked_refunds` checks only `refund_of`; it does not see `settles_receivable`. A collected settlement remains associated with a voided origin. This assertion verifies acceptance of the invalid operation, not a proposed coordinated-correction flow.

**Fix:** Guard every dependent economic component before revision/void, including settled receivables and used goal funds. Require an explicitly agreed atomic dependency correction where the original cannot be removed independently.

**Reproduction:** `test_void_paid_purchase_cannot_leave_collected_receivable`.

### CR-05: Voiding a linked payment leaves its obligation settled

**Classification:** BLOCKER. **Priority:** P1 for completing the money-service contract. **Reachability:** service-level reproduction; payment-link creation integration absent.

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:695-704`

**Related state writer:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/commitments/schedules.py:294-311`.

**Requirements:** FR-34, FR-46, FORM-01/R04; DATA_CONTRACT §2.5 explicitly requires cancellation or editing of an effect to change obligation execution atomically.

**Scenario:** Create an expected 1,000 ₽ payment, post and link a full payment, then void that transaction.

**Observed:** Obligation remains `settled`, `settled_minor=100000`; its contribution to outstanding commitments is **0 ₽**. Expected `planned`, `settled_minor=0`, commitments **1,000 ₽**. The expense disappears while the obligation is still considered paid.

**Fix:** Invalidate/reverse active `OccurrenceSettlement` components when their financial effect is replaced or voided, and recompute settled amount/state in the same transaction. Restore must revalidate and reapply the same coverage without duplication.

**Reproduction:** `test_void_scheduled_payment_reopens_obligation`.

### CR-06: Editing purchase allocations can leave a refund larger than its source part

**Classification:** BLOCKER. **Priority:** P1 for completing the money-service contract. **Reachability:** service-level reproduction; linked-refund creation integration absent.

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:519-521`

**Related constraint:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/db/migrations/versions/0002_rls_and_grants.py:292-295`.

**Requirements:** FR-29, FR-33, AR-17; DATA_CONTRACT §2.5 active refund sum must fit each current stable allocation.

**Scenario:** Purchase total 1,000 ₽ consists of 600 ₽ groceries and 400 ₽ restaurants. Refund all 600 ₽ from the groceries allocation. Revise the original allocations to 100 ₽ groceries and 900 ₽ restaurants, keeping the same stable IDs and total.

**Observed:** The revision commits instead of a dependency conflict. The existing groceries refund is now 600 ₽ against a 100 ₽ source part. The application compares refunds only with the transaction total. The deferred refund constraint fires on `transaction_links` mutations, so changing the source allocations/current revision does not cause this check to run.

**Fix:** Validate the new source allocation map against active links by `source_stable_line_id`; reject shrinking/removing a returned part without coordinated correction. Add the deferred check to source-revision/allocation changes too. Preserve allocation identity when editing.

**Reproduction:** `test_purchase_reallocation_cannot_shrink_refunded_part`.

### CR-07: Voiding a transaction leaves an accepted reconciliation falsely current

**Classification:** BLOCKER. **Priority:** P1 for completing the reconciliation contract. **Reachability:** normal void command after service-created reconciliation; reconciliation creation integration absent.

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:695-707`

**Related invalidator:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ledger/service.py:609-622` only runs in a subset of revisions; it is not called by void.

**Requirements:** FR-71, FR-34, AR-20; DATA_CONTRACT §2.5 monetary changes before cutoff invalidate reconciliation.

**Scenario:** Post a tracked 1,000 ₽ expense, accept a matching −1,000 ₽ balance reconciliation on its date, then void the expense.

**Observed:** Actual account balance becomes **0 ₽** but `is_stale` remains `False`. Expected `(True, 0)`, actual `(False, 0)`. The quality check can continue reporting no stale reconciliation despite a changed cutoff balance.

**Fix:** Centralize reconciliation invalidation around account-effect changes for post/revise/void/restore and coverage changes. Consider both old and new accounts/dates. Preserve accepted evidence while marking it stale; note-only changes should remain exempt.

**Reproduction:** `test_void_transaction_invalidates_accepted_reconciliation`.

### CR-08: Reconciliation acceptance applies an outdated adjustment without rechecking its basis

**Classification:** BLOCKER. **Priority:** P1 for completing the reconciliation contract. **Reachability:** service-level reproduction; reconciliation acceptance integration absent.

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/analytics/coverage.py:178-199`

**Related creation:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/analytics/coverage.py:125-134` stores `basis_revision=0`.

**Requirements:** FR-71, CMD-15; DATA_CONTRACT §2.6 expected version and accepted preview basis.

**Scenario:** At account balance 0 ₽, prepare reconciliation against observed balance 1,000 ₽. In a separate committed command, post a backdated 200 ₽ expense. Accept the earlier preview with `adjust=True`.

**Observed:** Acceptance succeeds instead of rejecting the changed basis. The service blindly copies the old `difference_minor` into a new adjustment; it neither recomputes the cutoff balance nor compares an expected ledger revision. From the recorded amounts, the resulting balance would be −200 + 1,000 = 800 ₽ rather than the user's observed 1,000 ₽; this arithmetic consequence is a code inference, while the probe directly asserts that acceptance was not rejected.

**Fix:** Save the effective data/coverage revision in the preview and require it at acceptance under the workspace lock. Reject or regenerate the preview if the cutoff balance or relevant account scope changed; never silently apply a stale difference.

**Reproduction:** `test_reconciliation_accept_revalidates_changed_basis`.

## Test evidence and reachability limits

Every named probe lives in `test_money_readiness.py`; the runner copies it to `tests/integration/` in the isolated snapshot so the ordinary project fixtures apply. To reproduce: `.venv/bin/python .planning/audits/2026-09-14-readiness/money/run_probes.py`. PostgreSQL on local port 55432 is required. The suite uses wall-clock date for the two real conversation callers; this saved run occurred on September 14, 2026 in the budget timezone. A later replay must pin that date or update the period fixtures; the six service probes use explicit dates.

Production search `rg -n 'post_mixed_payment|post_refund|settle_receivable|settle_occurrence|record_reconciliation|accept_reconciliation' src/fintracker` finds only definitions. The standard void action does call `void_transaction` at `conversation/corrections.py:434`; the missing part for CR-03–CR-08 is the end-to-end creation/linking workflow. This report does not conflate dormant broken capabilities with executed chat flows. Separately, `payments_flow.py:154-164` promises automatic closure after entering an expense without preserving an occurrence selection or calling a settlement service; this integration observation was sent to the parent/UI reviewer and is not counted as a ninth independently reproduced finding here.

The requirement registry currently marks FR-29/30/34/41/46/71/92/93 verified. Its linked tests cover individual mechanics rather than these compositions: FR-41 points only to a pure early-risk test with caller-supplied observation count; FR-92/93 tests materialization and template application separately; FR-71 tests a reconciliation and adjustment without an intervening ledger change. AR-16's random sequence covers expense/transfer effects, not receivables/obligations, and its account-balance assertion compares two sums over the same `AccountEntry` rows. Those tests cannot establish the missing cross-component invariants.

No exhaustive security conclusion is made. RLS and deferred constraints were active during the committed probes, but this review did not test every shared-member privilege transition. Broader production/UI/async and release evidence review belongs to the parent task. Static followups without a dedicated reproduction were passed to the parent rather than included in the eight-case count.
