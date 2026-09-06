# Runtime Soak Findings Remediation Implementation Plan

> **For Codex:** REQUIRED SKILL: Use `executing-plans` to implement this plan
> task-by-task in the exclusive runtime-serialization worktree. Do not start
> Claude, subagents, or background tasks.

**Goal:** Stop repeated complete-but-abnormal production audits, stop retries
for deterministic empty-input messages, and audit the five historical recovery
batches without changing recognition or trading semantics.

**Architecture:** Separate audit completion from audit health while retaining
the current monitor state schema. Represent exact empty-input authoritative
failure as a typed terminal queue outcome that settles the durable job but
preserves the persisted fail-closed recognition decision. Deliver and verify
the monitor and worker repairs independently; keep Phase 6 unclaimed.

**Tech Stack:** Python 3.13, SQLAlchemy, SQLite, pytest, systemd, existing
production safety-monitor and Deepcoin read-only adapters.

---

## Global execution rules

- Work only in
  `/Users/steven/Documents/telegram获取消息/.worktrees/runtime-serialization`.
- At takeover, read `AGENTS.md`,
  `docs/runtime-serialization-remediation-status.md`, and only its
  `current_phase_file`. Verify the exact actual HEAD, clean worktree, remote
  relationship, `phase_status`, and `claimed_by`. This remediation is not a
  Phase 6 claim; do not modify the status file.
- Stop immediately on another owner's claim, unexpected HEAD, or unrelated
  changes. Do not reset, clean, overwrite, or use `git add -A`.
- Preserve `message_lock_mode=global`, recognition decisions, strategy
  resolution, position ownership, and exchange execution semantics.
- Send no test Telegram messages and perform no exchange writes.
- Use focused tests while editing and exactly one full suite for each final
  production-code candidate. A production-code edit after that suite creates a
  new candidate and requires the affected focused tests plus one new full
  suite.
- Push or deploy only after presenting the exact reviewed SHA and obtaining
  explicit approval for that SHA and operation.

### Task 1: Add failing monitor scheduling tests

**Files:**

- Modify: `tests/test_production_safety_monitor.py`

**Step 1: Add the complete-abnormal daily scheduling regression**

Add a test named
`test_complete_abnormal_daily_audit_records_date_and_skips_same_day_rerun`.
Build an audit from `_healthy_audit()` with one internally consistent
`recovery_required` item, run the monitor after 09:00 Shanghai time, then run it
again later on the same Shanghai date without `force_full_audit`.

Assert:

- first result contains `audit_abnormal` but not `audit_incomplete`;
- `last_full_audit_date` equals that Shanghai date;
- the second adapter does not run `audit`;
- the second result still retains the prior active audit reason under the
  existing unrechecked-audit logic;
- notification deduplication is unchanged.

**Step 2: Add the incomplete-audit retry regression**

Add a parametrized test named
`test_incomplete_daily_audit_does_not_record_date_and_retries_next_cycle` for:

- `snapshot_status="snapshot_unstable"`;
- `snapshot_validation="not_run"`;
- `output_complete=False`;
- incomplete legacy scan metadata.

Assert the date remains unset and the next eligible invocation calls the audit
adapter again. Keep existing adapter/nonzero-command tests as coverage for
adapter failure.

**Step 3: Run the new tests and prove RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_production_safety_monitor.py::test_complete_abnormal_daily_audit_records_date_and_skips_same_day_rerun \
  tests/test_production_safety_monitor.py::test_incomplete_daily_audit_does_not_record_date_and_retries_next_cycle
```

Expected: the complete-abnormal case fails because the current code does not
advance `last_full_audit_date`. Confirm the incomplete cases express current
fail-closed behavior.

### Task 2: Implement completed-audit scheduling

**Files:**

- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `tests/test_production_safety_monitor.py`

**Step 1: Add one pure completion predicate**

Near `_audit_result_is_healthy`, add
`_audit_result_is_complete(audit: Mapping[str, Any]) -> bool`. Reuse
`_evaluate_audit` and return false when its reasons contain
`audit_incomplete` or `malformed_snapshot`. Do not duplicate the full audit
schema rules, and do not treat `audit_abnormal` as incomplete.

**Step 2: Change only the schedule-marker condition**

In `run_production_safety_monitor`, update `successful_audit_date` when `audit`
is present and `_audit_result_is_complete(audit)` is true. Leave
`audit_rechecked_healthy` on `_audit_result_is_healthy(audit)` so an abnormal
audit cannot falsely announce recovery.

Do not rename `last_full_audit_date` or alter `MonitorState` serialization.

**Step 3: Run focused monitor tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_production_safety_monitor.py \
  -k 'daily_audit or audit_recovery or unrechecked_audit or complete_abnormal or incomplete_daily'
```

Expected: pass.

**Step 4: Review the semantic diff**

Run:

```bash
git diff --check
git diff -- src/telegram_kol_research/production_safety_monitor.py \
  tests/test_production_safety_monitor.py
```

Verify that only the daily completion marker changed; audit findings,
notifications, and recovery checks remain intact.

**Step 5: Run the monitor candidate full suite once**

Run:

```bash
.venv/bin/python -m pytest -q
```

Record pass/fail counts, warnings, duration, and the exact pre-commit diff. Do
not rerun the full suite unless production code changes afterward.

**Step 6: Commit explicit paths**

Run:

```bash
git add src/telegram_kol_research/production_safety_monitor.py \
  tests/test_production_safety_monitor.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: schedule complete management audits once daily"
```

The cached path list must contain exactly the two files above.

### Task 3: Review, authorize, deploy, and verify the monitor repair

**Files:** None locally unless verification documentation is explicitly
requested after the checkpoint.

**Step 1: Obtain exact-SHA authorization**

Record the full SHA and review status:

```bash
git status --short
git rev-parse HEAD
git show --stat --oneline HEAD
```

Stop and request approval to fast-forward that exact SHA to
`codex/deepcoin-auto-trading-v1` and deploy it. Approval does not authorize a
force push, database mutation, test notification, historical replay, or
exchange write.

**Step 2: Push and deploy only after approval**

Run:

```bash
reviewed_sha="$(git rev-parse HEAD)"
git push origin "$reviewed_sha:refs/heads/codex/deepcoin-auto-trading-v1"
test "$(git ls-remote origin refs/heads/codex/deepcoin-auto-trading-v1 | awk '{print $1}')" = "$reviewed_sha"
EXPECTED_COMMIT="$reviewed_sha" BRANCH=codex/deepcoin-auto-trading-v1 \
  ./scripts/server_git_update.sh
```

The updater owns the exact-commit, clean-tree, active-write, stop/start, and
health gates. Stop on any refusal; do not work around it.

**Step 3: Verify two natural timer cycles**

Use no manual notifying monitor run and no interactive polling loop. At two
normal checkpoints, collect a bounded server evidence file containing:

- deployed HEAD and service state;
- first natural run's `audit_ran` and persisted `last_full_audit_date`;
- next timer run's `audit_ran=false` on the same Shanghai date;
- retained `audit_abnormal`/composite reason codes;
- monitor unit result and event-loop stall summaries.

The core acceptance is that the first complete abnormal audit records the date
and the next scheduled run skips the expensive full audit without clearing the
finding. If the audit is incomplete, treat the result as unknown and allow one
reasoned retry only.

### Task 4: Add failing terminal empty-input worker tests

**Files:**

- Modify: `tests/test_message_processing_worker.py`

**Step 1: Add the terminal settlement regression**

Add
`test_empty_input_authoritative_failure_settles_once_without_retry_or_notifier`.
Return a processing result with:

- `assessment.agreement_status="authoritative_failed"`;
- `assessment.mimo.error_message="message has no readable text or image"`;
- persisted-style recognition failure and skipped automation fields.

Run one worker tick and assert:

- `succeeded == 1`, `retried == 0`, and `failed == 0`;
- job status is `succeeded` with a stable explicit terminal reason;
- `attempt_count == 0`, `next_attempt_at is None`, and `completed_at` is set;
- the terminal failure notifier is not called;
- strategy and context-resolution worker callbacks are not called.

**Step 2: Add same-chat lane progress coverage**

Queue two ordered jobs for one chat. Make the first authoritative result the
exact empty-input failure, then return a normal result for the second. Assert
the first tick settles only the first job and the next tick claims and succeeds
the second.

**Step 3: Preserve transient failure coverage**

Extend or retain `test_returned_authoritative_failure_uses_durable_retry` with
a non-empty transient `assessment.mimo.error_message`. Assert it remains
pending with `attempt_count == 1` and
`processing_error:AuthoritativeProcessingFailed`. Keep
`test_retry_backoff_is_durable_and_max_attempts_are_terminal` unchanged as the
max-attempt gate.

**Step 4: Run the new tests and prove RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_message_processing_worker.py::test_empty_input_authoritative_failure_settles_once_without_retry_or_notifier \
  tests/test_message_processing_worker.py::test_empty_input_terminal_outcome_releases_same_chat_lane \
  tests/test_message_processing_worker.py::test_returned_authoritative_failure_uses_durable_retry
```

Expected: the empty-input cases fail because current behavior schedules a
retry; the transient case passes.

### Task 5: Implement the typed terminal queue outcome

**Files:**

- Modify: `src/telegram_kol_research/message_processing_worker.py`
- Modify: `tests/test_message_processing_worker.py`

**Step 1: Define a stable reason and typed exception**

Add a private constant for the exact deterministic source reason and a
`TerminalAuthoritativeProcessingFailed` exception distinct from
`AuthoritativeProcessingFailed`. Give the terminal exception a stable queue
reason such as `terminal_authoritative_failure:empty_input`.

**Step 2: Classify from the processing result**

Add a small pure helper that returns true only when:

- the agreement status is `authoritative_failed`; and
- `assessment.mimo.error_message`, stripped, exactly equals the deterministic
  empty-input reason.

Missing attributes, unknown payloads, provider failures, schema failures, and
all other reasons must return false and retain the current retry path. Do not
import the notification classifier or classify from rendered notification
text.

**Step 3: Stop the post-persist chain with the typed terminal outcome**

In `process_message_job`, where returned authoritative failure is currently
raised for queue processing, raise the terminal exception for the exact empty
input and the existing retryable exception for every other failure. Keep this
before strategy processing and the context-resolution worker.

**Step 4: Settle terminal outcomes before generic exception handling**

In `run_message_processing_worker_tick.run_claim`, catch the terminal exception
after `CancelledError` and before `BaseException`. Call
`_settle_message_processing_job` with:

- `status="succeeded"`;
- the stable terminal queue reason;
- `completed_at=tick_time`.

Increment `counts["succeeded"]` only when the claim-token guarded update wins,
then return. Do not call `_defer_or_fail_message_processing_job`, increment an
attempt, log it as a worker crash, or invoke the terminal notifier.

**Step 5: Run focused worker and queue regressions**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_message_processing_worker.py \
  tests/test_message_pipeline_mode_exclusivity.py
```

Expected: pass.

**Step 6: Review the semantic diff**

Run:

```bash
git diff --check
git diff -- src/telegram_kol_research/message_processing_worker.py \
  tests/test_message_processing_worker.py
```

Verify there is no change to recognition construction, strategy resolution,
execution, exchange adapters, schema, worker claim order, retry backoff, or
`message_lock_mode`.

**Step 7: Run the worker candidate full suite once**

Run:

```bash
.venv/bin/python -m pytest -q
```

Record counts, warnings, duration, and exact candidate SHA inputs. If any
production code changes after this run, rerun the affected focused tests and
this full suite exactly once on the new candidate.

**Step 8: Commit explicit paths**

Run:

```bash
git add src/telegram_kol_research/message_processing_worker.py \
  tests/test_message_processing_worker.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: settle deterministic empty-input queue jobs"
```

### Task 6: Review, authorize, deploy, and observe the worker repair

**Files:** None locally unless verification documentation is explicitly
requested after the checkpoint.

**Step 1: Obtain a second exact-SHA authorization**

Present the full SHA, both repair commits, focused results, and final suite
result. Stop for explicit approval to push and deploy that exact SHA.

**Step 2: Push and deploy through the gated updater**

After approval, use the exact commands from Task 3 Step 2 with the new SHA.
The normal updater restart is sufficient; do not add a second deliberate
restart because process lifecycle recovery is not this repair's core claim.

Record the worker deployment timestamp as the new start of the one-week queue
stability prerequisite for Phase 6.

**Step 3: Run the L2 observation window**

Observe 30 continuous minutes and at least five natural messages, trying to
cover two chats. If five messages do not arrive within 30 minutes, stop at 30
minutes and record the limited traffic rather than extending the window.

At pre-deploy, post-cutover, and observation-end checkpoints, record:

- raw-message/job parity, missing/orphan rows, pending/claimed backlog, and
  stale claims;
- succeeded/retried/failed counts and terminal empty-input reasons;
- unique decision per raw message and zero duplicate source executions;
- service state, restart count, event-loop stalls, SQLite lock errors, and
  journal errors;
- direct Deepcoin history for any naturally occurring execution, matching
  exact order/client/position identity.

Keep raw JSON and detailed rows in a private server evidence file. Treat an
incomplete exchange query as unknown, retry once for a reasoned transient
failure, then fail closed.

### Task 7: Audit the five historical recovery batches read-only

**Files:** No repository changes.

**Step 1: Capture a coherent bounded management audit**

On the server, create a private evidence directory and run:

```bash
umask 077
evidence_dir="data/evidence/runtime-soak-remediation-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$evidence_dir"
.venv/bin/telegram-kol-research audit-management-batches \
  --database-path data/research.db --limit 100 --output-format json \
  > "$evidence_dir/management-batches.json"
```

Require stable snapshots, `snapshot_validation=ok`, complete output, and the
expected five batch references `123`, `127`, `129`, `133`, and `144`. If the
evidence is incomplete or the set differs, stop and report the discrepancy.

**Step 2: Capture current read-only exchange protection evidence**

Run:

```bash
.venv/bin/telegram-kol-research audit-protection-incidents \
  --database-path data/research.db --limit 100 --output-format json \
  > "$evidence_dir/protection-incidents.json"
.venv/bin/telegram-kol-research audit-tpsl-ownership \
  --database-path data/research.db --output-json \
  > "$evidence_dir/tpsl-ownership.json"
```

Require complete exchange reads. One reasoned retry is allowed for an
incomplete external query; a second incomplete result remains unknown.

**Step 3: Build the per-batch evidence chain**

Using SQLite read-only mode and the existing read-only Deepcoin client paths,
classify each batch independently from:

- source message and strategy lifecycle;
- management batch and leg state;
- mutation-intent/outbox state;
- exact execution binding, client order ID, exchange order ID, and `posId`;
- current position, pending trigger/TPSL, order history, and fills.

Do not infer identity from symbol, side, time proximity, tag, or `clOrdId`
alone. For conditional-limit attribution, require parent trigger to unique child
regular order to `posId` lineage.

**Step 4: Produce a recommendation and stop before mutation**

For each batch, report evidence completeness and one of:

- historical terminal/informational;
- currently protected but locally stale;
- unresolved external identity;
- genuine recovery candidate.

Do not edit the database, replay messages, cancel orders, or invoke any `--apply`
command. A genuine recovery candidate requires a new L3 plan with exact change,
backup, `quick_check`, before/after counts, rollback, and separate user
approval.

### Task 8: Final handoff

**Files:**

- Do not modify `docs/runtime-serialization-remediation-status.md`.

**Step 1: Verify the repository boundary**

Run:

```bash
git status --short
git log --oneline --decorate -5
git diff origin/codex/deepcoin-auto-trading-v1...HEAD --stat
```

Confirm the worktree is clean, only reviewed remediation commits are ahead, and
Phase 6 remains unclaimed.

**Step 2: Report the new Phase 6 prerequisite boundary**

Return:

- exact deployed SHA and deployment timestamp;
- monitor two-cycle result and evidence path;
- worker 30-minute/message observation metrics and evidence path;
- historical batch classifications and any evidence gaps;
- the recalculated one-week queue-soak earliest eligibility timestamp;
- any separately authorized work still required.
