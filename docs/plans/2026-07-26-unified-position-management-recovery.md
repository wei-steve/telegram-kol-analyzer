# Unified Position Management Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make explicit Telegram risk-reduction instructions execute deterministically across verified same-group positions, recover safely from transient identity/protection failures, and provide fingerprinted one-action-at-a-time repair of the current real holdings.

**Architecture:** Add a pure deterministic directive/scope layer in front of the existing candidate, instruction-item, management-batch, and exact-position execution pipeline. Keep verified `ExecutionOrderLeg.pos_id` and the protection ledger authoritative, extend the planner/executor only where current fail-closed behavior permanently loses valid risk-reduction work, and orchestrate current-position repair through recomputed fingerprinted plans that reuse the same production execution path.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, Typer, pytest, existing Deepcoin REST client and strategy-management worker.

---

## Global Safety Constraints

- Work locally first. Real verification runs only on the production server.
- Do not place, cancel, reduce, close, or modify a real order while implementing or testing.
- Use fake Deepcoin clients for every local test.
- Keep `ExecutionOrderLeg.pos_id` with `attribution_status="verified"` as the position authority.
- Never infer ownership from symbol, side, price, size, or time alone.
- Future fan-out is restricted to risk-reducing actions.
- Current holdings require one action ID, one exact target, one current fingerprint, and one explicit operator confirmation per apply.
- A write with an unknown result is never retried until exchange readback resolves it.
- Preserve unrelated dirty worktree files.
- Commit each completed task separately.
- Push reviewed commits to `codex/deepcoin-auto-trading-v1`; deploy by server pull, editable reinstall, and `telegram-kol.service` restart.

## Approved Behavioral Rules

```python
DEFAULT_PARTIAL_CLOSE_FRACTION = 0.50
DEFAULT_TAIL_CLOSE_FRACTION = 0.80
FANOUT_RISK_REDUCING_INTENTS = frozenset({
    "partial_take_profit",
    "partial_then_break_even",
    "move_stop_to_break_even",
    "adjust_stop_loss",
    "full_exit",
    "cancel_entry",
})
```

- Reply-to and explicit lifecycle identity narrow the action to one strategy.
- Otherwise, an explicit risk-reduction message targets every verified live lifecycle with the same `chat_id + symbol + side`.
- “止盈一部分” defaults to 50%.
- “只留一点尾仓” closes 80% and retains 20%.
- If one message offers tail retention or full exit, choose tail retention.
- Any target conflict, snapshot incompleteness, or non-reducing action fails closed.

### Task 1: Define Deterministic Management Directives

**Files:**
- Create: `src/telegram_kol_research/management_directives.py`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Test: `tests/test_management_directives.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Write failing directive-normalization tests**

Create `tests/test_management_directives.py` with table-driven tests for:

```python
def test_unspecified_partial_defaults_to_half():
    directive = resolve_management_directive(
        text="BTC多单止盈一部分，继续持有",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
            "management_action": "partial_take_profit",
        },
    )
    assert directive.intent == "partial_take_profit"
    assert directive.fraction == 0.5
    assert directive.risk_reducing is True


def test_tail_and_optional_exit_choose_tail_reduction():
    directive = resolve_management_directive(
        text="建议只留一点尾仓，求稳也可以出局",
        lifecycle_event={
            "event_type": "position_update",
            "symbol": "BTC",
            "side": "long",
        },
    )
    assert directive.intent == "partial_take_profit"
    assert directive.fraction == 0.8
    assert directive.cancel_deferred_entries is True


def test_break_even_is_risk_reducing_but_add_position_is_not():
    assert resolve_management_directive(
        text="BTC多单修改止损到成本保护",
        lifecycle_event={"event_type": "position_update", "symbol": "BTC", "side": "long"},
    ).risk_reducing
    assert not resolve_management_directive(
        text="BTC多单再加仓一半",
        lifecycle_event={"event_type": "position_update", "symbol": "BTC", "side": "long"},
    ).fanout_allowed
```

Also cover explicit percentages, “减半”, retained percentage, full exit, cancel entry, tighter explicit stop, stop widening, and general commentary such as “激进的可以做空”.

**Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_management_directives.py -q
```

Expected: collection fails because `management_directives` does not exist.

**Step 3: Implement the pure directive type**

Add:

```python
@dataclass(frozen=True, slots=True)
class ManagementDirective:
    intent: str
    fraction: float | None
    symbol: str | None
    side: str | None
    stop_loss: str | None
    risk_reducing: bool
    fanout_allowed: bool
    cancel_deferred_entries: bool
    reason_code: str


def resolve_management_directive(
    *,
    text: str,
    lifecycle_event: Mapping[str, Any],
) -> ManagementDirective:
    """Convert one authoritative lifecycle event into deterministic policy."""
```

Move reusable fraction parsing from `message_recognition.py` into this module. Apply precedence:

1. explicit cancel/full-exit;
2. tail-retention language;
3. explicit or implicit partial reduction;
4. break-even/tighter stop;
5. unsupported or risk-increasing action.

Return `fanout_allowed=False` for unsupported, risk-increasing, stop-widening, or commentary-only text.

**Step 4: Make recognition use the directive**

Replace direct calls to `normalize_management_intent()` at lifecycle application boundaries with `resolve_management_directive()`. Keep `normalize_management_intent()` as a compatibility wrapper returning `(intent, fraction)` so existing callers and tests continue to work.

**Step 5: Run focused tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_management_directives.py \
  tests/test_message_recognition.py \
  -k 'management or partial or break_even or full_exit' -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/management_directives.py \
  src/telegram_kol_research/message_recognition.py \
  tests/test_management_directives.py tests/test_message_recognition.py
git commit -m "feat: normalize deterministic risk reduction directives"
```

### Task 2: Resolve Exact and Same-Group Fan-Out Targets

**Files:**
- Create: `src/telegram_kol_research/management_scope.py`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Test: `tests/test_management_scope.py`
- Test: `tests/test_message_recognition.py`
- Test: `tests/test_message_instruction_items.py`

**Step 1: Write failing scope tests**

Cover:

```python
def test_reply_target_wins_over_group_fanout(...):
    targets = resolve_management_scope_in_session(
        session,
        raw_message=reply_message,
        directive=break_even_directive,
        explicit_target_lifecycle_id=None,
        reply_target_lifecycle_id=first.id,
    )
    assert [row.lifecycle_id for row in targets] == [first.id]


def test_unscoped_break_even_fans_out_same_chat_symbol_and_side(...):
    targets = resolve_management_scope_in_session(
        session,
        raw_message=message,
        directive=break_even_directive,
        explicit_target_lifecycle_id=None,
        reply_target_lifecycle_id=None,
    )
    assert [row.lifecycle_id for row in targets] == [first.id, second.id]


def test_fanout_excludes_other_chat_side_symbol_and_unverified_binding(...):
    ...


def test_add_position_never_fans_out(...):
    with pytest.raises(ManagementScopeError, match="risk_increasing_fanout_forbidden"):
        ...
```

Targets must be stable by lifecycle ID and must include the exact strategy instance ID.

**Step 2: Run and verify RED**

```bash
.venv/bin/pytest tests/test_management_scope.py -q
```

Expected: module import failure.

**Step 3: Implement scope resolution**

Add:

```python
@dataclass(frozen=True, slots=True)
class ManagementScopeTarget:
    lifecycle_id: int
    strategy_instance_id: str
    chat_id: int
    symbol: str
    side: str
    scope_source: str


def resolve_management_scope_in_session(
    session: Session,
    *,
    raw_message: RawMessage,
    directive: ManagementDirective,
    explicit_target_lifecycle_id: int | None,
    reply_target_lifecycle_id: int | None,
) -> tuple[ManagementScopeTarget, ...]:
    ...
```

For fan-out, require:

- lifecycle status `entered` or `holding`;
- same chat, normalized symbol, and normalized side;
- exactly one active/open Deepcoin binding for the strategy;
- at least one nonterminal verified entry leg with exact `pos_id`;
- no attribution conflict.

Pending-entry cancellation remains exact-target only unless the message explicitly says all pending entries for the group/symbol/side.

**Step 4: Project one authoritative candidate per target**

In `message_recognition.py`, after authoritative lifecycle parsing:

- resolve the deterministic directive;
- resolve exact or fan-out targets;
- call `_apply_lifecycle_event_decision()` once per target with
  `_explicit_multi_target=True` when there is more than one;
- assign the same authoritative generation to every projected candidate;
- record the scope source and directive reason in the candidate review note or evidence payload without changing the public signal schema.

This deterministic projection is allowed when the authoritative model produced a clear risk-reducing event but omitted `target_lifecycle_id`. It must replace the current terminal `mimo_authoritative_not_safely_applied` outcome only when the scope resolver proves every target.

**Step 5: Verify durable work items**

Add tests proving one `MessageInstructionItem` per projected lifecycle, stable ordering, stable idempotency keys, and no duplicate items after reprocessing the raw message.

**Step 6: Run focused suites**

```bash
.venv/bin/pytest \
  tests/test_management_scope.py \
  tests/test_message_recognition.py \
  tests/test_message_instruction_items.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/management_scope.py \
  src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/message_instruction_items.py \
  tests/test_management_scope.py tests/test_message_recognition.py \
  tests/test_message_instruction_items.py
git commit -m "feat: fan out verified group risk reductions"
```

### Task 3: Preserve Management Work Until Position Identity Becomes Visible

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `src/telegram_kol_research/models.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_message_instruction_items.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_db_bootstrap.py`

**Step 1: Add visibility-retry persistence**

Add nullable fields to `MessageInstructionItem`:

```python
visibility_retry_attempts: Mapped[int] = mapped_column(Integer, default=0)
visibility_next_attempt_at: Mapped[Optional[datetime]]
visibility_first_failed_at: Mapped[Optional[datetime]]
```

Add additive SQLite bootstrap migrations and schema tests.

**Step 2: Write failing production-regression tests**

Reproduce:

- a management candidate exists but its lifecycle binding is not attached yet;
- a unique binding with matching `strategy_instance_id` already exists and can be relinked;
- no binding is visible yet, so the instruction is deferred rather than terminally failed;
- reconciliation later creates the binding and the same item succeeds;
- duplicate bindings remain blocked permanently.

Expected assertions:

```python
assert result.status == "deferred"
assert result.reason_code == "target_strategy_binding_not_visible_yet"
assert item.status == "pending"
assert item.visibility_retry_attempts == 1
assert item.visibility_next_attempt_at > now
```

**Step 3: Run focused tests and verify RED**

```bash
.venv/bin/pytest \
  tests/test_strategy_management_planner.py \
  tests/test_message_instruction_items.py \
  tests/test_strategy_management_worker.py \
  -k 'binding_not_visible or visibility_retry or relink' -q
```

Expected: failures show current terminal `target_strategy_binding_not_found`.

**Step 4: Implement exact binding recovery**

In `_load_exact_identity()`:

- first use `lifecycle.execution_binding_id`;
- if absent, compute the exact strategy instance ID and query active/open Deepcoin bindings;
- attach only when exactly one binding matches chat, message, symbol, side, and strategy ID;
- return a retryable visibility result when no binding exists;
- return a terminal conflict when more than one binding exists.

Do not match by symbol/side alone.

**Step 5: Implement bounded deferred retry**

Update instruction-item claiming so pending items with a future
`visibility_next_attempt_at` are skipped. Use bounded exponential backoff and a deadline long enough to cover delayed trigger fills. A retry must run execution-binding reconciliation before replanning.

After the deadline, mark the item failed with a high-priority operator notification; never discard it silently.

**Step 6: Run focused and full suites**

```bash
.venv/bin/pytest \
  tests/test_db_bootstrap.py \
  tests/test_message_instruction_items.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_worker.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py \
  src/telegram_kol_research/message_instruction_items.py \
  src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_worker.py \
  tests/test_db_bootstrap.py tests/test_message_instruction_items.py \
  tests/test_strategy_management_planner.py tests/test_strategy_management_worker.py
git commit -m "fix: retain management work until position visibility"
```

### Task 4: Let Safe Risk Reduction Recover Exact Protection

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_strategy_management_reconciliation.py`
- Test: `tests/test_protection_revisions.py`

**Step 1: Write failing planner tests**

Cover:

- `partial_then_break_even` with an open protection incident but exact current ledger/order IDs creates a ready recovery-aware batch;
- the same action with unattributed or conflicting protection remains blocked;
- each `posId` snapshots only its exact protection rows;
- full exit continues to use the existing stricter bypass marker.

Expected snapshot fragment:

```python
assert snapshot["protection_recovery"]["mode"] == "replace_after_reduction"
assert snapshot["protection_recovery"]["positions"][0]["pos_id"] == "pos-1"
assert snapshot["protection_recovery"]["positions"][0]["owned_order_ids"] == ["sl-1", "tp-1"]
```

**Step 2: Verify RED**

```bash
.venv/bin/pytest tests/test_strategy_management_planner.py \
  -k 'risk_reduction_protection_recovery' -q
```

Expected: blocked with `protection_recovery_required`.

**Step 3: Narrow the planner gate**

Replace the broad incident block with exact-position eligibility:

- every targeted entry leg is verified;
- live position is unique and economically compatible;
- every protection order to be touched has an exact active ledger mapping;
- the pending TPSL snapshot is complete;
- no order is claimed by another position;
- the action reduces risk.

Persist a recovery marker and the exact order allowlist in the immutable batch snapshot.

**Step 4: Write failing executor tests**

Test this exact ordering:

1. verify current position and protection;
2. cancel only snapshotted old protection;
3. submit partial close;
4. read actual remaining size;
5. set break-even stop and converge remaining TP sizes;
6. record completed only after readback.

Also test cancellation failure, close timeout, partial fill, protection rebuild failure, and restoration of old protection when no close request was submitted.

**Step 5: Implement the recovery-aware execution path**

Add a private executor helper:

```python
def _execute_risk_reduction_with_protection_recovery(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime,
) -> dict[str, Any]:
    ...
```

It must use only batch-snapshotted `posId` and order IDs, persist each write before sending, and delegate final protection creation to existing stop/TP convergence components.

**Step 6: Reconcile partial results**

Update reconciliation to distinguish:

- close not submitted: old protection may be restored;
- close result unknown: read back position/fills, never resubmit;
- close succeeded but protection incomplete: keep reduced position and converge protection;
- all complete: finalize the management batch.

**Step 7: Run suites**

```bash
.venv/bin/pytest \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_protection_revisions.py -q
```

Expected: PASS.

**Step 8: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/strategy_management_reconciliation.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_protection_revisions.py
git commit -m "fix: recover exact protection around risk reduction"
```

### Task 5: Make Multi-Position Execution Independent and Idempotent

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_strategy_management_reconciliation.py`
- Test: `tests/test_strategy_management_worker.py`

**Step 1: Add failing per-position isolation tests**

Create a two-position batch where:

- position A completes;
- position B fails before any write;
- the batch remains reconciling/partial_failed;
- A is never replayed;
- B can be retried only from its durable leg state.

Add a second test where B has an unknown exchange result; worker restart must read back B and issue zero additional close calls.

**Step 2: Verify RED**

```bash
.venv/bin/pytest \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_worker.py \
  -k 'independent_position or restart_unknown' -q
```

**Step 3: Refactor execution around durable leg state**

Iterate management legs in stable `leg_index, id` order. Before any call:

- skip succeeded/restored legs;
- reconcile unknown/recovery legs;
- execute only planned eligible legs;
- catch a target-local preflight error and persist it without abandoning untouched targets;
- stop the entire batch only for global snapshot/identity corruption.

**Step 4: Keep idempotency at the write boundary**

Derive client order IDs from batch ID, action, and management leg ID. Require a unique existing execution event or exchange readback before a retry.

**Step 5: Run full management suites**

```bash
.venv/bin/pytest \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_worker.py \
  tests/test_strategy_management_batches.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/strategy_management_reconciliation.py \
  src/telegram_kol_research/strategy_management_worker.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_worker.py
git commit -m "fix: isolate multi-position management execution"
```

### Task 6: Build Fingerprinted Current-Holding Remediation

**Files:**
- Create: `src/telegram_kol_research/position_management_remediation.py`
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_position_management_remediation.py`
- Test: `tests/test_cli_smoke.py`
- Test: `tests/test_recovery_live_submit_gate.py`

**Step 1: Write failing pure-plan tests**

Define immutable dataclasses:

```python
@dataclass(frozen=True, slots=True)
class PositionRemediationAction:
    action_id: str
    action_kind: str
    raw_message_id: int | None
    lifecycle_id: int
    strategy_instance_id: str
    pos_ids: tuple[str, ...]
    expected_effect: dict[str, Any]
    evidence: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PositionRemediationPlan:
    snapshot_fingerprint: str
    actions: tuple[PositionRemediationAction, ...]
    conflicts: tuple[dict[str, Any], ...]
```

Tests must reproduce the approved current cases using fixtures:

- cancelled-before-fill becomes exact full exit;
- announced take-profit becomes exact full exit;
- partial plus break-even becomes 50% plus protection rebuild;
- unscoped break-even fans out only to verified same-group positions;
- tail message retains 20% and cancels deferred entry legs;
- protection-only targets preserve quantity;
- snapshot change invalidates apply.

**Step 2: Run and verify RED**

```bash
.venv/bin/pytest tests/test_position_management_remediation.py -q
```

**Step 3: Implement read-only planning**

`build_position_management_remediation_plan()` must:

- capture one coherent Deepcoin reconciliation snapshot;
- find failed/skipped historical authoritative management instructions;
- re-run the deterministic directive and scope rules;
- include exact verified target entry legs and live `posId`s;
- classify protection-only repair candidates;
- include conflicts rather than weakening evidence;
- compute canonical JSON fingerprints.

Planning performs no database or exchange write.

**Step 4: Implement one-action apply**

Add:

```python
def apply_position_management_remediation_action(
    session_factory,
    *,
    deepcoin_client,
    action_id: str,
    expected_fingerprint: str,
    now: datetime,
) -> RemediationApplyResult:
    ...
```

The function recomputes the entire plan from a fresh snapshot, requires an exact action/fingerprint match, projects or reuses the durable instruction item, and invokes the normal planner/worker path. It must reject multi-action apply.

**Step 5: Add CLI**

Add:

```text
telegram-kol-research repair-position-management
telegram-kol-research repair-position-management \
  --apply \
  --action-id <one-action-id> \
  --expected-fingerprint <one-action-fingerprint>
```

Dry-run output must redact secrets and show message, group, lifecycle, `posId`, current size/average, proposed orders, before/after protection, reason, and expiration conditions.

**Step 6: Test the live-submit gate**

Prove:

- `--apply` without both exact arguments exits 2;
- stale fingerprint exits 2 before any exchange call;
- conflict exits 2;
- one action cannot execute another action’s target;
- dry-run produces zero exchange writes;
- apply still respects the global live-trading and management execution gates.

**Step 7: Run tests**

```bash
.venv/bin/pytest \
  tests/test_position_management_remediation.py \
  tests/test_cli_smoke.py \
  tests/test_recovery_live_submit_gate.py -q
```

Expected: PASS.

**Step 8: Commit**

```bash
git add src/telegram_kol_research/position_management_remediation.py \
  src/telegram_kol_research/cli.py \
  tests/test_position_management_remediation.py \
  tests/test_cli_smoke.py tests/test_recovery_live_submit_gate.py
git commit -m "feat: add fingerprinted position management remediation"
```

### Task 7: Classify and Repair Historical Protection Orders Conservatively

**Files:**
- Create: `src/telegram_kol_research/protection_order_cleanup.py`
- Modify: `src/telegram_kol_research/position_management_remediation.py`
- Test: `tests/test_protection_order_cleanup.py`
- Test: `tests/test_position_management_remediation.py`

**Step 1: Add classification tests**

Required outcomes:

```python
"keep_verified"
"backfill_exact_ledger"
"suggest_cancel_obsolete"
"leave_unattributed"
"conflict"
```

Test that:

- exact saved request `posId` plus response `ordId` and current pending readback permits ledger backfill;
- absent position plus terminal strategy and exact order ownership permits suggested cancellation;
- replacement proof permits suggested cancellation;
- same symbol/side/price without exact identity stays unattributed;
- conflicting direct exchange position ID and ledger becomes conflict.

**Step 2: Verify RED**

```bash
.venv/bin/pytest tests/test_protection_order_cleanup.py -q
```

**Step 3: Implement pure classification**

The classifier consumes already captured positions, pending TPSL, verified entry legs, protection ledgers, saved request/response evidence, and terminal lifecycle state. It performs no reads or writes itself.

**Step 4: Integrate remediation actions**

Ledger backfill and obsolete cancellation become separate one-action remediation items. Unattributed/conflict rows appear in the report but cannot be applied.

**Step 5: Run tests**

```bash
.venv/bin/pytest \
  tests/test_protection_order_cleanup.py \
  tests/test_position_management_remediation.py \
  tests/test_entry_protection_ledger_repair.py \
  tests/test_current_protection_backfill.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/protection_order_cleanup.py \
  src/telegram_kol_research/position_management_remediation.py \
  tests/test_protection_order_cleanup.py \
  tests/test_position_management_remediation.py
git commit -m "feat: classify historical protection cleanup safely"
```

### Task 8: Add Shadow Replay and a Management Write Circuit Breaker

**Files:**
- Create: `src/telegram_kol_research/management_shadow_replay.py`
- Create: `src/telegram_kol_research/management_write_gate.py`
- Modify: `src/telegram_kol_research/strategy_management_worker.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Test: `tests/test_management_shadow_replay.py`
- Test: `tests/test_management_write_gate.py`
- Test: `tests/test_strategy_management_worker.py`
- Test: `tests/test_system_operator_bot.py`

**Step 1: Write shadow replay tests**

Replay fixtures equivalent to messages `#1465`, `#3381`, `#1137`, `#9719`, and
`#4102`. Assert the normalized actions and targets, and assert zero exchange write calls.

**Step 2: Implement bounded replay**

Add CLI:

```text
telegram-kol-research replay-management-shadow \
  --database-path data/research.db \
  --raw-message-id <repeatable>
```

It must use a scratch database copy or rollback-only session, current read-only Deepcoin snapshot, and `execution_mode="shadow"`.

**Step 3: Write circuit-breaker tests**

Trip the gate when any of these are present:

- unresolved unknown exchange outcome;
- duplicate idempotency identity;
- current attribution conflict;
- incomplete Deepcoin position/TPSL snapshot.

Prove the worker continues readback/reconciliation but claims no new ready live batch. Shadow planning and Telegram ingestion remain active.

**Step 4: Implement the gate**

Add:

```python
@dataclass(frozen=True, slots=True)
class ManagementWriteGateResult:
    allowed: bool
    reason_codes: tuple[str, ...]


def evaluate_management_write_gate(
    session_factory,
    *,
    reconciliation_snapshot,
) -> ManagementWriteGateResult:
    ...
```

The gate is read-only. It does not mutate trading settings or stop the service.

**Step 5: Add operator notification**

Emit one deduplicated high-priority notification when the gate transitions from allowed to blocked, and one recovery notification when it becomes allowed again.

**Step 6: Run tests**

```bash
.venv/bin/pytest \
  tests/test_management_shadow_replay.py \
  tests/test_management_write_gate.py \
  tests/test_strategy_management_worker.py \
  tests/test_system_operator_bot.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/management_shadow_replay.py \
  src/telegram_kol_research/management_write_gate.py \
  src/telegram_kol_research/strategy_management_worker.py \
  src/telegram_kol_research/cli.py \
  src/telegram_kol_research/system_operator_bot.py \
  tests/test_management_shadow_replay.py \
  tests/test_management_write_gate.py \
  tests/test_strategy_management_worker.py \
  tests/test_system_operator_bot.py
git commit -m "feat: gate live management with shadow replay"
```

### Task 9: Full Regression, Documentation, and Review

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`
- Test: all focused files from Tasks 1–8

**Step 1: Document operator workflows**

Add exact commands for:

- setting management execution mode to shadow;
- replaying historical messages;
- generating remediation dry-run;
- applying exactly one action;
- verifying Deepcoin readback;
- responding to `recovery_required`;
- rolling management writes back to shadow without disabling Telegram ingestion.

State explicitly that historical action fingerprints expire on any snapshot change.

**Step 2: Run focused regression**

```bash
.venv/bin/pytest \
  tests/test_management_directives.py \
  tests/test_management_scope.py \
  tests/test_message_recognition.py \
  tests/test_message_instruction_items.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_strategy_management_worker.py \
  tests/test_position_management_remediation.py \
  tests/test_protection_order_cleanup.py \
  tests/test_management_shadow_replay.py \
  tests/test_management_write_gate.py \
  tests/test_system_operator_bot.py \
  tests/test_db_bootstrap.py \
  tests/test_cli_smoke.py -q
```

Expected: PASS.

**Step 3: Run broader safety regression**

```bash
.venv/bin/pytest \
  tests/test_auto_trade_execution.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_execution_bindings.py \
  tests/test_position_attribution.py \
  tests/test_protection_attribution.py \
  tests/test_protection_ledger.py \
  tests/test_backup_stop_repair.py \
  tests/test_position_take_profit_orders.py \
  tests/test_production_safety_monitor.py -q
```

Expected: PASS.

**Step 4: Review**

```bash
git diff --check
git status --short
git log --oneline -12
```

Review every exchange write boundary for:

- exact target identity;
- persisted submitting state;
- deterministic client order ID;
- readback before retry;
- no fan-out for risk-increasing actions.

**Step 5: Commit documentation**

```bash
git add docs/runbook.md docs/migration-handoff.md
git commit -m "docs: add unified management recovery runbook"
```

### Task 10: Production Shadow Rollout

**Files:**
- Verify: `scripts/server_git_update.ps1`
- Verify: production `/opt/telegram-kol-analyzer`

**Step 1: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds and remote branch points to reviewed HEAD.

**Step 2: Capture pre-deployment read-only baseline**

On the server:

```bash
cd /opt/telegram-kol-analyzer
systemctl is-active telegram-kol.service
git rev-parse HEAD
.venv/bin/python scripts/readonly_crosscheck_inspect.py
```

Also record current database/WAL/SHM component fingerprints using the existing runbook procedure. Do not copy credentials or the database off the server.

**Step 3: Deploy through the approved helper**

From the local workstation:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: server pulls the reviewed branch, reinstalls editable package, and restarts `telegram-kol.service`.

**Step 4: Verify service and focused server tests**

```bash
cd /opt/telegram-kol-analyzer
systemctl is-active telegram-kol.service
git rev-parse HEAD
.venv/bin/pytest \
  tests/test_management_directives.py \
  tests/test_management_scope.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_position_management_remediation.py \
  tests/test_management_write_gate.py -q
```

Expected: active service, expected SHA, all tests pass.

**Step 5: Force shadow mode**

Use the existing trading-settings interface to set:

```json
{"management_execution_mode": "shadow"}
```

Confirm the API and database both report `shadow` before replaying any message.

**Step 6: Replay the five production regressions**

Run shadow replay for raw messages corresponding to `#1465`, `#3381`, `#1137`,
`#9719`, and `#4102`.

Expected:

- `#1465`: cancel-entry late-fill guard/full-exit repair target;
- `#3381`: exact full exit;
- `#1137`: 50% partial close plus break-even;
- `#9719`: same-group verified break-even fan-out;
- `#4102`: retain 20% and cancel deferred entry legs.

Verify exchange write count is zero.

**Step 7: Observe new live messages in shadow**

Keep the service in shadow for at least one complete configured message cycle. Compare old recognition, new directive, resolved scope, management plan, and operator notification. Any unsafe or unexpected target blocks promotion.

### Task 11: Enable Future Risk Reduction and Repair Current Holdings

**Files:**
- Operate: production only after Task 10 passes

**Step 1: Capture a fresh coherent baseline**

Re-run positions, open orders, pending TPSL, ledger, management queue, circuit-breaker state, and service health. Stop if any snapshot is incomplete.

**Step 2: Enable future management live mode**

Set only `management_execution_mode` to `live`. Do not change entry trading gates or group risk budgets.

Immediately confirm:

- write gate allowed;
- no old shadow batch is eligible for automatic execution;
- only new messages or explicitly applied remediation actions can write.

**Step 3: Generate the current remediation plan**

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research repair-position-management \
  --database-path data/research.db
```

Expected: dry-run only.

**Step 4: Present one action at a time for operator approval**

For each approved action, show:

- group and source message;
- strategy and exact `posId`;
- current quantity/average;
- exact orders to cancel or submit;
- before/after protection;
- action fingerprint.

Do not proceed to the next action without explicit user approval.

**Step 5: Apply one action**

```bash
.venv/bin/telegram-kol-research repair-position-management \
  --database-path data/research.db \
  --apply \
  --action-id '<ACTION_ID>' \
  --expected-fingerprint '<FINGERPRINT>'
```

Expected: exactly one target action starts.

**Step 6: Read back immediately**

After every action:

- fetch positions and pending orders;
- verify target quantity;
- verify deferred entries are gone when required;
- verify primary stop, second stop, and planned TPs by exact ledger/order ID;
- inspect management batch, legs, execution events, and notification;
- regenerate the remediation plan.

If the next plan differs unexpectedly, stop and return management mode to shadow.

**Step 7: Apply the approved current actions in priority order**

Priority:

1. cancelled strategy with late fill (`#1462`);
2. announced exit still open (`#3374`);
3. missed 50% plus break-even (`#1136`);
4. missed break-even (`#9701`);
5. cancel deferred 25-contract leg while retaining the `#4101` tail;
6. protection-only repairs for remaining positions;
7. exact ledger backfills;
8. individually approved obsolete-order cancellations.

**Step 8: Restart idempotency check**

```bash
sudo systemctl restart telegram-kol.service
systemctl is-active telegram-kol.service
```

Run one reconciliation cycle and verify zero duplicate exchange writes.

**Step 9: Final acceptance**

Require all:

- every remaining automatic position has verified exact ownership;
- primary stop, second stop, and all planned TPs are verified;
- no cancelled strategy has a deferred entry leg;
- explicit group risk reduction no longer skips multi-position targets;
- circuit breaker is allowed;
- service is active at the expected SHA;
- restart and resync produce zero duplicate writes.

