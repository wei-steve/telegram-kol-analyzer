# Telegram Shutdown Safety Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the Telethon double-disconnect race, recover Bot polling from transient Telegram failures, prevent failed Bot tasks from breaking lifespan shutdown, and redact authenticated Bot URLs before log emission.

**Architecture:** The live-listener task becomes the sole owner of Telethon's normal disconnect path. Both Bot loops use one status-aware retry helper, FastAPI consumes failed Bot tasks during shutdown, and application handlers use a redacting formatter over the fully rendered record.

**Tech Stack:** Python 3.12, asyncio, Telethon 1.42, FastAPI/Starlette lifespan, httpx, Python logging, pytest.

---

## Global Rules

- Work only in `/Users/steven/Documents/telegram获取消息/.worktrees/runtime-serialization`.
- Use `/Users/steven/Documents/telegram获取消息/.venv/bin/python` with `PYTHONPATH=src`.
- Never run `git add -A`; stage only the named paths and inspect the staged list.
- Every production-code change must follow a focused failing test that was run and failed for the expected reason.
- Do not push, deploy, restart, rotate credentials, mutate production settings or data, manufacture Telegram traffic, cut over concurrency, or write to the exchange.
- Do not change recognition, queue, management, position, execution, callback-draining, or exchange-write semantics.
- Run the complete suite exactly once after the final production-code edit. A later production-code edit requires a new affected focused run and a new complete suite.

## Task 1: Make the Listener the Single Telethon Shutdown Owner

**Files:**

- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py`

### Step 1: Write the failing regression

Replace the old disconnect-before-listener test with a Telethon-shaped client
whose `run_until_disconnected()` owns `disconnect()` in `finally`:

```python
def test_lifespan_listener_owns_exactly_one_telegram_disconnect(tmp_path):
    class TelethonShapedClient:
        def __init__(self):
            self.disconnected = asyncio.Event()
            self.disconnect_calls = 0

        async def connect(self):
            return None

        def add_event_handler(self, *_args):
            return None

        async def disconnect(self):
            self.disconnect_calls += 1
            self.disconnected.set()

        async def run_until_disconnected(self):
            try:
                await self.disconnected.wait()
            finally:
                await self.disconnect()
```

Enter and leave the real app lifespan and assert `disconnect_calls == 1`.

### Step 2: Run RED

Run:

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_web_app.py::test_lifespan_listener_owns_exactly_one_telegram_disconnect -vv
```

Expected: FAIL because the app calls `disconnect()` and Telethon-shaped
`run_until_disconnected()` calls it again.

### Step 3: Implement the minimal ownership fix

Delete `_disconnect_shared_telegram_client()`. In
`_stop_live_listener_task()`, cancel the listener first and await it using the
existing shutdown bound. Consume `CancelledError` or an already-failed task,
but never invoke a second client disconnect.

### Step 4: Run GREEN and adjacent shutdown tests

Run:

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_web_app.py::test_lifespan_listener_owns_exactly_one_telegram_disconnect \
  tests/test_web_app.py::test_lifespan_bounds_listener_shutdown_when_telegram_disconnect_hangs -vv
```

Expected: both pass without pending-task errors.

### Step 5: Commit

```bash
git add tests/test_web_app.py src/telegram_kol_research/web_app.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: give listener sole telegram shutdown ownership"
```

## Task 2: Redact Authenticated Bot URLs Before Log Emission

**Files:**

- Modify: `tests/test_app_logging.py`
- Modify: `src/telegram_kol_research/app_logging.py`

### Step 1: Write the failing raw-output test

Create a synthetic `httpx.HTTPStatusError` whose request URL contains a fake
sentinel Bot token. Configure application logging, call `logger.exception()`,
flush both application handlers, and inspect both the raw rotating file and
captured stderr. Assert the sentinel is absent and
`https://api.telegram.org/bot[REDACTED]/getUpdates` is present.

### Step 2: Run RED

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_app_logging.py::test_application_handlers_redact_bot_token_before_emission -vv
```

Expected: FAIL because current redaction occurs only in `read_log_page()`.

### Step 3: Implement the minimal formatter

Add:

```python
class TelegramCredentialRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return TELEGRAM_BOT_URL_PATTERN.sub(r"\1[REDACTED]", rendered)
```

Use this formatter for both application handlers. Retain `read_log_page()`
redaction as defense for legacy files.

### Step 4: Run GREEN

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest tests/test_app_logging.py -vv
```

### Step 5: Commit

```bash
git add tests/test_app_logging.py src/telegram_kol_research/app_logging.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: redact telegram credentials before log emission"
```

## Task 3: Retry Only Recoverable Bot Poll Failures

**Files:**

- Modify: `tests/test_system_operator_bot.py`
- Modify: `src/telegram_kol_research/telegram_bot_commands.py`

### Step 1: Write RED tests

Add an async regression proving `_get_updates_with_retry()` retries a synthetic
502 exactly once and returns the next valid batch. Add a second regression
proving a synthetic 401 is raised immediately and is not retried. Assert retry
logs do not contain the sentinel token or authenticated URL.

### Step 2: Run RED

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_system_operator_bot.py -k 'bot_poll_retries_server_error or bot_poll_rejects_auth_error' -vv
```

Expected: FAIL because the shared helper does not exist.

### Step 3: Implement the shared polling helper

Add a classifier that treats `httpx.RequestError`, HTTP 429, and HTTP 5xx as
recoverable. Add an async helper that retries `_get_updates()` after
`poll_interval_seconds` and logs only Bot label, exception class, and status.
Replace the duplicated timeout-only blocks in both command loops with calls to
the helper. Leave initialization fail-fast and never catch cancellation.

### Step 4: Run GREEN and loop compatibility

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_system_operator_bot.py -k 'bot_poll' -vv
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_worker_loop_does_not_block_event_loop.py -k 'system_operator_callback' -q
```

### Step 5: Commit

```bash
git add tests/test_system_operator_bot.py src/telegram_kol_research/telegram_bot_commands.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: recover transient telegram bot polling"
```

## Task 4: Consume Failed Bot Tasks During Lifespan Shutdown

**Files:**

- Modify: `tests/test_web_app.py`
- Modify: `src/telegram_kol_research/web_app.py`

### Step 1: Write RED lifespan regression

Monkeypatch the system-operator loop to raise a synthetic authenticated
`httpx.HTTPStatusError`, wait until the task is done inside the real lifespan,
and then exit. Assert lifespan does not raise, app task state is cleared, and
the raw application log contains no sentinel token.

### Step 2: Run RED

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_web_app.py::test_lifespan_consumes_failed_system_bot_task_without_authenticated_url -vv
```

Expected: FAIL because lifespan catches only `CancelledError` for this task.

### Step 3: Implement minimal task cleanup

Attach `_log_background_task_result()` to both Bot command tasks. Add a helper
that cancels and awaits a Bot task, consumes `CancelledError` and an
already-recorded exception, clears the named app-state field, and does not log
the exception a second time. Use it for both regular and system Bot tasks before
the management executor shuts down.

### Step 4: Run GREEN and callback shutdown compatibility

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_web_app.py::test_lifespan_consumes_failed_system_bot_task_without_authenticated_url \
  tests/test_web_app.py::test_lifespan_stops_system_bot_before_management_executor_shutdown -vv
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_worker_loop_does_not_block_event_loop.py -q
```

### Step 5: Commit

```bash
git add tests/test_web_app.py src/telegram_kol_research/web_app.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: contain failed bot tasks during shutdown"
```

## Task 5: Assemble the Local Candidate

**Files:**

- Modify: `docs/per-chat-durable-lanes-status.md`

### Step 1: Run affected focused suites

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest \
  tests/test_app_logging.py \
  tests/test_system_operator_bot.py \
  tests/test_web_app.py \
  tests/test_worker_loop_does_not_block_event_loop.py -q
```

Expected: zero failures.

### Step 2: Run static checks

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m compileall -q \
  src/telegram_kol_research tests
git diff --check
git status --short --branch
```

### Step 3: Run the one final complete suite

```bash
PYTHONPATH=src /Users/steven/Documents/telegram获取消息/.venv/bin/python -m pytest -q
```

Record exact pass/skip/warning count and elapsed time. If any production code
changes afterward, repeat the affected focused tests and this complete suite.

### Step 4: Review the exact diff

Review the range from claim commit `6e06e8f` through HEAD for:

- exactly one normal Telethon disconnect;
- no retry of permanent Bot failures;
- cancellation not swallowed;
- no authenticated URL emitted to raw configured logs;
- failed Bot tasks contained before executor shutdown;
- no trading, queue, settings, schema, or exchange semantics changed.

### Step 5: Record and commit the candidate

Update the canonical status with RED/GREEN evidence, focused/full-suite results,
the production-code candidate SHA, review result, and the continuing no-push/no-
deploy boundary. Then:

```bash
git add docs/per-chat-durable-lanes-status.md
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record shutdown safety candidate"
```

Stop and return control to the owner. Any push, credential rotation, deployment,
restart, or production observation requires separate authorization.
