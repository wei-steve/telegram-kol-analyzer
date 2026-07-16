# Server Production Safety Monitor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Run zero-token, read-only Deepcoin production safety checks on the server and send deduplicated Telegram alerts only for system abnormalities.

**Architecture:** A new Python module separates pure evaluation, state/deduplication, and injected read-only adapters. A Typer command runs from a dedicated oneshot systemd service on a persistent 30-minute timer; root-only state and expected-HEAD files remain outside the trading database.

**Tech Stack:** Python 3.12, Typer, sqlite3 read-only URI, httpx, systemd, pytest, existing system-operator Telegram bot.

---

### Task 1: Pure health evaluation and bounded alert formatting

**Files:**
- Create: `src/telegram_kol_research/production_safety_monitor.py`
- Create: `tests/test_production_safety_monitor.py`

**Step 1: Write failing tests**

Define wished-for frozen inputs and assert an exact healthy result:

```python
result = evaluate_monitor_snapshot(
    MonitorSnapshot(
        service_state="active",
        head="reviewed-sha",
        settings={
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "max_concurrent_positions": 4,
        },
        journal_error_count=0,
        abnormal_events=(),
        audit=None,
    ),
    MonitorExpectations(
        head="reviewed-sha",
        auto_trade_enabled=True,
        management_execution_mode="live",
        max_concurrent_positions=4,
    ),
)
assert result.healthy is True
assert result.reason_codes == ()
```

Add cases for inactive service, SHA/gate drift, journal errors, adapter failure,
unknown/recovery event status, duplicate exact manual close for one `pos_id`,
and incomplete/abnormal audit. Normal submitted entry, TP, stop, and close events
must not alert. Formatter tests require fixed Chinese labels, bounded length,
sorted reason codes, and no token/passphrase/header/raw request/response or raw
exception text.

**Step 2: Verify RED**

```bash
.venv/bin/pytest tests/test_production_safety_monitor.py -q
```

Expected: collection fails because the module/types do not exist.

**Step 3: Implement minimally**

Add frozen `MonitorExpectations`, `MonitorSnapshot`, and `MonitorResult`
dataclasses. Use only fixed reason codes and safe detail keys. Treat malformed
input as abnormal without storing raw errors. Detect duplicates only for the
same exact `close_bound_position_market` `pos_id` in the bounded window. Build
Telegram text from allowlisted fields and truncate to a fixed safe bound.

**Step 4: Verify GREEN and commit**

```bash
.venv/bin/pytest tests/test_production_safety_monitor.py -q
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git commit -m "feat: evaluate production safety health"
```

### Task 2: Atomic state and notification deduplication

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `tests/test_production_safety_monitor.py`

**Step 1: Write failing tests**

Test missing/malformed state defaults, the exact persisted field allowlist,
same-directory temp file plus `os.replace`, mode `0600`, order-independent
fingerprints, immediate changed-fingerprint notification, six-hour suppression,
and silent clearing after recovery. Use:

```python
state = load_monitor_state(path)
decision = decide_monitor_notification(result, state, now=now)
save_monitor_state(path, decision.next_state)
```

**Step 2: Verify RED**

```bash
.venv/bin/pytest tests/test_production_safety_monitor.py -k 'state or fingerprint or notification' -q
```

Expected: missing state functions fail.

**Step 3: Implement minimally**

Use strict JSON, ISO timestamps, canonical JSON SHA-256, `flush`/`fsync`,
`chmod(0o600)`, and `os.replace`. Persist only `last_window_at`,
`last_full_audit_date`, `anomaly_fingerprint`, and `last_notification_at`.

**Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/test_production_safety_monitor.py -q
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git commit -m "feat: deduplicate safety monitor alerts"
```

### Task 3: Read-only adapters, daily audit, Telegram delivery, and CLI

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write failing orchestration tests**

With injected adapters, prove the lightweight run reads service, Git HEAD,
loopback settings, bounded error count, and bounded abnormal events; SQLite uses
`mode=ro` and no write SQL; daily audit starts only after 09:00 Asia/Shanghai;
`source_snapshots_differ` retries exactly once while other failures do not;
healthy runs send nothing; eligible anomalies send once; missing bot config or
delivery failure exits nonzero without successful-delivery state; `--notify` is
required; and `--test-notification` sends only fixed monitor-test text without
calling settings, DB, audit, or exchange adapters.

CLI smoke tests require `monitor-production-safety` and these flags:

```text
--expected-head
--expected-auto-trade-enabled
--expected-management-mode
--expected-max-concurrent-positions
--notify
--force-full-audit
--test-notification
```

**Step 2: Verify RED**

```bash
.venv/bin/pytest tests/test_production_safety_monitor.py tests/test_cli_smoke.py -k 'production_safety or monitor_production' -q
```

Expected: orchestration/CLI tests fail because no command exists.

**Step 3: Implement bounded adapters**

Use fixed-argv subprocesses with timeouts/output limits for systemctl, Git,
journalctl, and `audit-management-batches`; loopback-only settings HTTP; and
`sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)` selecting bounded
identity/status fields only. Run the audit after 09:00 when the last successful
audit date differs. Retry once only for `source_snapshots_differ`.

Use existing `load_system_operator_bot_config` and
`send_system_operator_bot_message` only after dedupe authorizes delivery. Do not
import or call a Deepcoin write client.

**Step 4: Add Typer command**

Add `monitor-production-safety`; print one compact secret-free JSON summary and
exit nonzero for unresolved abnormal/monitor failures. `--test-notification`
requires `--notify` and sends fixed text beginning
`【监控测试】服务器安全监控通知链路验证`.

**Step 5: Verify and commit**

```bash
.venv/bin/pytest tests/test_production_safety_monitor.py tests/test_cli_smoke.py tests/test_system_operator_bot.py -q
git add src/telegram_kol_research/production_safety_monitor.py src/telegram_kol_research/cli.py tests/test_production_safety_monitor.py tests/test_cli_smoke.py
git commit -m "feat: run read-only production safety monitor"
```

### Task 4: systemd units, gated installer, and operations docs

**Files:**
- Create: `deploy/systemd/telegram-kol-monitor.service`
- Create: `deploy/systemd/telegram-kol-monitor.timer`
- Create: `scripts/install_server_monitor.sh`
- Create: `tests/test_server_monitor_installation.py`
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `docs/migration-handoff.md`

**Step 1: Write failing static safety tests**

Assert the service is oneshot, uses the expected working directory/root-only
environment file, exact gates, state/DB paths, and `--notify`; the timer uses a
30-minute interval, randomized delay, and `Persistent=true`; the installer
requires root, captures `git rev-parse HEAD`, installs modes `0600`/`0644`/`0700`,
and reloads systemd; default install does not enable/start; `--enable` affects
only `telegram-kol-monitor.timer`; and nothing restarts/stops
`telegram-kol.service`, mutates its DB/settings, or calls Deepcoin writes.

**Step 2: Verify RED**

```bash
.venv/bin/pytest tests/test_server_monitor_installation.py -q
```

Expected: unit/installer files are missing.

**Step 3: Create units and installer**

The timer uses `OnBootSec=5min`, `OnUnitActiveSec=30min`,
`RandomizedDelaySec=2min`, and `Persistent=true`. The service invokes the
virtualenv CLI with `/etc/telegram-kol-monitor.env`, expected SHA expansion,
exact live gates, `/var/lib/telegram-kol-monitor/state.json`, production DB,
35-minute lookback, and `--notify`.

Installer accepts only optional `--enable`. Default copies files, freezes the
current HEAD, creates modes, and reloads. `--enable` additionally runs only:

```bash
systemctl enable --now telegram-kol-monitor.timer
```

**Step 4: Document operations**

Document install, no-notify dry run, one labelled notification test, enable,
status, and rollback. State that normal trade notifications are not duplicated.

**Step 5: Verify and commit**

```bash
.venv/bin/pytest tests/test_server_monitor_installation.py tests/test_production_safety_monitor.py tests/test_cli_smoke.py -q
git diff --check
git add deploy/systemd scripts/install_server_monitor.sh tests/test_server_monitor_installation.py docs/runbook.md docs/server-deployment.md docs/migration-handoff.md
git commit -m "ops: install server safety monitor timer"
```

### Task 5: Review, deploy, enable, and retire Codex monitoring

**Files:**
- Verify: all changes since `a6d100b`
- Production state only: systemd units and root-only monitor files
- Delete after server proof: Codex automation `deepcoin`

**Step 1: Review and complete local verification**

Use `requesting-code-review`; fix Critical/Important findings via TDD and
re-review. Then run:

```bash
.venv/bin/pytest -q
git diff --check a6d100b..HEAD
git status --short
```

**Step 2: Push and deploy**

```bash
git push origin codex/deepcoin-auto-trading-v1
./scripts/server_git_update.sh
```

Confirm server HEAD, main service active, live gates unchanged, stable audit,
and no new errors before monitor installation.

**Step 3: Run focused server tests**

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/pytest tests/test_production_safety_monitor.py tests/test_server_monitor_installation.py tests/test_cli_smoke.py -q
```

**Step 4: Install disabled and run no-notify full health check**

```bash
./scripts/install_server_monitor.sh
systemctl is-enabled telegram-kol-monitor.timer || true
.venv/bin/telegram-kol-research monitor-production-safety \
  --expected-head "$(git rev-parse HEAD)" \
  --expected-auto-trade-enabled \
  --expected-management-mode live \
  --expected-max-concurrent-positions 4 \
  --database-path data/research.db \
  --state-path /var/lib/telegram-kol-monitor/state.json \
  --force-full-audit
```

Expected: timer disabled, compact result healthy, no Telegram message.

**Step 5: Send exactly one labelled test notification**

Run the same command with `--notify --test-notification`. Confirm exactly one
message beginning `【监控测试】` and no trading control.

**Step 6: Enable and verify**

```bash
./scripts/install_server_monitor.sh --enable
systemctl start telegram-kol-monitor.service
systemctl is-active telegram-kol-monitor.timer
systemctl list-timers telegram-kol-monitor.timer --no-pager
journalctl -u telegram-kol-monitor.service -n 50 --no-pager
```

Confirm healthy output, next trigger, main trading service still active,
HEAD/gates unchanged, healthy audit, and no Deepcoin/DB mutation.

**Step 7: Delete temporary Codex automation and report**

Delete automation id `deepcoin` only after Step 6. Keep its independent thread
as inactive history. Report commit, tests, services/timer, next trigger, gates,
audit, labelled test delivery, deletion, and carried Minor findings.
