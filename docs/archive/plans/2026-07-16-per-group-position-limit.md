# Per-Group Effective Position Limit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop automatic new entries when the incoming Telegram group already owns four verified active Deepcoin positions, without blocking position management.

**Architecture:** Reinterpret the existing `max_concurrent_positions` setting as a per-`chat_id` cap and default it to four. Count distinct verified active entry-leg `pos_id` values through their exact Deepcoin binding, then apply the gate in the new-entry branch before any ticker or exchange access.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, FastAPI/Jinja, pytest.

---

### Task 1: Make the setting explicitly per-group with a default of four

**Files:**
- Modify: `tests/test_trading_settings.py`
- Modify: `tests/test_web_page_render.py`
- Modify: `src/telegram_kol_research/trading_settings.py:22-30`
- Modify: `src/telegram_kol_research/templates/index.html:332-338`
- Modify: `src/telegram_kol_research/static/app.js:1790-1802`

**Step 1: Write the failing default test**

Add to `test_load_trading_settings_returns_safe_defaults`:

```python
assert settings.max_concurrent_positions == 4
```

Add a Web assertion:

```python
def test_index_labels_position_limit_as_per_group(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/")

    assert response.status_code == 200
    assert "每群组最大有效持仓数" in response.text
    assert 'name="max_concurrent_positions"' in response.text
    assert 'value="4"' in response.text
```

**Step 2: Run the tests and verify RED**

Run:

```bash
pytest tests/test_trading_settings.py::test_load_trading_settings_returns_safe_defaults tests/test_web_page_render.py::test_index_labels_position_limit_as_per_group -q
```

Expected: FAIL because the default and label still use the old account-wide value `3`.

**Step 3: Implement the minimum setting and label changes**

Change the dataclass default:

```python
max_concurrent_positions: int = 4
```

Change the Jinja label to `每群组最大有效持仓数` and the JavaScript fallback to `4`. Do not rename the persisted key or add a schema migration.

**Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2.

Expected: `2 passed`.

**Step 5: Commit**

```bash
git add tests/test_trading_settings.py tests/test_web_page_render.py src/telegram_kol_research/trading_settings.py src/telegram_kol_research/templates/index.html src/telegram_kol_research/static/app.js
git commit -m "fix: define position limit per group"
```

### Task 2: Count exact effective positions for one group

**Files:**
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:1-35`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:660-735`

**Step 1: Write failing count tests**

Import `_count_group_effective_positions` and add one test that creates bindings for chat `100` and `200`, using `upsert_execution_order_leg` to cover:

```python
assert _count_group_effective_positions(session_factory, chat_id=100) == 4
assert _count_group_effective_positions(session_factory, chat_id=200) == 1
```

The four counted rows for chat `100` must have distinct non-empty `pos_id`, `purpose="entry"`, `status="active"`, and `attribution_status="verified"`. In the same fixture, add rows that must not count: unassigned attribution, terminal leg status, protection purpose, empty `pos_id`, non-Deepcoin binding, and another chat. The database already enforces unique `(venue, pos_id)` ownership, while `distinct` keeps the aggregate safe for legacy/bootstrap states.

**Step 2: Run the test and verify RED**

Run:

```bash
pytest tests/test_auto_trade_execution.py::test_count_group_effective_positions_uses_distinct_verified_active_entry_legs -q
```

Expected: FAIL because `_count_group_effective_positions` does not exist.

**Step 3: Implement the minimum SQLAlchemy query**

Import `func` and `ExecutionOrderLeg`, then add:

```python
def _count_group_effective_positions(
    session_factory: sessionmaker,
    *,
    chat_id: int,
) -> int:
    with session_factory() as session:
        count = (
            session.query(func.count(func.distinct(ExecutionOrderLeg.pos_id)))
            .join(
                ExecutionBinding,
                ExecutionBinding.id == ExecutionOrderLeg.execution_binding_id,
            )
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.chat_id == chat_id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.status == "active")
            .filter(ExecutionOrderLeg.attribution_status == "verified")
            .filter(ExecutionOrderLeg.pos_id.is_not(None))
            .filter(ExecutionOrderLeg.pos_id != "")
            .scalar()
        )
    return int(count or 0)
```

**Step 4: Run the focused test and verify GREEN**

Run the command from Step 2.

Expected: `1 passed`.

**Step 5: Commit**

```bash
git add tests/test_auto_trade_execution.py src/telegram_kol_research/auto_trade_execution.py
git commit -m "feat: count effective positions per group"
```

### Task 3: Gate new entries at four while preserving management

**Files:**
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:80-180`

**Step 1: Write the failing entry-gate test**

Create a normal entry candidate for chat `100`, save settings with auto trading enabled and `max_concurrent_positions=4`, and seed four verified active position legs for chat `100`. Call `auto_process_message_trade_signal` with a fake client whose ticker method raises if called.

Assert:

```python
assert result == {
    "status": "skipped",
    "reason": "group_position_limit_reached",
    "current_position_count": 4,
    "max_concurrent_positions": 4,
}
assert fake_client.orders == []
assert fake_client.trigger_orders == []

with session_factory() as session:
    event = session.query(ExecutionEvent).one()
    assert event.reason == "group_position_limit_reached"
    payload = json.loads(event.request_json)
    assert payload["current_position_count"] == 4
    assert payload["max_concurrent_positions"] == 4
```

Add a boundary case with three positions in chat `100` and four in another chat; it must reach the existing submission path for chat `100`. Retain the existing management tests as proof that management branches before this gate.

**Step 2: Run the focused tests and verify RED**

Run:

```bash
pytest tests/test_auto_trade_execution.py -k 'group_position_limit or management_disabled or management_planning' -q
```

Expected: the new limit test FAILS because the entry still reads the ticker/submits; existing management tests PASS.

**Step 3: Implement the minimum gate**

After group, symbol, confidence, and media gates but before `_safe_ticker_price`, add:

```python
current_position_count = _count_group_effective_positions(
    session_factory,
    chat_id=raw_message.chat_id,
)
if current_position_count >= settings.max_concurrent_positions:
    return _record_entry_auto_trade_skip(
        session_factory,
        raw_message=raw_message,
        candidate=candidate,
        reason="group_position_limit_reached",
        runtime_kol_id=str(runtime_config.get("kol_id") or ""),
        processed_at=now,
        extra={
            "current_position_count": current_position_count,
            "max_concurrent_positions": settings.max_concurrent_positions,
        },
    )
```

Do not apply this check to `_auto_process_management_signal`.

**Step 4: Run focused and module tests**

Run:

```bash
pytest tests/test_auto_trade_execution.py -q
pytest tests/test_trading_settings.py tests/test_web_page_render.py tests/test_web_app.py -q
```

Expected: all tests PASS.

**Step 5: Commit**

```bash
git add tests/test_auto_trade_execution.py src/telegram_kol_research/auto_trade_execution.py
git commit -m "feat: block entries at per-group position limit"
```

### Task 4: Document the operational meaning

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`

**Step 1: Add concise documentation**

Document that `max_concurrent_positions` is per `chat_id`, defaults to four, counts distinct verified active entry `posId`s, ignores pending orders, and never blocks management actions. Record the accepted non-transactional race boundary.

**Step 2: Verify documentation and formatting**

Run:

```bash
rg -n "每群组|per-group|max_concurrent_positions|group_position_limit_reached" docs src tests
git diff --check
```

Expected: the new rule appears in code, tests, and durable documentation; `git diff --check` exits `0`.

**Step 3: Commit**

```bash
git add docs/runbook.md docs/migration-handoff.md
git commit -m "docs: record per-group position cap"
```

### Task 5: Review, verify, deploy, and update production to four

**Files:**
- Verify: all changed files
- Production settings update only through `POST /api/trading-settings`

**Step 1: Inspect the complete change**

Run:

```bash
git status --short
git diff HEAD~4 --check
git diff HEAD~4 --stat
git log -5 --oneline
```

Use the `requesting-code-review` skill before declaring implementation complete. Address every correctness or safety finding with a new failing test first.

**Step 2: Run the full local suite**

Run:

```bash
pytest -q
```

Expected: all tests PASS.

**Step 3: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub branch advances to the reviewed commit.

**Step 4: Deploy through the approved helper**

Run:

```bash
./scripts/server_git_update.sh
```

Expected: server pulls the branch, reinstalls the editable package, and restarts `telegram-kol.service` successfully.

**Step 5: Run production read-only preflight**

Confirm the server SHA, `telegram-kol.service=active`, HTTP settings availability, live gates still equal `auto_trade_enabled=true` and `management_execution_mode=live`, complete stable management audit, and no new error logs. Do not send a test message or place an order.

**Step 6: Preserve all settings and update only the approved limit**

Fetch `GET /api/trading-settings`, change only `max_concurrent_positions` to `4`, and POST the complete payload back. Abort without writing if either live gate or any other field differs from the preflight snapshot.

**Step 7: Verify production**

Read settings again and confirm `max_concurrent_positions=4`, live gates unchanged, service active, audit stable/complete, and no new errors. Continue the existing read-only heartbeat; do not generate a test trade.

**Step 8: Report**

Report the final commit SHA, local/server test counts, service status, live gates, per-group limit, audit status, and the accepted race boundary.
