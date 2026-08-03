# Delayed Entry Exit and Protection Rescue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make an exact contextual full exit cancel every deferred entry leg, and automatically rescue a newly filled trigger leg that has no valid stop without weakening protection ownership rules.

**Architecture:** Add persisted current-risk evidence to strategy-thread candidates and a narrow internal authority marker for unique, risk-reducing contextual exits. Reuse the existing mixed-state management batch for cancellation/close races. Extend reconciliation with a separately gated stop-rescue orchestrator that preserves `candidate_predates_fill`, creates at most one exact-position rescue, and releases existing backup-stop/TP convergence only after primary-stop readback.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, existing MiMo/context-resolution pipeline, Deepcoin reconciliation, strategy-management batches, position mutation gateway, systemd production service.

---

### Task 1: Persist current-risk evidence in strategy-thread candidates

**Files:**
- Modify: `src/telegram_kol_research/strategy_thread_candidates.py:43-55,105-290`
- Modify: `src/telegram_kol_research/context_resolution_prompt.py`
- Test: `tests/test_strategy_thread_candidates.py`
- Test: `tests/test_context_resolution_prompt.py`

**Step 1: Write the failing ghost-lifecycle test**

Add `test_candidate_marks_entered_lifecycle_without_current_risk_as_inactive`.
Seed two entered BTC-long lifecycles in one chat:

- old lifecycle: active binding, verified entry leg with terminal/non-live status and no pending entry;
- current lifecycle: one `active + verified + pos_id` leg and one exact pending trigger leg.

Assert the candidates expose a closed structure such as:

```python
assert old.risk_state == "no_current_risk"
assert old.live_verified_pos_ids == ()
assert old.pending_entry_leg_ids == ()
assert current.risk_state == "current_risk"
assert current.live_verified_pos_ids == ("pos-current",)
assert current.pending_entry_leg_ids == (pending_leg_id,)
```

Do not infer current risk from lifecycle status, binding status, or the compatibility
`binding.pos_id` field alone.

**Step 2: Run the focused tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_strategy_thread_candidates.py \
  tests/test_context_resolution_prompt.py -q
```

Expected: FAIL because candidates do not yet expose current-risk evidence.

**Step 3: Implement deterministic risk summaries**

Extend `StrategyThreadCandidate` with immutable fields:

```python
risk_state: str
live_verified_pos_ids: tuple[str, ...]
pending_entry_leg_ids: tuple[int, ...]
uncertain_entry_leg_ids: tuple[int, ...]
```

Load all entry legs for the binding, not only verified legs. Classify from persisted
reconciliation state using closed status vocabularies:

- live risk: verified leg, non-empty `pos_id`, status `active` or `partially_filled`;
- pending risk: exact regular/trigger entry order in a pending/submitted state without a
  verified position;
- uncertain risk: attribution conflict/evidence unavailable or unknown-result state;
- no current risk: no live, pending, or uncertain entry leg.

Include the bounded summary in the context-resolution prompt. Keep order IDs and `posId`
only where existing prompt policy already permits them; never include raw exchange payloads.

**Step 4: Run tests and commit**

Run the same command and expect PASS, then:

```bash
git add src/telegram_kol_research/strategy_thread_candidates.py \
  src/telegram_kol_research/context_resolution_prompt.py \
  tests/test_strategy_thread_candidates.py \
  tests/test_context_resolution_prompt.py
git commit -m "feat: expose exact thread risk state"
```

### Task 2: Authorize only unique low-confidence contextual full exits

**Files:**
- Modify: `src/telegram_kol_research/authoritative_recognition.py:378-445`
- Modify: `src/telegram_kol_research/message_recognition.py:1038-1120,1774-1940`
- Test: `tests/test_authoritative_recognition.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Add the production-shaped failing replay**

Add `test_dabiaoke_4168_projects_exact_exit_despite_ghost_lifecycle` with:

- current raw text `63100没站稳，求稳就找机会出局`;
- target thread 63 with current-risk evidence;
- old same-symbol/same-side thread whose `risk_state` is `no_current_risk`;
- context decision `exit_thread`, one target thread, `exit_full`, confidence `0.62`,
  supporting message IDs `[4167, 4168]` and `multiple_candidates` conflict history.

Assert one exact full-exit candidate and one current management instruction are projected
for lifecycle 693. Assert the model confidence remains stored as `0.62`; do not rewrite it
to `0.70`.

Add negative parameterized cases for:

- confidence below `0.60`;
- two target thread IDs;
- a non-target candidate with `current_risk` or `uncertain_risk`;
- action `exit_partial`, add-position or stop widening;
- missing root/current supporting messages;
- missing exact lifecycle or source-chat mismatch.

Every negative case must produce no executable candidate.

**Step 2: Verify the replay fails**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_authoritative_recognition.py -k "dabiaoke_4168 or exact_context_exit" \
  tests/test_message_recognition.py -k "context_risk_reduction" -q
```

Expected: FAIL because `_resolved_mimo_result()` drops every decision below `0.70`.

**Step 3: Implement the internal authority marker**

Add a closed helper in `authoritative_recognition.py` that returns true only when all
approved conditions hold. When true, construct the lifecycle event with the original
confidence and an internal marker, for example:

```python
"_exact_context_risk_reduction_authorized": True
```

The marker must be created only by `_resolved_mimo_result()` after checking the selected
candidate and competitors; ignore or strip any same-named field supplied by MiMo.

In `message_recognition.py`, accept confidence `>= 0.60` only when the internal marker is
present, action resolves to `full_exit`, target count is one, and normal exact management
scope succeeds. All other events retain the `0.70` threshold.

Persist the original context decision/confidence for audit. Do not add symbol/side fallback
or risk-reducing fanout.

**Step 4: Run the full authoritative recognition set and commit**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py \
  tests/test_management_directives.py -q
```

Expected: PASS.

```bash
git add src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/message_recognition.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py
git commit -m "fix: project exact contextual exits"
```

### Task 3: Prove full exit covers both live and deferred entry legs

**Files:**
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_strategy_management_planner.py`
- Modify: `tests/test_strategy_management_executor.py`
- Modify only if the replay exposes a gap: `src/telegram_kol_research/strategy_management_planner.py`
- Modify only if the replay exposes a gap: `src/telegram_kol_research/strategy_management_executor.py`

**Step 1: Add one end-to-end mixed-state test**

Seed the projected `#4168` full exit against strategy `#4167` with:

- live verified leg: 8 contracts, exact `pos-live`;
- pending trigger leg: 14 contracts, exact parent order ID;
- another BTC-long strategy in the same chat.

Run the normal auto-trade/planner/worker path. Assert the batch snapshot includes only the
two `#4167` legs, cancels the exact pending trigger first, then closes only `pos-live`.
Assert the unrelated strategy receives zero requests.

**Step 2: Add the cancellation/fill-race replay**

Make the cancel return the existing definite “not pending” result. On the single bounded
readback expose one generated regular fill and one exact verified new `posId`. Assert the
same immutable batch closes both `pos-live` and the newly confirmed position, and retrying
the worker produces no duplicate cancel or close.

Add unknown-result and ambiguous-fill variants; both stop in `recovery_required` with no
unproven close submission.

**Step 3: Run tests before changing production code**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py -k "full_exit or deferred" -q
```

If all new tests pass, make no production change: the existing mixed-state batch is already
the reference implementation. If a test fails, implement only the missing invariant and
rerun the same set.

**Step 4: Commit the regression coverage**

```bash
git add tests/test_auto_trade_execution.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_executor.py
git commit -m "test: replay delayed entry full exit"
```

Stage only files actually changed.

### Task 4: Add a separately gated protection-rescue orchestrator

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Create: `src/telegram_kol_research/trigger_protection_rescue_worker.py`
- Modify: `src/telegram_kol_research/execution_bindings.py:304-370,1306-1495`
- Modify: `src/telegram_kol_research/strategy_management_planner.py:1946-2100`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_trigger_protection_stop_rescue.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Add failing settings tests**

Introduce `trigger_protection_stop_rescue_mode` with values `disabled`, `shadow`, `live`.
Safe default is `disabled`. `live` is effective only when both `auto_trade_enabled` and
`management_execution_mode=live` are true.

Test invalid values, persistence round-trip and effective-mode properties.

**Step 2: Add the exact Deepcoin timing replay**

Build a fake snapshot matching production:

- parent trigger request carries `slTriggerPx=62000`;
- historical TPSL is created at 16:11:14;
- generated position/order fills at 16:11:25;
- exact position has size 14 and pending TPSL is empty.

Assert adoption remains refused with `trigger_protection_candidate_predates_fill`.

For mode `disabled`, assert no rescue evaluation or exchange write. For `shadow`, assert a
bounded `stop_rescue_shadow_ready` observation/incident with zero exchange writes. For
`live`, assert one durable rescue is created and one exact `set_position_sltp` request is
submitted for `posId` and 62000.

**Step 3: Separate read-only eligibility from persistent planning**

Refactor `_prepare_trigger_protection_stop_rescue()` into a read-only evaluator returning a
closed plan/refusal value. It must reject:

- missing/ambiguous live position;
- changed size/economics;
- non-split position;
- existing verified or pending stop;
- opaque TP conflict;
- parent event mismatch;
- active close reservation, management reservation or mutation intent;
- snapshot errors or unknown exchange outcome.

Shadow mode calls only this evaluator and writes a bounded diagnostic record. It must not
create `trigger_protection_stop_rescues` or call Deepcoin writes.

**Step 4: Implement live orchestration**

Create `run_trigger_protection_rescue_tick()` that:

1. loads due exact intent IDs after reconciliation;
2. evaluates each against fresh exchange evidence;
3. calls the existing planner once;
4. calls the existing executor once;
5. stops on an unknown outcome;
6. returns bounded counts and reason codes.

Call it from `reconcile_deepcoin_execution_bindings()` only after attribution has committed,
while holding the existing re-entrant position-authority lock. Do not wait for the fifth
adoption retry when a complete snapshot already proves a live position and no stop.

**Step 5: Verify idempotency and restart recovery**

Add tests proving:

- two worker ticks create one rescue and one exchange order;
- process restart after `submitting` performs only readback;
- position disappears before submit -> noop, zero writes;
- result unknown -> recovery required, zero retries;
- accepted full exit or close reservation wins over rescue.

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trading_settings.py \
  tests/test_trigger_protection_stop_rescue.py \
  tests/test_execution_bindings.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/trigger_protection_rescue_worker.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/strategy_management_planner.py \
  tests/test_trading_settings.py \
  tests/test_trigger_protection_stop_rescue.py \
  tests/test_execution_bindings.py
git commit -m "feat: orchestrate exact stop rescue"
```

### Task 5: Converge backup stop, take profits and logical protection states

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py:940-1065`
- Modify: `src/telegram_kol_research/position_protection_legs.py`
- Modify: `src/telegram_kol_research/trigger_backup_stop_executor.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence.py`
- Modify: `src/telegram_kol_research/trigger_take_profit_convergence_executor.py`
- Test: `tests/test_execution_bindings.py`
- Test: `tests/test_backup_stop_repair.py`
- Test: `tests/test_trigger_take_profit_convergence.py`
- Test: `tests/test_trigger_take_profit_convergence_executor.py`

**Step 1: Add failing state-transition assertions**

When a trigger leg becomes `active + verified` with zero stop, its logical protection legs
must leave ordinary `waiting_fill` and enter `protection_recovery_pending`. After primary
stop rescue readback, primary becomes verified; after backup readback, existing TP
convergence becomes ready.

**Step 2: Reuse existing convergence paths**

Do not create a second TP allocator. After rescue verification:

- bind the exact primary stop to its logical leg and ledger;
- let `submit_verified_trigger_backup_stops()` establish the backup stop;
- reload one coherent snapshot;
- call `_ready_verified_trigger_take_profit_convergences()`;
- let the existing TP convergence executor allocate from actual current size.

Assert a 14-contract leg produces TP quantities totaling 14 and never exceeding current
position size. If current size changed, require replanning.

**Step 3: Add close-wins tests**

If a full exit, close reservation or terminal lifecycle appears between any two phases,
mark remaining protection work terminal/noop. No new SL or TP may race the close.

**Step 4: Run and commit**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_backup_stop_repair.py \
  tests/test_trigger_take_profit_convergence.py \
  tests/test_trigger_take_profit_convergence_executor.py -q
```

```bash
git add src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/position_protection_legs.py \
  src/telegram_kol_research/trigger_backup_stop_executor.py \
  src/telegram_kol_research/trigger_take_profit_convergence.py \
  src/telegram_kol_research/trigger_take_profit_convergence_executor.py \
  tests/test_execution_bindings.py \
  tests/test_backup_stop_repair.py \
  tests/test_trigger_take_profit_convergence.py \
  tests/test_trigger_take_profit_convergence_executor.py
git commit -m "fix: converge rescued trigger protection"
```

### Task 6: Remove ghost competitors and expose critical unprotected risk

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Modify: `src/telegram_kol_research/strategy_lifecycle.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Test: `tests/test_execution_bindings.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_runtime_incident_scanner.py`
- Test: `tests/test_web_queries_dashboard.py`

**Step 1: Add ghost-lifecycle convergence tests**

For an entered lifecycle whose exact verified positions are absent, deferred entries are
terminal and no mutation outcome is unknown, reconcile it out of current-risk competition.
Preserve historical audit identity. If exchange history is insufficient to prove terminal
state, mark manual review but set candidate `risk_state=uncertain_risk`; do not silently
close it.

**Step 2: Add the critical invariant**

Detect:

```text
live verified position
+ no verified/pending primary stop
+ no close or mutation in progress
```

Expose bounded strategy/binding/leg/position identifiers, planned stop, exposure start and
rescue state. Do not expose credentials or raw API payloads.

**Step 3: Gate new entries for the affected chat**

Before submitting a new entry, block only the same chat when it has an unresolved critical
unprotected position. Exact close, cancel and stop-rescue actions remain allowed. Add tests
showing other chats are unaffected and stale/closed incidents do not block.

**Step 4: Converge manual close reservations**

When the position mutation is confirmed and exchange position is absent, mark the exact
entry leg terminal and the `bound_position_close_reservation` confirmed. For a multi-leg
binding, keep the binding/lifecycle active only for remaining live/pending legs.

**Step 5: Run and commit**

```bash
.venv/bin/python -m pytest \
  tests/test_execution_bindings.py \
  tests/test_auto_trade_execution.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_web_queries_dashboard.py -q
```

```bash
git add src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/strategy_lifecycle.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/runtime_incident_scanner.py \
  src/telegram_kol_research/web_queries.py \
  tests/test_execution_bindings.py \
  tests/test_auto_trade_execution.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_web_queries_dashboard.py
git commit -m "fix: surface unprotected trigger risk"
```

### Task 7: Document, review and verify locally

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/runbook.md`
- Modify: `docs/runtime-incident-agent-runbook.md` only if the scanner rule is operator-visible

**Step 1: Document the invariants**

Record:

- unique exact contextual full-exit authority and its closed `0.60` exception;
- full exit covers live plus deferred legs;
- pre-fill protection remains unowned;
- complete live-position/no-stop evidence transitions to stop rescue;
- rescue mode defaults disabled and live requires explicit approval;
- close wins over protection writes;
- unprotected positions block only new entries in the same chat.

**Step 2: Run the focused suite**

```bash
.venv/bin/python -m pytest \
  tests/test_strategy_thread_candidates.py \
  tests/test_context_resolution_prompt.py \
  tests/test_authoritative_recognition.py \
  tests/test_message_recognition.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_trigger_protection_stop_rescue.py \
  tests/test_execution_bindings.py \
  tests/test_backup_stop_repair.py \
  tests/test_trigger_take_profit_convergence.py \
  tests/test_trigger_take_profit_convergence_executor.py \
  tests/test_runtime_incident_scanner.py -q
```

Expected: PASS.

**Step 3: Run full checks**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
git diff --check
```

The known pre-existing CLI monitor smoke failure from commit `578d65e` must be reported
separately if still present; do not alter unrelated monitor behavior in this fix.

**Step 4: Review the exact write boundaries**

Use the requesting-code-review workflow and verify:

- no model-supplied field can forge the internal low-confidence authority marker;
- no ghost filtering drops unknown-result risk;
- no symbol/side fallback was introduced;
- adoption still rejects `candidate_predates_fill`;
- stop rescue is exact-posId, idempotent and close-aware;
- shadow mode performs zero exchange writes;
- retry/restart cannot duplicate cancel, close, SL, backup SL or TP submissions.

**Step 5: Commit documentation and review corrections**

```bash
git add docs/migration-handoff.md docs/runbook.md docs/runtime-incident-agent-runbook.md
git commit -m "docs: define delayed entry rescue invariants"
```

Stage only files actually changed.

### Task 8: Push and deploy shadow-only

**Files:**
- Verify: `scripts/server_git_update.ps1`
- Verify: `scripts/server_git_update.sh`

**Step 1: Confirm repository scope**

```bash
git status --short --branch
git diff --check
git log --oneline --decorate -12
```

Preserve the user's existing `uv.lock` and unrelated untracked files.

**Step 2: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 3: Prove a safe deployment window**

On the server, use read-only checks to require:

- zero `execution_running` recognition decisions;
- zero in-flight management submissions;
- zero active/unknown position mutation intents;
- no newly arrived time-sensitive strategy message;
- no position currently being manually closed or rescued.

Old, known recovery rows must be listed separately and must not be mistaken for new work.

**Step 4: Deploy with rescue disabled**

Run the normal server update helper. Verify server SHA, service health and focused server
tests. Keep `trigger_protection_stop_rescue_mode=disabled` during the restart.

**Step 5: Enable shadow only after passive verification**

Set rescue mode to `shadow` only after confirming settings and allowlists are otherwise
unchanged. Verify the next natural eligible sample records the exact planned rescue with
zero Deepcoin write count.

Do not enable `live` in this implementation turn. Live rescue requires a later explicit
user approval after reviewed shadow evidence. Do not create a real Telegram signal,
position or trigger fill as a deployment test.

**Step 6: Record rollout evidence**

Record commit SHA, server SHA, service start time, focused test result, rescue mode, shadow
evaluation counts, and any verification that still depends on a natural message.
