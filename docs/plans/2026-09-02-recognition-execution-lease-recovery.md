# Recognition Execution Lease Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent authoritative recognition generations from remaining permanently `execution_running`, while guaranteeing that a lost process can never cause the same exchange-capable action to be replayed automatically.

**Architecture:** Add an attempt-grained durable lease with an explicit pre/post side-effect boundary. Fence every transition with an exact attempt token, persist automation outcomes before finalizing the mutable decision projection, drain worker execution on SIGTERM, and freeze post-boundary losses as `execution_uncertain`. Install schema, activate runtime, and repair the 29 legacy rows as three separately authorized L3 actions.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, asyncio/FastAPI lifespan, systemd process identity, pytest, immutable scoped releases and existing runtime-incident/operator-notification infrastructure.

---

## Authorization boundary

This plan is not authorization to implement or deploy it. The planning turn permits no production-code edit, schema write, decision/job mutation, deployment, restart, recognition replay or exchange write.

Later execution must obtain separate authorization for:

1. implementation/tests and production-copy rehearsal;
2. the additive production schema action;
3. worker runtime activation;
4. exact legacy-row repair.

Schema, runtime activation and data repair must never be combined into one action.

### Task 1: Preserve the root-cause regression as RED tests

**Files:**
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_context_resolution_worker.py`
- Modify: `tests/test_message_processing_worker.py`

1. Add a test that injects an exception at each current post-claim boundary:
   `apply_authoritative_assessment`, source barrier, candidate/item lookup,
   settings load and decision finalize. Assert the current implementation leaves
   `execution_running`; these are the RED characterization cases.
2. Add a test where `run_context_resolution_once().reanalyze` claims the
   authoritative generation and then raises. Assert context worker returns retry
   state rather than re-raising.
3. Embed that worker in `process_message_job()` and
   `run_message_processing_worker_tick()`; prove the outer message job becomes
   `succeeded/worker_completed` while the reanalyzed decision remains running.
4. Add the disproval case: an `authoritative_failed` assessment never obtains an
   execution claim, and assessment is not recomputed after claim.
5. Run:

```bash
pytest -q \
  tests/test_authoritative_recognition.py \
  tests/test_context_resolution_worker.py \
  tests/test_message_processing_worker.py
```

Expected: the new release-guarantee assertions fail against the current code;
the root-cause characterization passes.

### Task 2: Add RED schema and lease-state tests

**Files:**
- Modify: `tests/test_db_bootstrap.py`
- Add: `tests/test_authoritative_execution_attempts.py`
- Modify: `tests/test_recognition_decisions.py`

1. Specify the exact additive `authoritative_execution_attempts` table from the
   design: FK/index, `(raw_message_id, authoritative_generation)` uniqueness,
   status check, owner identity, lease, heartbeat, side-effect boundary,
   outcome and terminal timestamps.
2. Require two bootstraps to be idempotent and prove all pre-existing tables and
   counts remain unchanged.
3. Specify state-machine CAS tests:
   - one active attempt per generation;
   - wrong/stale token cannot change state;
   - only `claimed + side_effect_started_at IS NULL` is safe-reclaimable;
   - `executing` can become only `uncertain` after loss;
   - `outcome_recorded` can finalize without invoking an adapter;
   - `uncertain` is never claimable/replayable.
4. Add save tests proving both `execution_running` and
   `execution_uncertain` reject a new authoritative overwrite.
5. Run and observe RED:

```bash
pytest -q \
  tests/test_db_bootstrap.py \
  tests/test_authoritative_execution_attempts.py \
  tests/test_recognition_decisions.py
```

### Task 3: Implement the additive model and persistence API

**Files:**
- Modify: `src/telegram_kol_research/models.py`
- Add: `src/telegram_kol_research/authoritative_execution_attempts.py`
- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Modify: `src/telegram_kol_research/db.py` only if an explicit compatibility/index helper is required
- Modify: `src/telegram_kol_research/cli.py`
- Modify: tests from Task 2

1. Add `AuthoritativeExecutionAttempt` as an audit-preserving table; do not add
   owner/lease columns to `recognition_decisions`.
2. Implement immutable snapshot/claim dataclasses and exact-token helpers:
   - `claim_authoritative_execution_attempt()`;
   - `heartbeat_authoritative_execution_attempt()`;
   - `mark_authoritative_side_effect_started()`;
   - `record_authoritative_automation_outcome()`;
   - `finalize_authoritative_execution_attempt()`;
   - `fail_safe_authoritative_execution_attempt()`;
   - `mark_authoritative_execution_uncertain()`.
3. Couple decision claim + attempt insert in one transaction. Couple decision
   finalize + attempt succeeded in one transaction.
4. All canonical outcome/error fields must be bounded and secret-screened. Never
   persist provider credentials, Deepcoin headers, request bodies or message text.
5. Add a purpose-built schema command with `plan/rehearse/apply` separation. It
   may create only the new table/indexes and must emit a canonical plan hash. It
   must not initialize unrelated schema, change business rows or start runtime.
6. Keep this schema support dormant: no production runtime path imports or writes
   attempts yet.
7. Re-run Task 2 until GREEN.
8. Commit only the schema/persistence slice with explicit paths.

### Task 4: RED/GREEN guaranteed classification around claim/finalize

**Files:**
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_recognition_decisions.py`

1. Before implementation, extend RED tests for these exact outcomes:
   - exception before side-effect boundary → decision `completed`, automation
     `failed`, attempt `failed_safe`;
   - boundary CAS failure → adapter call count 0;
   - exception/cancellation after boundary → decision
     `execution_uncertain`, attempt `uncertain`, adapter never replayed;
   - outcome persisted then finalize fails → attempt `outcome_recorded` and a
     later finalize-only pass calls adapter 0 times;
   - normal block/hold/no-action/not-configured/failed/unknown/success outcomes
     remain byte-for-byte equivalent at the public result boundary.
2. Refactor only `process_authoritative_message()` current claim-to-finalize
   interval into the designed execution scope. Do not change MiMo/context
   decisions, candidate contents, action parameters or executor selection.
3. Persist `executing/side_effect_started_at` immediately before the existing
   `auto_trade_executor(raw_message_id)` call. Every possible wait or branch
   before the adapter must remain pre-boundary.
4. Persist canonical automation outcome before decision finalize. Recovery from
   `outcome_recorded` may call only finalize.
5. Catch `BaseException` for classification but re-raise it after best-effort
   durable state transition. Never turn cancellation into apparent success.
6. If classification persistence fails, retain fail-closed running state and
   emit an incident; do not clear token or claim success.
7. Run:

```bash
pytest -q \
  tests/test_authoritative_recognition.py \
  tests/test_recognition_decisions.py \
  tests/test_authoritative_execution_attempts.py
```

### Task 5: Add lease fencing, owner identity and SIGTERM drain

**Files:**
- Modify: `src/telegram_kol_research/message_processing_worker.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/authoritative_execution_attempts.py`
- Modify: `tests/test_message_processing_worker.py`
- Modify: `tests/test_runtime_role_selection.py`
- Add or modify: a focused lifespan/shutdown test in the existing Web app test module

1. Build one worker-instance identity at process startup from random instance ID,
   PID, boot ID, `/proc/self/stat` start ticks and optional systemd
   `INVOCATION_ID`. Pass it into attempt claims; do not read it from client input.
2. Add heartbeat/lease renewal for active attempts. Heartbeat is observability,
   not permission to cross the side-effect boundary.
3. Require the side-effect transition to CAS exact raw/generation/claim token.
   A reclaimed old owner must fail that CAS and exit before adapter invocation.
4. Replace immediate shutdown cancellation of the message worker with:
   stop-new-claims → bounded wait for `MessageProcessingActivity.active == 0`
   and owned active attempts → cancel only after the drain deadline.
5. Test that cancelling the awaitable returned by `asyncio.to_thread()` is not
   treated as stopping its underlying executor work.
6. Test cooperative SIGTERM completes finalize; simulate SIGKILL/power loss by
   abandoning the process object:
   - pre-boundary expiry can become `failed_safe` with adapter calls 0;
   - post-boundary expiry becomes `uncertain` with adapter calls exactly 1 and no
     future claim.
7. Run focused tests until GREEN.

### Task 6: Add active orphan detection and preserve context traceback

**Files:**
- Modify: `src/telegram_kol_research/context_resolution_worker.py`
- Modify: `src/telegram_kol_research/runtime_incident_capture.py` or the exact existing incident module selected during implementation
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_context_resolution_worker.py`
- Add or modify: focused runtime-incident/operator-notification tests

1. Before converting a reanalysis exception to retry/exhausted, call
   `logger.exception` with raw ID and context attempt ID. Preserve existing retry
   semantics and do not make an unrelated outer message job fail.
2. Add a bounded scanner for:
   - `job=succeeded + decision in execution_running/execution_uncertain`;
   - expired active lease;
   - owner identity mismatch;
   - stale `executing` attempt;
   - `outcome_recorded` without decision finalize;
   - failed terminalization/finalize CAS.
3. Run the scanner only in worker/all. Use once-only incident fingerprints and
   existing system-operator routing; do not send message text, payloads or secrets.
4. For `claimed` expiry, use fenced safe terminalization. For `executing`, mark
   uncertain only. For `outcome_recorded`, finalize locally only. Legacy running
   rows without an attempt are observation-only and never auto-mutated.
5. Test that a succeeded outer job with a failed nested context reanalysis is
   detected in one scan cycle.

### Task 7: Preserve and extend backlog-expiry protection

**Files:**
- Modify: `src/telegram_kol_research/message_processing_backlog_expiry.py`
- Modify: `tests/test_message_processing_backlog_expiry.py`
- Verify/modify as required: `src/telegram_kol_research/semantic_review_control.py`
- Verify/modify as required: `src/telegram_kol_research/message_operation_contracts.py`
- Verify/modify as required: `src/telegram_kol_research/message_operation_supervisor.py`
- Verify/modify as required: `src/telegram_kol_research/web_queries.py`

1. Add RED cases showing the plan refuses:
   - `execution_running`;
   - `execution_uncertain`;
   - attempt `executing`, `uncertain` or unreconciled `outcome_recorded`.
2. Preserve exact existing refusal for ordinary `execution_running` rows.
3. Permit expiry only after exact proof terminalizes the attempt as
   `failed_safe/succeeded`; never clear an attempt inside the expiry command.
4. Audit every production reader of `comparison_status`. Prove
   `execution_uncertain` is not claimable by semantic review, is not considered
   terminal by message-operation supervision, and is rendered as unresolved
   rather than success. Add focused tests wherever the existing generic
   non-completed behavior does not already prove this.
5. Run:

```bash
pytest -q tests/test_message_processing_backlog_expiry.py
```

### Task 8: Candidate review and final test evidence

1. Run all affected focused suites and `git diff --check`.
2. Audit imports and write paths: Web/ingest must not own leases or perform the
   scanner; no recognition, context, candidate or trading decision values may
   change in fixtures.
3. Compare adapter-call traces before/after for all normal outcomes. The only
   intended semantic change is post-claim failure/cancellation handling.
4. Request independent code review focused on fencing, transaction boundaries,
   false reclaim, SIGTERM, post-boundary uncertainty and backlog protection.
5. Fix findings with RED/GREEN, then run one final full suite after the last
   production-code edit:

```bash
pytest -q
```

6. Commit explicit paths only; never use `git add -A`. Fast-forward push the
   reviewed candidate to `codex/deepcoin-auto-trading-v1`.

### Task 9: Separate L3 schema-copy rehearsal and production schema action

This task installs schema only. It does not stage/activate runtime or mutate a
decision/job row.

1. Read all three live `/api/runtime/deployment-identity` endpoints and verify
   role-specific immutable release manifests. Record the measured worker rollback
   SHA; do not use `/opt` git HEAD.
2. Capture a root-owned mode-0600 online backup plus SHA-256,
   `PRAGMA quick_check=ok`, foreign-key check and before counts for at least:
   `raw_messages`, `message_processing_jobs`, `recognition_decisions`,
   `context_resolution_attempts`, `signal_candidates`, `message_instruction_items`,
   `strategy_lifecycles`, `execution_bindings`, `execution_order_legs`,
   `execution_events`, `strategy_management_batches` and
   `worker_command_jobs`.
3. Rehearse the exact schema plan on an independent copy. Prove only the new
   table/indexes/constraints appear, count is 0, second apply is idempotent and
   all existing schemas/counts remain unchanged.
4. Under the runtime-control/schema lock, run the exact plan-hash-bound schema
   command in one `BEGIN IMMEDIATE` transaction. Verify the same integrity and
   count gates. Do not restart a service.
5. Old runtime must continue operating and ignore the new empty table.
6. Record schema action evidence separately. Do not proceed to Task 10 in the
   same authorization.

**Schema rollback:** before runtime uses the table, drop only the new table and
indexes in a separate transaction, then repeat integrity/count checks. Once any
attempt exists, first export/hash it, roll back runtime, and only then drop the
table; otherwise active `create_all()` can recreate it.

### Task 10: Separate L3 worker runtime activation

This task activates code only after Task 9 is accepted. It performs no legacy
row repair.

1. Verify fresh role identities, complete read-only exchange state and zero
   unsafe deployment activity. Incomplete exchange evidence is a hard stop.
2. Verify the new table is empty/valid and the schema receipt matches the exact
   candidate.
3. Create separate stage/activate manifests for worker-only immutable activation.
   The activation action itself declares `schema_changed=false` because the schema
   was installed in Task 9; declare the actual L3 risk and
   `exchange_write_semantics_changed=true` because cancellation/failure fencing
   changes exchange-capable control flow.
4. Stage and activate using the measured current worker SHA as rollback. Do not
   activate Web/ingest unless a separately reviewed dependency requires it.
5. Verify worker identity, PID transition, release manifest, attempt-table writes,
   scanner health, backlog guard and complete exchange state. Do not submit a test
   trade or replay a historical message.
6. Observe 30 continuous minutes and at least 5 natural messages, trying to cover
   2 chats. If traffic is insufficient, stop at 30 minutes, leave the phase
   `in_progress` and record the limited traffic rather than extending.
7. Require zero new legacy running-without-attempt rows, zero duplicate adapter
   calls, and correct safe/uncertain classification for any natural failure.

**Runtime rollback:** stop new claims and drain. Refuse rollback while any
attempt is `executing/uncertain` without reconciliation. After all active attempts
are terminal/safe, activate the measured prior worker release; keep the new table
intact and read-only. The original 29 rows remain fail-closed and unrepaired.

### Task 11: Build and rehearse the exact legacy repair tool

**Files:**
- Add: `src/telegram_kol_research/recognition_execution_orphan_repair.py`
- Modify: `src/telegram_kol_research/cli.py`
- Add: `tests/test_recognition_execution_orphan_repair.py`

1. The tool has `plan`, `validate`, `apply` and receipt-scoped `rollback`; default
   is read-only plan. It exposes no recognition replay or exchange-write client.
2. Freeze the exact planning target IDs:
   `12798,12849,12897,13022,13076,13160,13166,13198,13307,13308,13396,13433,13503,13571,13589,13685,13723,13730,13835,14193,14196,14214,14220,14243,14289,14374,14378,14428,14497`.
   A new row or missing row requires a new review; do not silently expand the set.
3. For every row require exact status/token/automation preimage, terminal job and
   context attempts, zero current-generation candidate/batch, zero direct
   binding/order/event/envelope/target and no claimable instruction item.
4. Keep raw `14214` as a separate manifest entry proving item `909` terminalized
   before the current claim; never generalize it to the other 28.
5. For management-semantics rows, require complete worker GET exchange/current
   order evidence and exact local identity joins. Incomplete results mark only
   that row blocked; they are never interpreted as absence.
6. Plan one exact CAS per row to `failed_safe` semantics, retaining payload and
   generation provenance. If any current-generation execution evidence appears,
   exclude the row and require a separate reconciliation authorization.
7. Rehearse on a fresh production copy. Apply all exact actions in one transaction
   so any rowcount/preimage mismatch rolls back the entire repair. Prove repeat
   apply writes 0 and receipt rollback restores every byte of the 29 preimages.
8. Run quick check and before/after counts/hashes for the affected table and the
   critical execution/business tables.

### Task 12: Separate L3 production legacy-row repair

This task requires a fourth, exact plan-fingerprint authorization after Tasks
9–11 are complete and reviewed.

1. Freeze new authoritative claims and stop/drain worker under the approved
   maintenance boundary. Take a fresh backup and exact 29-row preimage.
2. Repeat all local/exchange proof predicates. Any drift or incomplete external
   result performs zero writes.
3. Apply the exact 29 CAS actions from the authorized manifest. Do not replay
   recognition and do not call any exchange adapter.
4. Verify all 29 are terminal failed-safe, none are uncertain, no execution table
   changed, backlog guard now ignores only the proven terminal rows, and all
   integrity/count gates pass.
5. Restart/activate no additional component beyond what the exact maintenance
   plan authorizes. Update status/known-issues documents with the final evidence.

**Data-repair rollback:** stop/drain worker, validate the exact repair receipt,
and restore each row's original preimage in one transaction. After rollback the
29 decisions are intentionally `execution_running` again and once more block
re-recognition/backlog expiry. Do not drop the attempt table and do not replay a
message during rollback.

## Final acceptance checklist

- [ ] No schema/runtime/data step shared one authorization or evidence receipt.
- [ ] Every adapter call is preceded by a committed side-effect boundary CAS.
- [ ] Pre-boundary owner loss performs zero adapter calls and ends failed-safe.
- [ ] Post-boundary owner loss never becomes replayable.
- [ ] Outcome-recorded recovery calls finalize only.
- [ ] SIGTERM drains; SIGKILL simulation yields safe or uncertain state, never a duplicate.
- [ ] Context reanalysis exceptions retain full traceback and no longer remain invisible.
- [ ] Succeeded-job/running-decision mismatch is detected proactively.
- [ ] Backlog-expiry protection is preserved and expanded to uncertain states.
- [ ] The 29-row repair is exact, per-row, production-copy rehearsed and independently authorized.
- [ ] Final full suite and independent review pass on the exact candidate.
