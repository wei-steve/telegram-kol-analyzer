# Cross-Platform Server Update Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Provide a safe macOS/Linux shell entrypoint for the existing production server update workflow.

**Architecture:** Keep the PowerShell helper untouched. Add a small Bash wrapper that validates local prerequisites and delegates to the server-owned update command over SSH. Test the script’s source structure locally and document OS-specific invocation.

**Tech Stack:** Bash, OpenSSH, pytest source assertions, Markdown.

---

### Task 1: Add shell helper with test-first coverage

**Files:**
- Create: `scripts/server_git_update.sh`
- Create: `tests/test_server_update_scripts.py`

**Step 1: Write failing test**

Add a test that reads `scripts/server_git_update.sh` and requires `set -euo pipefail`, defaults for `SERVER`, `KEY_PATH`, `BRANCH`, `command -v ssh`, readable-key validation, and an `ssh -i` invocation of `/usr/local/bin/telegram-kol-update` with `BRANCH`.

**Step 2: Verify RED**

Run: `./.venv/bin/python -m pytest tests/test_server_update_scripts.py -v`

Expected: FAIL because the shell helper does not yet exist.

**Step 3: Implement minimal helper**

Add a Bash script with:

```bash
SERVER="${SERVER:-root@43.167.220.225}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"
BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"
```

Validate `ssh` and `KEY_PATH`, safely shell-quote the branch for the remote command, then run SSH with the selected key. Mark the script executable.

**Step 4: Verify GREEN**

Run: `./.venv/bin/python -m pytest tests/test_server_update_scripts.py -v`

Expected: PASS.

### Task 2: Document native commands

**Files:**
- Modify: `docs/server-deployment.md:50-65`
- Modify: `docs/migration-handoff.md:22`

**Step 1: Write failing test**

Extend `tests/test_server_update_scripts.py` to require the documentation references `./scripts/server_git_update.sh` and keeps `server_git_update.ps1`.

**Step 2: Verify RED**

Run: `./.venv/bin/python -m pytest tests/test_server_update_scripts.py -v`

Expected: FAIL because macOS/Linux usage is undocumented.

**Step 3: Implement documentation**

Show the Bash command for macOS/Linux and the PowerShell command for Windows; state that both trigger the same server-side helper.

**Step 4: Verify GREEN and commit**

Run: `./.venv/bin/python -m pytest tests/test_server_update_scripts.py -v && git diff --check`

Expected: PASS.

```bash
git add scripts/server_git_update.sh tests/test_server_update_scripts.py docs/server-deployment.md docs/migration-handoff.md
git commit -m "feat: add cross-platform server update helper"
```
