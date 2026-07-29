# Runtime Agent Stale Context Claim Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a dormant, compare-and-set Phase 6 handler that returns one proven stale, side-effect-free contextual reanalysis claim to its existing queue and verifies the exact committed transition.

**Architecture:** A dedicated in-process coordinator validates the runtime incident and source attempt, refuses any live or business-writing target, performs one narrow SQL compare-and-set update, and retains a bounded one-shot verification proof. The existing CLI tool registry consumes that proof through `get_worker_state`; the executor remains the sole authority gate and the authoritative context worker remains the sole consumer of the restored queue item.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, Typer, pytest.

---

### Task 1: Define the claim recovery contract with failing tests

**Files:**
- Create: `tests/test_runtime_agent_context_claim_recovery.py`
- Test: `tests/test_runtime_agent_policy.py`

**Step 1: Write failing tests**

Cover a stale `ContextResolutionAttempt` referenced by a matching
`context_worker_exhausted` incident whose summary contains
`claim_status=stale` and `claim_side_effect_class=none`. Assert the desired
coordinator API returns true, restores `pending_reanalysis`, clears only the
claim fields, preserves attempts and decision data, and exposes one bounded
proof.

Add refusal tests for a live claim, wrong fingerprint, unsupported source,
missing target, malformed summary, terminal instruction, management batch,
revision batch, and a compare-and-set race.

**Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_runtime_agent_context_claim_recovery.py
```

Expected: collection or import failure because the coordinator does not exist.

### Task 2: Implement the minimal compare-and-set coordinator

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_context_claim_recovery.py`
- Test: `tests/test_runtime_agent_context_claim_recovery.py`

**Step 1: Implement validation and release**

Add `RuntimeAgentContextClaimRecovery` with:

- strict incident identity and summary validation;
- exact five-minute stale cutoff based on `DEFAULT_STALE_AFTER`;
- read-only absence checks for terminal instructions, management batches, and
  revision batches;
- one compare-and-set update matching the observed claim token and
  `claimed_at`;
- a lock-protected `OrderedDict` limited to 32 proofs;
- atomic one-shot `consume_verification`.

**Step 2: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_runtime_agent_context_claim_recovery.py
```

Expected: all tests pass.

### Task 3: Wire action and independent verification

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_runtime_agent_cli.py`
- Modify: `tests/test_runtime_agent_executor.py`

**Step 1: Write failing integration tests**

Assert:

- `get_worker_state` consumes a live recovery proof once and emits only the
  incident and context-attempt evidence references;
- `_build_runtime_agent_action_handlers` injects
  `recover_stale_side_effect_free_claim` only when the coordinator is supplied;
- both one-shot and worker entrypoints construct the coordinator;
- a missing coordinator still produces `executor_not_configured`;
- executor verification accepts only the exact bounded proof shape.

**Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_runtime_agent_cli.py \
  tests/test_runtime_agent_executor.py
```

Expected: focused assertions fail because the new coordinator is not wired.

**Step 3: Implement minimal wiring**

Pass the coordinator to the tool registry and handler builder. Consume its
proof before the passive `get_worker_state` projection. Construct one
coordinator per Agent process using the existing session factory.

**Step 4: Run focused tests**

Run the same command. Expected: all tests pass.

### Task 4: Run safety regressions and review

**Files:**
- Modify only files required by review findings.

**Step 1: Run runtime-focused tests**

```bash
.venv/bin/python -m pytest -q \
  tests/test_runtime_agent_*.py \
  tests/test_runtime_incident_*.py \
  tests/test_runtime_incidents.py \
  tests/test_llm_chat_request.py \
  tests/test_cli_smoke.py
```

**Step 2: Run contextual and management regressions**

```bash
.venv/bin/python -m pytest -q \
  tests/test_context_resolution*.py \
  tests/test_strategy_management*.py \
  tests/test_message_instruction_items.py
```

**Step 3: Run listener, monitor, and mutation regressions**

```bash
.venv/bin/python -m pytest -q \
  tests/test_live_listener.py \
  tests/test_production_safety_monitor.py \
  tests/test_position_mutation*.py
```

**Step 4: Run offline evaluation and static checks**

```bash
.venv/bin/telegram-kol-research runtime-incident-agent-evaluate \
  --corpus-path tests/fixtures/runtime_incidents
.venv/bin/python -m compileall -q src tests
git diff --check
```

**Step 5: Request code review**

Require no remaining Critical, Important, or Minor findings before deployment.

### Task 5: Commit, deploy dormant, and run an isolated canary

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Commit and push reviewed code**

Commit only the new design, plan, implementation, tests, and status changes to
`codex/deepcoin-auto-trading-v1`.

**Step 2: Prove the production safe window**

Confirm service health, complete latest recognition, zero evidence/context/
management/mutation/recovery work in flight, unchanged known audit baseline,
and all Agent/shadow/action flags off.

**Step 3: Deploy with all flags off**

Use the project server update helper. Confirm the service returns HTTP 200 and
the sidecar remains disabled and inactive.

**Step 4: Run isolated temporary-database canary**

Create one synthetic stale context attempt and matching incident in a temporary
database. Enable exactly `recover_stale_side_effect_free_claim` only in the
canary process. Require:

- executor status `verified`;
- incident recovery status `action_verified`;
- evidence kinds `incident` and `context-attempt`;
- source attempt restored to `pending_reanalysis`;
- no production incident, management, revision, instruction, or mutation row
  changes.

**Step 5: Record continuity**

Run deployed focused tests and the offline gate, run the no-notify production
monitor, update the canonical status, commit and push the documentation-only
checkpoint without another production restart.
