# Monitor Incident Writer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route independent monitor incident capture through a bounded authenticated loopback writer while preserving its read-only database sandbox.

**Architecture:** The monitor sends a small closed projection after evaluation. The trusted main service validates the projection, applies its own capture policy, and invokes the existing incident adapters and durable source scanner. A dedicated token authenticates the one endpoint; no business identifiers or arbitrary incident fields cross the boundary.

**Tech Stack:** Python, FastAPI/Starlette, httpx, SQLAlchemy/SQLite, Typer, systemd, pytest.

---

### Task 1: Define the closed projection and client

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Test: `tests/test_production_safety_monitor.py`

**Step 1:** Add failing tests for a fixed loopback URL, disabled proxy trust,
bounded timeout/body, exact projection fields, token header, and failure that
does not alter `MonitorRunOutcome`.

**Step 2:** Run the focused tests and verify they fail because the capture
client and projection do not exist.

**Step 3:** Implement an immutable projection builder and a best-effort httpx
client. Accept only the two monitor reason codes, sanitized adapter labels,
and closed notification failure values.

**Step 4:** Route monitor capture through the client when configured; do not
create a writable incident session in the monitor CLI.

**Step 5:** Run the focused tests and commit.

### Task 2: Add the trusted loopback writer

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_web_app.py`

**Step 1:** Add failing tests for loopback-only access, forwarded-header
rejection, constant-time token validation, missing/invalid token, duplicate
JSON keys, oversized bodies, unknown fields/values, and non-blocking
single-flight behavior.

**Step 2:** Add failing integration tests proving the endpoint invokes the
existing monitor and notification adapters plus the durable source scan under
the main service capture policy.

**Step 3:** Implement the minimal endpoint, strict parser, closed validator,
dedicated lock, and bounded response. Do not accept arbitrary incident types,
source IDs, fingerprints, or summaries.

**Step 4:** Run the focused tests and commit.

### Task 3: Configure the dedicated channel without widening secrets

**Files:**
- Modify: `src/telegram_kol_research/config.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `scripts/install_server_monitor.sh`
- Modify: `deploy/systemd/telegram-kol-monitor.service`
- Modify: `deploy/systemd/telegram-kol-monitor-diagnostic.service`
- Test: `tests/test_runtime_incident_adapters.py`
- Test: `tests/test_runtime_agent_cli.py`
- Test: `tests/test_server_monitor_installation.py`

**Step 1:** Add failing tests for absent/invalid dedicated token behavior and
for installer propagation of only the capture allowlist and monitor-capture
token.

**Step 2:** Add the dedicated token setting, fixed writer URL in monitor units,
and CLI injection. Keep every production database bind read-only.

**Step 3:** Run shell syntax, configuration, CLI, and installation tests.

**Step 4:** Commit.

### Task 4: Regression, review, and dormant deployment

**Files:**
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1:** Run the Phase 8R.2 capture, monitor, web, Agent, notification,
protection, management, and installation regression suites.

**Step 2:** Request code review. Resolve every Critical or Important finding
and rerun affected suites.

**Step 3:** Commit and push reviewed changes to
`codex/deepcoin-auto-trading-v1`.

**Step 4:** Prove a fresh production safe window. Generate and install the
dedicated token without printing it, deploy with capture still exactly
`management_partial_failed`, reinstall the monitor, and perform a bounded
authenticated no-op probe.

**Step 5:** Verify main service, listener, latest recognition, contextual
resolver, management, protection, Runtime Agent, and monitor continuity.

### Task 5: One-type-at-a-time capture comparison

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1:** Keep Telegram and Agent selectors exactly
`management_partial_failed`.

**Step 2:** Enable each remaining `READ_ONLY_CAPTURE_PROFILE` type one at a
time through the synchronized main/monitor capture policy.

**Step 3:** For each type, run three identical capture passes, compare incident
generation/repeat counts, and verify source fields plus `updated_at` are
unchanged. Event-driven types may have zero production source rows; record
that explicitly without synthesizing a production failure.

**Step 4:** Roll back immediately on any source mutation, unexpected claim,
Telegram delivery, Agent diagnosis, duplicate generation, or service impact.

**Step 5:** When every type passes, mark Phase 8R.2 complete and advance only
to Phase 8R.3 planned. Commit, push, and pull the documentation checkpoint
without another service restart.
