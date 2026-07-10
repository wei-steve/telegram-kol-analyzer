# Mac mini Development Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create safe, reproducible Windows-to-Mac-mini development migration assets without moving production secrets or runtime state.

**Architecture:** GitHub transfers source code and versioned non-secret operational knowledge. A Windows preflight script reports migration risks without exposing values, while a macOS bootstrap script validates prerequisites without changing the Mac or contacting production.

**Tech Stack:** Markdown, PowerShell 5.1+, POSIX Bash, Git, Python 3.12+, uv.

## Global Constraints

- Production Telegram sessions, Deepcoin credentials, databases, and live-trading authority stay on the production server.
- No real secret may be created, copied, printed, committed, or logged.
- The Windows script is read-only and reports suspected secrets only as `path:line`.
- The macOS script makes no network calls, installation, service, or credential changes.
- Remote access is private-network-only: no public SSH, VNC, or RustDesk port forwarding.
- Preserve unrelated uncommitted workspace changes.

## File Structure

- Create `docs/mac-mini-migration.md`: complete human migration and recovery procedure.
- Create `docs/migration-handoff.md`: durable non-secret project context index.
- Create `docs/remote-access.md`: Tailscale, RustDesk, SSH, and mobile policy.
- Create `config/development.env.example`: empty local-development-only variable template.
- Create `scripts/preflight_mac_migration.ps1`: read-only Windows readiness check.
- Create `scripts/bootstrap_mac_dev.sh`: macOS prerequisite checker.
- Modify `.gitignore`: explicitly ignore the local development environment copy.
- Create `tests/test_migration_assets.py`: static contracts for all assets.

### Task 1: Add durable migration documentation

**Files:** Create `docs/migration-handoff.md`, `docs/mac-mini-migration.md`, `docs/remote-access.md`; create `tests/test_migration_assets.py`.

**Interfaces:** Consume `README.md`, `AGENTS.md`, `docs/runbook.md`, and `docs/server-deployment.md`. Produce stable instructions referenced by the bootstrap script and Mac Codex handoff prompt.

- [ ] Write a failing test that opens all three documents; requires the words `production` and `secret` in each; requires `do not open public ports` in `remote-access.md`.
- [ ] Run `python -m pytest tests/test_migration_assets.py::test_migration_docs_exist_and_prohibit_secret_transfer -v`; expect failure because files are absent.
- [ ] Write `migration-handoff.md` with system purpose, source/config locations, production boundary, documentation index, deployment helper `scripts/server_git_update.ps1`, and a checklist excluding hosts, usernames, IDs, and values.
- [ ] Write `mac-mini-migration.md` in order: Windows preflight; resolve findings; push code; configure Mac user/FileVault/Tailscale; clone; bootstrap; optionally create a development-only config; run non-production checks; deploy only via the existing GitHub/server sequence. Include lost-device removal, secret rotation, and fresh-clone recovery.
- [ ] Write `remote-access.md` requiring MFA and device approval for Tailscale; recommending RustDesk for GUI and SSH for terminal; prohibiting port forwarding; covering FileVault, wake/sleep, macOS Accessibility and Screen Recording permissions, and emergency-only phone usage.
- [ ] Rerun the test; expect PASS.

### Task 2: Add safe development configuration template

**Files:** Create `config/development.env.example`; modify `.gitignore`; modify `tests/test_migration_assets.py`.

**Interfaces:** Consume documented local variable names. Produce a copyable but value-free template, while `config/development.env` remains untracked.

- [ ] Write a failing test requiring `DEVELOPMENT ONLY`, empty `TELEGRAM_API_ID=` and `TELEGRAM_API_HASH=` lines, absence of `DEEP`, and exact `config/development.env` in `.gitignore`.
- [ ] Run `python -m pytest tests/test_migration_assets.py::test_development_template_is_non_secret_and_local_copy_is_ignored -v`; expect failure.
- [ ] Create the template with comments and only blank assignments for `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH`, `TELEGRAM_KOL_LLM_BASE_URL`, `TELEGRAM_KOL_LLM_API_KEY`, `TELEGRAM_KOL_LLM_MODEL`, and `TELEGRAM_KOL_LLM_TIMEOUT_SECONDS`. State that this is never production configuration and that an empty session path means do not run authenticated Telegram commands.
- [ ] Add only `config/development.env` to `.gitignore`; retain the existing `config/*.env` rule and do not hide the example.
- [ ] Rerun the test; expect PASS.

### Task 3: Implement Windows preflight

**Files:** Create `scripts/preflight_mac_migration.ps1`; modify `tests/test_migration_assets.py`.

**Interfaces:** Consume Git and `.gitignore`; produce exit `0` for a clean check and `1` for a missing prerequisite/ignore rule or suspected tracked secret. Output must never contain matching secret values.

- [ ] Write a failing static test requiring `git ls-files`, `git status --short`, and a `path:line` failure format; forbid `Match.Value`, `Remove-Item`, and `Copy-Item`.
- [ ] Run `python -m pytest tests/test_migration_assets.py::test_windows_preflight_is_read_only_and_redacts_matches -v`; expect failure.
- [ ] Implement with `Set-StrictMode -Version Latest` and `$ErrorActionPreference = 'Stop'`. Resolve repository root from `$PSScriptRoot/..`; require Git; require ignore rules `.venv/`, `config/*.env`, `data/*.session`, `data/`, and `*.log`.
- [ ] Use `git ls-files` for tracked paths and `git status --short --untracked-files=all` for untracked paths. Report only untracked names matching `(?i)(\.env$|\.session|api[_-]?key|token|secret|credential|passphrase)`.
- [ ] Scan tracked UTF-8 text line-by-line with `(?i)(api[_-]?key|token|secret|password|passphrase)\s*[:=]\s*[^\s#]+`. Skip `.git/`, `.venv/`, `.pytest_cache/`, `videos/`, files larger than 1 MiB, and binary files. On match report only `relative/path:line`, increment failure count, and exit `1` after the summary.
- [ ] Parse with `[System.Management.Automation.Language.Parser]::ParseFile`, run it, then rerun static test; expect parser clean and static test PASS.

### Task 4: Implement macOS bootstrap

**Files:** Create `scripts/bootstrap_mac_dev.sh`; modify `tests/test_migration_assets.py`.

**Interfaces:** Consume a macOS Git checkout. Produce `0` only when prerequisites pass and `config/development.env` is not tracked; make no mutation.

- [ ] Write a failing static test requiring `[[ "$(uname -s)" == "Darwin" ]]`, `xcode-select -p`, checks for `git`, `python3`, and `uv`, and `git ls-files --error-unmatch config/development.env`; forbid `curl`, `brew install`, and `uv sync`.
- [ ] Run `python -m pytest tests/test_migration_assets.py::test_mac_bootstrap_checks_prerequisites_without_installing_or_network_calls -v`; expect failure.
- [ ] Implement `#!/usr/bin/env bash` with `set -euo pipefail`; resolve root from `${BASH_SOURCE[0]}`; require Darwin, Xcode CLI tools, Git, Python 3.12+, uv, an `origin` remote, and an untracked `config/development.env`.
- [ ] Report branch/status, then print these manual next steps: read the two migration docs; optionally copy the template and enter development-only password-manager values; do not copy production sessions/databases/trading credentials; run `uv sync` only after review. Do not invoke installers, services, SSH, or network clients.
- [ ] Run `bash -n scripts/bootstrap_mac_dev.sh` and the static test; expect PASS.

### Task 5: Verify and hand off

**Files:** All files above; modify only when verification finds a defect.

**Interfaces:** Produce a verified repository migration package and a Mac Codex prompt that points to the documentation and scripts.

- [ ] Run `python -m pytest tests/test_migration_assets.py -v`; expect all new tests to pass.
- [ ] Run PowerShell parser plus `scripts/preflight_mac_migration.ps1`. If it returns `1`, resolve or explicitly explain each redacted `path:line` result before describing a clean preflight.
- [ ] Run `bash -n scripts/bootstrap_mac_dev.sh`, `git diff --check`, and `git status --short`; expect syntax/whitespace success and only intentional migration files plus pre-existing user changes.
- [ ] Run a targeted `rg` scan of only the new migration assets for credential assignments; inspect hits without scanning runtime secrets.
- [ ] Stage only the migration files, excluding existing untracked video and unrelated spec/plan files; commit after all checks have fresh passing evidence.
