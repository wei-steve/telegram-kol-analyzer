# Multi-Position Management Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix live strategy-management planning so a verified split strategy with multiple `execution_order_legs.pos_id` values is not incorrectly blocked when `execution_bindings.pos_id` stores the same positions as a comma-separated legacy summary.

**Architecture:** Keep `execution_order_legs` as the authority for position ownership. Treat `execution_bindings.pos_id` only as a backward-compatible summary that may contain one posId or a comma-separated set, and require it to be a subset of verified entry-leg posIds before planning a close/protection batch. Do not loosen attribution, do not infer ownership from symbol/side, and do not submit any exchange action during verification.

**Tech Stack:** Python, SQLAlchemy ORM, SQLite, pytest, existing Deepcoin read-only client abstractions.

## Global Constraints

- Work locally first; production verification must run on the server because Deepcoin credentials and Telegram session are server-only.
- Start read-only for the live incident; do not place/cancel orders or mutate production data during diagnosis.
- Preserve the truth chain: `message -> candidate -> lifecycle -> binding -> entry legs -> exchange state`.
- `execution_order_legs` remains the persisted authority for exact Deepcoin `posId` ownership.
- Ambiguous, missing, conflicting, or evidence-unavailable ownership must still fail closed.
- Do not replay the already-blocked production management batch automatically after deploying this fix; any live remediation needs explicit operator approval.

---

## File Structure

- Modify: `src/telegram_kol_research/strategy_management_planner.py`
  - Add a small parser for legacy binding position summaries.
  - Update `_unsafe_entry_leg_reason()` to compare sets rather than treating `binding.pos_id` as one exact posId.
- Modify: `tests/test_strategy_management_planner.py`
  - Add focused regression tests for comma-separated binding posIds.
  - Keep all tests side-effect-free with `_ReadOnlyDeepcoin`.
- Optional Modify: `docs/migration-handoff.md`
  - Add a short non-secret operational note after the fix is verified, documenting that `execution_bindings.pos_id` can be a summary only and entry legs are authoritative.

---

### Task 1: Reproduce The False Mismatch

**Files:**
- Modify: `tests/test_strategy_management_planner.py`

**Interfaces:**
- Consumes: `_persist_exact_management_target(...)`, `_disable_reconciliation(...)`, `_ReadOnlyDeepcoin`, `_position`.
- Produces: a failing regression test proving that `binding.pos_id="pos-b,pos-c"` with verified entry legs `pos-b` and `pos-c` should plan a batch.

- [ ] **Step 1: Write the failing test**

Add this test near `test_unqualified_first_partial_plan_defaults_to_half`:

```python
def test_partial_plan_accepts_legacy_comma_separated_binding_pos_ids(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(
        session_factory,
        intent="partial_take_profit",
        management_fraction=None,
        pos_ids=("pos-b", "pos-c"),
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "pos-b,pos-c"
        session.commit()
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [
                _position("pos-b", size="10", avg_px="62000"),
                _position("pos-c", size="8", avg_px="62100"),
            ]
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "ready"
    assert result.batch.effective_action == "partial_close"
    assert result.batch.effective_fraction == 0.5
    assert [leg.pos_id for leg in result.batch.legs] == ["pos-b", "pos-c"]
    assert [leg.planned_close_size for leg in result.batch.legs] == ["5", "4"]
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
.venv/bin/pytest tests/test_strategy_management_planner.py::test_partial_plan_accepts_legacy_comma_separated_binding_pos_ids -q
```

Expected: FAIL with `result.status == "blocked"` and `reason_code == "target_binding_position_mismatch"`.

- [ ] **Step 3: Commit only if using strict TDD checkpoint commits**

Skip this commit if the current working tree already has unrelated user changes. Otherwise:

```bash
git add tests/test_strategy_management_planner.py
git commit -m "test: reproduce multi-position management binding mismatch"
```

---

### Task 2: Fix Binding Summary Validation

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `tests/test_strategy_management_planner.py`

**Interfaces:**
- Produces: `_binding_position_id_set(binding_pos_id: str | None) -> set[str]`.
- Produces: `_unsafe_entry_leg_reason(...)` accepts comma-separated summaries only when every summary posId is present in verified entry legs.

- [ ] **Step 1: Add parser tests**

Add these tests near the new regression test:

```python
def test_partial_plan_rejects_binding_summary_with_unknown_pos_id(
    monkeypatch, tmp_path
):
    planner = _planner()
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, binding_id = _persist_exact_management_target(
        session_factory,
        intent="partial_take_profit",
        management_fraction=None,
        pos_ids=("pos-b", "pos-c"),
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "pos-b,pos-x"
        session.commit()
    _disable_reconciliation(monkeypatch, planner)

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [
                _position("pos-b", size="10", avg_px="62000"),
                _position("pos-c", size="8", avg_px="62100"),
            ]
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )

    assert result.status == "blocked"
    assert result.reason_code == "target_binding_position_mismatch"
    assert result.batch is not None
    assert result.batch.legs == []
```

- [ ] **Step 2: Run the parser tests to verify current behavior**

Run:

```bash
.venv/bin/pytest tests/test_strategy_management_planner.py \
  -k 'legacy_comma_separated_binding_pos_ids or binding_summary_with_unknown_pos_id' -q
```

Expected: one FAIL for the valid comma-separated case; the unknown-pos case may PASS already or fail for the same broad reason.

- [ ] **Step 3: Implement the minimal helper and set comparison**

In `src/telegram_kol_research/strategy_management_planner.py`, add this helper immediately above `_unsafe_entry_leg_reason`:

```python
def _binding_position_id_set(binding_pos_id: str | None) -> set[str]:
    if not binding_pos_id:
        return set()
    return {
        item.strip()
        for item in str(binding_pos_id).split(",")
        if item.strip()
    }
```

Replace:

```python
    if binding.pos_id and str(binding.pos_id) not in set(position_ids):
        return "target_binding_position_mismatch"
```

with:

```python
    binding_position_ids = _binding_position_id_set(binding.pos_id)
    if binding_position_ids and not binding_position_ids.issubset(set(position_ids)):
        return "target_binding_position_mismatch"
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_strategy_management_planner.py \
  -k 'legacy_comma_separated_binding_pos_ids or binding_summary_with_unknown_pos_id' -q
```

Expected: PASS.

- [ ] **Step 5: Run planner regression tests**

Run:

```bash
.venv/bin/pytest tests/test_strategy_management_planner.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py tests/test_strategy_management_planner.py
git commit -m "fix: accept verified split position bindings"
```

---

### Task 3: Verify Execution Gates Stay Fail-Closed

**Files:**
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_executor.py`
- Test: `tests/test_strategy_management_reconciliation.py`

**Interfaces:**
- Consumes: Task 2 helper behavior.
- Produces: confidence that planning can create legs for verified split positions while execution/reconciliation still require exact entry-leg identity.

- [ ] **Step 1: Run management planner, executor, and reconciliation suites**

Run:

```bash
.venv/bin/pytest \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run a smoke subset around auto-trade management bridging**

Run:

```bash
.venv/bin/pytest tests/test_auto_trade_execution.py -k 'partial or management or close' -q
```

Expected: PASS.

- [ ] **Step 3: Inspect for accidental exchange writes in tests**

Run:

```bash
rg -n "place_order|cancel_order|cancel_trigger_order|set_position_sltp" tests/test_strategy_management_planner.py
```

Expected: only `_ReadOnlyDeepcoin` guard methods and no new direct write path from planner tests.

- [ ] **Step 4: Commit only if additional fixes were required**

```bash
git add src/telegram_kol_research/strategy_management_planner.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_strategy_management_reconciliation.py tests/test_auto_trade_execution.py
git commit -m "test: preserve management execution safety gates"
```

If no files changed in this task, do not commit.

---

### Task 4: Document And Deploy Safely

**Files:**
- Optional Modify: `docs/migration-handoff.md`

**Interfaces:**
- Consumes: all local tests passing.
- Produces: deployed code and read-only production evidence. No automatic replay of batch `5`.

- [ ] **Step 1: Optionally document the non-secret invariant**

Add this paragraph under `## Deepcoin position-attribution authority` in `docs/migration-handoff.md`:

```markdown
For legacy compatibility, `execution_bindings.pos_id` may be empty, a single
posId, or a comma-separated summary of split-position posIds. Management
planning must treat this field as a summary only. The authoritative ownership
set remains the verified nonterminal `execution_order_legs` entry rows; a
binding summary may only narrow that set, never expand it or prove ownership by
itself.
```

- [ ] **Step 2: Run the final local verification**

Run:

```bash
.venv/bin/pytest \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_strategy_management_reconciliation.py \
  tests/test_auto_trade_execution.py -q
```

Expected: PASS.

- [ ] **Step 3: Stage and commit only intended files**

```bash
git add src/telegram_kol_research/strategy_management_planner.py \
  tests/test_strategy_management_planner.py \
  docs/superpowers/plans/2026-07-17-multi-position-management-binding.md
git add -p docs/migration-handoff.md
git diff --cached
git commit -m "fix: accept verified split position bindings"
```

When staging `docs/migration-handoff.md`, include only the invariant under
`## Deepcoin position-attribution authority`; leave unrelated existing hunks
unstaged.

- [ ] **Step 4: Push the reviewed branch**

Run:

```bash
git status --short
git push origin codex/deepcoin-auto-trading-v1
```

Expected: only intentional changes are committed; push succeeds.

- [ ] **Step 5: Deploy through the approved server helper**

Run from the repo root:

```bash
./scripts/server_git_update.sh
```

Expected: server pulls the pushed commit, reinstalls the editable package, restarts `telegram-kol.service`, and reports the service active.

- [ ] **Step 6: Server read-only verification**

Run this on the server after deployment:

```bash
cd /opt/telegram-kol-analyzer
git rev-parse --short HEAD
systemctl is-active telegram-kol.service
.venv/bin/pytest tests/test_strategy_management_planner.py -k 'legacy_comma_separated_binding_pos_ids or binding_summary_with_unknown_pos_id' -q
```

Expected: HEAD is the pushed commit, service is `active`, focused tests PASS.

- [ ] **Step 7: Production incident re-check without replay**

Run this read-only SQL on the server:

```bash
cd /opt/telegram-kol-analyzer
sqlite3 -json data/research.db "
SELECT id, raw_message_id, target_lifecycle_id, execution_binding_id,
       intent, effective_action, execution_mode, effective_fraction,
       status, reason_code, target_snapshot_json
FROM strategy_management_batches
WHERE id=5;

SELECT id, execution_binding_id, leg_index, purpose, pos_id,
       attribution_status, status, terminal_reason
FROM execution_order_legs
WHERE execution_binding_id=142
ORDER BY leg_index;
"
```

Expected: batch `5` remains `blocked`; entry legs for binding `142` remain verified. Do not update batch `5`, do not replay message `4027`, and do not submit a manual close as part of verification.

---

## Self-Review

- Spec coverage: The plan fixes the exact false mismatch observed in production while preserving verified entry-leg authority and fail-closed behavior.
- Placeholder scan: No unfinished placeholder steps remain.
- Type consistency: The helper returns `set[str]`; `_unsafe_entry_leg_reason()` already receives ORM `ExecutionBinding` and entry legs, so no public interface changes are required.
