# Context Resolution Target Contract Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent future non-actionable strategy commentary from becoming an authoritative recognition failure when the contextual model incorrectly attaches targets to a non-target decision.

**Architecture:** Keep the closed parser and fail-closed execution boundary unchanged. Align prompt version 2 with the parser's target-cardinality rules, issue one deterministic corrective retry after `target_not_allowed`, and persist only a bounded rejected-response diagnostic. Historical message 9758 remains untouched and is never replayed.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite compatibility migrations, httpx-backed LLM providers, pytest.

---

## Constraints

- Use strict test-driven development.
- Do not replay, reclassify, notify from, or execute raw message 9758.
- Do not relax `parse_context_resolution_decision`.
- Do not automatically edit provider decisions or target lists.
- Keep the existing maximum of two provider calls.
- Do not change strategy candidate selection, contextual attribution, or any
  business-write path.
- Deploy only after a fresh server safe-window check; verify only future
  naturally arriving messages.

### Task 1: Add bounded rejected-response diagnostics additively

**Files:**
- Modify: `tests/test_db_migrations.py`
- Modify: `src/telegram_kol_research/models.py:429-482`
- Modify: `src/telegram_kol_research/db.py:141-164`

**Step 1: Write the failing schema tests**

Add to `tests/test_db_migrations.py`:

```python
def test_context_resolution_rejected_diagnostic_has_additive_compat_migration(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "context-diagnostic.db")
    columns = {
        column["name"]
        for column in inspect(session_factory.kw["bind"]).get_columns(
            "context_resolution_attempts"
        )
    }

    assert "rejected_response_diagnostic_json" in columns
    assert (
        SQLITE_COMPAT_COLUMNS["context_resolution_attempts"]
        ["rejected_response_diagnostic_json"]
        == "ALTER TABLE context_resolution_attempts "
        "ADD COLUMN rejected_response_diagnostic_json TEXT"
    )
```

Add a legacy-database migration test that creates the existing
`context_resolution_attempts` columns without the new field, runs
`create_session_factory`, and asserts the new nullable column exists without
rewriting the existing row.

**Step 2: Run the migration tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_db_migrations.py \
  -k 'context_resolution and diagnostic'
```

Expected: FAIL because the model and compatibility map do not contain the
column.

**Step 3: Add the nullable model and compatibility column**

Add to `ContextResolutionAttempt`:

```python
rejected_response_diagnostic_json: Mapped[Optional[str]] = mapped_column(
    Text,
    nullable=True,
)
```

Add to `SQLITE_COMPAT_COLUMNS["context_resolution_attempts"]`:

```python
"rejected_response_diagnostic_json": (
    "ALTER TABLE context_resolution_attempts "
    "ADD COLUMN rejected_response_diagnostic_json TEXT"
),
```

**Step 4: Run the tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_db_migrations.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/models.py \
  src/telegram_kol_research/db.py \
  tests/test_db_migrations.py
git commit -m "feat: record bounded context rejection diagnostics"
```

### Task 2: Make prompt version 2 match the closed target contract

**Files:**
- Modify: `tests/test_context_resolution_prompt.py`
- Modify: `src/telegram_kol_research/context_resolution_prompt.py:10-48`

**Step 1: Write the failing prompt contract test**

Add:

```python
def test_context_prompt_v2_states_target_cardinality_and_commentary_example():
    assert CONTEXT_RESOLUTION_PROMPT_VERSION == "context-resolution-v2"
    assert (
        "new_thread、hold、unresolved 的 target_thread_ids 必须是 []"
        in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    )
    assert (
        "revise_thread、manage_thread、cancel_thread、exit_thread"
        in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    )
    assert "仅讨论已有策略不等于产生可执行目标" in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert '"decision": "hold"' in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert '"target_thread_ids": []' in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert "9758" not in CONTEXT_RESOLUTION_SYSTEM_PROMPT
```

**Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_context_resolution_prompt.py
```

Expected: FAIL because the prompt is version 1 and omits the cardinality
rules.

**Step 3: Implement the minimal prompt change**

Set:

```python
CONTEXT_RESOLUTION_PROMPT_VERSION = "context-resolution-v2"
```

Add explicit target rules and this redacted semantic example after the JSON
contract:

```text
target_thread_ids 规则：
- new_thread、hold、unresolved 的 target_thread_ids 必须是 []。
- revise_thread、manage_thread、cancel_thread、exit_thread 必须填写一个或多个候选 thread_id。
- 仅讨论已有策略不等于产生可执行目标。

示例：消息回顾某个已有策略但没有提出开仓、修改、取消、平仓或仓位管理指令时：
{"decision":"hold","target_thread_ids":[],"management_action":null,...}
```

Use a complete valid JSON example with closed values for every required field.
Do not include a production message ID, strategy ID, contact detail, or copied
message text.

**Step 4: Run prompt and parser regressions**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_context_resolution_prompt.py \
  tests/test_context_resolution.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/context_resolution_prompt.py \
  tests/test_context_resolution_prompt.py
git commit -m "fix: align context prompt with target contract"
```

### Task 3: Add one targeted corrective retry without normalization

**Files:**
- Modify: `tests/test_context_resolution.py`
- Modify: `src/telegram_kol_research/context_resolution.py:500-580`

**Step 1: Write the failing corrective-retry test**

Add a test whose first response is:

```python
invalid_hold = _valid_payload(
    decision="hold",
    target_thread_ids=[12],
    management_action=None,
    supporting_message_ids=[1465],
)
corrected_hold = _valid_payload(
    decision="hold",
    target_thread_ids=[],
    management_action=None,
    supporting_message_ids=[1465],
)
```

Capture both `system_prompt` values from `model_caller`. Assert:

```python
assert decision.decision == "hold"
assert decision.target_thread_ids == ()
assert len(calls) == 2
assert "上一次响应违反 target_not_allowed" not in calls[0]
assert "上一次响应违反 target_not_allowed" in calls[1]
assert "不要修改 decision 来绕过校验" in calls[1]
```

Read the durable attempt and assert:

```python
diagnostic = json.loads(attempt.rejected_response_diagnostic_json)
assert diagnostic == {
    "decision": "hold",
    "error_class": "target_not_allowed",
    "target_thread_count": 1,
}
assert "12" not in attempt.rejected_response_diagnostic_json
assert attempt.status == "completed"
assert attempt.attempts == 2
```

**Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_context_resolution.py \
  -k 'target_not_allowed and corrective'
```

Expected: FAIL because both calls receive the same prompt and no rejected
diagnostic is stored.

**Step 3: Add bounded diagnostic projection**

First extend `_upsert_attempt` with an optional already-bounded JSON string:

```python
rejected_response_diagnostic_json: str | None = None,
```

Set it when creating a row. When updating, replace it only when the argument
is non-`None`, so a successful second response retains the first rejected
diagnostic. Do not put raw provider output into this field.

Implement a helper that accepts only decoded mappings:

```python
def _rejected_response_diagnostic(
    payload: Mapping[str, Any],
    *,
    error_class: str,
) -> str:
    decision = str(payload.get("decision") or "")
    targets = payload.get("target_thread_ids")
    return _canonical_json(
        {
            "decision": decision if decision in DECISIONS else None,
            "error_class": error_class,
            "target_thread_count": len(targets) if isinstance(targets, list) else None,
        }
    )
```

This summary is bounded by its closed values and integers. It must not contain
target IDs, reasons, evidence, request data, or raw output.

**Step 4: Add the targeted correction**

Define a fixed correction string:

```python
_TARGET_NOT_ALLOWED_CORRECTION = """
纠错：上一次响应违反 target_not_allowed。
如果 decision 是 new_thread、hold 或 unresolved，target_thread_ids 必须为 []。
只有 revise_thread、manage_thread、cancel_thread、exit_thread 可以携带候选目标。
不要修改 decision 来绕过校验；请保持原本语义并修正字段组合。
""".strip()
```

Track the prior closed error within the existing two-attempt loop. For attempt
2 only, pass:

```python
system_prompt=(
    CONTEXT_RESOLUTION_SYSTEM_PROMPT
    if prior_error_code != "target_not_allowed"
    else CONTEXT_RESOLUTION_SYSTEM_PROMPT
    + "\n\n"
    + _TARGET_NOT_ALLOWED_CORRECTION
)
```

Do not modify `decoded` before parsing. Pass the bounded diagnostic into
`_upsert_attempt` for rejected decoded mappings.

**Step 5: Prove all other retry behavior remains bounded**

Extend existing tests to assert:

- malformed JSON still receives exactly two calls and no invented decision
  diagnostic;
- a different closed contract failure still receives exactly two calls;
- two invalid `hold + target` responses end `exhausted` with
  `target_not_allowed`;
- no third call occurs;
- the parser still directly rejects `hold + target`.

Run:

```bash
.venv/bin/python -m pytest -q tests/test_context_resolution.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/context_resolution.py \
  tests/test_context_resolution.py
git commit -m "fix: correct invalid context target retries"
```

### Task 4: Reproduce the redacted 9758 shape and prove no business writes

**Files:**
- Modify: `tests/test_authoritative_recognition.py`
- Modify only if required by the test: `src/telegram_kol_research/authoritative_recognition.py`

**Step 1: Write a future-message integration test**

Create a synthetic raw message with no production IDs or copied text. Its
first-pass payload should describe:

```python
{
    "recognition_result": "非策略",
    "reason": "commentary matching a recent active ETH short",
    "strategy": {},
    "lifecycle_event": {"event_type": "none", "confidence": 0.8},
    "confidence": 0.8,
}
```

Create one recent candidate ETH-short thread. Stub contextual responses as
invalid `hold + [candidate]`, then corrected `hold + []`. Process the message
through `process_authoritative_message` with an executor that records calls.

Assert:

```python
assert result.assessment.agreement_status != "authoritative_failed"
assert result.recognition.status == "非策略"
assert result.automation == {"status": "skipped", "reason": "mimo_no_action"}
assert executor_calls == []
```

Also assert the temporary database contains zero new rows in
`StrategyManagementBatch`, `PositionMutationIntent`, and `ExecutionEvent`.

**Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_authoritative_recognition.py \
  -k 'commentary and target_contract'
```

Expected: FAIL until the prompt/corrective-retry behavior is wired through the
authoritative path. If Tasks 1-3 already make it pass, first prove the same
test fails against the parent commit, then restore the implementation and
record that RED evidence in the commit message or status document.

**Step 3: Make only the minimal integration correction if needed**

Do not add fallback normalization. Any source change here may only preserve
the corrected `hold` result through the existing `_resolved_mimo_result`
non-strategy path.

**Step 4: Run authoritative and listener regressions**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_authoritative_recognition.py \
  tests/test_telegram_live_listener.py \
  tests/test_web_live_listener_startup.py
```

Expected: PASS and zero synthetic executor calls.

**Step 5: Commit**

```bash
git add tests/test_authoritative_recognition.py
git add src/telegram_kol_research/authoritative_recognition.py  # only if changed
git commit -m "test: cover non-actionable context target correction"
```

### Task 5: Document operation, run regressions, and review

**Files:**
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`
- Modify: `tests/test_runtime_agent_architecture_boundary.py`

**Step 1: Write the failing boundary assertion**

Require the runbook to state all three boundaries:

```python
assert "context-resolution-v2" in runbook
assert "never replay raw message 9758" in runbook
assert "do not normalize invalid target lists" in runbook
```

**Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_runtime_agent_architecture_boundary.py
```

Expected: FAIL because the operational boundary is not documented.

**Step 3: Update the runbook and canonical status**

Document:

- prompt version 2 target rules;
- one bounded targeted retry;
- strict parser/fail-closed behavior;
- diagnostic field contains no IDs or raw response;
- future-natural-message-only verification;
- historical message 9758 is never replayed.

Record local RED/GREEN evidence in the status file. Do not claim deployment or
production success.

**Step 4: Run focused and adjacent regressions**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_context_resolution_prompt.py \
  tests/test_context_resolution.py \
  tests/test_db_migrations.py \
  tests/test_authoritative_recognition.py \
  tests/test_telegram_live_listener.py \
  tests/test_web_live_listener_startup.py \
  tests/test_runtime_incident_adapters.py \
  tests/test_runtime_incidents.py \
  tests/test_runtime_agent_architecture_boundary.py
```

Expected: PASS.

Run:

```bash
git diff --check
```

Expected: no output.

**Step 5: Request code review and fix every Critical or Important finding**

Review the range from the design/plan parent through the implementation head.
Confirm specifically that no parser relaxation, historical replay, selector
change, or business-write authority was introduced.

**Step 6: Commit**

```bash
git add docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md \
  tests/test_runtime_agent_architecture_boundary.py
git commit -m "docs: operate context target contract repair"
```

### Task 6: Push and deploy for future natural messages only

**Files:**
- Modify only `docs/runtime-incident-agent-status.md` after successful server verification.

**Step 1: Push reviewed commits**

```bash
git push origin HEAD:codex/deepcoin-auto-trading-v1
```

Expected: fast-forward push succeeds.

**Step 2: Prove a fresh production safe window**

On the server, prove:

- latest raw message has a successfully completed authoritative recognition;
- two stable passes have zero evidence, recognition, context, management,
  component, mutation, recent execution-event, Runtime Agent, notification,
  and recovery work in flight;
- two complete exchange snapshots have the same fingerprint;
- all runtime notification selectors and the watermark remain unchanged.

If any condition fails, do not restart. Record the exact deferral in the
status file and stop.

**Step 3: Deploy through the existing helper**

Use:

```bash
./scripts/server_git_update.sh
```

Expected: production pulls the reviewed branch, reinstalls the editable
package, and restarts `telegram-kol.service` in the proven safe window.

**Step 4: Verify dormant-safe production behavior**

Confirm:

- deployed SHA and HTTP 200;
- listener and recognition continuity;
- no historical row, including 9758, changed;
- context attempt rows for 9758 remain exhausted historical evidence;
- no historical replay or new business-write artifact exists;
- the Runtime Agent and notification selectors are unchanged;
- a no-notify safety monitor pass has no new blocker.

Run the deployed focused suites from Tasks 1-5. Do not create a test trade or
inject a production message.

**Step 5: Observe future natural messages only**

When a future naturally arriving message requires contextual resolution,
verify its prompt version, decision/target contract, final authoritative
status, and absence of unexpected business writes. Do not wait indefinitely
for such a message in the deployment turn; leave live behavioral observation
pending if none arrives.

**Step 6: Record and push the deployment checkpoint**

Update `docs/runtime-incident-agent-status.md` with exact deployed SHA, test
totals, safe-window evidence, unchanged 9758 evidence, and any pending natural
observation. Commit and push the documentation-only checkpoint.
