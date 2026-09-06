# Codex Telegram Completion Notification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Send a Telegram message to chat `8129644952` after every completed Codex task, with the Bot Token stored in macOS Keychain and macOS notification fallback.

**Architecture:** A dependency-free Python helper reads the Bot Token through the macOS `security` command and posts a URL-encoded message to Telegram's Bot API with bounded timeouts. Project-level `AGENTS.md` makes the helper mandatory before final responses and defines the existing macOS notification as fallback.

**Tech Stack:** Python 3 standard library, macOS Keychain CLI, Telegram Bot API, pytest.

---

### Task 1: Add the Telegram notification helper

**Files:**
- Create: `scripts/codex_telegram_notify.py`
- Test: `tests/test_codex_telegram_notify.py`

**Step 1: Write failing unit tests**

Cover:

- the helper reads service `telegram-kol-codex-notifier` and account
  `bot-token` from Keychain;
- it posts `chat_id=8129644952` and the supplied completion summary;
- it returns a nonzero exit when the Keychain item is missing;
- it returns a nonzero exit when Telegram returns `{"ok": false}`;
- credential values never appear in error output.

Use `unittest.mock` to replace `subprocess.run` and
`urllib.request.urlopen`; no real Keychain or network calls belong in unit tests.

**Step 2: Run the tests and verify failure**

Run:

```bash
uv run pytest tests/test_codex_telegram_notify.py -v
```

Expected: FAIL because `scripts/codex_telegram_notify.py` does not exist.

**Step 3: Implement the minimal helper**

Implement constants for the Keychain service/account and destination Chat ID.
Read the token with:

```python
subprocess.run(
    [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        KEYCHAIN_ACCOUNT,
        "-s",
        KEYCHAIN_SERVICE,
        "-w",
    ],
    check=True,
    capture_output=True,
    text=True,
)
```

Encode `chat_id` and `text` with `urllib.parse.urlencode`, post with
`urllib.request.Request`, and parse the JSON response. Use bounded timeouts,
print only credential-free errors to stderr, and exit nonzero on any failure.

**Step 4: Run the tests and verify success**

Run:

```bash
uv run pytest tests/test_codex_telegram_notify.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add scripts/codex_telegram_notify.py tests/test_codex_telegram_notify.py
git commit -m "feat: add secure Codex Telegram notifier"
```

### Task 2: Make Telegram completion notifications mandatory

**Files:**
- Modify: `AGENTS.md`

**Step 1: Update the project instruction**

Replace the current macOS-primary completion rule with:

```markdown
- At the end of every completed task in this project, before the final response,
  run `python3 scripts/codex_telegram_notify.py "<short non-sensitive completion
  summary>"`. Telegram is the primary completion notification. If delivery
  fails, send the existing macOS notification and clearly report the Telegram
  failure in the final response. Never include credentials or sensitive values
  in the summary.
```

Keep the exact fallback command in the instruction so future sessions can use it
without discovery.

**Step 2: Review the diff**

Run:

```bash
git diff --check -- AGENTS.md
git diff -- AGENTS.md
```

Expected: no whitespace errors; Telegram is primary and macOS is fallback.

**Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: require Telegram task completion alerts"
```

### Task 3: Store the token and verify live delivery

**Files:**
- No repository files.

**Step 1: Store the token without putting it in command history**

Run in a TTY so `security` prompts for the password:

```bash
/usr/bin/security add-generic-password \
  -U \
  -a bot-token \
  -s telegram-kol-codex-notifier \
  -w
```

Paste the Bot Token only at the hidden prompt. Do not pass it as a command-line
argument or echo it.

**Step 2: Confirm the Keychain item exists without printing it**

Run:

```bash
/usr/bin/security find-generic-password \
  -a bot-token \
  -s telegram-kol-codex-notifier >/dev/null
```

Expected: exit status 0 and no credential output.

**Step 3: Send a live test**

Run:

```bash
python3 scripts/codex_telegram_notify.py "Telegram 任务完成通知配置测试成功。"
```

Expected: exit status 0 and one Telegram message in chat `8129644952`.

**Step 4: Run focused verification**

Run:

```bash
uv run pytest tests/test_codex_telegram_notify.py -v
git diff --check
git status --short
```

Expected: focused tests pass, no whitespace errors, and only unrelated
pre-existing user changes remain uncommitted.
