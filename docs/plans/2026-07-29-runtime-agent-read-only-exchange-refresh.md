# Runtime Agent Read-Only Exchange Refresh Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a dormant, credential-isolated handler and independent positive
verification for `refresh_read_only_exchange_snapshot`.

**Architecture:** The main service returns a bounded redacted fingerprint of
two Deepcoin read endpoints. A sidecar-local coordinator captures one proof in
the handler and compares it with an independently fetched proof in the
verification tool. Existing executor fences remain unchanged.

**Tech Stack:** Python 3.13, FastAPI, httpx, SQLAlchemy, pytest, systemd.

---

### Task 1: Bounded exchange snapshot proof

**Files:**
- Create: `src/telegram_kol_research/runtime_agent_exchange_snapshot.py`
- Create: `tests/test_runtime_agent_exchange_snapshot.py`

1. Write failing tests for redacted stable fingerprints, the 200-row bound,
   malformed source refusal, coherent two-read verification, and drift refusal.
2. Run the focused tests and confirm they fail because the module is absent.
3. Implement the minimal projection and process-local coordinator.
4. Run the focused tests and confirm they pass.

### Task 2: Main-service read-only endpoint

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_app.py`

1. Write a failing endpoint test with an injected Deepcoin client.
2. Prove the response contains only kind, completeness, counts, and a
   fingerprint, and that client failures return an incomplete bounded result.
3. Implement the GET endpoint without database or exchange writes.
4. Run the endpoint and web startup tests.

### Task 3: Dormant production wiring

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `tests/test_runtime_agent_cli.py`
- Modify: `tests/test_runtime_agent_executor.py`

1. Write failing tests showing the handler performs the first read, the
   verification tool performs the second read, stable proofs verify, and drift
   fails closed.
2. Add the localhost reader, inject one coordinator into the tool registry and
   action handlers, and preserve passive durable comparison before refresh.
3. Run executor, CLI, worker, and web focused tests.

### Task 4: Review, regression, and rollout checkpoint

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`

1. Run runtime-agent, web endpoint, context-resolution, management,
   listener/monitor/mutation, and offline-corpus gates.
2. Request code review and resolve every Critical or Important finding.
3. Record local results, commit, and push.
4. Prove a fresh production safe window, deploy with every Agent/action flag
   off, and verify service/listener continuity.
5. Canary only `refresh_read_only_exchange_snapshot` in a bounded one-shot
   process; restore all flags to empty/off and record exact evidence.
