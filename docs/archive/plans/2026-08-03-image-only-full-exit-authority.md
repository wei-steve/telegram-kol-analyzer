# Image-Only Full-Exit Authority Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure an exact, authoritative image-only `exit_full` instruction reaches the existing verified-position close path instead of being downgraded by explanatory `成本价` wording.

**Architecture:** Preserve structured lifecycle authority first, use only raw text plus current-image `observed_text` as bounded instruction evidence, and recognize full-exit action aliases as a secondary guard. Keep strategy targeting, verified-leg ownership, management batching, fresh Deepcoin preflight, and reconciliation unchanged.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, existing MiMo/context-resolution pipeline, existing Deepcoin management executor.

---

### Task 1: Lock down directive and downgrade behavior with failing unit tests

**Files:**
- Modify: `tests/test_management_directives.py`
- Modify: `tests/test_message_recognition.py`

**Step 1: Add a failing directive-alias test**

Add this test beside `test_full_exit_and_cancel_entry_are_risk_reducing`:

```python
@pytest.mark.parametrize(
    "action",
    ["exit_full", "full_exit", "close_position"],
)
def test_structured_full_exit_action_survives_position_update_alias(action: str) -> None:
    directive = resolve_management_directive(
        text="",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": action,
            "symbol": "BTC",
            "side": "short",
            "reason": "BTC 空单成本价附近出局",
        },
    )

    assert directive.intent == "full_exit"
    assert directive.risk_reducing is True
    assert directive.cancel_deferred_entries is True
```

**Step 2: Add a failing downgrade-precedence test**

Import `_exit_decision_looks_like_management_update` from
`telegram_kol_research.message_recognition`, then add:

```python
def test_explicit_full_exit_is_not_downgraded_by_cost_price_reason() -> None:
    assert _exit_decision_looks_like_management_update(
        "",
        {
            "event_type": "exit_position",
            "management_action": "exit_full",
            "reason": "当前消息明确指示BTC空单成本价附近出局",
        },
    ) is False
```

**Step 3: Run both tests and verify the current bug**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_management_directives.py::test_structured_full_exit_action_survives_position_update_alias \
  tests/test_message_recognition.py::test_explicit_full_exit_is_not_downgraded_by_cost_price_reason \
  -q
```

Expected: both tests FAIL on the current implementation. The directive aliases
resolve to `none`, and the empty raw text allows explanatory `成本价` wording to
downgrade the full exit.

**Step 4: Keep the failing tests for the implementation step**

Do not commit a red test state. Continue directly to Task 2 and commit the
tests together with the minimal passing implementation.

### Task 2: Implement structured full-exit precedence and alias normalization

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py:1383-1442`
- Modify: `src/telegram_kol_research/management_directives.py:89-165`
- Test: `tests/test_management_directives.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Add closed full-exit action constants**

In `management_directives.py`, add near the existing term constants:

```python
FULL_EXIT_ACTIONS = frozenset({"exit_full", "full_exit", "close_position"})
```

In `message_recognition.py`, either import this constant or define one private
equivalent next to the exit-downgrade helpers. Prefer importing the shared
constant so both layers use the same closed vocabulary.

**Step 2: Normalize explicit action aliases**

Change the full-exit branch in `resolve_management_directive` to:

```python
if event_type in {
    "exit_position", "exit_full", "full_exit", "close_position",
} or raw_action in FULL_EXIT_ACTIONS or (
    any(term in combined for term in _FULL_EXIT_TERMS)
    and not any(
        term in combined
        for term in ("剩余仓位", "剩余持仓", "其余仓位", "剩下仓位")
    )
):
    return _directive(
        "full_exit",
        symbol=symbol,
        side=side,
        reason_code="explicit_full_exit",
        strategy_thread_id=strategy_thread_id,
    )
```

**Step 3: Give structured full exit highest downgrade precedence**

Update `_exit_decision_looks_like_management_update` so it first returns
`False` when `event_type` or `management_action` is an explicit full exit:

```python
management_action = str(
    decision.get("management_action") or ""
).strip().lower()
if management_action in FULL_EXIT_ACTIONS:
    return False
event_type = str(decision.get("event_type") or "").strip().lower()
if event_type in {"exit_full", "full_exit", "close_position"}:
    return False
```

Keep the explicit current-message full-exit phrase check after the structured
check. Replace the downgrade helper's use of `_combined_lifecycle_text` with a
bounded instruction string:

```python
instruction_text = " ".join(
    (
        str(text or ""),
        management_action,
    )
).lower()
if _has_full_exit_instruction(str(text or "").lower()):
    return False
return (
    _has_partial_take_profit_terms(instruction_text)
    or _has_protective_stop_terms(instruction_text)
)
```

Apply the same bounded instruction string in
`_management_action_for_exit_downgrade`. Do not use explanatory `reason` text
as proof that a full exit should be downgraded or converted to protection.

**Step 4: Run the focused unit tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_management_directives.py \
  tests/test_message_recognition.py \
  -q
```

Expected: PASS.

**Step 5: Commit the minimal rule fix**

```bash
git add src/telegram_kol_research/management_directives.py \
  src/telegram_kol_research/message_recognition.py \
  tests/test_management_directives.py \
  tests/test_message_recognition.py
git commit -m "fix: preserve authoritative full-exit actions"
```

### Task 3: Propagate bounded image-observed instruction evidence

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py:1775-1925`
- Modify: `src/telegram_kol_research/message_recognition.py:2231-2390`
- Modify: `tests/test_message_recognition.py`

**Step 1: Add the production-shaped failing integration test**

Create a test that inserts:

- `RawMessage(text="")` for the exit image;
- one entered BTC-short lifecycle in the same chat;
- one active Deepcoin execution binding;
- one active `ExecutionOrderLeg(purpose="entry", attribution_status="verified", pos_id="pos-feiyang")`;
- an authoritative payload with `input_reading.observed_text` equal to
  `BTC空单，目前成本价附近，出局吧`;
- `lifecycle_event={"event_type": "exit_position", "target_lifecycle_id":
  lifecycle.id, "confidence": 0.95}` with no explicit management action, so
  the observed image text is required to preserve the exit.

Call `apply_authoritative_mimo_payload`, then assert:

```python
assert result.status == "非策略"
candidate = session.query(SignalCandidate).one()
item = session.query(MessageInstructionItem).one()
assert candidate.event_type == "close_signal"
assert candidate.management_action == "full_exit"
assert candidate.target_lifecycle_id == lifecycle_id
assert item.instruction_kind == "management"
assert item.signal_candidate_id == candidate.id
```

Run the new test alone. Expected before evidence propagation: FAIL with no
projected candidate or a safely-not-applied recognition result.

**Step 2: Add a bounded evidence helper**

Add this helper near the downgrade functions:

```python
def _authoritative_current_message_text(
    raw_text: str | None,
    payload: Mapping[str, Any],
) -> str:
    input_reading = payload.get("input_reading")
    input_reading = (
        input_reading if isinstance(input_reading, Mapping) else {}
    )
    observed_text = str(input_reading.get("observed_text") or "").strip()
    parts = [str(raw_text or "").strip(), observed_text]
    return "\n".join(dict.fromkeys(part for part in parts if part))
```

The helper must not read either top-level or lifecycle-event `reason`.

**Step 3: Thread current-message evidence through authoritative management**

Add an optional `current_message_text: str | None = None` parameter to
`_apply_deterministic_management_scope_if_matched` and
`_apply_lifecycle_event_decision`. Use the supplied evidence for downgrade,
directive normalization, and explicit value extraction; default to
`raw_message.text` for existing callers.

In `apply_authoritative_mimo_payload`, compute:

```python
current_message_text = _authoritative_current_message_text(
    raw_message.text,
    payload,
)
```

Pass it through both deterministic management calls, recursive target calls,
and the final lifecycle-event application. Do not put it into persisted model
payloads or candidate notes.

**Step 4: Add negative evidence tests**

Add tests proving:

```python
# Holding language is not a close.
observed_text = "BTC空单目前成本价附近，继续拿着"

# Partial/protective language is not a full close.
observed_text = "BTC空单减仓一半，剩余仓位保护成本"
```

For the first, no close candidate may be created. For the second, the candidate
must remain partial/protective management and must not have
`management_action="full_exit"`.

**Step 5: Run focused recognition tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_message_recognition.py \
  tests/test_authoritative_recognition.py \
  tests/test_management_directives.py \
  -q
```

Expected: PASS, including the image-only production regression.

**Step 6: Commit evidence propagation**

```bash
git add src/telegram_kol_research/message_recognition.py \
  tests/test_message_recognition.py
git commit -m "fix: use bounded image evidence for exit decisions"
```

### Task 4: Verify exact-target fail-closed behavior and downstream execution

**Files:**
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_auto_trade_execution.py`
- Test: `tests/test_strategy_management_planner.py`
- Test: `tests/test_strategy_management_executor.py`

**Step 1: Strengthen the authoritative pipeline test**

Add or extend a test around
`test_fengge_exit_applies_mimo_while_execution_gate_is_pending` so an
image-only payload must produce a current instruction item before
`auto_trade_executor` is invoked. Assert automation is not
`mimo_authoritative_not_safely_applied` and that the executor is called exactly
once with the source raw-message ID.

**Step 2: Add an unverified-target regression**

Repeat the image-only exit setup without a verified entry leg. Assert no live
close submission occurs and the result is blocked or safely skipped by the
existing ownership gate. Never add a symbol/side fallback.

**Step 3: Run the management pipeline suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_authoritative_recognition.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  tests/test_management_directives.py \
  -q
```

Expected: PASS. Full exits still target only verified exact positions; partial
close, stop adjustment, cancellation, and conflict paths remain unchanged.

**Step 4: Commit pipeline coverage**

```bash
git add tests/test_authoritative_recognition.py \
  tests/test_auto_trade_execution.py
git commit -m "test: cover image exit execution pipeline"
```

### Task 5: Document the invariant and complete local verification

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/runbook.md`

**Step 1: Document the authority invariant**

Add a concise note stating:

- current-message instruction evidence is raw text plus authoritative image
  observed text;
- explanatory model rationale is never executable instruction text;
- exact structured `exit_full` cannot be downgraded by `成本价` wording;
- all ownership and fresh-position gates remain mandatory.

**Step 2: Run formatting and focused tests**

Run the project's configured formatter/linter if present, then:

```bash
.venv/bin/python -m pytest \
  tests/test_management_directives.py \
  tests/test_message_recognition.py \
  tests/test_authoritative_recognition.py \
  tests/test_auto_trade_execution.py \
  tests/test_strategy_management_planner.py \
  tests/test_strategy_management_executor.py \
  -q
```

Expected: PASS.

**Step 3: Run the full local suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. Any unrelated pre-existing failure must be recorded separately
and must not be hidden by changing this fix.

**Step 4: Review the change**

Use the `requesting-code-review` skill. Review specifically for:

- any use of AI `reason` as executable source evidence;
- any relaxation of exact lifecycle or verified-posId gates;
- accidental fan-out to other BTC-short strategies;
- regression from partial/protective management to full close;
- duplicate execution after retries.

**Step 5: Commit documentation and review corrections**

```bash
git add docs/migration-handoff.md docs/runbook.md
git commit -m "docs: define image exit authority invariant"
```

### Task 6: Push, deploy in a safe window, and verify passively

**Files:**
- Verify: `scripts/server_git_update.ps1`
- Verify: `scripts/server_git_update.sh`

**Step 1: Confirm repository scope**

```bash
git status --short --branch
git diff --check
git log --oneline --decorate -8
```

Expected: branch is `codex/deepcoin-auto-trading-v1`; only reviewed changes for
this fix are committed; unrelated user files remain untouched.

**Step 2: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: remote branch advances to the reviewed local SHA.

**Step 3: Prove a safe deployment window**

Use server-side read-only checks to confirm there is no in-flight management
batch, unknown exchange outcome, active mutation reservation, or newly arrived
time-sensitive strategy operation. If the safe window cannot be proven, stop
after the push and record the exact pending server verification.

**Step 4: Deploy through the normal helper**

On an approved workstation, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the reviewed SHA, reinstalls the editable package,
restarts `telegram-kol.service`, and reports it active.

**Step 5: Verify production without creating a live signal**

Confirm:

- server SHA equals the pushed SHA;
- `telegram-kol.service` is active with a new start time;
- focused server tests pass;
- trading settings and group allowlists are unchanged;
- no new unknown exchange outcome or management batch anomaly exists;
- the already closed Feiyang `posId` remains absent;
- the next naturally arriving image-only exact full exit, when one occurs,
  projects one management instruction and does not record
  `mimo_authoritative_not_safely_applied`.

Do not create a real position, Telegram exit message, or exchange mutation as a
deployment test.

**Step 6: Record rollout evidence**

Update the relevant handoff/status documentation with the local test result,
reviewed commit SHA, pushed SHA, server SHA, service state, focused server test
result, and any verification that still depends on a natural message.
