# Deepcoin Legacy Runtime Drain Bridge Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a default-read-only, fail-closed bridge that freezes legacy entry authority, installs exact non-expiring revision sentinels, permits only the existing one-order reviewed cancellation path, and releases the fence only after all seven targets are completely drained.

**Architecture:** Persist one closed-schema bridge document in `TradingSetting` and atomically bind it to exact `StrategyRevisionBatch.advance_claim_token` sentinels whose timestamps remain null so the old runtime cannot reclaim them. Keep protection workers outside the bridge, require a stable production SHA and worker PID/start-tick witness at every transition, and integrate the existing lease-aware cancellation helper so only exact bridge sentinels are exempted from its otherwise global fail-closed authority gate.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite `BEGIN IMMEDIATE`, Typer, pytest, existing trading-settings, entry-revision authority and reviewed pending-entry cancellation modules.

---

### Task 1: Add strict bridge state and read-only planning

**Files:**
- Create: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Create: `tests/test_legacy_runtime_drain_bridge.py`

**Step 1: Write strict state and read-only-plan tests**

Write tests for the wished-for public API:

```python
identity = LegacyRuntimeIdentity(
    production_sha=OLD_SHA,
    worker_pid=2350028,
    worker_start_ticks=987654,
)
plan = build_legacy_runtime_drain_bridge_plan(
    session_factory,
    runtime_identity=identity,
    expected_production_sha=OLD_SHA,
    reviewed_order_ids=REVIEWED_IDS,
    planned_at=NOW,
)
assert plan.mode == "dry_run"
assert plan.state == "absent"
assert plan.conflicts == ()
assert plan.fingerprint
```

Cover exact 40-character SHA validation, positive PID/start ticks, seven unique
bounded order IDs, deterministic fingerprints and zero database writes. Insert
malformed, unknown-version, unknown-state, extra-key, duplicate-ID and invalid
worker documents directly and assert the plan reports
`legacy_bridge_state_invalid` without exposing raw JSON.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest -q tests/test_legacy_runtime_drain_bridge.py \
  -k "state or read_only or fingerprint"
```

Expected: collection fails because `legacy_runtime_drain_bridge` does not exist.

**Step 3: Implement the closed model and planner**

Create these immutable types and constants:

```python
LEGACY_RUNTIME_DRAIN_BRIDGE_KEY = "legacy_runtime_drain_bridge"

@dataclass(frozen=True, slots=True)
class LegacyRuntimeIdentity:
    production_sha: str
    worker_pid: int
    worker_start_ticks: int

@dataclass(frozen=True, slots=True)
class LegacyRuntimeDrainBridgePlan:
    mode: str
    state: str
    fingerprint: str
    conflicts: tuple[dict[str, str], ...]
    fenced_batch_ids: tuple[int, ...]
    completed_order_ids: tuple[str, ...]
```

Implement strict canonical JSON parsing for persisted states `frozen`,
`fenced`, `cancelling`, `unknown_locked`, `drained` and
`released_for_deploy`. Use exact-key sets per state, bounded strings and aware
UTC timestamps. Planning opens sessions read-only in behavior, never calls
`commit`, never creates a row and never consumes a confirmation token.

The planner must inventory active revision batches, foreign claims, target
unknown mutation intents, governed settings and non-shadow message jobs at the
current raw-message watermark. It returns bounded reason codes only.

**Step 4: Run GREEN**

```bash
.venv/bin/python -m pytest -q tests/test_legacy_runtime_drain_bridge.py \
  -k "state or read_only or fingerprint"
```

**Step 5: Commit explicit paths**

```bash
git add -- src/telegram_kol_research/legacy_runtime_drain_bridge.py \
  tests/test_legacy_runtime_drain_bridge.py
git diff --cached --name-only
git commit -m "feat: plan legacy runtime drain bridge"
```

### Task 2: Implement atomic freeze and legacy revision fence

**Files:**
- Modify: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Modify: `tests/test_legacy_runtime_drain_bridge.py`
- Test: `tests/test_entry_revision_executor.py`
- Test: `tests/test_strategy_revision_planner.py`

**Step 1: Write failing transition and race tests**

Add independent tests proving:

- freeze atomically records the original two settings, sets
  `auto_trade_enabled=false` and `entry_revision_v2_mode=disabled`, captures
  `MAX(raw_messages.id)`, and persists state `frozen`;
- foreign held claims prevent fencing and are never overwritten;
- a non-shadow message job claimed at/before freeze blocks fencing;
- later messages do not block once they load frozen settings;
- fence installs one exact bridge token on every active revision batch with
  `advance_claimed_at is None`;
- if the old worker claims first, fence refuses; if fence commits first, the
  old worker returns already-claimed before any exchange call;
- advancing time beyond the legacy five-minute lease does not make the null-time
  sentinel reclaimable;
- a v2 batch planned after freeze performs zero exchange writes;
- worker PID/start ticks or production SHA drift refuses every transition;
- exact-token pre-write rollback restores settings and claims, while any
  completed or write-boundary state blocks rollback.

Use two independent SQLAlchemy session factories against the same SQLite file
for the race tests. Exercise the real legacy-compatible executor behavior; do
not merely assert mock call counts on the bridge.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest -q \
  tests/test_legacy_runtime_drain_bridge.py \
  tests/test_entry_revision_executor.py \
  tests/test_strategy_revision_planner.py \
  -k "legacy_bridge or sentinel or non_expiring"
```

Expected: transition functions are missing and the old-worker race is not
closed by the planner-only code.

**Step 3: Implement minimal atomic transitions**

Add:

```python
freeze_legacy_runtime_drain_bridge(
    session_factory,
    *,
    plan: LegacyRuntimeDrainBridgePlan,
    runtime_identity: LegacyRuntimeIdentity,
    expected_fingerprint: str,
    confirmation_token: str,
    frozen_at: datetime,
) -> LegacyRuntimeDrainBridgeResult

fence_legacy_runtime_revisions(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    fenced_at: datetime,
) -> LegacyRuntimeDrainBridgeResult

rollback_legacy_runtime_drain_bridge(
    session_factory,
    *,
    bridge_token: str,
    runtime_identity: LegacyRuntimeIdentity,
    rolled_back_at: datetime,
) -> LegacyRuntimeDrainBridgeResult
```

Each mutation begins with `BEGIN IMMEDIATE`, reloads and validates the exact
bridge document and runtime identity, then performs one commit. Freeze uses the
existing strict trading-settings parser and preserves the whole settings
payload while changing only the two governed fields. Fence requires every
pre-freeze non-shadow claimed job to be terminal and refuses all existing
claims before writing exact null-time sentinels.

Rollback is allowed only before any reviewed write boundary and removes only
exact stored tokens from exact stored batch IDs. It refuses rather than partly
restoring on any drift.

**Step 4: Run GREEN and adjacent revision tests**

```bash
.venv/bin/python -m pytest -q \
  tests/test_legacy_runtime_drain_bridge.py \
  tests/test_entry_revision_executor.py \
  tests/test_strategy_revision_planner.py
```

**Step 5: Commit explicit paths**

```bash
git add -- src/telegram_kol_research/legacy_runtime_drain_bridge.py \
  tests/test_legacy_runtime_drain_bridge.py \
  tests/test_entry_revision_executor.py \
  tests/test_strategy_revision_planner.py
git diff --cached --name-only
git commit -m "fix: fence legacy revision authority"
```

### Task 3: Bind exact cancellation to the legacy bridge

**Files:**
- Modify: `src/telegram_kol_research/reviewed_pending_entry_cancel.py`
- Modify: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Modify: `tests/test_reviewed_pending_entry_cancel.py`
- Modify: `tests/test_legacy_runtime_drain_bridge.py`

**Step 1: Write failing bridge-aware cancellation tests**

Cover:

- exact bridge sentinels matching the complete active-batch set do not trigger
  the global revision-claim conflict;
- an unrelated claim, missing sentinel, extra sentinel, token mismatch,
  non-null sentinel timestamp or malformed bridge state still blocks;
- state must be `fenced` and the runtime identity/settings must remain exact;
- one action changes bridge state to `cancelling` before the exchange boundary;
- confirmed exchange cancellation plus complete local terminalization records
  exactly that order and returns the bridge to `fenced`;
- a fresh plan is required for the next order;
- transport, response, readback, fill, worker-identity or local commit unknown
  retains the inner cancellation lease and outer sentinels, records or preserves
  `unknown_locked`, and makes a second apply stop before exchange access;
- pre-write drift releases only the inner cancellation lease and leaves the
  outer bridge `fenced`;
- protection, rescue and management rows are neither claimed nor changed.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_legacy_runtime_drain_bridge.py \
  -k "legacy_bridge or bridge_sentinel or unknown_locked"
```

Expected: current global claim gate rejects every sentinel and no bridge state
tracks the one-order write boundary.

**Step 3: Implement the narrow exemption and state hooks**

Add a strict bridge-sentinel validator that returns true only when the held
bridge document, exact token, exact stored batch set, null timestamps, frozen
settings and current active revision set all match. Call it from
`_active_exchange_authority_present`; do not weaken any child-ambiguity,
management, worker-command or mutation-intent gate.

Add bridge hooks immediately before and after the reviewed cancellation write
boundary. Hooks update state under `BEGIN IMMEDIATE` and exact token matching.
They never store raw exchange responses or confirmation tokens. Every escaping
post-boundary exception retains authority and becomes non-retryable.

**Step 4: Run GREEN and the complete cancellation files**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_legacy_runtime_drain_bridge.py \
  tests/test_entry_revision_exchange_authority.py
```

**Step 5: Commit explicit paths**

```bash
git add -- src/telegram_kol_research/reviewed_pending_entry_cancel.py \
  src/telegram_kol_research/legacy_runtime_drain_bridge.py \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_legacy_runtime_drain_bridge.py
git diff --cached --name-only
git commit -m "fix: bind reviewed cancellation to legacy fence"
```

### Task 4: Add drain, release and default-read-only CLI

**Files:**
- Modify: `src/telegram_kol_research/legacy_runtime_drain_bridge.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_legacy_runtime_drain_bridge.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write failing drain/release and CLI tests**

Prove that:

- default `bridge-reviewed-pending-entries` invocation only prints a redacted
  plan and does not create clients, settings, bridge rows or claims;
- mutation modes require exact action, fingerprint, bridge token, stable runtime
  identity and one-use confirmation token;
- runtime identity reads only exact checkout HEAD, service MainPID and
  `/proc/<pid>/stat` start ticks, and rejects symlink/non-process drift;
- drain refuses positions, regular orders, reviewed pending triggers,
  unreviewed/unidentified triggers, incomplete queries, capped history, local
  nonterminal rows, unknown mutation intents or a held inner lease;
- drain succeeds only for all seven exact locally terminalized targets;
- release removes only exact stored sentinel tokens, sets
  `released_for_deploy`, and leaves both governed settings frozen;
- release refuses any batch/token/timestamp drift and is atomic;
- CLI JSON exposes no raw response, credential, confirmation or bridge token.

**Step 2: Run RED**

```bash
.venv/bin/python -m pytest -q \
  tests/test_legacy_runtime_drain_bridge.py \
  tests/test_cli_smoke.py \
  -k "drain or release or bridge_reviewed"
```

Expected: drain/release functions and CLI command are absent.

**Step 3: Implement minimal drain/release and command routing**

Add:

```python
mark_legacy_runtime_bridge_drained(...)
release_legacy_runtime_bridge_for_deploy(...)
read_local_legacy_worker_identity(...)
```

Create Typer command `bridge-reviewed-pending-entries`. Keep plan as the default
mode. Explicit future mutation actions are `freeze`, `fence`, `rollback`,
`mark-drained` and `release-for-deploy`; each refuses missing confirmation
material before opening any write path. Reuse the existing reviewed target
constant and cancellation command rather than adding a second order list.

Deepcoin evidence is supplied through the existing strict client and planner;
one incomplete query gets at most the already-governed single retry, then
remains unknown. Do not add replay, bulk cancel, automatic iteration or settings
restore commands.

**Step 4: Run GREEN and CLI regressions**

```bash
.venv/bin/python -m pytest -q \
  tests/test_legacy_runtime_drain_bridge.py \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_cli_smoke.py
```

**Step 5: Commit explicit paths**

```bash
git add -- src/telegram_kol_research/legacy_runtime_drain_bridge.py \
  src/telegram_kol_research/cli.py \
  tests/test_legacy_runtime_drain_bridge.py \
  tests/test_cli_smoke.py
git diff --cached --name-only
git commit -m "feat: add legacy drain bridge CLI"
```

### Task 5: Verify protection isolation, review and record the candidate

**Files:**
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: Run focused authority and protection regression**

```bash
.venv/bin/python -m pytest -q \
  tests/test_legacy_runtime_drain_bridge.py \
  tests/test_reviewed_pending_entry_cancel.py \
  tests/test_entry_revision_exchange_authority.py \
  tests/test_entry_revision_executor.py \
  tests/test_strategy_revision_planner.py \
  tests/test_deployment_active_write_check.py \
  tests/test_position_authority_lock.py \
  tests/test_position_authority_boundary_coverage.py \
  tests/test_position_protection_legs.py \
  tests/test_trigger_protection_stop_rescue.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_strategy_management_worker.py
```

**Step 2: Run static checks**

```bash
.venv/bin/python -m compileall -q src/telegram_kol_research
git diff --check
```

**Step 3: Run one final full repository suite**

```bash
.venv/bin/python -m pytest -q
```

Run this once only after production code is settled. Any later production-code
edit requires affected focused tests and one new final full suite.

**Step 4: Review the exact diff**

Review base-to-candidate for pre-claim races, sentinel stealing, null-timestamp
legacy compatibility, queue watermark boundaries, settings rollback, unknown
release, incomplete exchange evidence, worker identity TOCTOU, protection-worker
coupling, secret leakage and missing negative tests. Resolve every Critical and
Important finding before handoff.

**Step 5: Update canonical status and commit explicit path**

Record the exact base/design/plan/code SHAs, each observed RED and GREEN result,
focused/full suite evidence, review findings, remaining production gates and all
prohibited actions that did not occur.

```bash
git add -- docs/deepcoin-contract-cache-ownership-repair-status.md
git diff --cached --name-only
git commit -m "docs: record legacy drain bridge candidate"
```

Do not push, deploy, SSH, freeze, restart, mutate production/settings/databases,
call Deepcoin, cancel an order, replay history or manufacture traffic.
