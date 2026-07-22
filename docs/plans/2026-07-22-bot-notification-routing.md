# Bot Notification Routing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep pending-entry expiry decisions on the existing system bot, route all other system notifications to a new bot using the same chat ID, and suppress only deterministic empty-input MiMo failure alerts.

**Architecture:** Introduce a notification-bot configuration alongside the existing decision-bot configuration. Startup and worker wiring passes the decision configuration solely to expiry delivery and callback polling, while every informational notification path receives the notification configuration. The empty-input condition remains a failed authoritative decision with skipped automation, but its delivery state is explicitly suppressed.

**Tech Stack:** Python 3.11, asyncio, FastAPI, SQLAlchemy, SQLite, httpx, pytest, Telegram Bot API.

---

### Task 1: Specify bot roles and empty-input suppression with failing tests

**Files:**
- Modify: `tests/test_system_operator_bot.py`
- Modify: `tests/test_telegram_live_listener.py`
- Modify: `tests/test_web_live_listener_startup.py`

**Step 1: Write configuration tests**

Add a test proving `load_notification_bot_config()` reads only
`TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN`, `TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID`,
and its timeout variable; the existing loader remains the decision-bot loader.

**Step 2: Write the empty-input red test**

Create an authoritative processor result with MiMo error
`message has no readable text or image`, persist an event with an empty text,
and assert no sender call plus one audit update:

```python
assert sent == []
assert audit[0]["notification_status"] == "suppressed_empty_input"
assert audit[0]["automation_reason"] == "mimo_authoritative_failed"
```

Add the adjacent control test for a nonempty management instruction whose MiMo
request times out; it must still send once.

**Step 3: Write startup routing red test**

Give the test app different `SystemOperatorBotConfig` objects for decision and
notification roles. Assert the expiry sender and command loop receive the
decision config, while the live listener/reconcile runner receives the
notification config.

**Step 4: Run red tests**

Run: `uv run pytest tests/test_system_operator_bot.py tests/test_telegram_live_listener.py tests/test_web_live_listener_startup.py -q`

Expected: FAIL because notification configuration and empty-input suppression do
not exist.

### Task 2: Add a notification-bot configuration without changing callbacks

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `tests/test_system_operator_bot.py`

**Step 1: Add the configuration surface**

Add `NotificationBotConfig`, `load_notification_bot_config()`, and
`notification_bot_enabled()`. Keep `SystemOperatorBotConfig`, its loader, and
callback code unchanged so the existing Bot token continues to own interactive
expiry buttons.

**Step 2: Generalize only the outbound sender annotation**

Allow `send_system_operator_bot_message()` and the informational sender helpers
to accept either config shape. Do not run `run_system_operator_bot_command_loop`
for the notification bot.

**Step 3: Run focused tests**

Run: `uv run pytest tests/test_system_operator_bot.py -q`

Expected: PASS.

### Task 3: Route informational notifications to the new configuration

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: relevant tests in `tests/test_telegram_live_listener.py`, `tests/test_web_app.py`, `tests/test_web_live_listener_startup.py`, `tests/test_cli_authoritative_recognition.py`, and `tests/test_production_safety_monitor.py`

**Step 1: Thread an explicit `notification_bot_config` through live and recovery paths**

Replace informational uses of `system_operator_bot_config` with
`notification_bot_config` for authoritative recognition, semantic review,
instruction summaries, position-attribution incidents, strategy-management
incidents, CLI recognition, and production safety monitoring.

**Step 2: Preserve decision routing**

Keep `system_operator_bot_config` only at pending-entry expiry review delivery
and `run_system_operator_bot_command_loop`. Verify button callback behavior is
unchanged.

**Step 3: Run focused routing tests**

Run: `uv run pytest tests/test_telegram_live_listener.py tests/test_web_live_listener_startup.py tests/test_cli_authoritative_recognition.py tests/test_system_operator_bot.py -q`

Expected: PASS.

### Task 4: Implement the exact empty-input notification suppression

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Test: `tests/test_telegram_live_listener.py`

**Step 1: Classify only deterministic empty input as suppressible**

In `_classify_authoritative_failure_notification()`, inspect the authoritative
MiMo reason carried in the payload. Return `suppressed_empty_input` only when it
equals `message has no readable text or image`. Keep external-stock suppression
and every other failure behavior unchanged.

**Step 2: Persist the exact classification**

Reuse `_handle_authoritative_failure_notification()` to persist the new status
without scheduling a delivery or delayed retry.

**Step 3: Run regression tests**

Run: `uv run pytest tests/test_telegram_live_listener.py -q`

Expected: PASS; empty input is silent, text/image failures remain fail-closed
and deliver through the notification bot.

### Task 5: Document configuration and verify the complete change

**Files:**
- Modify: `docs/runbook.md`
- Modify: appropriate example environment/config file if present

**Step 1: Document server variables**

Document that the server must retain the old `TELEGRAM_KOL_SYSTEM_BOT_*`
variables for the decision bot and add notification variables with the supplied
third-bot token and the existing decision chat ID. Do not place real tokens in
the repository.

**Step 2: Run local verification**

Run:

```bash
uv run pytest tests/test_system_operator_bot.py tests/test_telegram_live_listener.py tests/test_web_live_listener_startup.py tests/test_cli_authoritative_recognition.py tests/test_production_safety_monitor.py -q
uv run ruff check src tests
git diff --check
```

Expected: all pass with no whitespace errors.

**Step 3: Commit and deploy**

Commit only task files, push `codex/deepcoin-auto-trading-v1`, then run the
existing `scripts/server_git_update.ps1` helper. On the server, add the three
notification environment variables, restart `telegram-kol.service`, and verify
that the decision Bot still receives an expiry button while an informational
test notification is delivered by the new Bot. Do not use a live trade signal
as verification.
