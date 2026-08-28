# Action-Scoped Deployment Gates Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the implicit universal deployment gate with a fail-closed action planner so local work, push, candidate staging, runtime activation, and trading writes each require only the evidence relevant to that action.

**Architecture:** Introduce a pure, deterministic planner that accepts an explicit action, risk level, affected runtime components, and declared impact flags. It rejects incomplete or inconsistent declarations and emits the exact gates for that action. The first batch is read-only and local: planner, CLI, tests, and policy documentation. Later batches connect the planner to an immutable server-side release staging path and then to activation; the existing production updater remains conservative until those integrations are independently reviewed.

**Tech Stack:** Python 3.11, dataclasses/enums, argparse/json, pytest, existing shell deployment helpers.

---

## Non-negotiable boundaries

- Local edit/test never requires production DB, Deepcoin, systemd, SSH, or deployed-runtime evidence.
- Push requires reviewable Git evidence only; it must not imply staging, activation, restart, or trading enablement.
- Stage writes only an immutable inactive candidate artifact. It must not touch the active checkout, settings, database, services, Telegram, or Deepcoin.
- Activate proves the staged artifact identity, exact affected-service scope, rollback readiness, and only the live invariants relevant to those services.
- Trading actions are separate from activation and always require fresh exchange/runtime evidence, explicit authorization, fail-closed unknown handling, and single-action confirmation.
- Worker or ingest authority changes retain active-write quiescence and protection-authority proof. Web- or monitor-only activation must not inherit those unrelated gates.
- An undeclared action, risk, component, or impact is an error, never a permissive default.
- This plan does not authorize push, SSH, deployment, freeze, restart, settings/DB mutation, or exchange writes.

## Batch 1 — Local action planner

### Task 1: Define the fail-closed action model

**Files:**
- Create: `src/telegram_kol_research/deployment_action_plan.py`
- Test: `tests/test_deployment_action_plan.py`

**Step 1: Write failing model tests**

Cover:

- the five actions: `local`, `push`, `stage`, `activate`, `trading`;
- the four repository risk levels: `L0` through `L3`;
- closed component names: `web`, `monitor`, `ingest`, `worker`;
- rejection of unknown fields and inconsistent declarations;
- rejection when runtime activation has no declared component scope;
- escalation to `L3` when schema/data/exchange semantics are declared.

**Step 2: Run the focused test and prove RED**

Run: `pytest -q tests/test_deployment_action_plan.py`

Expected: FAIL because the planner module does not exist.

**Step 3: Implement the smallest strict model**

Use immutable dataclasses and enums. Parsing must require every safety-relevant field and reject unknown JSON keys. Do not infer safety from settings values or a checkout HEAD.

**Step 4: Run the focused test and prove GREEN**

Run: `pytest -q tests/test_deployment_action_plan.py`

Expected: PASS.

### Task 2: Emit exact gates per action

**Files:**
- Modify: `src/telegram_kol_research/deployment_action_plan.py`
- Modify: `tests/test_deployment_action_plan.py`

**Step 1: Write failing gate-matrix tests**

Assert:

- `local` has workspace/test gates and no production, service, DB, or exchange gate;
- `push` has clean-tree/review/exact-commit gates and no production gate;
- `stage` has immutable-candidate and inactive-destination gates and explicitly prohibits active mutations;
- `activate` for `web` or `monitor` does not request Deepcoin/protection/active-write evidence;
- `activate` for `ingest` or `worker` requires runtime identity, active-write quiescence, protection authority, rollback readiness, and affected-service health;
- `L3` activation with schema/data mutation additionally requires scoped backup/integrity/rollback evidence, while exchange-semantics-only changes do not inherit database gates;
- `trading` requires fresh runtime/exchange evidence, no relevant unknown, explicit authorization, one target, and one confirmation token.

**Step 2: Run the focused test and prove RED**

Run: `pytest -q tests/test_deployment_action_plan.py`

Expected: FAIL on missing gate output.

**Step 3: Implement deterministic gate output**

Gate identifiers are stable machine-readable strings. Each gate records whether it is required or prohibited and why. Output order is deterministic for review and logging.

**Step 4: Run the focused test and prove GREEN**

Run: `pytest -q tests/test_deployment_action_plan.py`

Expected: PASS.

### Task 3: Add a read-only CLI

**Files:**
- Modify: `src/telegram_kol_research/deployment_action_plan.py`
- Modify: `tests/test_deployment_action_plan.py`

**Step 1: Write failing CLI tests**

Test JSON input/output, deterministic ordering, non-zero exit on incomplete input, and absence of environment values, credentials, order details, or arbitrary input echoing in output.

**Step 2: Run the focused test and prove RED**

Run: `pytest -q tests/test_deployment_action_plan.py`

Expected: FAIL on missing CLI.

**Step 3: Implement CLI**

Invocation:

```bash
python -m telegram_kol_research.deployment_action_plan --manifest path/to/manifest.json --format json
```

The CLI only plans and validates. It never runs Git, SSH, systemd, SQLite, Telegram, or Deepcoin commands.

**Step 4: Run focused tests and prove GREEN**

Run: `pytest -q tests/test_deployment_action_plan.py`

Expected: PASS.

### Task 4: Document the action matrix

**Files:**
- Create: `docs/deployment-action-gates.md`
- Test: `tests/test_deployment_action_plan.py`

Document required/prohibited effects and evidence for every action. State prominently that a generated plan is not authorization and that current production activation remains on the existing conservative updater until later batches land.

**Verification:**

Run: `pytest -q tests/test_deployment_action_plan.py`

Expected: PASS, including documentation assertions for all five action names and authorization boundary.

## Batch 2 — Immutable candidate staging

### Task 5: Add a server-side stage-only command

**Files:**
- Create: `deploy/telegram-kol-stage`
- Test: `tests/test_server_update_scripts.py`
- Test: `tests/test_minimal_server_updater.py`

The command must materialize an exact-SHA immutable release directory, validate its manifest, and write a non-secret receipt. It must have no code path to systemd, the production database, settings, Telegram, or Deepcoin. Re-running the same SHA is idempotent; a content mismatch is fail-closed.

Implemented locally after Batch 1 review. The stage receipt is stored inside the same atomically published read-only release directory and binds the exact commit, Git tree, normalized content digest, branch, canonical stage action manifest, and action-plan digest. The active source checkout is used only to read its origin URL; fetch and archive operations occur in an isolated temporary bare repository.

## Batch 3 — Scoped activation

### Task 6: Activate only a staged release

**Files:**
- Modify: `deploy/telegram-kol-update`
- Modify: `tests/test_minimal_server_updater.py`
- Modify: `tests/test_server_update_scripts.py`

Activation consumes a verified stage receipt and an independently supplied authorization token. It may restart only declared affected services. Worker/ingest scope retains active-write and protection gates; web/monitor scope excludes them. Schema/data changes retain L3 backup, integrity, and rollback gates.

Rollback ends at the last verified immutable release. It never silently re-enables trading or replays frozen messages.

Do not begin this task without a separate review of the immutable-release layout and rollback behavior.

Implemented locally after the immutable-layout review. The implementation uses
a separate Python activator and an explicit updater dispatcher so the legacy
checkout-mutating path is not mixed into immutable activation. Component
drop-ins load the exact read-only release while preserving the existing data
working directory and trusted virtual environment. Runtime proof binds imported
module path and manifest digest to systemd `MainPID` plus `/proc` start ticks;
worker scope also directly observes the running management, protection, close,
TPSL, and rescue tasks. Authorization is fresh, plan-bound, and single-use.

Any worker/ingest authority activation now restarts `web`, `ingest`, and
`worker` as one bounded maintenance group and installs the same root-owned
entry freeze on all three. Partial authority activation is rejected. The
freeze blocks only new entry admission: message consumption is dormant, Web
rejects entry commands, the durable command lane skips its two entry-submission
types, and both recovery submission and entry-revision replacement recheck the
freeze at their final write boundaries. Close/sync plus protection, management,
TPSL, close, and rescue remain separate directly proven capabilities.
Candidate success and immutable rollback both stay entry-frozen; entry
resumption is a future independently authorized `trading` action with a bounded
maintenance watermark and no automatic backlog replay. Subsequent Web-only
activation preserves a pre-existing freeze and cannot perform a true-to-false
transition.

The implementation intentionally accepts a bounded maintenance interruption.
It performs pre-stop and post-stop active-write checks for worker authority,
then either proves the candidate or returns only to the separately validated
immutable rollback release. Ingest-only scope is rejected because it cannot
close the worker-authority TOCTOU. L3 schema/data activation remains explicitly
unavailable until its backup/integrity executor exists. The first production
immutable switch also remains blocked until a separately authorized migration
deployment has installed the loaded-artifact endpoint; legacy checkout HEAD is
never treated as runtime identity. Worker/ingest activation also fails closed
if the canonical durable entry-authority row is absent or malformed; creating
that row is a separately reviewed and authorized production database write,
not an activator side effect.

## Batch 4 — Workstation helpers and legacy removal

### Task 7: Split workstation entry points

**Files:**
- Modify: `scripts/server_git_update.sh`
- Modify: `scripts/server_git_update.ps1`
- Modify: `scripts/bootstrap_server_updater.sh`
- Modify: `tests/test_server_update_scripts.py`

Expose explicit `plan`, `push`, `stage`, and `activate` commands. No command advances to the next action automatically. Trading enablement remains outside these helpers.

### Task 8: Remove the universal path

Delete the legacy one-command stage+activate behavior only after the separate stage and activate paths have passed focused failure-injection tests and one final full suite on the exact local candidate.

## Acceptance and first falsifier

The first falsifying test is: request `stage` for an `L3` worker change while production DB/runtime evidence is unavailable. The planner must still produce a valid stage-only plan and must prohibit service, DB, settings, Telegram, and exchange mutation. If it instead demands live evidence or permits activation side effects, the architecture has recreated the universal gate and must be rejected.
