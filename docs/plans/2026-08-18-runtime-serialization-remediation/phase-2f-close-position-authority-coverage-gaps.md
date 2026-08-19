# Phase 2f — Close the position-authority coverage gaps

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> This phase exists because Phase 2's own Task 1 found the design doc's core
> safety claim for per-chat sharding false: "the state that genuinely requires
> exclusion is per-position, and it already has its own boundary in
> `position_authority_lock.py`" is not true today. See
> `docs/runtime-serialization-remediation-status.md`, section "Phase 2 — Task
> 1's decision gate FAILED", for the full trace this phase file summarizes.
> Line anchors below were verified against commit `5c6aedb`. If a line number
> no longer matches, the symbol name next to it is authoritative — search for
> that instead of trusting the number.

**Goal:** Make the claim Phase 2 needs actually true — every exchange
mutation reachable from `auto_trade_executor` serialized by
`position_authority_lock()`, independent of the message lock — so that a
future session can re-run Phase 2's Task 1 and get a real "yes", and only
then reconsider enabling `message_lock_mode: per_chat`.

**Nature:** Correctness/safety fix to already-shipped production code, not a
new feature. Nothing about `message_lock_mode` changes in this phase, and it
is not touched — it stays `"global"`. This phase is deployable and useful on
its own regardless of whether `per_chat` sharding is ever enabled: even under
the current global lock, closing these gaps removes a latent hazard for any
future concurrency (a second worker thread, a retry path, anything that ever
calls these functions from somewhere other than the single serialized message
handler).

**Prerequisite:** Phase 2 is deployed (`3f5ed78` or later on
`codex/deepcoin-auto-trading-v1`). `message_lock_mode` reads `"global"` in
production. Do not start this phase expecting to also enable `per_chat` —
that is a separate decision for a separate session, made only after this
phase's own Task 4 re-confirms the boundary holds.

## Why this phase exists

Phase 2 Task 1 traced every leaf function reachable from `auto_trade_executor`
(`src/telegram_kol_research/web_app.py:3542` → `auto_process_message_trade_signal`
in `src/telegram_kol_research/auto_trade_execution.py`) that submits a Deepcoin
order or mutates position/protection state, and checked each against
`position_authority_lock()` / `@serialized_position_authority_mutation`
(`src/telegram_kol_research/position_authority_lock.py`). Three are covered:
`cancel_revision_entry_leg`, `execute_management_batch`,
`reconcile_deepcoin_execution_bindings`. Two are not — this phase closes both.

### Gap A — entry-signal and revision-replacement submission

`src/telegram_kol_research/recovery_live_submit.py:1142`,
`_submit_recovery_signal_direct`, is decorated
`@serialized_source_message_execution` (`:1140`), which acquires
`_source_execution_lock` (`src/telegram_kol_research/source_message_deletion.py:58`)
— a *different* `RLock`, keyed by DB bind, not `_POSITION_AUTHORITY_LOCK`.
Neither `recovery_live_submit.py` nor `auto_trade_execution.py` imports
`position_authority_lock` anywhere.

Reached two ways from `auto_trade_executor`:
- `process_trade_signal_live` (`recovery_live_submit.py:889`) →
  `_submit_recovery_signal_direct` (`:933`) ← the entry-signal path,
  `auto_trade_execution.py:1058` and `:1132`.
- `submit_strategy_revision_replacement_live` (`recovery_live_submit.py:387`) →
  `_submit_recovery_signal_direct` (`:492`) ← the `replacement_writer` closure
  in `auto_trade_execution.py:1420-1439`.

Leaf writes inside it, all currently under `_source_execution_lock` only:
`deepcoin_client.place_order` (`:1247`), `deepcoin_client.trigger_order`
(`:1360`, `:1388`), `submit_exact_position_sltp(...)` (`:1305`, via
`position_mutation_gateway.py:788`), `upsert_execution_binding` (`:1432`, via
`execution_bindings.py:231`), `upsert_execution_order_leg` (`:1452`), and the
`StrategyLifecycle` write in `_attach_lifecycle_binding` (`:1474`).

### Gap B — composite management batch execution

`src/telegram_kol_research/strategy_management_composite_executor.py:74`,
`execute_composite_management_batch`, imports `position_authority_lock`
nowhere in the file. This is the "management v2" close/revise/partial-close
path, taken whenever a management candidate carries
`management_contract_json` (`auto_trade_execution.py:1502`).

Leaf writes: `PositionMutationGateway(...).close_exact_position(...)`
(`:737-748`) and `submit_exact_position_sltp(...)` (`:991`) — both go through
`position_mutation_gateway.py`, which provides a DB-backed
`PositionMutationIntent` reserve→submitting→submitted state machine
(idempotency against retries) but **no mutual exclusion of its own**. Its
"coverage" in the one path that does work today (`execute_management_batch`)
exists only because that caller happens to hold `position_authority_lock` —
the gateway itself does not enforce it.

### Why this is not an active incident, and why it still matters

The single global message lock (`app.state.telegram_operation_lock`, or
`MessageLockProvider` in `"global"` mode) means only one message is ever in
flight anywhere today, so gap A and gap B can never race each other in
production right now. This phase is preventive: it makes the boundary this
whole remediation's Phase 2 depends on actually true, rather than leaving a
latent hazard for the next thing that introduces concurrency — `per_chat`
sharding, a retry-on-a-worker-thread change, anything.

## Scope

Add `position_authority_lock` coverage to both gaps, verify no new deadlock
risk from doing so, and update the Phase 2 architecture test to prove the
boundary now holds. Out of scope: `message_lock_mode`, `per_chat` sharding
itself, and any change to what these functions decide or execute — only
*when mutual exclusion is held* changes.

### Task 0: Confirm lock-ordering safety before writing any code

**Do this first. If it fails, stop and report rather than proceeding.**

`_POSITION_AUTHORITY_LOCK` and `_source_execution_lock`/`_SOURCE_EXECUTION_LOCKS`
are both `threading.RLock` instances (reentrant, so a thread already holding
one can re-acquire it, but nesting *two different* locks in an order that
varies by call path is a classic deadlock risk if two threads ever acquire
them in opposite order). Enumerate every existing call path that acquires
`position_authority_lock` and check whether any of them, directly or
transitively, also acquire `_source_execution_lock` — and vice versa. If gap
A's fix wraps `_submit_recovery_signal_direct` in
`position_authority_lock` *outside* `serialized_source_message_execution`
(i.e., position lock acquired first, then source-execution lock), every other
caller that already holds `position_authority_lock` must never itself call
into something that acquires `_source_execution_lock` afterward — confirm
this by reading `execute_management_batch`'s and
`reconcile_deepcoin_execution_bindings`'s full call trees, not by assuming.
Record the ordering decided on (which lock is acquired first) and the
evidence that no path acquires them in the reverse order.

### Task 1: Close gap A

**Files:**
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `tests/test_recovery_live_submit*.py` (or the relevant existing
  test file covering `_submit_recovery_signal_direct` — locate by grep before
  assuming a filename)

Add `@serialized_position_authority_mutation`
(`src/telegram_kol_research/position_authority_lock.py:21`) to
`_submit_recovery_signal_direct`, stacked with the existing
`@serialized_source_message_execution` and `@_report_entry_submission_progress`
decorators in the order Task 0 determined is safe. Add a test asserting the
function is observably covered (e.g. that `position_authority_lock` is held
for the duration of a call, using the same technique
`tests/test_position_authority_boundary_coverage.py` or the existing
Phase 1b/1c thread-sharing tests use — an injected lock or thread-identity
check, not just a source-text grep).

### Task 2: Close gap B

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_composite_executor.py`
- Modify: the existing test file covering `execute_composite_management_batch`
  (locate by grep before assuming a filename)

Add `@serialized_position_authority_mutation` to
`execute_composite_management_batch`, following the exact pattern
`execute_management_batch` (`strategy_management_executor.py:1258`) already
uses. Add a test asserting the function is observably covered, same technique
as Task 1.

### Task 3: Update the Phase 2 architecture test

**Files:**
- Modify: `tests/test_position_authority_boundary_coverage.py`

Move `recovery_live_submit._submit_recovery_signal_direct` and
`strategy_management_composite_executor.execute_composite_management_batch`
from `KNOWN_UNCOVERED_LEAVES` to `COVERED_LEAVES`. Delete or repurpose
`test_per_chat_sharding_decision_gate_is_not_yet_met` — if
`KNOWN_UNCOVERED_LEAVES` is now empty, that test's assertion
(`len(KNOWN_UNCOVERED_LEAVES) > 0`) is backwards; replace it with the honest
opposite claim (the decision gate is now met, cite this phase's commit), or
delete it if it no longer expresses a meaningful invariant. Do not leave it
passing by accident with a now-wrong docstring.

### Task 4: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

Expected: no new failures beyond the tests this phase adds or intentionally
changes. Confirm the count delta matches exactly what Tasks 1-3 added.

[local]

```bash
git status --short
git add src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/strategy_management_composite_executor.py \
  tests/test_position_authority_boundary_coverage.py \
  <the two leaf-specific test files Tasks 1 and 2 modified>
git diff --cached --name-only
git commit -m "fix: close position_authority_lock coverage gaps in entry submission and composite management"
```

### Task 5: Deploy and verify

Follow `docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md`.
This is a normal deploy — no change class, no snapshot argument, run
`EXPECTED_COMMIT=<branch tip> ./scripts/server_git_update.sh` from a checkout
of the exact commit being deployed (this workstation has no PowerShell).

Because `message_lock_mode` stays `"global"` in production, the added locks
are acquired uncontended (nothing else can be in flight concurrently right
now) — this deploy should be behaviorally invisible. Confirm after deploy:

- `GET /api/trading-settings` returns 200 and `message_lock_mode` still reads
  `"global"` (unchanged by this phase).
- `GET /api/runtime/loop-health` answers and `stall_count`/`p99_ms` are in
  the same range as the Phase 1e/Phase 2 baseline — these locks are
  `threading.RLock`, held briefly and synchronously inside code that already
  runs off the event loop via `asyncio.to_thread`, so they should not appear
  on the loop at all. If they do, something is wrong and must be investigated
  before calling this phase done.
- No new `position_attribution_audits` or `position_protection_incidents`
  rows appear after the deploy that weren't there before (same check Phase
  2's own Task 6 Step 4 specifies).

## Completion criteria

- Task 0's lock-ordering evidence is recorded, not assumed.
- Both gaps closed; each has a test proving observable coverage, not just a
  source-text check.
- `tests/test_position_authority_boundary_coverage.py` reflects reality:
  `KNOWN_UNCOVERED_LEAVES` is empty (or whatever remains is a *newly*
  discovered gap, recorded with the same rigor Phase 2 used, not silently
  dropped).
- Full suite passes; deploy verified over ssh, not inferred from exit code.

## Rollback

Revert the two decorator additions (or redeploy the pre-Phase-2f commit with
the same updater command). No schema change, no persisted state.

## Status file update

Set `phase_status: completed` for this phase entry, record the deployed
commit, and explicitly state in `docs/runtime-serialization-remediation-status.md`
whether Phase 2's `per_chat` decision gate now passes (it should, if both
gaps closed cleanly and Task 0's ordering check found no new deadlock risk).
**Do not set `message_lock_mode: per_chat` in production as part of this
phase** — that is Phase 2's own Task 6 Steps 2-3, deliberately left for a
separate session and a separate explicit decision, per the original
instruction that enabling it must be asked about independently from
deploying dormant infrastructure.
