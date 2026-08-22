# Production Monitor Remediation Implementation Plan

> **For Codex:** REQUIRED SUB-SKILLS: use `executing-plans` to execute this plan task by task and `test-driven-development` for every production-code change. Do not start Claude, subagents, background agents, or parallel implementation sessions.

**Goal:** Make production-monitor composite evidence independent of UI cache traffic, make monitor capture adapters use one strict contract, and transactionally synchronize the monitor expected HEAD with every managed deployment.

**Architecture:** Add one authenticated loopback-only, read-only live-position-size projection owned by the main service; the independent monitor validates and consumes it without receiving exchange credentials. Export one immutable adapter-name contract for both capture client and server. Extend the exact-SHA updater so it stops the monitor timer, atomically aligns the pin with the actual checkout, and restores the prior timer state across success and rollback.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest, SQLite read-only access, Bash, systemd, Git, existing Deepcoin read-only client.

---

## Authoritative context

Read these files before any edit:

1. `AGENTS.md`
2. `docs/production-monitor-remediation-status.md`
3. `docs/plans/2026-08-21-production-monitor-remediation-design.md`
4. this file

Do not read unrelated runtime-serialization phase files. Phase 6 is already complete; this is a separate monitor remediation.

The verified production failure chain was:

```text
UI-owned position cache older than 5 minutes
  -> composite adapter raises live_position_snapshot_stale
  -> monitor result contains adapter_failure/composite
  -> capture client sends composite
  -> web endpoint's older allowlist rejects it with HTTP 422
  -> Monitor incident capture writer is unavailable
```

The expected-HEAD drift is separate:

```text
installed expected HEAD: 767497010baf1e1db56080fe80b3e619358b64fa
observed production HEAD: fdaff6b12d0aa4470e9bfcc63239c8541c01c5ff
```

DeepSeek HTTP 402 remediation is out of scope and may be changing in another worktree or session. Do not absorb, revert, stage, push, or deploy unrelated DeepSeek changes.

The following invariants are non-negotiable:

```text
message_lock_mode=global
message_pipeline_mode=queue
no recognition semantic change
no strategy-resolution semantic change
no position-ownership semantic change
no exchange-write semantic change
no database schema or production-data mutation
```

## Task 0: Exclusive preflight and claim

**Files:**

- Modify: `docs/production-monitor-remediation-status.md`

### Step 1: Verify the exact handoff

Run from the fixed worktree:

```bash
cd /Users/steven/Documents/telegram获取消息/.worktrees/runtime-serialization
git status --short --branch
git rev-parse HEAD
git log -1 --oneline
git fetch origin codex/deepcoin-auto-trading-v1
git rev-parse origin/codex/deepcoin-auto-trading-v1
```

Expected:

- no modified or untracked file;
- local HEAD exactly equals the full handoff SHA supplied in the new-session prompt;
- no other session owns `docs/production-monitor-remediation-status.md`;
- any local-ahead commits are understood from the handoff; do not push them merely because they exist.

Stop immediately on a dirty tree, unexpected HEAD, contradictory status, or another owner. Do not reset, clean, checkout, stash, or overwrite.

### Step 2: Claim using a unique session identifier

Edit only the two canonical fields:

```yaml
phase_status: claimed
claimed_by: codex-monitor-remediation-<UTC timestamp or unique suffix>
```

### Step 3: Verify and commit only the claim

```bash
git diff --check
git add docs/production-monitor-remediation-status.md
git diff --cached --name-only
git commit -m "chore: claim production monitor remediation"
```

Expected staged path:

```text
docs/production-monitor-remediation-status.md
```

Never use `git add -A`.

### Step 4: Enter implementation state

Change `phase_status` to `in_progress` before the first code edit. Leave `claimed_by` unchanged. Commit this transition together with Task 1's implementation, not as an empty administrative commit.

## Task 1: Establish one adapter-name contract

**Files:**

- Modify: `src/telegram_kol_research/production_safety_monitor.py:158-175`
- Modify: `src/telegram_kol_research/web_app.py:85-88`
- Modify: `src/telegram_kol_research/web_app.py:5270-5285`
- Modify: `tests/test_production_safety_monitor.py:4700-4740`
- Modify: `tests/test_web_app.py:6680-6750`
- Modify: `docs/production-monitor-remediation-status.md`

### Step 1: Write failing projection tests

Extend the production-monitor capture test so all real collector names survive projection construction:

```python
def test_monitor_incident_capture_projection_accepts_every_monitor_adapter():
    projection = build_monitor_incident_capture_projection(
        checked_at=datetime(2026, 8, 22, 1, 0, tzinfo=UTC),
        reason_codes=("adapter_failure",),
        adapter_failures=(
            "service",
            "head",
            "settings",
            "journal",
            "events",
            "audit",
            "composite",
            "coverage",
            "entry_preamble",
        ),
        notification_status="suppressed",
        monitor_error=None,
    )
    assert projection["adapter_failures"] == sorted(MONITOR_ADAPTER_NAMES)
```

Add an evaluator regression test proving `entry_preamble` becomes an `adapter_failure` detail rather than `unknown`/`malformed_snapshot`.

### Step 2: Write the failing endpoint contract test

Post one authenticated projection containing all canonical adapter names to `/api/runtime-incidents/monitor-capture`. Stub the capture functions so the test cannot mutate unrelated rows. Assert HTTP 200 and `accepted=true`.

Add a parameterized companion proving one unknown adapter name still returns HTTP 422 and calls no capture function.

### Step 3: Run the tests and prove the current drift

```bash
.venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py::test_monitor_incident_capture_projection_accepts_every_monitor_adapter \
  tests/test_web_app.py -k 'monitor_incident_writer and adapter' -q
```

Expected before implementation: failure because `entry_preamble` is not in the monitor set and/or the endpoint rejects `composite`, `coverage`, or `entry_preamble`.

### Step 4: Implement the minimum shared contract

In `production_safety_monitor.py`, replace `_ADAPTER_NAMES` with an exported immutable value:

```python
MONITOR_ADAPTER_NAMES = frozenset(
    {
        "service",
        "head",
        "settings",
        "journal",
        "events",
        "audit",
        "composite",
        "coverage",
        "entry_preamble",
    }
)
```

Use it in projection construction and adapter-failure normalization. Do not loosen unknown-value handling.

Import the same value in `web_app.py` beside `capture_uncaptured_runtime_incident_sources`. Replace the endpoint's literal six-name set and literal maximum with:

```python
or len(adapter_failures) > len(MONITOR_ADAPTER_NAMES)
or any(failure not in MONITOR_ADAPTER_NAMES for failure in adapter_failures)
```

No dynamic names, prefixes, arbitrary strings, or caller-provided incident types may be accepted.

### Step 5: Run focused tests

```bash
.venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py -k 'adapter_failure or capture_projection' \
  tests/test_web_app.py -k 'monitor_incident_writer' -q
```

Expected: all selected tests pass.

### Step 6: Commit explicit paths

```bash
git diff --check
git add \
  src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/web_app.py \
  tests/test_production_safety_monitor.py \
  tests/test_web_app.py \
  docs/production-monitor-remediation-status.md
git diff --cached --name-only
git commit -m "fix: unify monitor adapter capture contract"
```

## Task 2: Add the bounded authenticated live-position endpoint

**Files:**

- Modify: `src/telegram_kol_research/web_app.py:5167-5212`
- Modify: `tests/test_web_app.py:6600-6690`

### Step 1: Write failing authentication tests

Add tests proving `/api/runtime-incidents/live-position-sizes`:

- returns 404 to a non-loopback client;
- returns 404 when `x-forwarded-for` is present;
- returns 404 for a missing or incorrect monitor token;
- does not construct the Deepcoin client on any rejected request.

Use the existing `monitor_capture_token` test pattern. Do not assert or print a real token.

### Step 2: Write the failing success-projection test

Use a fake client whose `list_positions()` returns bounded rows containing only test values. Assert the exact response keys and normalized strings:

```python
assert response.json() == {
    "schema_version": 1,
    "complete": True,
    "captured_at": "2026-08-22T01:00:00+00:00",
    "positions": [
        {"pos_id": "position-1", "size_text": "0.25"},
        {"pos_id": "position-2", "size_text": "1"},
    ],
}
```

Inject `now_provider` for a deterministic timestamp. Assert the fake client is closed exactly once.

### Step 3: Write failing bounded-failure tests

Cover:

- provider exception;
- duplicate position identity;
- missing identity;
- negative, non-finite, boolean, or malformed size;
- more than 100 positions;
- client cleanup failure.

Provider/data failures must return a closed response with:

```python
{
    "schema_version": 1,
    "complete": False,
    "captured_at": expected_timestamp,
    "positions": [],
}
```

The raw provider exception and position payload must not appear in the HTTP body.

### Step 4: Run the new tests and verify they fail

```bash
.venv/bin/python -m pytest tests/test_web_app.py -k 'live_position_sizes' -q
```

Expected before implementation: 404 because the route does not exist.

### Step 5: Implement the endpoint

Place the route beside the other authenticated monitor endpoints and reuse `require_monitor_capture_auth`.

Implementation requirements:

```python
@app.get("/api/runtime-incidents/live-position-sizes")
def api_runtime_incidents_live_position_sizes(request: Request):
    require_monitor_capture_auth(request)
    captured_at = app.state.now_provider()
    client = None
    try:
        client = app.state.deepcoin_client_factory()
        rows = client.list_positions()
        projection = _project_monitor_live_position_sizes(
            rows,
            captured_at=captured_at,
            limit=100,
        )
    except Exception:
        logger.warning("Monitor live-position projection is unavailable")
        projection = _incomplete_monitor_live_position_sizes(captured_at)
    finally:
        close_client = getattr(client, "close", None)
        if callable(close_client):
            try:
                close_client()
            except Exception:
                logger.warning("Monitor live-position client cleanup failed")
    return projection
```

Keep projection helpers pure and private to `web_app.py` unless an existing bounded snapshot helper already provides the exact required contract. Do not return a raw Deepcoin row.

### Step 6: Run focused tests

```bash
.venv/bin/python -m pytest \
  tests/test_web_app.py -k 'live_position_sizes or monitor_capture_health or read_only_exchange_snapshot' -q
```

Expected: all selected tests pass.

### Step 7: Commit

```bash
git diff --check
git add src/telegram_kol_research/web_app.py tests/test_web_app.py
git diff --cached --name-only
git commit -m "feat: expose bounded monitor position sizes"
```

## Task 3: Make composite monitoring consume only the fresh projection

**Files:**

- Modify: `src/telegram_kol_research/production_safety_monitor.py:482-595`
- Modify: `src/telegram_kol_research/production_safety_monitor.py:2144-2370`
- Modify: `tests/test_production_safety_monitor.py:5100-5200`
- Modify: `tests/test_production_safety_monitor.py` composite-invariant section

### Step 1: Write failing strict-reader tests

Add `read_monitor_live_position_sizes` tests for:

- exact loopback URL and fixed path;
- `trust_env=False`;
- token format;
- HTTP timeout;
- 32 KiB response bound;
- exact top-level and position-row keys;
- `schema_version == 1`;
- `complete is True`;
- aware timestamp no older than five minutes;
- no timestamp materially in the future;
- at most 100 unique position identities;
- finite non-negative decimal sizes;
- duplicate JSON keys rejected;
- incomplete, malformed, stale, duplicate, oversized, or non-200 response rejected without raw body leakage.

### Step 2: Write the failing adapter test

Construct `ProductionSafetyAdapters` with a fake HTTP response and a deliberately stale or missing cache file. Assert:

- `read_composite_invariants(now=...)` uses the HTTP projection;
- the cache file is not read;
- returned composite reasons reflect the supplied live size.

Add a failure case proving endpoint failure is reported as the `composite` adapter failure and never falls back to the stale file.

### Step 3: Write the failing pure composite test

Extend `read_composite_management_invariants` to accept:

```python
live_position_sizes={"position-1": Decimal("0.25")}
```

Use a fixture containing a confirmed `converge_partial_close` component and verified retained take profit. Assert `live_position_retained_tp_oversized` depends on the provided live size.

Add a mutual-exclusion test if the legacy `live_position_snapshot_path` argument remains: passing both a mapping and a path must raise `ValueError`.

### Step 4: Run the tests and verify failure

```bash
.venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py -k 'live_position_sizes or composite' -q
```

Expected before implementation: failures because the HTTP reader and mapping parameter do not exist.

### Step 5: Implement the bounded reader

Add a fixed default URL to `ProductionSafetyAdapters`:

```python
live_position_sizes_url: str = (
    "http://127.0.0.1:8000/api/runtime-incidents/live-position-sizes"
)
```

`read_composite_invariants` must first obtain and validate a fresh mapping using `monitor_capture_token`, then pass the mapping into `read_composite_management_invariants`. Do not use the file path in the production adapter.

The HTTP reader must stream into a bounded bytearray, use strict JSON parsing, and return `dict[str, Decimal]`. It must never log or retain the raw body.

### Step 6: Refactor the pure invariant reader minimally

Add the mapping argument without changing any invariant rule. Replace only the source of `live_position_sizes`; keep completed-batch, stalled-component, duplicate-submission, TP-size, and verified-stop semantics byte-for-byte equivalent.

Do not alter recognition, strategy, attribution, management, or execution code.

### Step 7: Run focused tests

```bash
.venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py \
  tests/test_web_app.py -k 'live_position_sizes or monitor_incident_writer' -q
```

Expected: selected monitor tests pass, with no stale-file fallback.

### Step 8: Commit

```bash
git diff --check
git add \
  src/telegram_kol_research/production_safety_monitor.py \
  tests/test_production_safety_monitor.py
git diff --cached --name-only
git commit -m "fix: read fresh positions for composite monitor"
```

## Task 4: Synchronize expected HEAD in the managed updater

**Files:**

- Modify: `deploy/telegram-kol-update`
- Modify: `tests/test_minimal_server_updater.py`
- Modify: `tests/test_server_update_scripts.py`

### Step 1: Extend the fake updater environment

In `tests/test_minimal_server_updater.py`, add fake monitor state without using the real `/etc` or systemd:

- a configurable `MONITOR_ENV_FILE` under the test temporary directory;
- fake systemctl responses for timer enabled/active and monitor oneshot inactive;
- an event log for timer stop/start/enable/disable ordering;
- a root-owned-mode analogue that can be asserted without requiring root;
- exact prior and candidate HEAD values.

The production script may expose path overrides only when an explicit test-mode guard already used by the harness is active. Production defaults must remain fixed absolute paths.

### Step 2: Write failing success-path tests

Add tests proving:

1. Monitor timer stop occurs before `telegram-kol.service` stop.
2. A stale pre-deploy pin is normalized to `previous_commit` before checkout mutation.
3. The pin advances to `EXPECTED_COMMIT` only after checkout, package installation, service start, and HTTP health succeed.
4. Every non-HEAD line remains byte-for-byte unchanged.
5. The final environment file contains exactly one expected-HEAD line.
6. Previously active/enabled timer state is restored only after candidate pin verification.
7. A completely absent monitor installation preserves existing updater behavior.

### Step 3: Write failing rollback and fail-closed tests

Cover failures at:

- malformed/symlinked/incorrect-mode monitor env;
- monitor timer stop;
- pre-deploy pin normalization;
- checkout/package/service/HTTP health after normalization;
- candidate pin advance;
- timer restoration.

Assert:

- malformed partial monitor installation fails before checkout mutation;
- post-mutation failure rolls application code back to `previous_commit`;
- expected HEAD ends at the actual rollback HEAD;
- prior timer state is restored when safe;
- no environment contents or credentials appear in captured output;
- no force push, reset, or broad file deletion is introduced.

### Step 4: Run the focused updater tests and prove failure

```bash
.venv/bin/python -m pytest \
  tests/test_minimal_server_updater.py \
  tests/test_server_update_scripts.py -q
```

Expected before implementation: new monitor lifecycle assertions fail.

### Step 5: Implement monitor lifecycle bookkeeping

Add explicit variables near the updater's other fixed paths:

```bash
MONITOR_ENV_FILE="${MONITOR_ENV_FILE:-/etc/telegram-kol-monitor.env}"
MONITOR_TIMER="telegram-kol-monitor.timer"
```

Implement narrowly scoped functions for:

- classifying monitor installation as `absent`, `complete`, or invalid partial state;
- remembering enabled/active state;
- stopping the timer and proving monitor oneshots inactive;
- validating the environment file without printing it;
- atomically replacing exactly one `TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=` line;
- verifying the written SHA;
- restoring timer state.

The atomic candidate must be created beside the destination, mode `0600`, owner root in production, and installed only after full validation. Never source or echo the environment file in this updater.

Required deployment ordering:

```text
validate monitor installation
record timer state
stop timer and monitor oneshots
sync expected HEAD -> previous_commit
existing active-write gates
stop application service
checkout/install/start/HTTP-health candidate
sync expected HEAD -> EXPECTED_COMMIT
restore prior monitor timer state
finalize deployment
```

Required rollback ordering:

```text
stop application service if needed
restore previous application commit/package/service
sync expected HEAD -> actual previous_commit
restore prior monitor timer state
report rollback result
```

Do not change the updater's active-write checks, database backup behavior, exact-SHA validation, or branch rollback protections.

### Step 6: Run focused tests and shell syntax checks

```bash
bash -n deploy/telegram-kol-update
.venv/bin/python -m pytest \
  tests/test_minimal_server_updater.py \
  tests/test_server_update_scripts.py \
  tests/test_server_monitor_installation.py -q
```

Expected: all selected tests pass.

### Step 7: Commit

```bash
git diff --check
git add \
  deploy/telegram-kol-update \
  tests/test_minimal_server_updater.py \
  tests/test_server_update_scripts.py
git diff --cached --name-only
git commit -m "fix: synchronize monitor head during deploy"
```

## Task 5: Update operator documentation

**Files:**

- Modify: `docs/runtime-incident-agent-runbook.md:446-480`
- Modify: `docs/server-deployment.md:250-315`
- Modify: `docs/production-monitor-remediation-status.md`

### Step 1: Document the live-position endpoint

Record:

- exact route and authentication;
- closed response fields;
- read-only exchange semantics;
- no production-DB write and no cache write;
- fail-closed incomplete behavior;
- 32 KiB/100-position bounds;
- prohibition on using capture POST as a health probe.

### Step 2: Document automatic expected-HEAD synchronization

Replace manual assumptions with the actual updater transaction:

- timer stopped before cutover;
- old pin normalized before mutation;
- new pin written after exact-SHA service health;
- rollback pin follows actual rollback HEAD;
- timer state restored last.

State that HEAD mismatch remains informational and does not create a new reason code.

### Step 3: Update status with local work completed so far

Record focused test commands and their actual results. Do not mark the workflow completed and do not invent production evidence.

### Step 4: Verify and commit docs

```bash
git diff --check
git add \
  docs/runtime-incident-agent-runbook.md \
  docs/server-deployment.md \
  docs/production-monitor-remediation-status.md
git diff --cached --name-only
git commit -m "docs: document monitor remediation operations"
```

## Task 6: Final local candidate verification

**Files:**

- Potentially modify only files already listed above if a test exposes a defect.
- Modify: `docs/production-monitor-remediation-status.md`

### Step 1: Review the complete candidate diff

```bash
git status --short
git diff <HANDOFF_SHA_FROM_PROMPT>...HEAD -- \
  src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/web_app.py \
  deploy/telegram-kol-update \
  tests/test_production_safety_monitor.py \
  tests/test_web_app.py \
  tests/test_minimal_server_updater.py \
  tests/test_server_update_scripts.py \
  docs/runtime-incident-agent-runbook.md \
  docs/server-deployment.md \
  docs/production-monitor-remediation-status.md
```

Verify that no recognition, strategy, attribution, exchange-write, model, schema, migration, or unrelated DeepSeek file changed.

### Step 2: Run all focused groups once more

```bash
.venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py \
  tests/test_web_app.py -k 'monitor or live_position_sizes' \
  tests/test_minimal_server_updater.py \
  tests/test_server_update_scripts.py \
  tests/test_server_monitor_installation.py -q
```

Expected: zero failures.

### Step 3: Run the full suite exactly once on the final code candidate

```bash
.venv/bin/python -m pytest -q
```

Record the exact passed/skipped/failed/warning totals and duration. Do not rerun the full suite unless production code changes afterward. Documentation-only evidence updates do not create a new code candidate.

### Step 4: Verify repository hygiene

```bash
git diff --check
git status --short --branch
git log --oneline --decorate -8
```

Expected: clean worktree after all intended commits.

### Step 5: Record the candidate

Update status with:

```yaml
candidate_commit: <full 40-character SHA>
focused_tests: <exact summary>
full_suite: <exact summary and duration>
outstanding: exact-SHA push/deploy authorization required
```

Commit only the status file:

```bash
git add docs/production-monitor-remediation-status.md
git diff --cached --name-only
git commit -m "docs: record monitor remediation candidate"
```

This documentation commit becomes the candidate only if it changes no production code. Report both the production-code commit and final documentation commit.

### Step 6: Stop for explicit integration authorization

Do not push or deploy based only on this plan. Return the exact candidate SHA, remote SHA, commit list, tests, and changed paths to the user. Wait for explicit authorization to push that exact history and deploy the exact resulting SHA.

## Task 7: Push and pre-deploy gate after explicit authorization

**Files:** none unless evidence is later recorded in the status document.

### Step 1: Revalidate local and remote history

```bash
git status --short --branch
git fetch origin codex/deepcoin-auto-trading-v1
git rev-parse HEAD
git rev-parse origin/codex/deepcoin-auto-trading-v1
git merge-base --is-ancestor origin/codex/deepcoin-auto-trading-v1 HEAD
git log --oneline origin/codex/deepcoin-auto-trading-v1..HEAD
```

Stop on divergence, unexpected commits, a dirty tree, or a changed candidate. Never force push.

### Step 2: Push the reviewed history

Only after the user authorizes the displayed commit chain:

```bash
git push origin HEAD:codex/deepcoin-auto-trading-v1
git rev-parse HEAD
git rev-parse origin/codex/deepcoin-auto-trading-v1
```

Expected: exact equality.

### Step 3: Capture the production pre-deploy checkpoint

Use read-only SSH checks to record, without secrets:

- production HEAD and branch;
- clean tracked worktree;
- `telegram-kol.service` active state and start timestamp;
- monitor timer enabled/active state and latest result;
- installed expected HEAD;
- `message_lock_mode=global` and `message_pipeline_mode=queue`;
- `/api/runtime/loop-health`;
- `/api/runtime/message-pipeline-parity` baseline watermark and counts;
- `active_write_count=0` using the existing deployment check;
- no planned/executing/reconciling management batch that makes the deployment window unsafe.

If an external response is incomplete, retry once for a stated reason. If still incomplete, stop and keep `in_progress`.

Do not call a capture POST as a probe and do not query or mutate exchange orders.

## Task 8: Exact-SHA deployment and immediate verification

**Files:**

- Modify after evidence only: `docs/production-monitor-remediation-status.md`

### Step 1: Deploy through the reviewed updater

From the exact candidate checkout:

```bash
EXPECTED_COMMIT=<FULL_AUTHORIZED_SHA> \
BRANCH=codex/deepcoin-auto-trading-v1 \
bash scripts/server_git_update.sh
```

Capture the exit code without piping away failures. The updater performs the one allowed application restart and owns monitor timer stop/restore.

### Step 2: Independently verify exact identity

Read back:

```text
production checkout HEAD == authorized SHA
origin deployment branch == authorized SHA
installed monitor expected HEAD == authorized SHA
telegram-kol.service == active
telegram-kol-monitor.timer == active if it was active before deployment
```

Any mismatch is a hard stop. Do not repair by blindly rewriting a pin or checking out another commit.

### Step 3: Probe authenticated read-only endpoints

Source the token on the server without printing it and GET:

```text
/api/runtime-incidents/monitor-capture-health
/api/runtime-incidents/message-operation-coverage
/api/runtime-incidents/live-position-sizes
```

Expected:

- HTTP 200 for all three;
- health `available=true`;
- coverage exact schema, even if the feature is disabled;
- live-position `complete=true`, aware current timestamp, bounded unique rows.

One reasoned retry is allowed for an incomplete live-position response. A second incomplete response is a fail-closed stop.

### Step 4: Run one non-notifying diagnostic cycle

Run the installed diagnostic unit, not the normal notifying service:

```bash
systemctl start telegram-kol-monitor-diagnostic.service
journalctl -u telegram-kol-monitor-diagnostic.service -n 80 --no-pager -o short-iso
```

The diagnostic may still report a separately existing `audit_abnormal`, but acceptance requires:

- no `adapter_failure` caused by `composite`;
- no `malformed_snapshot` from adapter-name normalization;
- no `Monitor incident capture writer is unavailable`;
- no HTTP 422 capture request;
- no secret or raw position payload in logs.

Do not manually start `telegram-kol-monitor.service`, because it includes `--notify`.

### Step 5: Confirm runtime semantics did not move

Verify settings still report:

```text
message_lock_mode=global
message_pipeline_mode=queue
```

Do not change either setting.

## Task 9: L2 observation and completion

**Files:**

- Modify: `docs/production-monitor-remediation-status.md`

### Step 1: Start the bounded window

Record UTC start time, production SHA, maximum raw-message ID, service PID/start time, loop-health baseline, and message-pipeline parity baseline in a root-owned server evidence file. Keep detailed JSON and logs on the server, not in the status document.

### Step 2: Observe quietly

Observe 30 continuous minutes and at least five real messages, trying to cover two chats. Use the existing timer and bounded read-only checkpoints; do not continuously poll or emit repetitive updates.

If five messages do not arrive within 30 minutes:

- stop instead of extending indefinitely;
- record the number of messages and chats actually observed;
- leave `phase_status: in_progress`;
- set `outstanding` to limited traffic;
- do not claim completion.

### Step 3: Capture the observation-end checkpoint

Verify:

- service PID/start timestamp unchanged after the managed restart;
- monitor timer active and at least one scheduled cycle ran;
- no new `adapter_failure` from composite snapshot staleness;
- no new capture-writer unavailable warning or capture HTTP 422;
- actual HEAD still equals installed expected HEAD;
- pipeline parity is not truncated and has `missing_job_count=0`, `orphan_job_count=0`, `stuck_pending_count=0`;
- all observed queue jobs are terminal as expected, with no duplicate raw-message job identity;
- loop health shows no new stall attributable to the endpoint or monitor;
- `message_lock_mode=global` and `message_pipeline_mode=queue` remain unchanged.

Try to cover two chats, but do not fabricate coverage if traffic comes from only one.

Because the change has no exchange-write path, do not run a direct order/fill/trigger-history audit solely for this remediation.

### Step 4: Record evidence concisely

Update every field in the status evidence block with exact facts and the server evidence path. Do not paste raw position rows, message text, credentials, or long logs.

If any required gate failed or remains unknown, keep `phase_status: in_progress` and record `outstanding` precisely.

### Step 5: Complete only when all gates pass

When every local and production acceptance criterion is proven:

```yaml
phase_status: completed
claimed_by: null
outstanding: null
```

Commit only the status/documentation changes:

```bash
git diff --check
git add docs/production-monitor-remediation-status.md
git diff --cached --name-only
git commit -m "docs: record production monitor remediation result"
```

Push this final evidence commit only after verifying the remote branch is still an ancestor and no unrelated commit will be included.

## Rollback procedure

Rollback is required if the new endpoint is incomplete twice, the writer still returns 422, expected HEAD cannot be synchronized, timer state cannot be restored, queue parity regresses, or loop health materially regresses due to this change.

Use the managed updater with the exact reviewed previous commit. Do not use `git reset --hard`, force push, or manual file replacement.

After rollback, verify:

```text
production HEAD == previous reviewed commit
monitor expected HEAD == actual rollback HEAD
telegram-kol.service == active
monitor timer restored to its prior state
message_lock_mode == global
message_pipeline_mode == queue
```

Keep status `in_progress`, record the rollback SHA and evidence path, and return control to the user. No database or exchange rollback action is authorized or required.

## Final handoff contents

The executing session's final report must include:

- exact production-code candidate SHA and final documentation SHA;
- local/remote/production SHA equality or any remaining difference;
- focused and full-suite totals;
- deployment and restart timestamp;
- expected/actual HEAD result;
- authenticated endpoint results;
- monitor diagnostic and scheduled-cycle result;
- 30-minute window, message count, chat count, parity, duplicates, and loop health;
- server evidence path;
- rollback status;
- explicit statement that recognition, strategy, position ownership, exchange-write semantics, `message_lock_mode=global`, and `message_pipeline_mode=queue` remained unchanged.
