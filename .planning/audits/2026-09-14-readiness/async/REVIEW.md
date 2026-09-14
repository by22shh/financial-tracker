---
phase: readiness-async
reviewed: 2026-09-13T19:06:03Z
depth: deep
git_revision: 7a56769e2a7dc465a4985ce2755d398d08b0f3de
files_reviewed: 17
files_reviewed_list:
  - src/fintracker/application/ingestion/accept_update.py
  - src/fintracker/application/ingestion/process_event.py
  - src/fintracker/application/platform/queue.py
  - src/fintracker/application/delivery/dispatch.py
  - src/fintracker/application/delivery/render.py
  - src/fintracker/application/intelligence/analysis.py
  - src/fintracker/application/intelligence/extraction.py
  - src/fintracker/application/intelligence/media_pipeline.py
  - src/fintracker/application/intelligence/prompts.py
  - src/fintracker/application/intelligence/quota.py
  - src/fintracker/application/intelligence/schedule.py
  - src/fintracker/application/identity/security_change.py
  - src/fintracker/application/identity/actor.py
  - src/fintracker/runtime/worker.py
  - src/fintracker/runtime/scheduler.py
  - src/fintracker/infra/telegram/sender.py
  - src/fintracker/infra/security_log.py
findings:
  critical: 8
  warning: 0
  info: 0
  total: 8
status: issues_found
---

# Async processing and access recovery review

## Narrative Findings (AI reviewer)

Eight concrete defects remain in the reviewed scope. These extend beyond the exact V-01–V-09 regression cases: the other notification handler, extraction rather than analysis cancellation, snapshot freshness rather than execution ownership, and missing rather than newer access-journal evidence.

The 11 targeted diagnostic cases deliberately assert the observed defects. Their passing results are reproduction evidence, **not passing application acceptance tests**. Nine cases are in `junit.xml`; two additional cases are in `junit-extra.xml`. PostgreSQL was recreated under unique database names `fintracker_async_audit_20260914_b83e` and `fintracker_async_audit_20260914_c39e`, using existing migration fixtures. Runtime paths use actual API/WORKER roles and RLS; OWNER is used only to prepare fixtures, inspect results and inject external state transitions. Telegram and AI calls use controlled test providers. No live external API was called and no source, project test, registry or current evidence file was edited.

Call chains traced: durable ingress → worker → conversation/media → extraction/quota; worker → outbox expansion → notification sender; worker → scheduled analysis → prepare/generate/store → outbox; startup → journal reconciliation → actor resolution. Supporting models, UnitOfWork, RLS/session setup, retention and recommendation presentation were inspected where these paths cross module boundaries. No structural pre-pass was supplied.

## Critical Issues

### CR-01: BLOCKER — Notification dispatcher sends despite expired lease or closed security gate

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/delivery/dispatch.py:373-381`, `:445`.

**Issue:** `handle_deliver_notification` checks active membership and only deleting/deleted workspace states. It never validates its job lease or checks `quarantined` / `security_fence`. The execution-fence context installed by `_run_with_lease` is passive and this handler never invokes it. Consequently a worker whose lease expired before entering the handler still sends, and a workspace placed in recovery quarantine or an unfinished ACL change still emits its financial notifications. This is separate from the fixed `handle_deliver_reply` route.

**Reproduction:** `test_notification_sends_without_execution_authority[expired_lease|security_fence|quarantined]`. A real leased notification job is invalidated before `_run_with_lease` invokes the registered handler. All three cases produce one recorded send and a `NotificationDelivery.state == 'sent'` row.

**Expected:** An already expired worker cannot send or finalize delivery; a security-gated workspace cannot release ordinary financial content. **Actual:** The notification is sent in every case.

**Affected requirements:** ADR-05, ADR-06, ADR-14, TECH-06, AR-04, SEC-10.

**Fix:** Share the actual lease/access gate used by author replies with the notification dispatcher; recheck it immediately before each external send, require an active ungated workspace for financial classes, and fence result updates. Preserve the narrow terminal-notification exception explicitly. This does not attempt to make database revocation atomic with bytes already sent to Telegram.

### CR-02: BLOCKER — Quiet-hours and failed notifications have no runnable continuation

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/delivery/dispatch.py:353-355`, `:398-409`, `:474-485`.

**Issue:** A not-yet-due delivery is skipped; a quota-delayed or transport-failed delivery only receives a new `available_at`. The handler returns normally, so `runtime/worker.py:113` marks the sole delivery job succeeded. The queue considers `Job.available_at`, not `NotificationDelivery.available_at`. Expansion enqueues only when `plan.created` is nonzero (`dispatch.py:263-273`) and records the consumer receipt, so later outbox sweeps cannot provide the missing continuation. Retention cancels expired rows; it does not retry them.

**Reproduction:** `test_notification_has_no_future_runnable_job[future_delivery|failed_transport]`. The normal registered worker handler leaves the delivery `pending` or `failed` while its job is `succeeded`. Moving the delivery's `available_at` into the past still produces no claimable job.

**Expected:** Sending resumes when quiet hours end or the provider retry delay expires, within the delivery deadline. **Actual:** The notification is stranded until cancelled or manually repaired.

**Affected requirements:** ADR-05, TECH-05, TECH-06, FR-53, A159, LIM-06, LIM-07.

**Fix:** Persist a next job scheduled for the earliest pending delivery time, or retain/reschedule the existing event job until all deliveries are terminal. The durable continuation and delivery-state transition must commit together and honor `retry_after` and expiry.

### CR-03: BLOCKER — Missing access-journal evidence allows startup to expose existing budgets

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/security_change.py:441-467`.

**Issue:** Reconciliation only quarantines pending operations or a journal revision newer than the database. If `last_committed()` returns `None` and no prepared records exist, it leaves the existing budget open. The filesystem adapter creates a missing directory, so a wrong/empty replacement journal mount looks like a successful empty journal. This violates both the recovery function's own stated rule and `infra/security_log.py:232-233`: absence of a proven latest ACL version must not reopen access.

**Reproduction:** `test_missing_security_journal_leaves_workspace_accessible`. First complete a real `run_security_change` against the real filesystem journal. Reconcile startup against a second empty journal directory. Reconciliation returns `{'checked': 1, 'pending_operations': 0}`; the budget has ACL revision 2 and remains unquarantined; `resolve_actor` succeeds under the API role.

**Expected:** Existing access with no independent proven version is quarantined or startup fails closed. **Actual:** Startup reports successful reconciliation and access remains available.

**Affected requirements:** ADR-14, SEC-10, AR-31, AR-32.

**Fix:** Require proof for every established workspace; distinguish an explicitly permitted initial provisioning state from an existing ACL. Treat missing journal, an older journal than the DB, and unexplained mismatches as unreconciled; quarantine and report the workspace. Only the missing-journal case was executed in this diagnostic; the other mismatch cases are required design considerations, not additional reproduced findings.

### CR-04: BLOCKER — Cancelled extraction can occupy AI slots for the rest of the quota month

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/extraction.py:201-234`, `:394-415`.

**Issue:** Text/voice extraction and receipt extraction settle quota only on normal return, `ProviderUnavailable`, or `ValidationFailed`. `asyncio.CancelledError` and process death leave the reservation `reserved` and both quota counters' `in_flight` incremented. Unlike the revised analysis path, extraction persists no recoverable attempt owner/deadline and has no abandoned-reservation cleanup. Retrying the draft under a new version creates another reservation but does not release the old concurrency slot. Sufficient cancellations block a workspace, then potentially the whole service, despite no live model calls.

**Reproduction:** `test_extraction_cancellation_exhausts_workspace_slots`. With a one-slot workspace limit, cancel the provider call after reservation commit. Both counters remain `in_flight=1`; the reservation is `reserved`; the next extraction using draft version 2 raises `QuotaExceeded` before any provider call.

**Expected:** Preserve unknown cost but release an abandoned call's concurrency slot, and allow a fresh attempt under a fresh reservation. **Actual:** The abandoned slot blocks new work until explicit recovery or a new quota month. The cancellation path was executed; hard-crash exposure follows the same committed reservation without any cleanup.

**Affected requirements:** ADR-05, ADR-12, TECH-06, AR-29, LIM-11.

**Fix:** Extend persisted attempt ownership/deadlines and abandoned-reservation recovery to extraction/receipts. Use cancellation-shielded cleanup for cooperative cancellation and a recovery path for process death, preserving unknown cost exactly as analysis does. Do not merely release uncertain cost.

### CR-05: BLOCKER — Changed analysis basis is published and offered as current

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/analysis.py:603-611`, `:650-658`.

**Issue:** Result storage verifies lease/attempt ownership but never compares the preparation's revision vector with the current workspace. It validates against the previously captured protected lines, rejected directions and mute state, then saves recommendations with `status='proposed'` and publishes the old summary. `mark_stale_recommendations` has no production callers. The recommendations menu only filters by `proposed`, and `delivery/render.py:299-300` renders the stored summary without checking its basis. Thus a financial edit while the model runs can generate an immediately outdated current recommendation; later financial edits also leave old proposals current.

**Reproduction:** `test_old_analysis_snapshot_is_published_as_current`. Prepare and generate analysis, commit a new `data_revision` under the API RLS context, then store the result. A recommendation remains `proposed` with a vector different from the workspace and one `AnalysisCompleted` event is emitted.

**Expected:** An obsolete basis is rejected, marked stale, regenerated, or explicitly presented as historical. **Actual:** Old data is published through the current proposal path without a stale warning.

**Affected requirements:** ADR-05, ADR-08, ADR-09, FR-76, A133, AI-08.

**Fix:** Compare the complete revision vector under the result commit lock and revalidate mutable recommendation restrictions. Wire invalidation or an equivalent read-time freshness check into relevant changes, proposal listing and delivery. Keep historical snapshots available as historical evidence.

### CR-06: BLOCKER — Recommendation arithmetic is trusted merely because a formula string exists

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/intelligence/analysis.py:280-309`.

**Issue:** `validate_cards` checks that referenced metric IDs exist and an effect formula is nonempty, then accepts any positive model-supplied effect. It neither evaluates a permitted calculation nor proves operands came from the snapshot. The claimed server check against invented amounts therefore does not exist. Accepted amounts are persisted and displayed by `conversation/analytics_flow.py:294-299` as the expected effect.

**Reproduction:** `test_recommendation_formula_is_not_verified`. A valid reference to a snapshot with 100 minor units, formula `1 - 1`, and claimed effect `999999999.00` RUB is accepted with no rejection and effect `99999999900` minor units.

**Expected:** Unsupported or mathematically inconsistent effects are rejected or shown as unquantified. **Actual:** The fabricated effect is accepted for delivery as a verified recommendation.

**Affected requirements:** AI-08, FR-75, ADR-08, ADR-09; `docs/TZ.md:832` explicitly requires rejecting unsupported sums.

**Fix:** Define typed permitted effect calculations with snapshot metric references and explicit operands, calculate results in server code and compare the returned effect. If a valid calculation cannot be established, require an unavailable-effect explanation. Never execute the model's formula text using `eval`.

### CR-07: BLOCKER — Immediate and queued author replies race and send the same response twice

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/ingestion/process_event.py:648-669`, `:317-326`.

**Issue:** The durable `deliver_reply` job is committed and immediately claimable before the inbound worker calls `_send_now`. Another worker can claim it while the immediate sender is still waiting. Both workers see the same pending answer and have distinct valid leases; no atomic delivery owner prevents the two sends. `_settle_delivery` subsequently clears even a running reply job's lease, but that occurs after its external call may already have started.

**Reproduction:** `test_immediate_and_durable_reply_both_send`. Block the first successful sender before it returns, claim and run the queued reply on the second worker, then unblock the first. The recording sender captures two identical financial responses. Both calls succeed; no ambiguous network failure is needed. Business handling is replaced with a fixed financial reply to isolate this transport race.

**Expected:** Immediate and queued paths participate in one logical delivery owner. **Actual:** Normal concurrent workers deterministically send twice.

**Affected requirements:** ADR-05 (author confirmation uses the same logical delivery), FR-86, A100. This does not assert exactly-once delivery across ambiguous external transport results.

**Fix:** Give the reply one durable claim/lease before either path sends. Have the immediate path claim that same delivery atomically, or let only the delivery worker send. Do not publish an independently claimable fallback while its primary sender owns the attempt.

### CR-08: BLOCKER — Crash recovery ignores the configured maximum attempts

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/platform/queue.py:122-151`.

**Issue:** Expired running jobs become `retry_wait` without checking attempts; the ready query likewise omits `attempts < max_attempts`. The only exhaustion check is in `fail`, which is never called when a process crashes. Repeated crashes can therefore reclaim and execute a job indefinitely, including expensive external work, despite the promised attempt cap.

**Reproduction:** `test_expired_running_job_bypasses_max_attempts`. Claim a job configured for one total attempt, expire its lease and reclaim. The returned job has `attempts=2`, `max_attempts=1`.

**Expected:** Expiry of the last allowed execution produces a terminal failed state and the appropriate recovery/fallback path. **Actual:** Another execution is granted beyond the limit.

**Affected requirements:** ADR-05, TECH-06, NFR-07.

**Fix:** Apply exhaustion and deadline transitions during lease reclamation, exclude exhausted jobs from claims, and expose terminal completion/fallback for the underlying logical subject. Keep the attempt increment atomic with the claim.

## Reproduction

Use a new unique database name on every invocation:

```sh
FINTRACKER_TEST_DB=fintracker_async_audit_UNIQUE FINTRACKER_AI__ENABLED=false FINTRACKER_ASR__PROVIDER=none .venv/bin/python -m pytest -q -s --tb=short -p tests.conftest .planning/audits/2026-09-14-readiness/async/test_async_diagnostics.py
```

The preserved diagnostics and JUnit files are local to this audit. Historical findings and the current release evidence were not overwritten. This review makes no claim of live Telegram/AI acceptance, full corpus quality, production PITR or exhaustive absence of other defects.
