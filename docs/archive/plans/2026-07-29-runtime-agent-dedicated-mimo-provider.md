# Runtime Agent Dedicated MiMo Provider Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route the Runtime Incident AI Agent through a dedicated, fail-closed MiMo configuration while leaving token accounting to the MiMo console.

**Architecture:** Add a dedicated configuration loader that reads only `TELEGRAM_KOL_RUNTIME_AGENT_LLM_*` values and never falls back to the shared LLM or recognition credentials. Wire both Agent CLI entry points to validate this configuration before they create a claim, then deploy the secret only in the root-owned sidecar environment file with every Agent/action flag still disabled.

**Tech Stack:** Python 3.12, Typer, httpx, pytest, systemd, SQLite.

---

### Task 1: Dedicated provider configuration

**Files:**
- Modify: `src/telegram_kol_research/llm_chat.py`
- Test: `tests/test_llm_chat_request.py`

**Step 1: Write the failing isolation tests**

Add tests that load:

```python
environ = {
    "TELEGRAM_KOL_LLM_API_KEY": "shared-key",
    "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL": "https://api.xiaomimimo.com/v1",
    "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY": "agent-key",
    "TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL": "mimo-v2.5",
}
```

Assert that the returned configuration uses only the dedicated values. Add a
second test proving that shared values alone raise a bounded configuration
error whose message contains no credential.

**Step 2: Run the tests to verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_llm_chat_request.py::test_runtime_agent_llm_config_is_isolated \
  tests/test_llm_chat_request.py::test_runtime_agent_llm_config_never_falls_back_to_shared_key -q
```

Expected: FAIL because `load_runtime_agent_llm_config` does not exist.

**Step 3: Implement the minimal loader**

Add `load_runtime_agent_llm_config` to `llm_chat.py`. It must:

- read `.env`, `config/llm.env`, and
  `config/runtime_incident_agent.env` unless explicit paths are passed;
- read only the four `TELEGRAM_KOL_RUNTIME_AGENT_LLM_*` keys;
- require a non-empty HTTPS base URL, API key, and model;
- parse and bound timeout to 5–120 seconds;
- raise `RuntimeAgentLLMConfigError` with a fixed redacted message when
  incomplete or invalid;
- return the existing `LLMProxyConfig`.

It must not inspect or fall back to `TELEGRAM_KOL_LLM_*`.

**Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_llm_chat_request.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/llm_chat.py tests/test_llm_chat_request.py
git commit -m "feat: isolate runtime agent MiMo credentials"
```

### Task 2: Fail before claiming an incident

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Test: `tests/test_runtime_agent_cli.py`

**Step 1: Write the failing CLI tests**

Add tests that enable the Runtime Agent, provide only shared LLM credentials,
and invoke `runtime-incident-agent-once` against a database containing one
claimable incident. Assert:

```python
assert result.exit_code != 0
assert "dedicated Runtime Agent provider configuration is invalid" in result.output
assert shared_key not in result.output
assert incident.status == "pending"
assert incident.claim_token is None
assert incident.agent_attempt_count == 0
```

Add a wiring test that monkeypatches `load_runtime_agent_llm_config` and proves
both `runtime-incident-agent-once` and `runtime-incident-agent-worker` pass the
dedicated configuration to `request_structured_chat_turn`.

**Step 2: Run the tests to verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_cli.py -k "dedicated_provider or missing_dedicated" -q
```

Expected: FAIL because the CLI still calls `load_llm_proxy_config`.

**Step 3: Wire the dedicated loader**

Replace the generic loader only in the two Runtime Agent commands. Preserve
the disabled path so a disabled Agent never requires credentials. Do not
change grounded chat, strategy alerts, authoritative recognition, or
contextual resolution.

**Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_cli.py tests/test_llm_chat_request.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py tests/test_runtime_agent_cli.py
git commit -m "fix: require dedicated runtime agent provider"
```

### Task 3: Operational contract and secret-safe examples

**Files:**
- Create: `config/runtime_incident_agent.env.example`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`
- Test: `tests/test_runtime_agent_architecture_boundary.py`

**Step 1: Write the failing boundary test**

Add a source-boundary assertion that Runtime Agent CLI code references
`load_runtime_agent_llm_config` and does not load a generic provider within
either Agent command. Add an example-file test asserting that the dedicated
variable names and `mimo-v2.5` placeholder exist but no value matching an API
key pattern is committed.

**Step 2: Run the test to verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_architecture_boundary.py -q
```

Expected: FAIL until the example and boundary are updated.

**Step 3: Update examples and operations docs**

Document:

- direct MiMo endpoint and `mimo-v2.5`;
- dedicated-key isolation and console-only token accounting;
- root ownership and mode `0600`;
- no shared-key fallback;
- disabled-first deployment and rollback;
- key-redacted server verification.

Update Phase 6 status to record the reviewed provider configuration work while
leaving the phase `in_progress` and action flags disabled.

**Step 4: Run tests to verify GREEN**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_architecture_boundary.py \
  tests/test_runtime_incident_phase5_config.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add config/runtime_incident_agent.env.example \
  docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md \
  tests/test_runtime_agent_architecture_boundary.py
git commit -m "docs: define dedicated MiMo agent provider"
```

### Task 4: Full local gate and review

**Files:**
- Review all files changed since the design commit.

**Step 1: Run focused and boundary suites**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_llm_chat_request.py \
  tests/test_runtime_agent_cli.py \
  tests/test_runtime_agent_worker.py \
  tests/test_runtime_agent_executor.py \
  tests/test_runtime_incident_phase5_config.py \
  tests/test_runtime_agent_architecture_boundary.py -q
```

Expected: PASS.

**Step 2: Run the Runtime Agent regression gate**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_runtime_agent_policy.py \
  tests/test_runtime_agent_playbooks.py \
  tests/test_runtime_incidents.py \
  tests/test_runtime_incident_handoff.py \
  tests/test_runtime_agent_prompt.py \
  tests/test_runtime_agent_tools.py \
  tests/test_system_operator_bot.py \
  tests/test_db_bootstrap.py \
  tests/test_db_migrations.py -q
```

Expected: PASS.

**Step 3: Run the offline corpus**

```bash
PYTHONPATH=src .venv/bin/python -m telegram_kol_research.cli \
  runtime-incident-agent-evaluate \
  --corpus-path tests/fixtures/runtime_incidents
```

Expected: `all_passed: true`.

**Step 4: Request code review**

Review for secret leakage, fallback to shared credentials, claim-before-config
ordering, unchanged authoritative recognition, and incomplete rollback.
Resolve every Critical and Important finding.

**Step 5: Commit review fixes and push**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

### Task 5: Install the secret and verify the dormant provider

**Files:**
- Server-only secret: `/opt/telegram-kol-analyzer/config/runtime_incident_agent.env`
- Modify after verification: `docs/runtime-incident-agent-status.md`

**Step 1: Prove a deployment safe window**

Use the runbook read-only checks. Require zero active evidence/context claims,
zero active management batches, zero in-flight position mutations, a completed
latest recognition, active HTTP service, and a disabled/inactive Agent
sidecar. If any check is incomplete, stop and record the exact blocker.

**Step 2: Deploy code with the Agent disabled**

Push the reviewed commits, run the existing server update helper, and confirm
the main service/listener continuity before touching the secret file.

**Step 3: Install the dedicated secret without displaying it**

Merge the four dedicated provider variables into
`config/runtime_incident_agent.env`; preserve existing capture and notification
settings; keep:

```text
TELEGRAM_KOL_RUNTIME_AGENT_ENABLED=false
TELEGRAM_KOL_RUNTIME_AGENT_ACTIONS_ENABLED=false
TELEGRAM_KOL_RUNTIME_AGENT_ACTION_PLAYBOOKS=
```

Set root ownership and mode `0600`. Never print the file or key.

**Step 4: Validate configuration without a model call**

As the sidecar user, load the configuration and print only:

```text
configured=true
host=api.xiaomimimo.com
model=mimo-v2.5
api_key_present=true
```

Confirm the sidecar remains disabled/inactive and no recovery-attempt row was
created.

**Step 5: Perform a bounded direct provider compatibility probe**

From the server, make one redacted OpenAI-compatible request using the
dedicated configuration. The prompt must request a fixed non-business JSON
response, include no incident or Telegram data, and expose only HTTP status,
selected model, and response-shape validity. Do not print headers, request
body, response text, or usage fields.

If the probe fails, keep the sidecar disabled, leave Phase 6 `in_progress`,
record the redacted failure class, and stop.

**Step 6: Record verification and commit**

Update the canonical status with the deployed commit, secret file permissions,
redacted provider proof, unchanged zero-action ledger, and continuity checks.
Do not record the key or token usage. Commit, push, and pull the documentation
commit without restarting production.
