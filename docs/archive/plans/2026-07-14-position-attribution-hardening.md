# Deterministic Position Attribution Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace heuristic Deepcoin position attribution with an entry-leg ownership state machine that preserves manual terminal actions, fails closed on ambiguity, and correctly associates TPSL protection.

**Architecture:** Add persisted leg attribution state and immutable audit records, build a pure global evidence matcher, and make reconciliation apply its decisions in three phases: exact order-state refresh, global one-to-one ownership, then derived binding/lifecycle state. Web display and every live mutation consume the same persisted ownership state; TPSL association uses a separate unique matcher.

**Tech Stack:** Python 3.11+, SQLAlchemy 2, SQLite, FastAPI, Typer, Jinja2, pytest, Deepcoin REST client.

---

## Working Rules

- Work in the isolated worktree created for this design.
- Use `@test-driven-development` for every behavior change.
- Keep Deepcoin calls read-only until the production rollout task reaches reviewed deployment.
- Never close a position, cancel an order, or apply a database repair during diagnosis or tests.
- Treat unrelated test failures as a recorded baseline; relevant and new tests must be green and the total failure count must not increase.
- After review, integrate commits onto `codex/deepcoin-auto-trading-v1`; only that branch is pushed and deployed.

### Task 1: Persist entry-leg ownership and immutable audit state

**Files:**
- Modify: `src/telegram_kol_research/models.py:373-474`
- Modify: `src/telegram_kol_research/db.py:14-220`
- Modify: `src/telegram_kol_research/execution_bindings.py:45-82`
- Test: `tests/test_db_bootstrap.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write failing schema tests**

Prove bootstrapping an existing database adds these leg fields:

```python
expected = {
    "venue",
    "attribution_status",
    "attribution_evidence_json",
    "terminal_reason",
    "last_verified_at",
}
assert expected <= execution_order_leg_columns
```

Prove two non-null Deepcoin leg rows cannot own the same position, while multiple null `pos_id` rows remain valid:

```python
first.venue = second.venue = "deepcoin"
first.pos_id = second.pos_id = "pos-1"
session.add_all([first, second])
with pytest.raises(IntegrityError):
    session.commit()
```

Prove `position_attribution_audits` contains binding ID, leg ID, venue, position ID, event type, prior/new state, fingerprint, evidence JSON, and creation time.

**Step 2: Run tests and verify failure**

```bash
./.venv/bin/python -m pytest \
  tests/test_db_bootstrap.py \
  tests/test_execution_bindings.py::test_execution_order_leg_position_ownership_is_unique \
  -q
```

Expected: FAIL because the new columns, audit model, and unique index do not exist.

**Step 3: Add minimal models and compatibility migration**

Add to `ExecutionOrderLeg`:

```python
venue: Mapped[str] = mapped_column(String(64), nullable=False, default="deepcoin")
attribution_status: Mapped[str] = mapped_column(
    String(32), nullable=False, default="unassigned", index=True
)
attribution_evidence_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
terminal_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
```

Add the SQLite compatibility index:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_order_legs_venue_pos
ON execution_order_legs (venue, pos_id)
WHERE pos_id IS NOT NULL AND pos_id != ''
```

Add append-only `PositionAttributionAudit` with a unique fingerprint. Update `ExecutionOrderLegRecord` and `ExecutionOrderLegSnapshot` with safe defaults for old call sites.

**Step 4: Run schema tests**

```bash
./.venv/bin/python -m pytest tests/test_db_bootstrap.py tests/test_execution_bindings.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_db_bootstrap.py tests/test_execution_bindings.py
git commit -m "feat: persist verified position ownership"
```

### Task 2: Build a pure global entry-leg evidence matcher

**Files:**
- Create: `src/telegram_kol_research/position_attribution.py`
- Create: `tests/test_position_attribution.py`
- Reference: `docs/plans/2026-07-14-position-attribution-hardening-design.md`

**Step 1: Write failing production-incident tests**

Create fixtures for a cancelled 三马哥 trigger leg and two 智哥 entry legs/positions with the observed equal size, close price, and 69-second separation. Assert:

```python
result = match_entry_legs_to_positions(legs, positions, evidence)
assert result.assignments == {
    smart_market_leg.id: "1001124083084014",
    smart_trigger_leg.id: "1001124083099498",
}
assert horse_cancelled_leg.id not in result.assignments
assert result.conflicts == []
```

Add cases for exact time beating one second, one second beating sixty-nine seconds, tied evidence producing conflict, input order independence, symbol/side-only evidence producing no assignment, terminal legs being excluded, and one position never belonging to two legs.

**Step 2: Run tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_position_attribution.py -q
```

Expected: FAIL with import error.

**Step 3: Implement pure evidence types**

```python
@dataclass(frozen=True)
class LegEvidence:
    leg_id: int
    binding_id: int
    venue: str
    symbol: str
    side: str
    order_id: str | None
    client_order_id: str | None
    requested_size: float | None
    terminal: bool

@dataclass(frozen=True)
class PositionEvidence:
    pos_id: str
    symbol: str
    side: str
    size: float | None
    average_price: float | None
    created_at_ms: int | None

@dataclass(frozen=True, order=True)
class MatchRank:
    evidence_tier: int
    time_distance_ms: int
    size_distance: float
    price_distance: float

@dataclass
class AttributionResult:
    assignments: dict[int, str]
    evidence_by_leg: dict[int, dict[str, object]]
    conflicts: list[dict[str, object]]
    unassigned_position_ids: set[str]
```

Use evidence tiers `DIRECT_POS_ID`, `EXACT_REGULAR_ORDER_ID`, `EXACT_CLIENT_ORDER_ID`, and `UNIQUE_TRIGGER_FILL`. Do not define a symbol/side fallback tier.

Require exact symbol/side and compatible quantity. Rank actual timestamp distance numerically. Price is the last diagnostic discriminator.

**Step 4: Implement mutual-unique global assignment**

Assign an edge only when it is the unique best edge for both its leg and its position:

```python
while edges:
    leg_best = unique_best_edges_by_leg(edges)
    pos_best = unique_best_edges_by_position(edges)
    accepted = [
        edge for edge in edges
        if leg_best.get(edge.leg_id) == edge
        and pos_best.get(edge.pos_id) == edge
    ]
    if not accepted:
        break
    for edge in accepted:
        assignments[edge.leg_id] = edge.pos_id
        remove_edges_for_leg_and_position(edge)
```

Return remaining candidate components as conflicts.

**Step 5: Run tests**

```bash
./.venv/bin/python -m pytest tests/test_position_attribution.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_attribution.py tests/test_position_attribution.py
git commit -m "feat: match positions to entry legs globally"
```

### Task 3: Classify pending, filled, and cancelled leg evidence

**Files:**
- Modify: `src/telegram_kol_research/position_attribution.py`
- Modify: `src/telegram_kol_research/execution_bindings.py:232-445`
- Test: `tests/test_position_attribution.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write failing state tests**

```python
assert classify_leg_exchange_state(cancelled_trigger_history) == "manually_cancelled"
assert is_fill_evidence(cancelled_trigger_history) is False
assert is_fill_evidence(explicit_filled_order_history) is True
assert is_fill_evidence(trade_fill_row) is True
```

Add a matching `cancel_trigger_entry` event and assert the terminal state becomes `exchange_cancelled`. Prove a recovery scan of the old Telegram message cannot reopen either terminal state.

**Step 2: Run tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_position_attribution.py tests/test_execution_bindings.py -q
```

Expected: FAIL because cancelled history with numeric fields may still count as fill evidence.

**Step 3: Implement explicit classification**

Fill evidence requires a fills-endpoint row, an explicit filled/partial state, or an exact regular filled response already recorded in the execution ledger. Never infer fill from `sz`, price, or time alone.

Use exact order/client IDs to refresh nonterminal legs before matching. Terminal states are immutable except through the audited repair command.

**Step 4: Run tests**

```bash
./.venv/bin/python -m pytest tests/test_position_attribution.py tests/test_execution_bindings.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/position_attribution.py \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_position_attribution.py tests/test_execution_bindings.py
git commit -m "fix: preserve cancelled entry legs as terminal"
```

### Task 4: Refactor reconciliation into snapshot, global decision, and application

**Files:**
- Modify: `src/telegram_kol_research/execution_bindings.py:232-445`
- Modify: `src/telegram_kol_research/execution_bindings.py:1090-1730`
- Modify: `src/telegram_kol_research/position_attribution.py`
- Test: `tests/test_execution_bindings.py`
- Test: `tests/test_position_attribution.py`

**Step 1: Write failing integration tests**

Prove reconciliation is independent of binding query order; the incident fixture produces two 智哥-owned positions; manual cancellation closes the old binding/lifecycle; active plus pending legs keep a binding active; all-terminal legs are never recovery candidates; API failure preserves verified ownership and records `evidence_unavailable`; repeated passes neither oscillate ownership nor duplicate audit rows.

**Step 2: Run tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_execution_bindings.py -q
```

Expected: FAIL on order independence, terminal preservation, and unavailable evidence.

**Step 3: Load one coherent evidence snapshot**

Load positions, pending/history orders, trigger/TPSL data, fills, bindings, legs, and relevant execution events before database mutation. Preserve source-specific errors:

```python
snapshot.errors["trigger_history"] = str(exc)
```

Never convert an API exception into an empty successful result.

**Step 4: Apply decisions transactionally**

In one transaction:

1. Refresh exact leg states.
2. Run the global matcher for all eligible legs/positions.
3. Insert audit transitions.
4. Update leg ownership/evidence/verification time.
5. Derive binding `pos_id` from verified legs only.
6. Derive binding and lifecycle states from all legs.

Delete `_select_recovered_position_for_unbound_binding` and its calls. Replace any tests expecting symbol/side guessing with fail-closed assertions. Contradictory existing ownership becomes conflict; it is not silently moved.

**Step 5: Run tests**

```bash
./.venv/bin/python -m pytest tests/test_position_attribution.py tests/test_execution_bindings.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/position_attribution.py \
  tests/test_execution_bindings.py tests/test_position_attribution.py
git commit -m "refactor: reconcile positions from leg evidence"
```

### Task 5: Gate every live mutation on verified ownership

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py:51-530`
- Modify: `src/telegram_kol_research/web_app.py:2680-2810`
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `src/telegram_kol_research/position_attribution.py`
- Test: `tests/test_deepcoin_execution_actions.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: Write failing fail-closed tests**

Parameterize `unassigned`, `attribution_conflict`, and `evidence_unavailable`. For each state, prove full close, partial close, stop adjustment, TP adjustment, and manual bind make zero fake-client mutation calls and raise:

```text
position_ownership_not_verified:<state>
```

Add a verified-state control proving the exact-position flow submits once.

**Step 2: Run tests and verify failure**

```bash
./.venv/bin/python -m pytest \
  tests/test_deepcoin_execution_actions.py \
  tests/test_web_app.py \
  tests/test_recovery_live_submit.py -q
```

Expected: FAIL because active binding currently suffices.

**Step 3: Add one shared server-side gate**

```python
def require_verified_position_ownership(session, *, venue: str, pos_id: str):
    rows = load_legs_for_position(session, venue=venue, pos_id=pos_id)
    if len(rows) != 1:
        raise PositionAttributionError("position_ownership_not_unique")
    leg = rows[0]
    if leg.attribution_status != "verified":
        raise PositionAttributionError(
            f"position_ownership_not_verified:{leg.attribution_status}"
        )
    if leg.status in TERMINAL_LEG_STATES:
        raise PositionAttributionError("position_ownership_terminal")
    return leg
```

Call it before reservation or exchange submission. Manual binding may resolve a deliberately unassigned manual position after unique review, but cannot override a conflict; conflict correction uses the repair workflow.

**Step 4: Run tests**

```bash
./.venv/bin/python -m pytest \
  tests/test_deepcoin_execution_actions.py \
  tests/test_web_app.py \
  tests/test_recovery_live_submit.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/position_attribution.py \
  src/telegram_kol_research/deepcoin_execution_actions.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/recovery_live_submit.py \
  tests/test_deepcoin_execution_actions.py tests/test_web_app.py \
  tests/test_recovery_live_submit.py
git commit -m "fix: require verified ownership for live mutations"
```

### Task 6: Match TPSL independently and render uncertainty truthfully

**Files:**
- Create: `src/telegram_kol_research/protection_attribution.py`
- Create: `tests/test_protection_attribution.py`
- Modify: `src/telegram_kol_research/web_app.py:400-500`
- Modify: `src/telegram_kol_research/web_app.py:1165-1230`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:1-100`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing protection tests**

Cover position time `00:27:56` versus full stop time `00:27:57`, `sz=0` full protection, TP sizes `0.9 + 0.6` for position size `1.5`, exact `posId` priority, indistinguishable same-side positions producing ambiguity, and uncertain protection being unusable for cancellation/replacement.

Expected API:

```python
result = match_position_protection(positions, tpsl_orders)
assert result.by_pos_id["pos-smart-market"].stop_loss == 1820
assert result.by_pos_id["pos-smart-market"].status == "verified"
```

**Step 2: Run tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_protection_attribution.py -q
```

Expected: FAIL with import error.

**Step 3: Implement pure protection matching**

Use exact `posId` first. Otherwise build same-instrument/same-side edges, rank actual timestamp distance, treat zero size as full position, and require mutual-unique mapping. Merge stop and partial TP rows only after they map to the same position evidence group.

Return:

```python
@dataclass
class PositionProtection:
    status: str  # verified | present_but_ambiguous | absent | evidence_unavailable
    stop_loss: float | None
    take_profits: list[float]
    order_ids: list[str]
    evidence: dict[str, object]
```

**Step 4: Replace exact-timestamp Web mapping**

Make `_load_deepcoin_live_position_rows` consume the shared matcher. Render verified SL, `止损存在，归属待确认`, `止损证据暂不可用`, or proven `无止损`. Only verified ownership and verified protection may expose protection mutation controls.

**Step 5: Run tests**

```bash
./.venv/bin/python -m pytest \
  tests/test_protection_attribution.py \
  tests/test_web_page_render.py tests/test_web_app.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/protection_attribution.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  src/telegram_kol_research/static/app.css \
  tests/test_protection_attribution.py tests/test_web_page_render.py tests/test_web_app.py
git commit -m "fix: associate position protection uniquely"
```

### Task 7: Expose persisted evidence and deduplicate abnormal alerts

**Files:**
- Modify: `src/telegram_kol_research/web_app.py:400-850`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html:1-100`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/execution_bindings.py`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_system_operator_bot.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write failing UI and notification tests**

Verified cards must render group, strategy instance, leg index, evidence type, position ID, and last verified time. Conflict cards must render `归属待确认`, `归属冲突`, and `自动管理已冻结`.

Two identical reconcile passes must insert/send one conflict incident; a changed canonical fingerprint must produce a new incident.

**Step 2: Run tests and verify failure**

```bash
./.venv/bin/python -m pytest \
  tests/test_web_page_render.py tests/test_system_operator_bot.py \
  tests/test_execution_bindings.py -q
```

Expected: FAIL because Web still computes live-position candidates independently.

**Step 3: Make persisted attribution the only live-position source**

Build cards from the unique leg/audit state. Remove live-position candidate scoring as a source of `bound` labels or action availability. Historical research may show candidates, but candidates never authorize live mutations.

**Step 4: Add durable alert deduplication**

Hash venue, position ID, state, candidate leg IDs, and evidence-source errors. Insert the audit transition first; notify only for a newly inserted fingerprint. Record delivery status without changing ownership.

**Step 5: Run tests**

```bash
./.venv/bin/python -m pytest \
  tests/test_web_page_render.py tests/test_system_operator_bot.py \
  tests/test_execution_bindings.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  src/telegram_kol_research/static/app.css \
  src/telegram_kol_research/system_operator_bot.py \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_web_page_render.py tests/test_system_operator_bot.py \
  tests/test_execution_bindings.py
git commit -m "feat: expose verified position attribution"
```

### Task 8: Add dry-run-first historical repair

**Files:**
- Create: `src/telegram_kol_research/position_attribution_repair.py`
- Create: `tests/test_position_attribution_repair.py`
- Modify: `src/telegram_kol_research/cli.py:600-650`
- Modify: `tests/test_cli_smoke.py`
- Modify: `docs/server-deployment.md`

**Step 1: Write failing repair tests**

Build bindings `112` and `120`. Dry-run must propose removal of `1001124083099498` from `112`, terminal cancellation for proven cancelled 三马哥 legs, assignment of that position to 智哥 binding `120` leg 2, no database changes, no ambiguous repairs, and no cancellation of the live 舒琴 trigger legs.

Apply tests must prove transactionality, audit logging, stale-evidence rejection, and idempotency.

**Step 2: Run tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_position_attribution_repair.py tests/test_cli_smoke.py -q
```

Expected: FAIL because the module and CLI command do not exist.

**Step 3: Implement dry-run by default**

```python
def build_position_attribution_repair_plan(
    session_factory, *, deepcoin_client, now
) -> PositionAttributionRepairPlan:
    ...

def apply_position_attribution_repair_plan(
    session_factory, plan: PositionAttributionRepairPlan
) -> PositionAttributionRepairResult:
    ...
```

The plan contains old/new ownership, terminal transitions, evidence summaries, and unresolved conflicts. Apply rejects stale plans or changed live position IDs.

Add:

```python
@app.command("repair-position-attribution")
def repair_position_attribution(
    database_path: Path = Path("data/research.db"),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    ...
```

Dry-run is default. `--apply` is explicit. The command never submits an exchange mutation.

**Step 4: Run tests**

```bash
./.venv/bin/python -m pytest tests/test_position_attribution_repair.py tests/test_cli_smoke.py -q
```

Expected: PASS.

**Step 5: Update deployment docs and commit**

Document database backup, dry-run review, apply, restart, repeated read-only reconciliation, and re-enable boundaries.

```bash
git add src/telegram_kol_research/position_attribution_repair.py \
  src/telegram_kol_research/cli.py \
  tests/test_position_attribution_repair.py tests/test_cli_smoke.py \
  docs/server-deployment.md
git commit -m "feat: add audited attribution repair command"
```

### Task 9: Verify locally and request review

**Files:**
- Modify: `docs/migration-handoff.md`
- Verify: all changed source and tests

**Step 1: Run focused safety tests**

```bash
./.venv/bin/python -m pytest \
  tests/test_position_attribution.py \
  tests/test_position_attribution_repair.py \
  tests/test_protection_attribution.py \
  tests/test_execution_bindings.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_web_app.py tests/test_web_page_render.py \
  tests/test_system_operator_bot.py -q
```

Expected: PASS.

**Step 2: Run syntax and asset checks**

```bash
./.venv/bin/python -m py_compile \
  src/telegram_kol_research/position_attribution.py \
  src/telegram_kol_research/protection_attribution.py \
  src/telegram_kol_research/position_attribution_repair.py \
  src/telegram_kol_research/execution_bindings.py \
  src/telegram_kol_research/web_app.py
node --check src/telegram_kol_research/static/app.js
git diff --check
```

Expected: exit 0.

**Step 3: Run the full suite**

```bash
./.venv/bin/python -m pytest tests -q
```

Expected: PASS, or exactly the recorded pre-existing baseline with no new/relevant failures.

**Step 4: Update durable documentation**

Document ownership authority, terminal manual actions, fail-closed states, repair command, and TPSL rules in `docs/migration-handoff.md`.

**Step 5: Request code review**

Use `@requesting-code-review`. Review specifically for remaining symbol/side fallback, transaction gaps, API failure interpreted as absence, mutation paths missing the gate, repair idempotency/stale-plan checks, and tests bypassing the real reconcile pipeline.

**Step 6: Commit review fixes and docs**

```bash
git add docs/migration-handoff.md src tests
git commit -m "docs: document position attribution safety"
```

### Task 10: Integrate, deploy fail closed, repair proven records, and verify

**Files:**
- Follow: `AGENTS.md`
- Follow: `docs/server-deployment.md`
- Verify: `scripts/server_git_update.ps1`
- Verify: `scripts/server_git_update.sh`

**Step 1: Integrate reviewed commits**

Update the local deployment branch without overwriting unrelated user work. Cherry-pick or fast-forward reviewed worktree commits onto `codex/deepcoin-auto-trading-v1`.

```bash
git status --short --branch
git log --oneline --decorate -12
```

Expected: reviewed attribution commits are on the deployment branch; unrelated files remain untouched.

**Step 2: Push the deployment branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub matches reviewed local HEAD.

**Step 3: Disable global automatic trading**

Use the existing operator control and verify `auto_trade_enabled=false`. Do not close positions or cancel orders.

**Step 4: Back up production database**

Create a timestamped copy of `/opt/telegram-kol-analyzer/data/research.db` before install or repair.

**Step 5: Deploy from GitHub**

Preferred Windows helper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

macOS/Linux equivalent:

```bash
./scripts/server_git_update.sh
```

Expected: pull deployment branch, reinstall editable package, restart service.

**Step 6: Run repair dry-run only**

```bash
cd /opt/telegram-kol-analyzer
. .venv/bin/activate
telegram-kol-research repair-position-attribution --database-path data/research.db
```

Expected: propose binding `112` correction and binding `120` leg-2 ownership, leave ambiguous records unresolved, and perform no exchange mutation.

**Step 7: Review evidence before apply**

Compare the plan with live positions, pending triggers, histories, fills, bindings, legs, events, and lifecycles. Stop if evidence is missing or stale.

**Step 8: Apply only reviewed changes**

```bash
telegram-kol-research repair-position-attribution \
  --database-path data/research.db \
  --apply
```

Expected: uniquely proven corrections only; every change audited.

**Step 9: Restart with automatic trading still disabled**

```bash
systemctl restart telegram-kol.service
systemctl is-active telegram-kol.service
```

Expected: `active`.

**Step 10: Verify repeated reconcile stability**

Across several intervals confirm both ETH shorts remain 智哥, binding `112` stays terminal, binding `120` keeps distinct leg ownership, both positions show SL `1820`, alerts do not duplicate, and the 舒琴 pending orders remain unchanged unless separately acted on.

**Step 11: Run server tests and HTTP smoke checks**

Run focused and full documented server tests. Verify `/`, `/positions-panel`, and read APIs return HTTP 200 and live HTML contains verified attribution markers.

**Step 12: Wait for explicit re-enable approval**

Do not infer approval from successful deployment. Report verified state and wait for the user to decide whether global automatic trading may be re-enabled.
