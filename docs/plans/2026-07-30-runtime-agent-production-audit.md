# Runtime Agent Production Audit Rerun Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a dormant, bounded `rerun_production_audit` handler with one-shot positive verification.

**Architecture:** A loopback-only main-service endpoint runs the existing stable private-snapshot management audit in a killable, output-bounded subprocess. A sidecar coordinator captures only the bounded proof and lets `get_service_audit_state` consume it once; the existing Phase 6 executor supplies identity, idempotency, circuit breaking, durable evidence, and fail-closed verification.

**Tech Stack:** Python 3.13, SQLAlchemy/SQLite, Typer, pytest, existing Runtime Agent executor and management-audit helpers.

---

### Task 1: Add the bounded audit proof coordinator

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_production_audit.py`
- Create: `tests/test_runtime_agent_production_audit.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write failing tests**

Cover one successful complete audit, historical abnormal counts, incomplete
proof, invalid result shape, runner exception, atomic single consumption, and
the 32-capture bound.

**Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_runtime_agent_production_audit.py
```

Expected: FAIL because the module does not exist.

**Step 3: Implement the coordinator**

Add:

- injected `runner`;
- executor identity validation;
- fixed-field bounded audit projection;
- `rerun`, `has_capture`, and `consume_verification`;
- atomic one-shot consumption;
- a maximum of 32 ephemeral captures;
- a loopback-only, proxy-refusing endpoint;
- a 20-second killable subprocess and 1 MiB output ceiling.

**Step 4: Verify GREEN**

Run the Task 1 tests and expect PASS.

### Task 2: Wire the handler and live verification

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_runtime_agent_cli.py`

**Step 1: Write failing CLI wiring tests**

Require:

- `rerun_production_audit` is absent without coordinator injection;
- exact handler injection runs the supplied audit runner;
- `get_service_audit_state` consumes the live audit proof;
- a later query returns to passive monitor-state behavior;
- both one-shot and worker commands construct the coordinator from their
  `database_path`.

**Step 2: Verify RED**

Run the focused CLI tests and expect failure because no audit coordinator is
wired.

**Step 3: Implement minimal wiring**

Construct the coordinator with a 25-second loopback HTTP reader, inject it into
the tool registry and action-handler builder, and prefer one live proof in
`get_service_audit_state` before the existing passive projection. Do not grant
the sidecar database ownership, `CAP_FOWNER`, or systemd access.

**Step 4: Verify GREEN**

Run the focused CLI and coordinator tests and expect PASS.

### Task 3: Run safety regressions and review

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Run focused Runtime Agent tests**

```bash
.venv/bin/python -m pytest -q \
  tests/test_runtime_agent_*.py \
  tests/test_runtime_incident_*.py \
  tests/test_runtime_incidents.py \
  tests/test_llm_chat_request.py \
  tests/test_web_app.py \
  tests/test_web_live_listener_startup.py
```

**Step 2: Run contextual, management, listener, monitor, and mutation
regressions**

Use the same bounded regression groups recorded by the Phase 6 status file.

**Step 3: Run the offline evaluation and static checks**

Require all nine evaluation metrics at 1.0, `compileall` success, and a clean
`git diff --check`.

**Step 4: Review**

Review the implementation against the Phase 6 boundary. Fix all Critical and
Important findings before deployment.

**Step 5: Commit and push**

Commit the reviewed runtime change and push
`codex/deepcoin-auto-trading-v1`.

### Task 4: Deploy dormant and run an isolated canary

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Prove a production safe window**

Confirm service/listener health, completed latest recognition, zero evidence,
context, management, mutation, incident, and recovery work in flight, complete
read-only snapshots, and the known monitor baseline.

**Step 2: Deploy**

Use `scripts/server_git_update.sh`. Keep sidecar, Agent, shadow, actions, and
action allowlists disabled.

**Step 3: Verify continuity**

Confirm the deployed commit, HTTP 200, inactive sidecar, completed latest
recognition, unchanged production incident count, no in-flight work, and the
bounded monitor result.

**Step 4: Run the isolated canary**

Use a temporary database incident and the deployed production database as the
read-only audit source. Enable only the in-process exact action configuration.
Require one verified recovery attempt with `audit-state` evidence, no
production ledger or business-row change, and no notification.

**Step 5: Record the checkpoint**

Update the canonical status with tests, deployment, canary, exact remaining
work, and the next Phase 6 candidate. Commit and push the documentation-only
checkpoint without another production restart.
