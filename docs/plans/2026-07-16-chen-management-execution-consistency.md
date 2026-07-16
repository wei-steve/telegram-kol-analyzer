# Chen Strategy Management Execution Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Telegram position-management percentages, Deepcoin contract sizing, lifecycle state, and Web execution status agree with exchange-confirmed reality.

**Architecture:** Recognition persists immutable management intent on `SignalCandidate`; the management planner resolves exact positions and step-aligned quantities; the executor revalidates persisted quantities; reconciliation alone promotes confirmed lifecycle state. Web message and strategy timelines combine recognition outcomes with batch state so intent is never presented as execution success.

**Tech Stack:** Python 3.11+, SQLAlchemy, SQLite, FastAPI/Jinja2, pytest, Deepcoin REST models.

## Global Constraints

- MiMo remains authoritative for trading intent.
- Ambiguous percentages, ownership, contract specifications, or exchange state fail closed.
- No live Deepcoin order is used as a test fixture.
- Real verification runs on the production server after reviewed commits are pushed.
- Deployment uses GitHub push, server pull/editable reinstall, and `telegram-kol.service` restart.

---

### Task 1: Normalize close percentages without mutating confirmed lifecycle state

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py`
- Test: `tests/test_message_recognition.py`

**Interfaces:**
- Consumes: MiMo lifecycle event mapping and Telegram message text.
- Produces: `normalize_management_intent(decision, text) -> tuple[str, float | None]` and an intent-only `SignalCandidate` containing requested stop/take-profit values.

- [ ] **Step 1: Write failing percentage-semantics tests**

Add cases proving `止盈 60%` returns `0.6`, `保留 40%` returns `0.6`, and conflicting `止盈 30%，保留 40%` is rejected as ambiguous.

```python
def test_normalize_management_intent_converts_retained_fraction_to_close_fraction():
    action, fraction = normalize_management_intent(
        {"event_type": "position_update", "management_action": "partial_take_profit"},
        "其余可以保留40%底仓",
    )
    assert action == "partial_take_profit"
    assert fraction == pytest.approx(0.6)
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
../.venv/bin/python -m pytest tests/test_message_recognition.py -k 'retained_fraction or conflicting_management_fraction' -v
```

Expected: the retained-fraction assertion fails because current code returns `None`; the conflict test fails because no ambiguity error exists.

- [ ] **Step 3: Implement explicit close-versus-retain parsing**

Add a small parser that extracts close-context percentages and retain-context percentages separately, converts retain `x` into close `1 - x`, and raises a dedicated normalization error when independently explicit values conflict. Preserve the existing unqualified `None` result so the two-round policy can apply its 50% default.

- [ ] **Step 4: Write and verify lifecycle immutability RED tests**

Create an entered lifecycle with confirmed stop `67100`, apply Chen message `#9527`, and assert the candidate carries the requested intent while the lifecycle retains `67100`, its prior management metadata, and its entered status.

- [ ] **Step 5: Implement intent-only position updates**

Change `_apply_lifecycle_event_decision` and `_upsert_management_signal_candidate` so recognition does not update confirmed lifecycle stop-loss, take-profit, management message, action, or success note. Pass requested stop/take-profit values explicitly into the candidate; use neutral intent wording rather than `已调整`.

- [ ] **Step 6: Run focused tests and commit**

```bash
../.venv/bin/python -m pytest tests/test_message_recognition.py tests/test_authoritative_recognition.py -q
git add src/telegram_kol_research/message_recognition.py tests/test_message_recognition.py
git commit -m "fix: preserve confirmed lifecycle during recognition"
```

Expected: all selected tests pass.

---

### Task 2: Enforce Deepcoin quantity step and minimum quantity at planning and execution

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_sizing.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Test: `tests/test_strategy_management_sizing.py`
- Test: `tests/test_strategy_management_executor.py`

**Interfaces:**
- Consumes: live per-position sizes, requested close fraction, `quantity_step`, and `min_quantity`.
- Produces: `allocate_close_sizes(...) -> tuple[str, ...]` and executor-side validation of `planned_close_size`.

- [ ] **Step 1: Write failing Chen `6 + 5` contract tests**

Add a sizing test for a 60% aggregate close with step/minimum `1`. Assert every result is integral, total close is `6`, no leg exceeds its position, and no output is `2.4` or `3.6`.

```python
def test_chen_split_positions_allocate_sixty_percent_on_integer_contract_steps():
    planned = allocate_close_sizes(
        ["6", "5"], fraction="0.6", quantity_step="1", min_quantity="1"
    )
    assert planned == ("3", "3")
```

- [ ] **Step 2: Add failing executor tamper tests**

Persist a planned leg with `planned_close_size="2.4"` and `quantity_step="1"`; assert execution blocks before `place_order`. Add cases below `min_quantity`, above live position size, and a valid integer request.

- [ ] **Step 3: Run the tests and verify RED**

```bash
../.venv/bin/python -m pytest tests/test_strategy_management_sizing.py tests/test_strategy_management_executor.py -k 'chen_split or off_step or below_minimum' -v
```

Expected: the executor off-step test reaches the fake client or lacks the required structured rejection.

- [ ] **Step 4: Implement executor-side decimal validation**

Before each close request, parse current size, planned size, persisted `quantity_step`, and contract minimum as bounded `Decimal` values. Require positive finite values, exact step alignment, minimum quantity, and `planned <= current`. Reject with fixed reasons such as `planned_close_size_off_step`, `planned_close_size_below_minimum`, and `planned_close_size_exceeds_live_position`.

- [ ] **Step 5: Run sizing/executor tests and commit**

```bash
../.venv/bin/python -m pytest tests/test_strategy_management_sizing.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py -q
git add src/telegram_kol_research/strategy_management_sizing.py src/telegram_kol_research/strategy_management_executor.py tests/test_strategy_management_sizing.py tests/test_strategy_management_executor.py
git commit -m "fix: enforce Deepcoin management quantity steps"
```

Expected: all selected tests pass and fake Deepcoin calls contain only step-aligned quantities.

---

### Task 3: Promote lifecycle management state only after exchange confirmation

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_reconciliation.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Test: `tests/test_strategy_management_reconciliation.py`
- Test: `tests/test_strategy_management_executor.py`

**Interfaces:**
- Consumes: succeeded batch, source raw message, confirmed close legs, and confirmed replacement-protection rows.
- Produces: confirmed `StrategyLifecycle.management_*`, stop-loss, take-profit, partial event, or exit transition.

- [ ] **Step 1: Write failure-preservation tests**

For `blocked`, `partial_failed`, `submit_unknown`, and disabled outcomes, assert lifecycle stop, status, exit time, and management metadata remain unchanged.

- [ ] **Step 2: Write success-promotion tests**

Assert a reconciled partial close records the source message/action without exiting; a successful protection replacement stores the confirmed stop; and a reconciled full exit alone sets `lifecycle_status="exited"`.

- [ ] **Step 3: Run and verify RED**

```bash
../.venv/bin/python -m pytest tests/test_strategy_management_reconciliation.py tests/test_strategy_management_executor.py -k 'lifecycle and (confirmed or failed or disabled)' -v
```

Expected: protection and partial confirmation tests fail because current code does not own all lifecycle promotion, while recognition currently supplied stale values.

- [ ] **Step 4: Implement one confirmation helper**

Add a transaction-local helper that resolves the exact source message and target lifecycle from the immutable batch. Call it only when all applicable legs are confirmed/succeeded. Keep `_terminalize_full_close` as the full-exit authority and route partial/protection success through the same confirmed-state boundary.

- [ ] **Step 5: Run reconciliation/executor tests and commit**

```bash
../.venv/bin/python -m pytest tests/test_strategy_management_reconciliation.py tests/test_strategy_management_executor.py -q
git add src/telegram_kol_research/strategy_management_reconciliation.py src/telegram_kol_research/strategy_management_executor.py tests/test_strategy_management_reconciliation.py tests/test_strategy_management_executor.py
git commit -m "fix: confirm lifecycle management from exchange truth"
```

---

### Task 4: Show recognition and execution outcomes separately in Web views

**Files:**
- Modify: `src/telegram_kol_research/web_queries.py`
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/templates/_position_card.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_queries.py`

**Interfaces:**
- Consumes: `RecognitionDecision.automation_status/reason`, latest matching management batch status/reason, and confirmed lifecycle values.
- Produces: normalized execution outcome with `label`, `state`, and `reason` for message cards and lifecycle events.

- [ ] **Step 1: Write failing serialization and render tests**

Cover labels `已识别，未执行`, `执行失败`, `执行中/待交易所确认`, and `已执行`. Assert a failed #9527-style message does not render `止损已调整` and the strategy card retains the confirmed `67100` value.

- [ ] **Step 2: Run and verify RED**

```bash
../.venv/bin/python -m pytest tests/test_web_queries.py tests/test_web_page_render.py -k 'management_execution_outcome' -v
```

Expected: execution outcome fields and labels are absent.

- [ ] **Step 3: Implement bounded outcome serialization**

Bulk-load relevant management batches by raw message ID alongside existing recognition decisions. Prefer durable batch state when present; otherwise use the recognition automation outcome. Map only fixed status/reason labels and do not expose raw exception text.

- [ ] **Step 4: Render outcome badges and confirmed values**

Add compact status markup to management message details and strategy timeline. Keep the strategy summary sourced from confirmed lifecycle/exchange fields; show requested intent separately.

- [ ] **Step 5: Run Web tests and commit**

```bash
../.venv/bin/python -m pytest tests/test_web_queries.py tests/test_web_page_render.py tests/test_web_app.py -q
git add src/telegram_kol_research/web_queries.py src/telegram_kol_research/templates/_messages.html src/telegram_kol_research/templates/_position_card.html src/telegram_kol_research/static/app.css tests/test_web_queries.py tests/test_web_page_render.py
git commit -m "fix: distinguish management intent from execution"
```

---

### Task 5: Regression, documentation, review, and production verification

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/runbook.md`

**Interfaces:**
- Consumes: completed implementation and test evidence.
- Produces: durable operating guidance and verified deployment.

- [ ] **Step 1: Add Chen incident regression coverage**

Add an end-to-end test fixture using messages #9520, #9522, #9525, and #9527. Assert exact target lifecycle, two verified `posId` legs, correct 60% interpretation, step-aligned plans, no premature lifecycle mutation, and explicit Web outcome.

- [ ] **Step 2: Run focused and full local verification**

```bash
../.venv/bin/python -m pytest tests/test_message_recognition.py tests/test_authoritative_recognition.py tests/test_strategy_management_sizing.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_strategy_management_reconciliation.py tests/test_web_queries.py tests/test_web_page_render.py tests/test_web_app.py -q
../.venv/bin/python -m pytest tests -q
```

Expected: zero failures.

- [ ] **Step 3: Update durable docs and commit**

Document percentage semantics, quantity-step rejection, confirmation authority, Web outcome labels, and the prohibition on replaying old management messages.

```bash
git add docs/migration-handoff.md docs/runbook.md
git commit -m "docs: record confirmed management execution rules"
```

- [ ] **Step 4: Review exact scope**

```bash
git status --short
git diff --check origin/codex/deepcoin-auto-trading-v1...HEAD
git log --oneline origin/codex/deepcoin-auto-trading-v1..HEAD
```

Confirm no secrets, unrelated files, live test orders, or unreviewed local commits are included.

- [ ] **Step 5: Push, deploy, and verify server**

After reconciling the pre-existing local commits, push reviewed commits to `codex/deepcoin-auto-trading-v1`, run:

```bash
./scripts/server_git_update.sh
```

Then verify on `/opt/telegram-kol-analyzer`:

```bash
git rev-parse HEAD
systemctl is-active telegram-kol.service
sqlite3 -readonly data/research.db "PRAGMA quick_check;"
.venv/bin/python -m pytest tests/test_message_recognition.py tests/test_strategy_management_sizing.py tests/test_strategy_management_executor.py tests/test_strategy_management_reconciliation.py tests/test_web_page_render.py -q
```

Expected: deployed SHA matches the reviewed commit, service is `active`, SQLite reports `ok`, and focused server tests have zero failures. Do not submit a live test order.

