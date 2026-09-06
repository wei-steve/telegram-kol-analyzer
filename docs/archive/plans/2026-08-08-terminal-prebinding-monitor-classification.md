# Terminal Pre-Binding Monitor Classification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the independent read-only monitor distinguish one exact terminal pre-binding safety refusal from a live entry missing binding evidence, without changing trading or historical state.

**Architecture:** Add one fail-closed SQLite evidence helper inside `production_safety_monitor.py`. The existing assembly fingerprint and reconciliation checks remain authoritative; only a binding-less assembly with one exact failed instruction and no downstream trade signal, binding, or execution event is classified as non-live.

**Tech Stack:** Python 3.12+, SQLite read-only URI/query-only mode, pytest, existing production safety monitor CLI and systemd diagnostic.

---

### Task 1: Reproduce the terminal pre-binding classification

**Files:**
- Modify: `tests/test_production_safety_monitor.py`

**Step 1: Add a production-shaped fixture**

Add `_seed_terminal_prebinding_refusal(database)` with the minimum current
schemas for:

- `raw_messages`;
- `signal_candidates`;
- `message_instruction_items`;
- `entry_preambles`;
- `entry_strategy_assemblies`;
- `execution_bindings`;
- `trade_signals`;
- `execution_events`.

Seed one assembly with no binding and one linked instruction whose durable
error is:

```python
json.dumps(
    {
        "type": "RecoveryLiveSubmitError",
        "message": (
            "signal_enqueue_blocked:missing_ready_confirmation,"
            "contract_size_unverified"
        ),
    },
    sort_keys=True,
)
```

The instruction must be `entry`, terminal `failed`, unretired, and linked to
the assembly's signal candidate and raw message. Do not seed a trade signal,
binding, or execution event.

**Step 2: Write the failing read-only regression**

```python
def test_entry_preamble_monitor_accepts_exact_terminal_prebinding_refusal(tmp_path):
    database = tmp_path / "terminal-prebinding-refusal.db"
    _seed_terminal_prebinding_refusal(database)
    before = database.read_bytes()

    assert read_entry_preamble_invariants(
        database,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    ) == ()
    assert database.read_bytes() == before
```

**Step 3: Run the test and prove the current defect**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py::test_entry_preamble_monitor_accepts_exact_terminal_prebinding_refusal \
  -q
```

Expected: FAIL with
`('live_entry_preamble_binding_evidence_missing',)`.

**Step 4: Commit the red test**

```bash
git add tests/test_production_safety_monitor.py
git commit -m "test: reproduce terminal prebinding monitor mismatch"
```

### Task 2: Implement the fail-closed evidence classifier

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Test: `tests/test_production_safety_monitor.py`

**Step 1: Add the closed refusal constants**

Near the existing reconciliation JSON bounds, add:

```python
_TERMINAL_PREBINDING_ERROR_TYPE = "RecoveryLiveSubmitError"
_TERMINAL_PREBINDING_PREFIX = "signal_enqueue_blocked:"
_TERMINAL_PREBINDING_REASONS = frozenset(
    {"missing_ready_confirmation", "contract_size_unverified"}
)
```

**Step 2: Add the bounded read-only helper**

Implement:

```python
def _has_exact_terminal_prebinding_refusal(
    connection: sqlite3.Connection,
    *,
    available_tables: set[str],
    signal_candidate_id: object,
    strategy_raw_message_id: object,
    strategy_instance_id: object,
) -> bool:
```

The helper must:

1. require `message_instruction_items`, `trade_signals`, `execution_bindings`,
   `execution_events`, and `raw_messages` in `available_tables`;
2. require positive integer candidate/raw IDs and a non-empty bounded strategy
   identity;
3. load at most two instruction rows and require exactly one row with matching
   `raw_message_id`, `instruction_kind='entry'`, `status='failed'`, and
   `retired_at IS NULL`;
4. parse `error_json` through `_read_reconciliation_json` and require exactly
   the keys `type` and `message`;
5. require the exact error type and prefix, exactly two unique comma-separated
   reason codes, and equality with `_TERMINAL_PREBINDING_REASONS`;
6. load the source chat/message identity from `raw_messages`;
7. return false if any matching trade signal, binding, or execution event
   exists;
8. perform no commit, update, insert, service call, provider call, or network
   call.

Use bounded `LIMIT 2`/`LIMIT 1` queries throughout.

**Step 3: Preserve partial-schema fail-closed behavior**

The existing monitor tests construct older minimal table shapes. Extend the
dynamic `entry_strategy_assemblies` column inspection so the main evidence
query selects:

```python
assembly_candidate_column = (
    "a.signal_candidate_id" if "signal_candidate_id" in assembly_columns else "NULL"
)
assembly_raw_column = (
    "a.strategy_raw_message_id"
    if "strategy_raw_message_id" in assembly_columns
    else "NULL"
)
```

Do not add the new tables to the top-level `required_tables` set. Missing
tables or columns must make the new helper return false, which preserves the
existing invariant rather than suppressing it.

**Step 4: Wire the helper after existing authority checks**

For a row that neither matches the assembly fingerprint nor has exact
reconciliation, compute the terminal refusal only when `binding_id is None`:

```python
terminal_prebinding_refusal = (
    binding_id is None
    and _has_exact_terminal_prebinding_refusal(
        connection,
        available_tables=available,
        signal_candidate_id=signal_candidate_id,
        strategy_raw_message_id=strategy_raw_message_id,
        strategy_instance_id=strategy_instance_id,
    )
)
if not matches and not reconciled and not terminal_prebinding_refusal:
    reasons.add("live_entry_preamble_binding_evidence_missing")
    break
```

Never allow this branch to bypass a mismatched existing binding.

**Step 5: Run the focused test**

Run the command from Task 1.

Expected: PASS.

**Step 6: Run the full monitor reader file**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py -q
```

Expected: all tests pass.

**Step 7: Commit the minimal implementation**

```bash
git add src/telegram_kol_research/production_safety_monitor.py \
  tests/test_production_safety_monitor.py
git commit -m "fix: classify terminal prebinding safety refusals"
```

### Task 3: Prove every ambiguous case still fails closed

**Files:**
- Modify: `tests/test_production_safety_monitor.py`

**Step 1: Add parameterized instruction-evidence mutations**

Starting from `_seed_terminal_prebinding_refusal`, mutate one fact at a time:

- remove the item;
- insert a second linked item;
- change status to `pending`, `executing`, `unknown`, or `succeeded`;
- set `retired_at`;
- change instruction kind;
- use malformed, duplicate-key, oversized, or non-object error JSON;
- change error type;
- remove either reason, duplicate a reason, or add an unknown reason.

Each case must assert:

```python
assert read_entry_preamble_invariants(database, now=NOW) == (
    "live_entry_preamble_binding_evidence_missing",
)
```

**Step 2: Add downstream-evidence mutations**

Parameterize insertion of exactly one of:

- a matching `trade_signals` row;
- a matching `execution_bindings` row;
- an `execution_events` row matching the strategy identity;
- an `execution_events` row matching the source chat/message.

All cases must retain the invariant. The binding case must prove that the new
exception cannot override existing binding fingerprint authority.

**Step 3: Add partial-schema coverage**

Add or extend a test proving that absent instruction/trade/event tables do not
raise and do not suppress the existing invariant.

**Step 4: Verify red/green discipline**

Run each new parameter group before any necessary implementation correction.
Expected before correction: at least the deliberately unsupported shape fails.
Make only the smallest helper correction and rerun until every case passes.

**Step 5: Run focused and adjacent regressions**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_safety_monitor.py \
  tests/test_monitor_cli.py \
  tests/test_runtime_agent_architecture_boundary.py \
  tests/test_message_operation_projection.py \
  -q
```

If `tests/test_monitor_cli.py` does not exist, use the actual monitor CLI test
file returned by:

```bash
rg --files tests | rg 'monitor.*cli|production_safety_monitor'
```

Expected: all selected tests pass; known warnings only.

**Step 6: Commit the fail-closed matrix**

```bash
git add tests/test_production_safety_monitor.py \
  src/telegram_kol_research/production_safety_monitor.py
git commit -m "test: keep prebinding classification fail closed"
```

### Task 4: Review, deploy dormant, and restore monitor health

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Request independent code review**

Use the `requesting-code-review` skill. Resolve every Critical or Important
finding with TDD and rerun Task 3 regressions. Do not deploy with an open
Critical or Important finding.

**Step 2: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 3: Prove a fresh production safe window**

Using read-only server checks, require:

- latest raw message has a terminal recognition decision;
- zero recent evidence, recognition, context, management, component,
  position-mutation, recovery, Runtime Agent, and notification claims;
- no recent execution event or raw-message arrival during two passes;
- two complete, stable read-only protection audits with identical counts;
- the Phase 8R.5 supervisor keys remain absent and both contract tables remain
  empty.

If any check fails, update the status file and stop without restart.

**Step 4: Deploy with Phase 8R.5 still disabled**

Stop the independent monitor timer, then use the standard reviewed update
helper:

```bash
BRANCH=codex/deepcoin-auto-trading-v1 /usr/local/bin/telegram-kol-update
```

Verify main service, Runtime Agent, scanner, HTTP docs, latest checkpoint, and
empty contract tables. Run the focused server tests.

**Step 5: Synchronize and run the independent diagnostic**

With the timer inactive, reinstall the monitor with the current live entry
mode expectations and new expected HEAD. Run
`telegram-kol-monitor-diagnostic.service` and require:

```json
{
  "healthy": true,
  "monitor_error": null,
  "notification_status": "disabled",
  "reason_codes": []
}
```

Re-enable the timer only after this result. If the diagnostic is not healthy,
keep the supervisor disabled and stop the phase.

**Step 6: Commit and push the deployment checkpoint**

Record exact test, review, deployment, monitor, and rollback evidence in
`docs/runtime-incident-agent-status.md`, then:

```bash
git add docs/runtime-incident-agent-status.md
git commit -m "docs: verify terminal prebinding monitor repair"
git push origin codex/deepcoin-auto-trading-v1
```

### Task 5: Resume the Phase 8R.5 future-only canary

**Files:**
- Modify: `docs/runtime-incident-agent-status.md`
- Modify on server only after the gate: `config/runtime_incident_agent.env`

**Step 1: Prove a second fresh safe window**

Repeat Task 4 Step 3 after monitor health is restored. Record the current
maximum `raw_messages.id` only after the final quiet pass.

**Step 2: Atomically configure the manual-only supervisor**

In the root-owned mode-0600 policy file, set exactly:

```dotenv
TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_ENABLED=true
TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_SHADOW_ONLY=true
TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_AFTER_RAW_MESSAGE_ID=<current-max-raw-id>
TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_BATCH_LIMIT=50
```

Do not restart the main service because no normal service imports or invokes
the projector. Keep a mode-0600 backup for immediate recovery.

**Step 3: Run exactly one bounded one-shot cycle**

```bash
.venv/bin/telegram-kol-research message-operation-supervisor \
  --database-path data/research.db --shadow --once
```

Require `model_calls=0`, `errors=0`, and no row at or below the watermark.

**Step 4: Compare protected state**

Before and after hashes/counts must prove no change to recognition, context,
instruction, strategy, management, execution, protection, incident,
notification, Agent, recovery, or exchange state. Only future eligible rows in
`message_operation_contracts` and `message_operation_items` may differ.

**Step 5: Prove rollback**

Invoke the CLI with an environment override setting supervisor enabled false
and require `{"status":"disabled"}` with zero writes. Restore the reviewed
shadow-only configuration; no timer or long-running unit is installed.

**Step 6: Advance the canonical phase**

If all checks pass, update `docs/runtime-incident-agent-status.md`:

- mark 8R.5 complete;
- set `last_completed_phase: "8R.5"` and its deployed commit;
- advance only to the next planned phase from the approved roadmap;
- preserve action authority false and both playbook allowlists empty.

Run the architecture/status tests, commit, and push. Send exactly one project
stop notification immediately before returning control.
