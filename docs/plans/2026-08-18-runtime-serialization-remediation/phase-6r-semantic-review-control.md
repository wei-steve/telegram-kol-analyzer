# Phase 6R Semantic Review Control Implementation Plan

> **For Codex:** REQUIRED SUB-SKILLS: use `executing-plans` and
> `test-driven-development` to implement this plan task by task. Do not start
> Claude, subagents, background agents, or parallel implementation sessions.

**Goal:** Make DeepSeek semantic disagreement review explicitly controllable
and disabled by default, terminalize existing review backlog safely, and
preserve every authoritative recognition and trading semantic.

**Architecture:** Add a strict persisted boolean setting. Final authoritative
automation writes either the existing enabled-review `pending` state or a
compatibility terminal `completed/review_disabled` state. The semantic worker
loads no provider configuration and claims no work while disabled. A separate
read-only-by-default, plan-fingerprinted CLI performs the rehearsed historical
L3 transition and targeted rollback.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, SQLite WAL, Typer, Jinja2,
vanilla JavaScript, pytest, the existing exact-SHA gated updater, and SQLite
online backups.

---

## Scope and hard invariants

Read this file completely before implementation. Do not read another phase
file. The approved design is
`docs/plans/2026-08-22-phase-6r-semantic-review-control-design.md`.

This phase controls only `semantic_disagreement_review`. It must not disable or
change context resolution or another DeepSeek consumer.

It must preserve:

- MiMo as the sole authoritative recognition model;
- recognition payloads and result selection;
- contextual strategy resolution;
- strategy ownership and position attribution;
- automation ordering and outcomes;
- every exchange-write argument, identity, ordering, and result;
- `message_lock_mode=global`;
- `message_pipeline_mode=queue`;
- `worker_command_mode=shadow`;
- the monolith topology;
- Phase 6A Candidate A
  `f257a93121ba1d547955f0b4dd5a270dd347904d` and its Task 12 checkpoint.

It must not process, send, delete, mark, or otherwise absorb the 2463 pending
position-attribution or 331 pending position-protection notifications. It must
not advance Phase 6A or Phase 6.

This phase is L3 because it plans a production data mutation. It adds no
database column or table. Missing external evidence is unknown; one reasoned
retry is allowed, then fail closed.

## Verification discipline

For every production-code edit:

1. add the named focused failing test first;
2. run it and record the expected failure;
3. make the minimum production change;
4. rerun the affected focused slice;
5. stage exact paths, inspect `git diff --cached --name-only`, and commit.

Do not run the full suite during normal task development. Run it exactly once
on the assembled final production candidate. If production code changes after
that run, run affected focused tests and exactly one new final full suite.
Documentation-only changes do not invalidate the candidate.

Never use `git add -A`, `git pull`, force push, reset, clean, or stash. Send no
extra Telegram notification during work. Use only the required stop
notification immediately before returning control.

## Task 0: Exclusive preflight and implementation claim

**Files:**

- Modify: `docs/runtime-serialization-remediation-status.md`

### Step 1: Verify the canonical pointer and checkout

Run:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/codex/deepcoin-auto-trading-v1
git diff --check
rg -n "^(current_phase|phase_name|phase_status|claimed_by|current_phase_file):" \
  docs/runtime-serialization-remediation-status.md
find "$(git rev-parse --git-dir)" -maxdepth 1 \
  \( -name '*.lock' -o -name 'index.lock' \) -print
lsof +D "$PWD" 2>/dev/null
```

Expected: clean tree; exact documented planning HEAD; current phase `6r`, name
`semantic-review-control`, status `planned`, `claimed_by=null`, and this file as
`current_phase_file`; no Git lock or another writer. Any contradiction or
concurrent writer is a hard stop.

### Step 2: Claim before touching implementation files

Set `phase_status: claimed` and `claimed_by` to the current session id. Stage
and commit only the status file:

```bash
git add docs/runtime-serialization-remediation-status.md
git diff --cached --name-only
git commit -m "chore: claim runtime serialization phase 6r implementation"
```

Record the exact claim SHA later in the status evidence.

## Task 1: Freeze current authority and review boundaries

**Files:**

- Modify: `tests/test_recognition_decisions.py`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_semantic_disagreement_worker.py`

### Step 1: Add characterization tests before changing behavior

Add focused tests proving:

- MiMo authority and automation finish before semantic review is claimable;
- DeepSeek never changes `automation_status` or `automation_reason`;
- a review notification occurs only after a completed critical review;
- authoritative failures remain terminal and never become review work;
- physical `comparison_status=completed` is terminal downstream.

Use injected reviewer/notifier fakes only. Do not call a model or exchange.

### Step 2: Run and commit the characterization slice

```bash
./.venv/bin/python -m pytest -q \
  tests/test_recognition_decisions.py \
  tests/test_authoritative_recognition.py \
    -k 'semantic_review or automation_outcome or authoritative_failed' \
  tests/test_semantic_disagreement_worker.py \
    -k 'notification or authoritative or claim'
git add tests/test_recognition_decisions.py \
  tests/test_authoritative_recognition.py \
  tests/test_semantic_disagreement_worker.py
git diff --cached --name-only
git commit -m "test: freeze semantic review authority boundary"
```

Expected: tests pass before production changes. If an invariant is false, stop
and update the design rather than encoding new semantics.

## Task 2: Add the default-off persisted setting and UI control

**Files:**

- Modify: `tests/test_trading_settings.py`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_web_assets_smoke.py`
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`

### Step 1: RED — setting and API tests

Require:

- `TradingSettings().semantic_review_enabled is False`;
- a legacy payload without the key loads `False`;
- booleans round-trip through storage and the settings API;
- unrelated partial updates preserve the value;
- non-booleans fail `422` without changing storage;
- global/queue/shadow remain unchanged.

```bash
./.venv/bin/python -m pytest -q \
  tests/test_trading_settings.py -k semantic_review_enabled \
  tests/test_web_app.py -k semantic_review_enabled
```

Expected RED: field absent.

### Step 2: Implement the minimum setting and rerun

Add:

```python
semantic_review_enabled: bool = False
```

Parse it with the existing strict `_boolean_setting` helper. Do not touch AI
provider configuration. Rerun Step 1; expected GREEN.

### Step 3: RED — first-party form tests

Require a checkbox named `semantic_review_enabled`, labelled
`开启 DeepSeek 辅助复核`, unchecked by default, that sends an exact boolean while
preserving every existing form field.

```bash
./.venv/bin/python -m pytest -q tests/test_web_assets_smoke.py \
  -k 'trading_settings and semantic_review'
```

Expected RED.

### Step 4: Implement the checkbox, rerun, and commit

Add one existing-style toggle row and payload value:

```javascript
semantic_review_enabled: Boolean(
  form.querySelector('[name="semantic_review_enabled"]')?.checked,
),
```

```bash
./.venv/bin/python -m pytest -q \
  tests/test_trading_settings.py -k 'semantic_review_enabled or message_lock_mode or message_pipeline_mode or worker_command_mode' \
  tests/test_web_app.py -k 'semantic_review_enabled or trading_settings' \
  tests/test_web_assets_smoke.py -k 'trading_settings and semantic_review'
git add tests/test_trading_settings.py tests/test_web_app.py \
  tests/test_web_assets_smoke.py \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js
git diff --cached --name-only
git commit -m "feat: add default-off semantic review setting"
```

## Task 3: Make disabled review a compatible terminal policy

**Files:**

- Modify: `tests/test_recognition_decisions.py`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `src/telegram_kol_research/recognition_decisions.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`

### Step 1: RED — state transition tests

Require:

- disabled finalization atomically writes `completed/review_disabled` with the
  unchanged automation outcome;
- enabled finalization preserves current pending behavior;
- authoritative failure is never relabelled;
- generation-token stale-write protection is unchanged;
- unchanged re-recognition of a disabled row remains disabled;
- a changed authoritative candidate follows the current switch;
- no reviewer/notifier is involved.

The explicit API is:

```python
finalize_authoritative_automation_outcome(
    session_factory,
    raw_message_id=raw_message_id,
    authoritative_generation=generation,
    automation_status=status,
    automation_reason=reason,
    semantic_review_enabled=False,
)
```

```bash
./.venv/bin/python -m pytest -q \
  tests/test_recognition_decisions.py -k 'review_disabled or finalize_authoritative' \
  tests/test_authoritative_recognition.py -k 'review_disabled or semantic_review_status'
```

Expected RED.

### Step 2: Implement minimum state transition

Keep the existing generation CAS. When disabled, set exactly:

```python
comparison_status = "completed"
agreement_status = "review_disabled"
comparison_next_attempt_at = None
comparison_started_at = None
comparison_claim_token = None
```

Load settings immediately before authoritative finalization and pass only the
boolean. Do not move or repeat automation.

### Step 3: Rerun focused tests and commit

```bash
./.venv/bin/python -m pytest -q \
  tests/test_recognition_decisions.py \
  tests/test_authoritative_recognition.py \
    -k 'semantic_review or automation_outcome or authoritative_failed or generation'
git add tests/test_recognition_decisions.py \
  tests/test_authoritative_recognition.py \
  src/telegram_kol_research/recognition_decisions.py \
  src/telegram_kol_research/authoritative_recognition.py
git diff --cached --name-only
git commit -m "feat: terminalize disabled semantic reviews"
```

## Task 4: Gate worker provider load, claims, and notifications

**Files:**

- Modify: `tests/test_semantic_disagreement_worker.py`
- Modify: `tests/test_recognition_decisions.py`
- Modify: `src/telegram_kol_research/semantic_disagreement_review.py`
- Modify: `src/telegram_kol_research/recognition_decisions.py`

### Step 1: RED — disabled loop tests

Inject counters and prove disabled ticks perform zero provider-config loads,
claims, reviewer calls, retries/incidents, and notifier calls, then sleep
without busy looping. Prove enabled behavior is unchanged.

```bash
./.venv/bin/python -m pytest -q tests/test_semantic_disagreement_worker.py \
  -k 'disabled or enabled_policy or provider_config'
```

Expected RED: provider configuration loads unconditionally.

### Step 2: Implement the pre-load gate

At each loop iteration load `TradingSettings` through `asyncio.to_thread`.
When disabled, skip AI config and `run_semantic_review_once`. Keep the lifespan
task alive so runtime re-enable needs no restart.

### Step 3: RED — post-claim and in-flight races

Require:

- disabled after claim but before provider terminalizes through the exact token;
- wrong/stale tokens cannot terminalize;
- disabling during an already-issued reviewer suppresses critical notification;
- completed audit is not rewritten as agreement;
- cancellation remains unchanged.

```bash
./.venv/bin/python -m pytest -q \
  tests/test_semantic_disagreement_worker.py -k 'disable_after_claim or in_flight or notification' \
  tests/test_recognition_decisions.py -k disable_claimed
```

Expected RED.

### Step 4: Implement guarded settlement and rechecks

Add one claim-token CAS helper for a `running` row. Re-read the setting after
claim and before provider invocation, and again before reserving/delivering a
critical notification. Do not attempt to cancel a blocking call already sent
through `asyncio.to_thread`.

### Step 5: Rerun and commit

```bash
./.venv/bin/python -m pytest -q \
  tests/test_semantic_disagreement_worker.py \
  tests/test_recognition_decisions.py -k 'semantic or disable_claimed' \
  tests/test_runtime_event_loop_blocking_census.py
git add tests/test_semantic_disagreement_worker.py \
  tests/test_recognition_decisions.py \
  src/telegram_kol_research/semantic_disagreement_review.py \
  src/telegram_kol_research/recognition_decisions.py
git diff --cached --name-only
git commit -m "feat: gate semantic review worker by policy"
```

Do not add a blocking-census allowlist entry.

## Task 5: Project disabled truth and prove downstream terminality

**Files:**

- Modify: `tests/test_web_queries_messages.py`
- Modify: `tests/test_web_page_render.py`
- Modify: `tests/test_message_operation_contracts.py`
- Modify: `tests/test_message_operation_supervisor.py`
- Modify: `src/telegram_kol_research/web_queries.py`

### Step 1: RED — projection tests

Require physical `completed/review_disabled` to project:

```python
{
    "status": "review_disabled",
    "severity": "disabled",
    "label": "辅助复核已关闭",
    "reason": None,
    "conflict_types": [],
}
```

Require no DeepSeek model assertion, critical alert role, or implication of
agreement, failure, or waiting.

```bash
./.venv/bin/python -m pytest -q \
  tests/test_web_queries_messages.py -k review_disabled \
  tests/test_web_page_render.py -k review_disabled
```

Expected RED.

### Step 2: Implement projection and terminal tests

Handle `agreement_status == "review_disabled"` before generic completed and
agreement branches. Add contract/supervisor tests proving physical completed
does not block and performs no model call.

```bash
./.venv/bin/python -m pytest -q \
  tests/test_web_queries_messages.py -k 'semantic_review or review_disabled' \
  tests/test_web_page_render.py -k 'semantic_review or review_disabled' \
  tests/test_message_operation_contracts.py -k 'semantic or review_disabled' \
  tests/test_message_operation_supervisor.py -k 'semantic or review_disabled'
git add tests/test_web_queries_messages.py tests/test_web_page_render.py \
  tests/test_message_operation_contracts.py \
  tests/test_message_operation_supervisor.py \
  src/telegram_kol_research/web_queries.py
git diff --cached --name-only
git commit -m "feat: expose disabled semantic review state"
```

## Task 6: Build the historical transition and rollback engine

**Files:**

- Create: `tests/test_semantic_review_control.py`
- Create: `src/telegram_kol_research/semantic_review_control.py`

### Step 1: RED — canonical dry-run plan

Build completed, pending, failed, running, authoritative-failed, and disabled
fixtures. Require a plan that:

- targets only pending and failed;
- orders by raw-message id;
- includes exact status, timestamps, claim/retry fields, and row fingerprints;
- includes counts, cutoff, running count, and deterministic SHA-256;
- performs no database write;
- reports zero provider, notification, and exchange writes;
- refuses a missing database rather than creating it.

```bash
./.venv/bin/python -m pytest -q tests/test_semantic_review_control.py \
  -k 'plan or dry_run or missing_database'
```

Expected RED: module missing.

### Step 2: Implement immutable plan building

Use `create_existing_session_factory`, canonical sorted compact JSON, SHA-256,
and bounded fields. Suggested public API:

```python
@dataclass(frozen=True, slots=True)
class SemanticReviewDisablePlan: ...

def build_semantic_review_disable_plan(
    session_factory,
    *,
    cutoff: datetime,
) -> SemanticReviewDisablePlan: ...
```

### Step 3: RED — guarded apply

Require apply to:

- refuse when review is enabled or any row is running;
- require the exact expected plan SHA;
- start one `BEGIN IMMEDIATE` transaction;
- re-read the target set and every CAS field;
- update exactly the planned pending/failed rows;
- preserve authority, automation, errors, attempts, payloads, prompt versions,
  differences, and notification audit;
- clear only retry/claim scheduling fields;
- use one apply timestamp;
- roll back all targets on one drift;
- return a deterministic post-apply fingerprint;
- become a zero-target operation after a fresh post-success plan.

```bash
./.venv/bin/python -m pytest -q tests/test_semantic_review_control.py \
  -k 'apply or drift or running or enabled or idempotent'
```

Expected RED: apply missing.

### Step 4: Implement minimum CAS apply

Do not import or invoke provider, notifier, runtime incident, prompt, strategy,
or exchange modules. Apply only:

```python
comparison_status = "completed"
agreement_status = "review_disabled"
comparison_next_attempt_at = None
comparison_started_at = None
comparison_claim_token = None
updated_at = apply_timestamp
```

### Step 5: RED — targeted rollback

From the preserved preimage, require rollback to target only exact applied
rows, require every current post-apply fingerprint, restore only fields changed
by apply, refuse drift atomically, never restore the whole DB, and report zero
external writes.

```bash
./.venv/bin/python -m pytest -q tests/test_semantic_review_control.py \
  -k rollback
```

Expected RED.

### Step 6: Implement rollback, run the file, and commit

```bash
./.venv/bin/python -m pytest -q tests/test_semantic_review_control.py
git add tests/test_semantic_review_control.py \
  src/telegram_kol_research/semantic_review_control.py
git diff --cached --name-only
git commit -m "feat: plan semantic review terminalization safely"
```

## Task 7: Add read-only-default CLI surfaces

**Files:**

- Create: `tests/test_semantic_review_control_cli.py`
- Modify: `src/telegram_kol_research/cli.py`

### Step 1: RED — terminalization CLI

Test this dry-run-default interface:

```bash
telegram-kol semantic-review-terminalize \
  --database-path data/research.db \
  --plan-output /safe/path/plan.json
```

Apply requires both `--apply` and `--expected-plan-sha <64-hex>`. Require JSON
output containing mode, SHA, counts, changed count, quick check, and all three
zero-external-write counters. Cover missing DB, invalid SHA, enabled setting,
running rows, drift, and write failure.

```bash
./.venv/bin/python -m pytest -q tests/test_semantic_review_control_cli.py \
  -k terminalize
```

Expected RED: command missing.

### Step 2: Implement the thin command

Open only an existing database and delegate to Task 6. Do not bootstrap, load
AI configuration, or create a DB. Dry-run writes only the explicitly requested
evidence JSON.

### Step 3: RED — rollback CLI

Test a second dry-run-default interface:

```bash
telegram-kol semantic-review-terminalize-rollback \
  --database-path data/research.db \
  --preimage-plan /safe/path/plan.json \
  --plan-output /safe/path/rollback-plan.json
```

Apply requires `--apply --expected-plan-sha <64-hex>`. Require drift refusal
and zero external-write counters.

### Step 4: Implement, rerun, and commit

```bash
./.venv/bin/python -m pytest -q tests/test_semantic_review_control_cli.py
git add tests/test_semantic_review_control_cli.py \
  src/telegram_kol_research/cli.py
git diff --cached --name-only
git commit -m "feat: add guarded semantic review control CLI"
```

## Task 8: Assemble the final local candidate

**Files:** all production and test paths changed in Tasks 1-7.

### Step 1: Run focused acceptance

```bash
./.venv/bin/python -m pytest -q \
  tests/test_trading_settings.py \
  tests/test_recognition_decisions.py \
  tests/test_authoritative_recognition.py -k 'semantic_review or automation_outcome or authoritative_failed or generation' \
  tests/test_semantic_disagreement_worker.py \
  tests/test_semantic_disagreement_review.py \
  tests/test_web_queries_messages.py -k 'semantic_review or review_disabled' \
  tests/test_web_page_render.py -k 'semantic_review or review_disabled' \
  tests/test_message_operation_contracts.py -k 'semantic or review_disabled' \
  tests/test_message_operation_supervisor.py -k 'semantic or review_disabled' \
  tests/test_semantic_review_control.py \
  tests/test_semantic_review_control_cli.py \
  tests/test_web_app.py -k 'semantic_review or trading_settings or lifespan' \
  tests/test_web_assets_smoke.py -k 'trading_settings or semantic_review' \
  tests/test_runtime_event_loop_blocking_census.py
```

Expected: all focused tests pass without provider or exchange access.

### Step 2: Run the one final full suite

```bash
./.venv/bin/python -m pytest -q
git diff --check
```

Expected: zero failures and no unexpected XPASS. Record exact counts and
runtime. Any later production-code change invalidates this candidate.

### Step 3: Record the exact candidate or stop locally

Commit any remaining test-only assembly change with explicit paths. Do not
create an empty commit. If production integration is not explicitly authorized,
key rotation is unconfirmed, or a safe window cannot be proven, stop here with
Phase 6R `in_progress`; do not push or deploy.

## Task 9: L3 apply and rollback rehearsal on production copies

**Files:** evidence/status documentation only after rehearsal.

### Step 1: Fast-forward push only when authorized

Verify the remote deploy branch is an ancestor of the candidate, then push
without force. Never use the poisoned local deploy branch.

### Step 2: Create immutable and rehearsal copies

Use SQLite online backup. Store evidence under:

```text
/opt/telegram-kol-analyzer/data/backups/phase6r-<candidate>-<utc>/
```

Create immutable backup, apply rehearsal copy, repeated-apply copy, and
rollback copy. Never rehearse against the live DB.

### Step 3: Capture before evidence

Record exact candidate, DB size, quick check, schema, counts for every table,
and targeted hashes for recognition decisions plus critical raw-message,
strategy/lifecycle, trade-signal, mutation-intent, execution, management, and
protection tables. Record exact review counts, target ids, running count, dry
plan, and plan SHA.

### Step 4: Rehearse apply, repeat, and rollback

On the apply copy:

- persist `semantic_review_enabled=false` through the normal settings store;
- plan, apply with exact SHA, and replan;
- prove repeated apply has zero targets;
- run quick check after every step.

On the rollback copy:

- build rollback plan from the preimage;
- apply with exact rollback SHA;
- prove target rows match preimage;
- prove all unrelated counts and targeted hashes match backup.

Expected: only the global setting row and exact planned decisions change on
apply. Any mismatch, drift, running row, incomplete evidence, or failed quick
check is a hard stop.

## Task 10: Exact deploy, guarded apply, and observation

Proceed only with explicit deployment authorization and owner confirmation that
the previously exposed DeepSeek, GLM, and MiMo keys were rotated.

### Step 1: Prove the quiet gate

At one checkpoint verify exact production SHA, service health, global/queue/
shadow modes, monolith topology, `active_write_count=0`, no active management
mutation, message/worker-command backlogs, semantic `running=0` after at most
one bounded recheck, WAL, quick check, backup path, and non-secret key-rotation
confirmation. Incomplete evidence after one retry fails closed.

### Step 2: Deploy exact SHA through the gated updater

Use only `scripts/server_git_update.ps1` or the existing gated shell wrapper
with exact `EXPECTED_COMMIT`. Never pull manually. Verify exact HEAD, active
service, `semantic_review_enabled=false`, unchanged modes, and topology.

### Step 3: Prove semantic provider work stopped

Before historical apply, prove no running review and no new
`semantic_disagreement_review` provider call, 402, retry, or notification.
Report other DeepSeek consumers separately. Any continuing semantic activity
stops the phase before data mutation.

### Step 4: Apply the fresh exact live plan

Create a new dry plan, compare invariant shape with rehearsal, then apply only
with its exact SHA. Capture JSON and exit code without a pipe. Immediately
verify quick check, zero targeted pending/failed/running, preserved reviewed
rows and audit fields, unchanged unrelated tables/hashes, and zero external
write counters.

### Step 5: Observe the L2 runtime behavior on the L3 candidate

Run one quiet server-side monitor for 30 continuous minutes and at least five
real Telegram messages, trying to cover two chats. Stop at 30 minutes if five
messages do not arrive and record limited traffic.

Record exact SHA/window, traffic, MiMo outcomes, automation ordering, new review
states, semantic provider/402/retry/notification deltas, other DeepSeek errors
separately, backlogs, duplicates, SQLite_BUSY, loop stalls, unchanged modes and
topology, and direct exchange history for naturally occurring write-capable
actions. Never manufacture a message, strategy, position, or order.

## Task 11: Completion, rollback decision, and handoff

**Files:**

- Modify: `docs/runtime-serialization-remediation-status.md`

### Step 1: Evaluate and record evidence

Complete only if all design criteria pass. Record planning/implementation claim,
design, plan, candidate, pushed, and deployed SHAs; focused/full-suite results;
non-secret key-rotation confirmation; backup/rehearsal/apply/rollback paths and
SHAs; quick checks, target counts and hash deltas; review/provider/402/retry/
notification evidence; traffic, modes, topology, SQLite_BUSY, duplicates,
exchange evidence, rollback state, and incomplete items.

### Step 2: Restore or retain the canonical pointer

If complete, mark Phase 6R completed and restore Phase 6A as current,
`in_progress`, `claimed_by=null`, with its original file pointer and exact Task
12 plus 2463/331 blocker. Do not resume Phase 6A in the same turn.

If incomplete, keep Phase 6R current and `in_progress`, release the claim when
stopping, and record exact safe live state and outstanding gate.

### Step 3: Commit status only and stop

```bash
git add docs/runtime-serialization-remediation-status.md
git diff --cached --name-only
git commit -m "docs: record phase 6r semantic review control result"
```

Push the status commit only when implementation authorization includes
integration. Send the single required stop notification and return control.
