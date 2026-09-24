---
phase: 2026-09-15-recheck-597029c-delivery
reviewed: 2026-09-14T17:27:39Z
depth: deep
files_reviewed: 2
files_reviewed_list:
  - src/fintracker/application/delivery/dispatch.py
  - src/fintracker/application/identity/security_change.py
findings:
  critical: 3
  warning: 0
  info: 0
  total: 3
status: issues_found
---

# Delivery and identity review: 597029c versus 5646887

**Reviewed:** 2026-09-14T17:27:39Z (15 September in project timezone)
**Depth:** deep
**Files Reviewed:** 2 primary files; dependencies traced below
**Status:** issues_found

## Narrative Findings (AI reviewer)

### CR-01 [BLOCKER]: Completed revocation or deletion during rendering still sends financial content

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/delivery/dispatch.py:485-494`

**Related lines:** `dispatch.py:307-335`, `399-428`, `499-509`; `application/identity/membership.py:143-151`, `612-618`.

**Issue:** The new final check only rereads the worker lease and workspace fence/quarantine/state. It does not reread the recipient's status and generation, or the delivery's cancellation state. It even accepts a deleting/deleted workspace for every delivery class, since `_delivery_authority` does not receive the class. A revocation or deletion can complete while `render_event` performs its database awaits. By the final check, the security fence has already been cleared, so the check succeeds despite the domain command having cancelled the delivery. The cached financial text is sent to the removed member or after workspace deletion. The final unconditional delivery update then rewrites `cancelled` to `sent`.

**Proof:** Two independent integration cases call the real `render_event` for an actual expense, then finish the real `remove_member` or `delete_workspace` before returning the rendered text. PostgreSQL roles, journal writes, cancellation, and queue lease checks are real. Immediately before sending, the states are respectively `(fence=None, workspace=active, member=removed, delivery=cancelled)` and `(fence=None, workspace=deleting, member=active, delivery=cancelled)`. Both send `Участник добавил расход: 450,00 ₽ — Продукты` and leave the delivery `sent`.

**Fix:** Make the final authorization recipient- and delivery-specific. Reread scalar values for membership generation/status, delivery state/class, and workspace state immediately before the external send, avoiding cached ORM objects. Permit deleting/deleted workspaces only for proven terminal messages. A cancelled delivery must remain cancelled. Guard the result update with the expected delivery state and current recipient generation. Cover both completed revocation and completed deletion during rendering, in addition to an in-progress fence.

**Reproduction:** `test_delivery_boundaries.py::test_completed_access_change_during_render_prevents_send[remove_member]` and `[delete_workspace]` fail.

### CR-02 [BLOCKER]: A fence lasting two delivery attempts permanently strands the notification

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/delivery/dispatch.py:561-567`

**Related lines:** `dispatch.py:359-360`; `application/platform/queue.py:101-107`; `runtime/worker.py:98-125`.

**Issue:** On an access fence, the new early-return path invokes `_schedule_unfinished`. The first invocation schedules `deliver:{event_id}:{available_at-second}`. Since an access fence does not move `NotificationDelivery.available_at`, the next attempt has that same logical key. If the fence is still present, `_schedule_unfinished` tries to insert its own existing key. `enqueue` uses `ON CONFLICT DO NOTHING`, so no successor is created. The handler returns successfully and the worker marks this last job `succeeded`. Clearing the fence later leaves the delivery pending with no runnable task. A normal security change only needs to overlap two fast worker attempts to lose the notification.

**Proof:** The test leaves a real workspace fenced for the initial job and its first queued successor, runs both through `_run_with_lease`, then clears the fence. Delivery state is `pending`; the two jobs are both `succeeded`; `claim_jobs` returns an empty list.

**Fix:** Reschedule the current leased job with bounded backoff when the access condition is temporary, and ensure the worker does not complete a rescheduled job. Alternatively create a successor with a monotonically advanced due time/key and verify a runnable job exists before returning. Preserve idempotency without using a key that can refer to the current or an already completed job.

**Reproduction:** `test_delivery_boundaries.py::test_fence_surviving_two_attempts_keeps_delivery_runnable` fails.

### CR-03 [BLOCKER]: Concurrent normal ACL change triggers persistent quarantine during worker startup

**File:** `/Users/Bayramov_N/Desktop/Other/financial-tracker/src/fintracker/application/identity/security_change.py:529-540`

**Related lines:** `security_change.py:494-504`, `547-568`; `runtime/worker.py:149-155`.

**Issue:** The journal is read before the workspace row is locked. The newly added `database_revision > last.proposed_acl_revision` comparison assumes both observations describe the same moment. During a worker restart while the API continues serving users, an ordinary membership change can fully commit to the database and independent journal after the earlier journal read. Reconciliation compares that stale in-memory record with the new database revision, logs a nonexistent journal lag, and quarantines the healthy workspace. Later reconciliation does not clear quarantine even when revisions match, so a harmless overlap blocks the budget until manual intervention.

**Proof:** A real baseline security change is committed. Immediately after the real `last_committed` read returns revision 2, the barrier completes the real `remove_member` protocol, producing database revision 3 and committed journal revision 3 with no fence. Reconciliation then reports journal revision 2 and sets `quarantined=True`. A fresh real journal read verifies revision 3 equals the database; the test fails on the unexpected quarantine.

**Fix:** Compare a stable observation of both stores. Capture the database ACL revision/fence before the journal read and verify it has not changed when acquiring the workspace lock; retry the whole observation on a concurrent change. Likewise distinguish a live pending protocol from an abandoned one. Do not hold the workspace transaction across external journal I/O. Only quarantine a mismatch demonstrated against an unchanged database version.

**Reproduction:** `test_delivery_boundaries.py::test_reconciliation_retries_after_concurrent_committed_access_change` fails.

## Verification evidence and scope

Command:

```sh
FINTRACKER_TEST_DB=fintracker_audit_597_delivery PYTHONPATH=src:. .venv/bin/pytest -q -p tests.conftest .planning/audits/2026-09-15-recheck-597029c/delivery/test_delivery_boundaries.py --tb=short
```

Result: **4 failed in 4.27s**, all at assertions for the defects described above. Earlier fixture setup typo was corrected before this final run. The final run has no setup errors or skips. The only monkeypatches insert deterministic scheduling barriers after real reads/rendering; no authorization or persistence method is mocked. External Telegram sends use `RecordingSender`. Only the dedicated disposable database `fintracker_audit_597_delivery` is used.

Both primary files were read fully. Cross-module call chains were followed through membership removal/deletion, outbox rendering, queue insertion/claim/completion, worker startup/execution, session transaction boundaries, independent security-log storage, and fixture creation. This review covers the assigned delivery/identity changes, not all project functionality. No product source or normal tests were edited, and no commit was created.

_Reviewer: gsd-code-reviewer, delegated delivery/identity review_
