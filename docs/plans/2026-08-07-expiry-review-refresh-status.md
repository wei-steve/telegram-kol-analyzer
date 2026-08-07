# Expiry Review Status Refresh Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace “过期但保留挂单” with a safe live “更新状态” action that refreshes per-leg Deepcoin state, edits the original Telegram message, and removes the buttons when no unresolved entry leg remains.

**Architecture:** Reuse `reconcile_deepcoin_execution_bindings` as the only authority for exchange state and attribution. A Telegram-facing result reads the selected lifecycle, binding, and entry legs after reconciliation, formats one replaceable status section, and decides whether the keyboard remains by combining lifecycle eligibility with the existing unresolved-entry-leg predicate. Keep the legacy `expiry_expire_keep` handler for already-sent messages.

**Tech Stack:** Python 3.12, SQLAlchemy, httpx, pytest, Telegram Bot API, existing Deepcoin reconciliation modules.

---

### Task 1: Replace the emitted button and recognize refresh callbacks

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py:1761-1782`
- Modify: `src/telegram_kol_research/telegram_bot_commands.py:26-32`
- Modify: `src/telegram_kol_research/telegram_bot_commands.py:99-181`
- Test: `tests/test_system_operator_bot.py:2110-2127`

**Step 1: Write the failing markup test**

Change the expected third button to:

```python
{
    "text": "更新状态",
    "callback_data": "expiry_refresh:354",
}
```

Add a focused test:

```python
@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("expiry_refresh:354", True),
        ("expiry_expire_cancel:354", True),
        ("expiry_continue:354", False),
        ("expiry_expire_keep:354", False),
    ],
)
def test_expiry_callback_deepcoin_client_requirement(callback_data, expected):
    assert bot_commands_module._expiry_callback_needs_deepcoin_client(callback_data) is expected
```

**Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q tests/test_system_operator_bot.py::test_build_pending_entry_expiry_review_reply_markup_uses_lifecycle_id_callbacks tests/test_system_operator_bot.py::test_expiry_callback_deepcoin_client_requirement
```

Expected: FAIL because the old button still emits `expiry_expire_keep` and the helper does not exist.

**Step 3: Implement the minimal callback constant and predicate**

```python
EXPIRY_REFRESH_COMMAND = "expiry_refresh"


def _expiry_callback_needs_deepcoin_client(callback_data: str) -> bool:
    action, separator, _ = callback_data.partition(":")
    return separator == ":" and action in {
        EXPIRY_EXPIRE_CANCEL_COMMAND,
        EXPIRY_REFRESH_COMMAND,
    }
```

Use the predicate in the bot loop and emit `更新状态` / `expiry_refresh:<id>` from the markup builder. Do not remove the legacy keep-order constant or handler.

**Step 4: Run the tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/system_operator_bot.py src/telegram_kol_research/telegram_bot_commands.py tests/test_system_operator_bot.py
git commit -m "feat: add expiry review refresh button"
```

### Task 2: Reconcile and summarize the selected strategy

**Files:**
- Modify: `src/telegram_kol_research/telegram_bot_commands.py:10-23`
- Modify: `src/telegram_kol_research/telegram_bot_commands.py:189-430`
- Test: `tests/test_system_operator_bot.py`

**Step 1: Write the failing partial-entry test**

Create one lifecycle, one binding, and two `ExecutionOrderLeg` rows: an `active` verified leg with `pos_id="pos-filled"`, and a `pending` leg with `order_id="order-pending"`. Monkeypatch `reconcile_deepcoin_execution_bindings` to prove the processor invokes the existing authority without network credentials.

```python
assert result.keep_actions is True
assert "入场进度：1/2 条腿已入场，1/2 条腿挂单中" in result.status_text
assert "第1腿：已入场" in result.status_text
assert "仓位 ID: pos-filled" in result.status_text
assert "第2腿：挂单中" in result.status_text
assert "订单 ID: order-pending" in result.status_text
```

Also assert the reconciler receives the injected client and `recovered_at`.

**Step 2: Run the test and verify RED**

```bash
pytest -q tests/test_system_operator_bot.py::test_refresh_expiry_review_status_reports_active_and_pending_legs
```

Expected: FAIL because the result type and processor do not exist.

**Step 3: Add the minimal result type and processor**

```python
@dataclass(frozen=True, slots=True)
class ExpiryReviewRefreshResult:
    answer_text: str
    status_text: str
    keep_actions: bool
```

Implement `refresh_expiry_review_status(...)` to:

1. Resolve the lifecycle identifier with `_parse_operator_lifecycle_identifier`.
2. Require a Deepcoin client and call `reconcile_deepcoin_execution_bindings(session_factory, client=..., recovered_at=event_at)`.
3. Reload the lifecycle after reconciliation.
4. Resolve its exact binding by `execution_binding_id`, then by the existing stable source keys only if needed.
5. Load `purpose == "entry"` legs ordered by `leg_index`.
6. Format lifecycle and leg states through an explicit Chinese label map.
7. Set `keep_actions` only when lifecycle status is `pending_entry` or `entered` and `binding_has_unresolved_entry_leg(session, binding)` is true.

Do not directly mutate lifecycle, binding, or leg state outside reconciliation.

**Step 4: Run the test and verify GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Write failing terminal-state tests**

Add separate cases for:

- two verified active legs with positions: remove actions;
- one active leg plus one cancelled leg: remove actions;
- lifecycle already cancelled by a group message: remove actions even if stale order fields remain;
- pending lifecycle with a genuinely pending leg: retain actions.

Each test must assert the Chinese per-leg summary, not only the boolean.

**Step 6: Run the new cases and verify RED**

```bash
pytest -q tests/test_system_operator_bot.py -k "refresh_expiry_review_status and (all_entered or cancelled or unresolved)"
```

Expected: at least one FAIL until both lifecycle and exact-leg rules are complete.

**Step 7: Complete the minimal label map and action rule**

```python
LEG_STATUS_LABELS = {
    "active": "已入场",
    "partially_filled": "部分成交",
    "pending": "挂单中",
    "open": "挂单中",
    "submitted": "已提交",
    "cancelled": "已取消",
    "manually_cancelled": "已取消",
    "exchange_cancelled": "已取消",
    "expired": "已失效",
    "unknown": "状态待确认",
}
```

Treat a verified leg with a non-empty `pos_id` as entered for display, without altering persisted status. Bound displayed identifiers to keep the Telegram message below its limit.

**Step 8: Run all processor tests and verify GREEN**

```bash
pytest -q tests/test_system_operator_bot.py -k "refresh_expiry_review_status"
```

Expected: PASS.

**Step 9: Commit**

```bash
git add src/telegram_kol_research/telegram_bot_commands.py tests/test_system_operator_bot.py
git commit -m "feat: summarize refreshed expiry strategy state"
```

### Task 3: Replace one status section and preserve or remove buttons

**Files:**
- Modify: `src/telegram_kol_research/telegram_bot_commands.py:127-165`
- Modify: `src/telegram_kol_research/telegram_bot_commands.py:728-822`
- Test: `tests/test_system_operator_bot.py:2580-2640`

**Step 1: Write failing edit-message tests**

Verify:

1. Partial-entry refresh sends the refreshed text with the three-button `reply_markup`.
2. All-entered or cancelled refresh sends `{"inline_keyboard": []}` to remove the keyboard.
3. Refreshing text that already contains `【最新策略状态】` leaves exactly one heading.

```python
edit_payload = client.posts[-1][1]
assert edit_payload["text"].count("【最新策略状态】") == 1
assert edit_payload["reply_markup"] == build_pending_entry_expiry_review_reply_markup(
    {"lifecycle_id": 789}
)
```

**Step 2: Run the edit tests and verify RED**

```bash
pytest -q tests/test_system_operator_bot.py -k "refresh_callback_response or replace_expiry_refresh_status"
```

Expected: FAIL because message editing cannot pass reply markup and refresh uses the irreversible legacy resolution formatter.

**Step 3: Implement replacement and keyboard handling**

```python
EXPIRY_REFRESH_STATUS_HEADING = "【最新策略状态】"


def _replace_expiry_refresh_status(original_text: str, status_text: str) -> str:
    base = original_text.split(EXPIRY_REFRESH_STATUS_HEADING, 1)[0].rstrip()
    return f"{base}\n\n{EXPIRY_REFRESH_STATUS_HEADING}\n{status_text}".strip()
```

Extend `_edit_message_text` with optional `reply_markup`. Route `expiry_refresh` through the refresh processor and use the three-button markup only when `keep_actions` is true; otherwise send an empty inline keyboard. Keep legacy actions on `_format_callback_resolution_text`.

**Step 4: Run the edit tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

**Step 5: Add and pass the failure-path test**

Make the reconciler raise. Assert the result says `更新失败，未改变策略或挂单状态`, does not include secrets, and retains the three buttons. Catch the reconciliation boundary, log the exception, and return the conservative result instead of letting the outer loop swallow the update.

```bash
pytest -q tests/test_system_operator_bot.py::test_refresh_expiry_review_status_failure_keeps_actions
```

Expected: PASS after the minimal failure handler.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/telegram_bot_commands.py tests/test_system_operator_bot.py
git commit -m "feat: refresh expiry review message in place"
```

### Task 4: Regression verification and review

**Files:**
- Test: `tests/test_system_operator_bot.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Run focused tests**

```bash
pytest -q tests/test_system_operator_bot.py -k "expiry or callback_response or refresh"
```

Expected: PASS.

**Step 2: Run the complete operator-bot module**

```bash
pytest -q tests/test_system_operator_bot.py
```

Expected: PASS.

**Step 3: Run reconciliation regressions**

```bash
pytest -q tests/test_execution_bindings.py
```

Expected: PASS. If it is too slow locally, run directly affected reconciliation and unresolved-leg cases, then explicitly leave the full server test pending.

**Step 4: Run static checks**

```bash
python -m compileall -q src/telegram_kol_research tests/test_system_operator_bot.py
git diff --check
```

Expected: both exit 0.

**Step 5: Review the final diff**

Confirm no credentials were added, refresh calls no exchange write path, the legacy callback remains compatible, incomplete evidence retains buttons, and unrelated dirty files are untouched. Use the available `requesting-code-review` workflow before deployment.

**Step 6: Commit final test/doc fixes if needed**

```bash
git add src/telegram_kol_research/telegram_bot_commands.py src/telegram_kol_research/system_operator_bot.py tests/test_system_operator_bot.py docs/
git commit -m "test: cover expiry review status refresh"
```

Skip this commit when there are no remaining feature changes.

### Task 5: Push and verify on the production server

**Files:**
- No source changes expected

**Step 1: Confirm a safe deployment window**

Before restart, prove no time-sensitive strategy operation is active. If that cannot be proven, stop after pushing and record the exact server verification still required.

**Step 2: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: reviewed commits are present on GitHub.

**Step 3: Update the server**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

This must pull the branch, reinstall the editable package, and restart `telegram-kol.service`.

**Step 4: Verify real behavior**

1. Confirm the third button reads “更新状态”.
2. On a partial-entry strategy, verify one entered leg and one pending leg are shown and buttons remain.
3. On an all-entered or group-cancelled strategy, verify the keyboard disappears.
4. Confirm refresh emits no cancel, entry, close, or other exchange write request.
5. Inspect service logs for callback, reconciliation, Deepcoin, and Telegram edit failures without exposing credentials.

**Step 5: Record deployment evidence**

Record the deployed commit, restart result, tests, and remaining risks in the appropriate tracked operational note, then commit and push it if changed.
