# Historical Position Attribution Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing audited Deepcoin attribution repair workflow so it can plan and safely apply evidence-backed historical cleanup, then deploy only through a fresh production dry-run review gate.

**Architecture:** Add a focused pure historical-cleanup planner that consumes the coherent exchange snapshot plus local execution history and returns deterministic cleanup actions or unresolved conflicts. Integrate those actions into the existing fingerprinted repair plan, apply them transactionally with immutable audits, and create the partial unique ownership index only after the same transaction removes every duplicate. Keep exchange mutations entirely out of the workflow.

**Tech Stack:** Python 3.12+, SQLAlchemy 2, SQLite, Typer, pytest, Deepcoin authenticated read-only REST APIs.

## Global Constraints

- Default execution remains dry-run; a nonempty apply requires the exact reviewed fingerprint.
- Missing or failed exchange evidence produces no cleanup action.
- Current live positions, pending orders, trigger orders, and position-linked TPSL orders cannot be cleaned.
- Position absence alone never proves ownership or terminal state.
- Research-only lifecycles without an execution binding remain untouched.
- No code path may submit, cancel, close, reduce, bind, or modify a Deepcoin order or position.
- Production work stops after the fresh post-deployment dry run until the operator separately approves its exact nonzero actions and fingerprint.

---

## File Structure

- Create `src/telegram_kol_research/historical_attribution_cleanup.py`: pure eligibility, component grouping, deterministic historical cleanup actions, and state-transition helpers.
- Create `tests/test_historical_attribution_cleanup.py`: focused unit tests for eligibility, ambiguity, terminal evidence, pending-order gates, ordering, and research-only exclusions.
- Modify `src/telegram_kol_research/position_attribution_repair.py`: integrate historical actions/conflicts, fingerprint all relevant local evidence, and apply cleanup transactionally.
- Modify `tests/test_position_attribution_repair.py`: integration, fingerprint drift, audit, rollback, idempotency, and live-position exclusion tests.
- Modify `src/telegram_kol_research/db.py`: expose one shared unique-index SQL constant/helper without weakening legacy bootstrap behavior.
- Modify `tests/test_db_bootstrap.py`: prove index creation after cleanup and duplicate rejection.
- Modify `src/telegram_kol_research/cli.py`: treat historical actions as nonempty repair work and preserve the existing expected-fingerprint gate.
- Modify `docs/runbook.md` and `docs/server-deployment.md`: document the historical dry-run output, unresolved-conflict behavior, unique-index verification, and production stop point.

---

### Task 1: Pure Historical Cleanup Planner

**Files:**
- Create: `src/telegram_kol_research/historical_attribution_cleanup.py`
- Create: `tests/test_historical_attribution_cleanup.py`

**Interfaces:**
- Consumes: `ExecutionBinding`, `ExecutionOrderLeg`, `StrategyLifecycle`, `ExecutionEvent`, `BoundPositionCloseReservation`, `TERMINAL_ENTRY_LEG_STATES`, and one already-loaded reconcile snapshot.
- Produces: `HistoricalCleanupAction`, `HistoricalCleanupConflict`, and `plan_historical_attribution_cleanup(...) -> HistoricalCleanupDecision`.

- [ ] **Step 1: Write failing tests for deterministic duplicate components and exact historical owners**

```python
def test_same_binding_duplicate_retains_only_exact_direct_owner():
    decision = plan_historical_attribution_cleanup(
        bindings=[binding(id=10, status="unknown")],
        legs=[
            leg(id=1, binding_id=10, pos_id="p1", order_id="p1", status="manually_closed"),
            leg(id=2, binding_id=10, pos_id="p1", order_id="child-2", status="manually_closed"),
        ],
        lifecycles=[lifecycle(binding_id=10, status="exited", exit_reason="manual")],
        events=[],
        reservations=[],
        snapshot=empty_successful_snapshot(),
    )

    assert [action.action for action in decision.actions] == [
        "clear_redundant_historical_position"
    ]
    assert decision.actions[0].leg_id == 2
    assert decision.actions[0].old_pos_id == "p1"
    assert decision.conflicts == ()


def test_cross_binding_duplicate_without_unique_authority_is_unresolved():
    decision = plan_historical_attribution_cleanup(
        bindings=[binding(id=10), binding(id=11)],
        legs=[
            leg(id=1, binding_id=10, pos_id="p1", order_id="a"),
            leg(id=2, binding_id=11, pos_id="p1", order_id="b"),
        ],
        lifecycles=[],
        events=[],
        reservations=[],
        snapshot=empty_successful_snapshot(),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_owner_ambiguous"
    assert decision.conflicts[0].pos_ids == ("p1",)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_historical_attribution_cleanup.py -v`

Expected: collection fails because `historical_attribution_cleanup` and its public types do not exist.

- [ ] **Step 3: Implement the minimal immutable result types and component planner**

```python
@dataclass(frozen=True, slots=True)
class HistoricalCleanupAction:
    action: str
    binding_id: int | None
    leg_id: int | None
    lifecycle_id: int | None
    venue: str
    old_pos_id: str | None
    new_pos_id: str | None
    old_state: str | None
    new_state: str | None
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HistoricalCleanupConflict:
    reason: str
    binding_ids: tuple[int, ...]
    leg_ids: tuple[int, ...]
    lifecycle_ids: tuple[int, ...]
    pos_ids: tuple[str, ...]
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HistoricalCleanupDecision:
    actions: tuple[HistoricalCleanupAction, ...]
    conflicts: tuple[HistoricalCleanupConflict, ...]
```

Group candidates by `(venue, pos_id)`, sort every component numerically by binding and leg ID, and retain a historical owner only when exactly one leg passes existing authoritative persisted-position evidence.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_historical_attribution_cleanup.py -v`

Expected: the duplicate-component tests pass.

- [ ] **Step 5: Write failing tests for terminal evidence and operation gates**

```python
@pytest.mark.parametrize("blocked_source", ["positions", "open_orders", "trigger_orders", "tpsl_orders"])
def test_live_or_pending_exchange_identity_blocks_historical_cleanup(blocked_source):
    snapshot = snapshot_with_exact_position_identity(blocked_source, "p1")
    decision = plan_historical_attribution_cleanup(
        bindings=[binding(id=10)],
        legs=[leg(id=1, binding_id=10, pos_id="p1", status="manually_closed")],
        lifecycles=[lifecycle(binding_id=10, status="exited", exit_reason="manual")],
        events=[],
        reservations=[],
        snapshot=snapshot,
    )
    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_position_still_exchange_active"


def test_entered_lifecycle_without_exact_terminal_evidence_is_unresolved():
    decision = plan_historical_attribution_cleanup(
        bindings=[binding(id=96, status="unknown")],
        legs=[leg(id=188, binding_id=96, pos_id="old", status="active")],
        lifecycles=[lifecycle(id=420, binding_id=96, status="entered")],
        events=[],
        reservations=[],
        snapshot=empty_successful_snapshot(),
    )
    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_terminal_evidence_missing"


def test_research_only_lifecycle_is_not_a_cleanup_candidate():
    decision = plan_historical_attribution_cleanup(
        bindings=[], legs=[],
        lifecycles=[lifecycle(id=120, binding_id=None, status="entered")],
        events=[], reservations=[], snapshot=empty_successful_snapshot(),
    )
    assert decision == HistoricalCleanupDecision(actions=(), conflicts=())
```

- [ ] **Step 6: Run the new tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_historical_attribution_cleanup.py -v`

Expected: failures show that exchange-active identities, terminal evidence, and research-only exclusion are not yet implemented.

- [ ] **Step 7: Implement evidence classification and deterministic action ordering**

Implement helpers with these exact responsibilities:

```python
def terminal_evidence_for_binding(
    binding,
    *,
    lifecycles,
    events,
    reservations,
    history_rows,
) -> dict[str, object] | None:
    """Return one exact terminal proof or None; position absence is never proof."""


def exchange_active_position_ids(snapshot) -> set[str]:
    """Return exact IDs from live positions and pending position-linked rows."""


def plan_historical_attribution_cleanup(
    *, bindings, legs, lifecycles, events, reservations, snapshot
) -> HistoricalCleanupDecision:
    """Return stable evidence-backed actions and unresolved conflicts."""
```

Lifecycle terminal evidence requires `exited/cancelled`, a nonempty reason, and its terminal timestamp. Execution evidence must identify the exact binding or position. API errors return one evidence-unavailable conflict and no actions.

- [ ] **Step 8: Run the whole new unit-test file**

Run: `.venv/bin/python -m pytest tests/test_historical_attribution_cleanup.py -v`

Expected: all tests pass with deterministic action order under a shuffled-input test.

- [ ] **Step 9: Commit the pure planner**

```bash
git add src/telegram_kol_research/historical_attribution_cleanup.py tests/test_historical_attribution_cleanup.py
git commit -m "feat: plan historical attribution cleanup"
```

---

### Task 2: Integrate Historical Evidence Into Repair Plans and Fingerprints

**Files:**
- Modify: `src/telegram_kol_research/position_attribution_repair.py`
- Modify: `tests/test_position_attribution_repair.py`

**Interfaces:**
- Consumes: `plan_historical_attribution_cleanup(...)` from Task 1.
- Produces: `PositionAttributionRepairPlan.historical_actions`, merged unresolved conflicts, and fingerprints covering bindings, legs, lifecycles, terminal events, and reservations.

- [ ] **Step 1: Write failing integration tests for plan content and read-only behavior**

```python
def test_repair_plan_includes_historical_cleanup_without_mutating_database(tmp_path):
    session_factory = seed_historical_duplicate_fixture(tmp_path)
    before = database_state(session_factory)

    plan = build_position_attribution_repair_plan(
        session_factory,
        deepcoin_client=HistoricalRepairClient(),
        now=NOW,
    )

    assert plan.historical_actions
    assert plan.historical_actions[0].action == "clear_redundant_historical_position"
    assert database_state(session_factory) == before


def test_repair_plan_excludes_current_live_position_from_historical_actions(tmp_path):
    session_factory = seed_historical_duplicate_fixture(tmp_path, pos_id="live")
    client = HistoricalRepairClient(live_position_ids=["live"])
    plan = build_position_attribution_repair_plan(session_factory, deepcoin_client=client, now=NOW)
    assert all(action.old_pos_id != "live" for action in plan.historical_actions)
```

- [ ] **Step 2: Run the two integration tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_position_attribution_repair.py -k 'historical_cleanup or excludes_current_live' -v`

Expected: failures because the repair plan has no `historical_actions` field.

- [ ] **Step 3: Extend the repair plan and load local evidence read-only**

Add this field without changing current live-repair action behavior:

```python
@dataclass(frozen=True, slots=True)
class PositionAttributionRepairPlan:
    created_at: datetime
    live_position_ids: tuple[str, ...]
    exchange_evidence_fingerprint: str
    actions: tuple[PositionAttributionRepairAction, ...]
    historical_actions: tuple[HistoricalCleanupAction, ...]
    unresolved_conflicts: list[dict[str, object]]
    database_fingerprint: str
    fingerprint: str

    @property
    def has_actions(self) -> bool:
        return bool(self.actions or self.historical_actions)
```

Load execution-backed lifecycles, exact-position execution events, and close reservations in the same session as bindings and legs. Convert historical conflicts with `asdict` before merging them into the JSON-compatible unresolved list.

- [ ] **Step 4: Run integration tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_position_attribution_repair.py -k 'historical_cleanup or excludes_current_live' -v`

Expected: both tests pass and the plan remains read-only.

- [ ] **Step 5: Write failing drift tests for lifecycle, event, and reservation evidence**

```python
@pytest.mark.parametrize("mutation", ["lifecycle", "execution_event", "close_reservation"])
def test_historical_repair_fingerprint_changes_when_terminal_evidence_changes(tmp_path, mutation):
    session_factory = seed_historical_duplicate_fixture(tmp_path)
    first = build_position_attribution_repair_plan(session_factory, deepcoin_client=HistoricalRepairClient(), now=NOW)
    mutate_terminal_evidence(session_factory, mutation)
    second = build_position_attribution_repair_plan(session_factory, deepcoin_client=HistoricalRepairClient(), now=NOW)
    assert second.database_fingerprint != first.database_fingerprint
    assert second.fingerprint != first.fingerprint
```

- [ ] **Step 6: Run drift tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_position_attribution_repair.py -k 'terminal_evidence_changes' -v`

Expected: at least one mutation leaves the fingerprint unchanged.

- [ ] **Step 7: Extend `_database_fingerprint` with only cleanup-authoritative local evidence**

Include stable, sorted projections of:

```python
{
    "lifecycles": [id, execution_binding_id, lifecycle_status, exit_reason, entered_at, exited_at, updated_at],
    "terminal_events": [id, execution_binding_id, action, status, pos_id, related_order_id, exchange_event_time, created_at],
    "close_reservations": [id, pos_id, execution_binding_id, status, created_at, updated_at],
}
```

Do not fingerprint message text, secrets, exception bodies, or unrelated recognition data.

- [ ] **Step 8: Run repair-plan and existing drift suites**

Run: `.venv/bin/python -m pytest tests/test_position_attribution_repair.py -v`

Expected: all current and new repair-plan tests pass.

- [ ] **Step 9: Commit planner integration**

```bash
git add src/telegram_kol_research/position_attribution_repair.py tests/test_position_attribution_repair.py
git commit -m "feat: include historical cleanup in repair plans"
```

---

### Task 3: Transactional Apply, Immutable Audit, and Unique Index

**Files:**
- Modify: `src/telegram_kol_research/position_attribution_repair.py`
- Modify: `src/telegram_kol_research/db.py`
- Modify: `tests/test_position_attribution_repair.py`
- Modify: `tests/test_db_bootstrap.py`

**Interfaces:**
- Consumes: `PositionAttributionRepairPlan.historical_actions` and the existing reviewed-fingerprint apply boundary.
- Produces: atomic historical state changes, `historical_cleanup` audits, lifecycle transitions, and `ensure_position_ownership_unique_index(connection)`.

- [ ] **Step 1: Write failing tests for atomic audited apply and lifecycle behavior**

```python
def test_historical_cleanup_apply_is_atomic_audited_and_idempotent(tmp_path):
    session_factory = seed_historical_duplicate_fixture(tmp_path)
    client = HistoricalRepairClient()
    plan = build_position_attribution_repair_plan(session_factory, deepcoin_client=client, now=NOW)

    result = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )

    assert result.applied == len(plan.actions) + len(plan.historical_actions)
    assert duplicate_position_ids(session_factory) == []
    assert historical_cleanup_audits(session_factory)
    repeated = apply_position_attribution_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        expected_fingerprint=plan.fingerprint,
    )
    assert repeated.already_applied is True


def test_entered_lifecycle_changes_only_with_explicit_exit_action(tmp_path):
    session_factory = seed_entered_historical_fixture(tmp_path, with_terminal_event=True)
    plan = build_position_attribution_repair_plan(session_factory, deepcoin_client=HistoricalRepairClient(), now=NOW)
    assert any(action.action == "exit_historical_lifecycle" for action in plan.historical_actions)
```

- [ ] **Step 2: Run the apply tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_position_attribution_repair.py -k 'historical_cleanup_apply or entered_lifecycle_changes' -v`

Expected: historical actions are planned but not applied or audited.

- [ ] **Step 3: Implement historical action application inside the existing transaction**

Apply actions in this order:

```python
ACTION_ORDER = {
    "clear_redundant_historical_position": 10,
    "terminalize_historical_entry_leg": 20,
    "close_historical_binding": 30,
    "exit_historical_lifecycle": 40,
    "install_position_ownership_unique_index": 50,
}
```

Before each mutation, compare every planned prior value to the current row. Insert one immutable `PositionAttributionAudit(event_type="historical_cleanup")` containing old/new values, terminal proof, policy version, plan fingerprint, and applied database fingerprint. Flush cleanup rows before deriving binding state.

- [ ] **Step 4: Run apply tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_position_attribution_repair.py -k 'historical_cleanup_apply or entered_lifecycle_changes' -v`

Expected: apply and idempotency tests pass.

- [ ] **Step 5: Write failing tests for unique-index creation and rollback**

```python
def test_cleanup_apply_installs_unique_position_index(tmp_path):
    session_factory = seed_historical_duplicate_fixture(tmp_path)
    apply_reviewed_historical_cleanup(session_factory)
    assert "uq_execution_order_legs_venue_pos" in execution_leg_indexes(session_factory)
    with pytest.raises(IntegrityError):
        insert_second_owner_for_existing_position(session_factory)


def test_unique_index_failure_rolls_back_cleanup_and_audits(tmp_path, monkeypatch):
    session_factory = seed_historical_duplicate_fixture(tmp_path)
    monkeypatch.setattr(db, "ensure_position_ownership_unique_index", raise_integrity_error)
    before = database_state(session_factory)
    with pytest.raises(PositionAttributionRepairError, match="repair transaction failed"):
        apply_reviewed_historical_cleanup(session_factory)
    assert database_state(session_factory) == before
    assert historical_cleanup_audits(session_factory) == []
```

- [ ] **Step 6: Run index tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_db_bootstrap.py tests/test_position_attribution_repair.py -k 'unique_position_index or unique_index_failure' -v`

Expected: the index is absent after cleanup or failure is not atomic.

- [ ] **Step 7: Extract and use the shared index helper**

```python
POSITION_OWNERSHIP_UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_order_legs_venue_pos "
    "ON execution_order_legs (venue, pos_id) "
    "WHERE pos_id IS NOT NULL AND pos_id != ''"
)


def ensure_position_ownership_unique_index(connection) -> None:
    duplicate = connection.execute(text(
        "SELECT 1 FROM execution_order_legs "
        "WHERE pos_id IS NOT NULL AND pos_id != '' "
        "GROUP BY venue, pos_id HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate is not None:
        raise RuntimeError("duplicate position ownership remains")
    connection.execute(text(POSITION_OWNERSHIP_UNIQUE_INDEX_SQL))
```

Legacy bootstrap continues to skip this helper while duplicates exist. Reviewed cleanup calls it only after flushing cleanup mutations and before committing.
The apply boundary converts helper or SQLite failures to
`PositionAttributionRepairError("repair transaction failed")` after rollback.

- [ ] **Step 8: Run repair, database-bootstrap, and execution-binding tests**

Run: `.venv/bin/python -m pytest tests/test_position_attribution_repair.py tests/test_db_bootstrap.py tests/test_execution_bindings.py -v`

Expected: all tests pass, including the existing legacy-bootstrap test that expects no unique index before cleanup.

- [ ] **Step 9: Commit transactional apply**

```bash
git add src/telegram_kol_research/position_attribution_repair.py src/telegram_kol_research/db.py tests/test_position_attribution_repair.py tests/test_db_bootstrap.py
git commit -m "feat: apply audited historical attribution cleanup"
```

---

### Task 4: CLI Gate and Operator Documentation

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_cli_repair_position_attribution.py` if present; otherwise add CLI assertions to `tests/test_position_attribution_repair.py`
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`

**Interfaces:**
- Consumes: `PositionAttributionRepairPlan.has_actions`.
- Produces: unchanged dry-run JSON with `historical_actions`, correct nonempty fingerprint enforcement, and an operator verification checklist.

- [ ] **Step 1: Write failing CLI tests for historical-only plans**

```python
def test_cli_requires_expected_fingerprint_for_historical_only_plan(monkeypatch):
    monkeypatch.setattr(cli, "build_position_attribution_repair_plan", historical_only_plan)
    result = runner.invoke(cli.app, ["repair-position-attribution", "--apply"])
    assert result.exit_code == 2
    assert "--expected-fingerprint is required" in result.output


def test_cli_dry_run_serializes_historical_actions(monkeypatch):
    monkeypatch.setattr(cli, "build_position_attribution_repair_plan", historical_only_plan)
    result = runner.invoke(cli.app, ["repair-position-attribution"])
    assert result.exit_code == 0
    assert '"historical_actions"' in result.output
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `.venv/bin/python -m pytest tests -k 'cli_requires_expected_fingerprint_for_historical_only or cli_dry_run_serializes_historical' -v`

Expected: historical-only apply is treated as empty or the output lacks the field.

- [ ] **Step 3: Update the CLI to use the plan-level action gate**

```python
if plan.has_actions and not expected_fingerprint:
    typer.echo(
        "Refusing apply: --expected-fingerprint is required for a nonempty plan.",
        err=True,
    )
    raise typer.Exit(code=2)
```

Preserve the unresolved-conflict refusal before calling the apply boundary.

- [ ] **Step 4: Run CLI tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests -k 'repair_position_attribution' -v`

Expected: dry-run serialization and both fingerprint refusal paths pass.

- [ ] **Step 5: Document the production review contract**

Add exact operator checks:

```text
- Confirm historical_actions contain no current live_position_ids.
- Review every old/new leg, binding, and lifecycle state.
- Treat unresolved_conflicts as an apply blocker.
- Verify automatic trading remains false.
- Verify the timestamped backup size and SHA-256 before apply.
- After apply, require zero duplicate (venue, pos_id) groups and confirm
  uq_execution_order_legs_venue_pos exists.
```

State explicitly that a zero-action plan authorizes no database mutation and a nonzero production plan requires separate approval.

- [ ] **Step 6: Run documentation and syntax checks**

Run: `git diff --check && .venv/bin/python -m compileall -q src`

Expected: exit code 0.

- [ ] **Step 7: Commit CLI and documentation**

```bash
git add src/telegram_kol_research/cli.py tests docs/runbook.md docs/server-deployment.md
git commit -m "docs: operate historical attribution cleanup safely"
```

---

### Task 5: Local Verification, Push, Deploy, and Production Dry Run

**Files:**
- Verify: `scripts/server_git_update.sh`
- Verify: `docs/superpowers/specs/2026-07-15-historical-position-attribution-cleanup-design.md`
- Verify: `docs/superpowers/plans/2026-07-15-historical-position-attribution-cleanup.md`

**Interfaces:**
- Consumes: all implementation commits and existing server deployment helper.
- Produces: reviewed local verification evidence and a fresh production dry-run report; no production apply.

- [ ] **Step 1: Run focused local verification**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_historical_attribution_cleanup.py \
  tests/test_position_attribution_repair.py \
  tests/test_db_bootstrap.py \
  tests/test_execution_bindings.py \
  tests/test_deepcoin_execution_actions.py \
  -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Run the full local suite and syntax checks**

Run:

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q src
git diff --check
```

Expected: pytest exits 0, compileall exits 0, and diff check is clean. Record any known baseline separately; do not increase failures.

- [ ] **Step 3: Verify branch and commit scope**

Run:

```bash
git status --short --branch
git log --oneline origin/codex/deepcoin-auto-trading-v1..HEAD
git diff --stat origin/codex/deepcoin-auto-trading-v1...HEAD
```

Expected: only the approved design, plan, implementation, tests, and operator docs are ahead of the remote branch.

- [ ] **Step 4: Push the reviewed branch**

Run: `git push origin codex/deepcoin-auto-trading-v1`

Expected: the remote branch advances to the reviewed local HEAD.

- [ ] **Step 5: Verify production safety preconditions and create a backup**

Run read-only checks first, then create the required backup:

```bash
ssh -i "$HOME/.ssh/tecent.pem" root@43.167.220.225 '
  cd /opt/telegram-kol-analyzer &&
  .venv/bin/python - <<"PY"
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.trading_settings import load_trading_settings
settings = load_trading_settings(create_session_factory("data/research.db"))
print(settings.auto_trade_enabled)
raise SystemExit(0 if settings.auto_trade_enabled is False else 2)
PY
  stamp=$(date +%Y%m%d-%H%M%S) &&
  backup="data/research.db.${stamp}.historical-cleanup.bak" &&
  cp data/research.db "$backup" &&
  stat -c "%n %s" "$backup" && sha256sum "$backup"
'
```

Expected: global automatic trading is `False`; one timestamped backup path, byte size, and SHA-256 are printed.

- [ ] **Step 6: Deploy through the existing helper**

Run: `./scripts/server_git_update.sh`

Expected: the server fast-forwards, reinstalls the editable package, restarts `telegram-kol.service`, and returns a new active PID.

- [ ] **Step 7: Verify server revision and service health**

Run:

```bash
ssh -i "$HOME/.ssh/tecent.pem" root@43.167.220.225 '
  cd /opt/telegram-kol-analyzer &&
  git rev-parse HEAD &&
  systemctl is-active telegram-kol.service &&
  systemctl show telegram-kol.service -p MainPID -p ActiveEnterTimestamp --no-pager
'
```

Expected: server HEAD equals local HEAD and the service is active with a current PID.

- [ ] **Step 8: Run a fresh production dry run only**

Run:

```bash
ssh -i "$HOME/.ssh/tecent.pem" root@43.167.220.225 '
  cd /opt/telegram-kol-analyzer &&
  .venv/bin/telegram-kol-research repair-position-attribution \
    --database-path data/research.db
'
```

Expected: `DRY RUN` JSON includes current `live_position_ids`, `actions`, `historical_actions`, `unresolved_conflicts`, and a new fingerprint. Do not pass `--apply`.

- [ ] **Step 9: Review and report the production plan**

Verify line by line:

```text
- none of the three current live position IDs appears in historical_actions;
- every historical action has exact terminal evidence;
- every lifecycle action targets an execution-backed lifecycle;
- unresolved_conflicts is empty before any apply can be considered;
- the unique-index action appears only if the planned cleanup removes every duplicate;
- global automatic trading remains false;
- no exchange write method was called.
```

Expected: report the exact action list, unresolved conflicts, backup path/hash, server HEAD, service PID, and fingerprint to the operator. Stop here for separate approval of any nonzero plan.

---

## Plan Self-Review

- Spec coverage: eligibility, fail-closed behavior, immutable audit, lifecycle handling, unique index, fingerprint drift, idempotency, deployment backup, and production dry-run stop point all have explicit tasks.
- Placeholder scan: no incomplete markers, deferred implementation, or unspecified test steps remain.
- Type consistency: Task 1 produces `HistoricalCleanupAction` and `HistoricalCleanupDecision`; Task 2 integrates `historical_actions`; Tasks 3 and 4 consume the same field and `has_actions` property.
- Scope: no exchange mutation, no research-only lifecycle conversion, and no production apply are included.
