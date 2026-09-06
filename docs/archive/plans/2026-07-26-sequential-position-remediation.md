# Sequential Position-Management Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replay missed position-management instructions in deterministic per-strategy order, exposing and applying only one safe chain head at a time.

**Architecture:** Extend the existing read-only remediation planner with immutable chain and step records. Build all candidate steps first, group them by exact strategy identity, order them by source time and instruction sequence, then classify only the first unresolved step as executable. Apply rebuilds the plan and accepts only that exact head; reconciliation and a fresh plan are mandatory before the next step.

**Tech Stack:** Python 3.12+, SQLAlchemy, SQLite, Typer, pytest, existing Deepcoin reconciliation/planner/executor state machines.

---

### Task 1: Represent Deterministic Remediation Chains

**Files:**
- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Test: `tests/test_position_management_remediation.py`

**Step 1: Write failing ordering and chain-shape tests**

Add fixtures with two active strategies and multiple failed management
instructions. Deliberately insert database rows out of chronological order.

```python
def test_plan_groups_steps_by_strategy_and_orders_by_source_time(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first = persist_failed_management(
        session_factory,
        strategy_instance_id="deepcoin:1:10:BTC:long",
        posted_at=NOW,
        sequence=1,
        action="move_stop_to_break_even",
    )
    second = persist_failed_management(
        session_factory,
        strategy_instance_id="deepcoin:1:10:BTC:long",
        posted_at=NOW + timedelta(minutes=1),
        sequence=0,
        action="full_exit",
    )

    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client
    )

    chain = next(
        row for row in plan.chains
        if row.strategy_instance_id == "deepcoin:1:10:BTC:long"
    )
    assert [step.raw_message_id for step in chain.steps] == [first, second]
    assert [step.state for step in chain.steps] == [
        "ready_for_approval",
        "waiting_for_predecessor",
    ]
```

Also assert that the stable order key is:

```python
(
    raw_message.posted_at,
    raw_message.id,
    instruction_item.sequence,
    candidate.id,
)
```

**Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest -q \
  tests/test_position_management_remediation.py::test_plan_groups_steps_by_strategy_and_orders_by_source_time
```

Expected: FAIL because `PositionRemediationPlan` has no chain representation
and every candidate is currently emitted as an independent action.

**Step 3: Add immutable chain and step records**

Introduce:

```python
@dataclass(frozen=True, slots=True)
class PositionRemediationStep:
    raw_message_id: int
    instruction_item_id: int | None
    candidate_id: int
    sequence: int
    posted_at: datetime
    action_kind: str
    state: str
    reason: str | None
    action: PositionRemediationAction | None


@dataclass(frozen=True, slots=True)
class PositionRemediationChain:
    strategy_instance_id: str
    lifecycle_id: int
    execution_binding_id: int
    steps: tuple[PositionRemediationStep, ...]
    conflicts: tuple[dict[str, Any], ...]
    fingerprint: str
```

Add `chains` to `PositionRemediationPlan`. Preserve the top-level `actions`
tuple as a compatibility projection containing only executable chain heads.

Build internal unresolved-step drafts first, then group and sort them before
creating any action fingerprint.

**Step 4: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_position_management_remediation.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/position_management_remediation.py \
  tests/test_position_management_remediation.py
git commit -m "feat: order remediation steps by strategy"
```

### Task 2: Convert Missed Entry Cancellation After a Late Fill

**Files:**
- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Modify: `src/telegram_kol_research/management_directives.py` only if a shared pure helper is required
- Test: `tests/test_position_management_remediation.py`

**Step 1: Write the failing late-fill tests**

Cover both sides:

```python
def test_cancel_entry_with_exact_live_fill_becomes_full_exit(tmp_path):
    # Source says cancel entry, but the exact verified entry leg now has a
    # live posId in the coherent exchange snapshot.
    plan = build_plan(...)
    action = plan.actions[0]
    assert action.action_kind == "full_exit"
    assert action.evidence["original_action_kind"] == "cancel_entry"
    assert action.evidence["late_fill_conversion"] is True


def test_cancel_entry_without_exact_live_fill_never_becomes_full_exit(tmp_path):
    plan = build_plan_with_binding_or_pos_drift(...)
    assert plan.actions == ()
    assert chain.conflicts[0]["reason"] == "late_fill_identity_not_exact"
```

Assert conversion checks binding ID, strategy ID, verified entry-leg ID,
`posId`, instrument, side, and current live position. Add an extra same-side
position and prove it is never included.

**Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_position_management_remediation.py \
  -k 'late_fill or cancel_entry'
```

Expected: FAIL because `cancel_entry` is currently emitted even though the
normal management planner does not support it.

**Step 3: Implement exact late-fill conversion**

Before constructing the step action:

```python
effective_intent = directive.intent
late_fill_conversion = False
if directive.intent == "cancel_entry":
    if exact_live_position_identity:
        effective_intent = "full_exit"
        late_fill_conversion = True
    else:
        emit_chain_conflict("late_fill_identity_not_exact")
```

Persist the original and effective intents in evidence. The canonical projected
candidate must use `full_exit`, and its exact target lifecycle must not change.

**Step 4: Run focused and planner tests**

Run:

```bash
uv run pytest -q \
  tests/test_position_management_remediation.py \
  tests/test_strategy_management_planner.py -k 'late_fill or full_exit or remediation'
```

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/position_management_remediation.py \
  tests/test_position_management_remediation.py
git commit -m "fix: close exact late-filled cancelled entries"
```

### Task 3: Enforce Head-Only Apply and Full-Exit Termination

**Files:**
- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py` only if the existing durable terminal state is insufficient
- Test: `tests/test_position_management_remediation.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Step 1: Write failing chain-gate tests**

Add:

```python
def test_waiting_step_cannot_be_applied_with_its_own_or_head_fingerprint(...):
    plan = build_two_step_plan(...)
    waiting = plan.chains[0].steps[1]
    with pytest.raises(ValueError, match="not executable chain head"):
        apply_position_management_remediation_action(
            session_factory,
            action_id=waiting.action_id,
            expected_fingerprint=waiting.fingerprint,
            ...
        )


def test_confirmed_full_exit_terminalizes_later_old_lifecycle_steps(...):
    mark_head_full_exit_succeeded_and_reconciled(...)
    plan = build_position_management_remediation_plan(...)
    assert plan.actions == ()
    assert [step.state for step in plan.chains[0].steps] == [
        "resolved",
        "terminally_skipped",
        "terminally_skipped",
    ]
```

Also cover `ready`, `executing`, `reconciling`, `partial_failed`,
`recovery_required`, and `succeeded` predecessor batches. Only a durably
resolved predecessor advances the chain.

**Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_position_management_remediation.py \
  -k 'chain_head or predecessor or terminal'
```

Expected: FAIL because apply currently locates any top-level action without a
predecessor state.

**Step 3: Implement head classification**

Classify steps in order:

- existing succeeded/reconciled effect: `resolved`;
- same old lifecycle after confirmed full exit: `terminally_skipped`;
- first unresolved safe step: `ready_for_approval`;
- later step: `waiting_for_predecessor`;
- earlier unresolved/unknown batch: chain conflict and no executable head.

Include this immutable predecessor summary in the head fingerprint:

```python
"predecessors": [
    {
        "raw_message_id": step.raw_message_id,
        "state": step.state,
        "management_batch_id": step.management_batch_id,
        "batch_status": step.batch_status,
    }
    for step in prior_steps
]
```

Apply must rebuild the plan and select from `plan.actions`, which now contains
only chain heads.

**Step 4: Verify GREEN**

Run:

```bash
uv run pytest -q \
  tests/test_position_management_remediation.py \
  tests/test_strategy_management_reconciliation.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/position_management_remediation.py \
  tests/test_position_management_remediation.py \
  tests/test_strategy_management_reconciliation.py
git commit -m "fix: gate remediation by predecessor state"
```

### Task 4: Isolate Conflicts Per Strategy Chain

**Files:**
- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_position_management_remediation.py`
- Test: `tests/test_cli_smoke.py`

**Step 1: Write failing isolation tests**

```python
def test_conflicted_chain_does_not_block_unrelated_exact_head(...):
    plan = build_one_conflicted_and_one_exact_chain(...)
    assert len(plan.actions) == 1
    assert plan.actions[0].strategy_instance_id == "deepcoin:exact"

    result = apply_position_management_remediation_action(
        ...,
        action_id=plan.actions[0].action_id,
        expected_fingerprint=plan.actions[0].fingerprint,
    )
    assert result.action_id == plan.actions[0].action_id
```

Add the inverse: a conflict at or before the requested chain head prevents that
chain from exposing an action. A later conflict remains visible and does not
invalidate the current predecessor.

**Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest -q \
  tests/test_position_management_remediation.py \
  tests/test_cli_smoke.py -k 'conflict or repair_position_management'
```

Expected: FAIL because both CLI and apply reject any non-empty global
`plan.conflicts`.

**Step 3: Remove global conflict gating**

Keep top-level conflicts for reader compatibility, but make them a flattened
view of chain-local conflicts plus truly global exchange snapshot failures.

Rules:

- an incomplete coherent exchange snapshot remains a global blocker;
- an exact requested head is blocked only by its chain conflicts at or before
  the head;
- CLI `--apply` delegates this decision to the apply function instead of
  rejecting every non-empty conflict list.

Add reader output showing chain state and conflict ownership.

**Step 4: Verify GREEN**

Run:

```bash
uv run pytest -q \
  tests/test_position_management_remediation.py \
  tests/test_cli_smoke.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add \
  src/telegram_kol_research/position_management_remediation.py \
  src/telegram_kol_research/cli.py \
  tests/test_position_management_remediation.py \
  tests/test_cli_smoke.py
git commit -m "fix: isolate remediation conflicts by strategy"
```

### Task 5: Add Production-Derived Sequential Regression Coverage

**Files:**
- Modify: `tests/test_position_management_remediation.py`
- Modify: `docs/server-deployment.md`
- Modify: `docs/migration-handoff.md`

**Step 1: Add redacted regression chains**

Create database fixtures that preserve semantics without private message text:

- cancelled strategy followed by a late exact fill;
- break-even, full exit, then a later old-lifecycle protection instruction;
- one exact full exit;
- one 50% partial close plus break-even;
- one unrelated historical conflict.

Assert:

- late fill head is exact `full_exit`;
- only one head per strategy is executable;
- later steps wait;
- confirmed exit stops the old chain;
- unrelated conflict does not block an exact chain.

**Step 2: Run regression tests**

Run:

```bash
uv run pytest -q tests/test_position_management_remediation.py
```

Expected: PASS after Tasks 1–4; the new fixtures guard the production failure
shape.

**Step 3: Document the server shadow procedure**

Document:

1. deploy reviewed code;
2. force management execution mode to `shadow`;
3. replay known production raw-message IDs without exchange writes;
4. verify chain ordering and exact target identity;
5. generate dry run;
6. present one chain head;
7. apply only after explicit approval;
8. reconcile and regenerate before the next action.

Do not document or run a bulk apply command.

**Step 4: Run the complete relevant suite**

Run:

```bash
uv run pytest -q \
  tests/test_management_directives.py \
  tests/test_management_scope.py \
  tests/test_message_instruction_items.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_position_management_remediation.py \
  tests/test_cli_smoke.py
python3 -m compileall -q src/telegram_kol_research
git diff --check
```

Expected: PASS.

**Step 5: Request independent review**

Review:

- deterministic ordering and tie-breakers;
- exact late-fill identity;
- head-only action fingerprints;
- terminal full-exit behavior;
- chain-local conflict isolation;
- no exchange write in dry-run or shadow;
- every exchange write remains behind the live gate.

**Step 6: Commit**

```bash
git add \
  tests/test_position_management_remediation.py \
  docs/server-deployment.md \
  docs/migration-handoff.md
git commit -m "test: cover sequential remediation replay"
```

### Task 6: Production Shadow Validation

**Files:**
- Verify: production `/opt/telegram-kol-analyzer`

**Step 1: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: remote branch contains every reviewed task commit.

**Step 2: Capture server baseline**

Run read-only:

```bash
cd /opt/telegram-kol-analyzer
systemctl is-active telegram-kol.service
git rev-parse HEAD
.venv/bin/python scripts/readonly_crosscheck_inspect.py
```

Expected: active service and complete Deepcoin snapshot.

**Step 3: Deploy in shadow mode**

Back up the production database, apply schema initialization, ensure
`management_execution_mode=shadow`, then restart through the approved server
update helper. Do not change the entry-trading gate.

**Step 4: Run focused server tests**

```bash
.venv/bin/pytest -q \
  tests/test_position_management_remediation.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py
```

Expected: PASS.

**Step 5: Replay and inspect without writes**

Replay the approved production regression messages in shadow, verify exchange
write count is zero, and inspect per-strategy chains.

Expected:

- no unsupported `cancel_entry` head for a verified late fill;
- one executable head per strategy;
- later steps wait;
- conflicts are chain-local;
- no action is applied.

**Step 6: Return for operator approval**

Present the exact first head for each strategy with source message, `posId`,
current quantity/average, planned request, protection effect, and fingerprint.
Stop before any `--apply`.
