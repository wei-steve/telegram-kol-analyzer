# Runtime Agent Telegram Evidence Probe Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a dormant Phase 6 handler that fetches bounded, read-only Telegram Bot API recovery evidence for a real failed-notification incident without exposing bot credentials to the Runtime Agent sidecar.

**Architecture:** The main web process owns a loopback-only, single-flight `getMe`/`getChat` probe and returns a fixed boolean projection. A sidecar coordinator validates the durable failed-notification source, calls that endpoint, stores one bounded in-memory proof, and lets `get_incident_summary` consume the proof once for executor verification.

**Tech Stack:** Python 3.12, FastAPI, httpx, SQLAlchemy, SQLite, Typer, pytest.

---

### Task 1: Define the bounded probe and coordinator contracts

**Files:**
- Create: `tests/test_runtime_agent_telegram_evidence.py`
- Create: `src/telegram_kol_research/runtime_agent_telegram_evidence.py`

**Step 1: Write failing tests**

Test fixed boolean projection, exact identity validation, the two supported
source kinds, failed-source requirement, unsupported-source refusal, malformed
endpoint proof, maximum 32 captures, and one-shot consumption.

**Step 2: Verify red**

```bash
.venv/bin/python -m pytest -q \
  tests/test_runtime_agent_telegram_evidence.py
```

Expected: import failure because the module does not exist.

**Step 3: Implement the minimal coordinator**

Add:

- `RuntimeAgentTelegramEvidenceRefresh`;
- `RuntimeAgentTelegramEvidenceError`;
- strict source-row validation;
- a fixed proof dataclass/projector;
- lock-protected bounded one-shot capture storage.

**Step 4: Verify green**

Run the same command. Expected: all tests pass.

### Task 2: Add the main-service loopback probe

**Files:**
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write failing endpoint tests**

Require:

- loopback-only access and `X-Forwarded-For` refusal;
- exact `system_operator`/`notification` enum;
- exact bot-config mapping;
- fixed response keys only;
- concurrent `getMe` and `getChat`;
- one nonblocking single-flight lock and HTTP 409 when busy;
- HTTP 503 for missing config or probe failure;
- no token, chat ID, response body, URL, or exception text in output.

**Step 2: Verify red**

```bash
.venv/bin/python -m pytest -q tests/test_web_app.py \
  -k runtime_agent_telegram_evidence
```

Expected: endpoint tests fail with HTTP 404.

**Step 3: Implement the probe and endpoint**

Add an async read-only probe using `httpx.AsyncClient`, fixed method names,
`trust_env=False`, five-second timeout, and concurrent requests. Store an
injectable runner and an `asyncio.Lock`-compatible single-flight guard on app
state. Return only the fixed projection.

**Step 4: Verify green**

Run the same focused command. Expected: all tests pass.

### Task 3: Wire the handler and one-shot verification

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_runtime_agent_cli.py`
- Modify: `tests/test_runtime_agent_executor.py`

**Step 1: Write failing integration tests**

Require the sidecar reader to call only the loopback endpoint with the exact
channel; handler injection only when the coordinator exists; one live
`get_incident_summary` proof with `incident` and `telegram-evidence`
references; passive behavior after consumption; and missing-handler refusal.

**Step 2: Verify red**

```bash
.venv/bin/python -m pytest -q \
  tests/test_runtime_agent_cli.py \
  tests/test_runtime_agent_executor.py
```

Expected: new assertions fail because wiring is absent.

**Step 3: Implement minimal wiring**

Construct one coordinator per Agent process, inject its handler, and let
`get_incident_summary` consume one proof before returning its existing passive
projection.

**Step 4: Verify green**

Run the same command. Expected: all tests pass.

### Task 4: Review and regression verification

**Files:**
- Modify only files required by valid review findings.

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_runtime_agent_*.py \
  tests/test_runtime_incident_*.py \
  tests/test_runtime_incidents.py \
  tests/test_llm_chat_request.py \
  tests/test_web_app.py \
  tests/test_web_live_listener_startup.py \
  tests/test_cli_smoke.py

.venv/bin/python -m pytest -q \
  tests/test_context_resolution*.py \
  tests/test_strategy_management*.py \
  tests/test_message_instruction_items.py

.venv/bin/python -m pytest -q \
  tests/test_telegram_live_listener.py \
  tests/test_production_safety_monitor.py \
  tests/test_position_mutation*.py

.venv/bin/telegram-kol-research runtime-incident-agent-evaluate \
  --corpus-path tests/fixtures/runtime_incidents

.venv/bin/python -m compileall -q src tests
git diff --check
```

Request code review and require no remaining Critical, Important, or Minor
findings.

### Task 5: Commit, deploy dormant, and canary

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`

Commit and push reviewed changes. Before deployment, require a newer successful
recognition after raw message `8357`, complete read-only production audit, zero
time-sensitive work in flight, and all Agent/action flags off. If that cannot
be proven, do not restart.

Deploy only through the project helper with all new authority dormant. Run an
isolated temporary-database canary for one synthetic failed notification using
the deployed endpoint. Verify `action_verified`, bounded evidence references,
no Telegram send, and no production incident/notification/business-row change.
Record deployed tests, monitor result, continuity, and the exact next candidate
in the canonical status.
