# Multi-Instruction Recognition Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve and safely execute every explicit cancellation, position-management, revision, and new-entry instruction that coexists in one Telegram message.

**Architecture:** Introduce a validated normalized instruction contract at the authoritative-recognition boundary, while retaining legacy `strategy`/`lifecycle_event` compatibility. Resolve targets only for management instructions, project each accepted instruction to its own existing `SignalCandidate` and `MessageInstructionItem`, and reuse the existing ordered independent executor. Add a separate exact-revision confidence policy and strengthen Deepcoin cancel readback without weakening ownership proof.

**Tech Stack:** Python 3.12+, SQLAlchemy, SQLite, MiMo/DeepSeek prompt registry, Deepcoin REST client, pytest via `uv run pytest`.

---

## Global constraints

- Keep first-pass MiMo recognition authoritative; contextual resolution may target an instruction but must not erase another instruction.
- Never infer an old strategy owner from symbol, side, price, text proximity, or time.
- Management items execute before entry items; every item has an independent terminal result and idempotency key.
- An exchange-unknown write is never retried automatically.
- New entry items retain all existing risk, confidence, capability, concurrency, and protection gates.
- Historical replay is side-effect-free. Production activation applies only to messages newer than the activation watermark.
- Preserve unrelated local and server worktree files.

### Task 1: Define and validate the normalized instruction contract

**Files:**
- Create: `src/telegram_kol_research/authoritative_instructions.py`
- Modify: `src/telegram_kol_research/prompt_defaults.py`
- Modify: `src/telegram_kol_research/ai_recognition_config.py`
- Test: `tests/test_authoritative_instructions.py`
- Test: `tests/test_ai_recognition_config.py`
- Test: `tests/test_prompt_defaults.py`

**Step 1: Write failing contract tests**

Cover:

```python
def test_normalizes_cancel_and_entry_from_one_payload():
    payload = {
        "instructions": [
            {"kind": "cancel_pending_entry", "confidence": 0.95},
            {
                "kind": "entry",
                "confidence": 0.95,
                "strategy": {
                    "symbol": "BTC",
                    "side": "long",
                    "entry": "64700-63800",
                    "stop_loss": "63400",
                    "take_profit": "65400-66100-66800",
                },
            },
        ]
    }
    assert [row.kind for row in normalize_authoritative_instructions(payload)] == [
        "cancel_pending_entry",
        "entry",
    ]
```

Also require stable ordering, bounded count, supported kinds, per-item confidence, complete entry fields, canonical management actions, and rejection of duplicate/conflicting items.

Add legacy tests proving a simultaneous complete `strategy` and executable `lifecycle_event` become two instructions instead of one overwriting the other.

**Step 2: Verify RED**

Run:

```bash
uv run pytest -q tests/test_authoritative_instructions.py tests/test_ai_recognition_config.py tests/test_prompt_defaults.py
```

Expected: import and prompt-contract assertions fail because `instructions` is not supported.

**Step 3: Implement the minimal contract**

Add immutable normalized instruction values containing `kind`, `confidence`, `reason`, optional `strategy`, and optional unresolved target metadata. The legacy adapter must only translate structured legacy fields; it must not parse free-form text into orders.

Update the shared prompt JSON schema and rules to state explicitly that cancellation/management and entry can coexist and must be returned as separate list items. Retain legacy top-level fields during rollout.

**Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/test_authoritative_instructions.py tests/test_ai_recognition_config.py tests/test_prompt_defaults.py
git add src/telegram_kol_research/authoritative_instructions.py src/telegram_kol_research/prompt_defaults.py src/telegram_kol_research/ai_recognition_config.py tests/test_authoritative_instructions.py tests/test_ai_recognition_config.py tests/test_prompt_defaults.py
git commit -m "feat: normalize authoritative message instructions"
```

### Task 2: Resolve management targets without erasing entry instructions

**Files:**
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `src/telegram_kol_research/context_resolution.py`
- Modify: `src/telegram_kol_research/context_resolution_prompt.py`
- Test: `tests/test_authoritative_recognition.py`
- Test: `tests/test_context_resolution.py`

**Step 1: Write failing 大镖客 `#4206` regression tests**

Construct one exact pending BTC-short thread and a message payload containing:

- `cancel_pending_entry` targeting the prior pending strategy;
- a complete independent BTC-long entry.

Assert contextual resolution adds the exact lifecycle/thread only to the cancellation instruction and leaves the entry instruction unchanged:

```python
assert resolved.instructions[0].target_lifecycle_id == old_lifecycle.id
assert resolved.instructions[1].strategy.side == "long"
assert resolved.instructions[1].strategy.entry == "64700-63800"
```

Add a condition-only `#4212` case proving “64500不破就走，反手再多” does not synthesize an immediate long entry without complete parameters and a satisfied condition.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_authoritative_recognition.py -k 'multi_instruction or dabiaoke_4206 or dabiaoke_4212' tests/test_context_resolution.py
```

Expected: current `_resolved_mimo_result` replaces `strategy` with `{}` for a management decision.

**Step 3: Implement instruction-scoped resolution**

Run candidate generation and context targeting per management/revision instruction. Preserve unrelated entry instructions byte-for-byte after validation. Retain the old single-action output projection only as a compatibility view; do not let it control instruction persistence.

**Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/test_authoritative_recognition.py -k 'multi_instruction or context or dabiaoke' tests/test_context_resolution.py
git add src/telegram_kol_research/authoritative_recognition.py src/telegram_kol_research/context_resolution.py src/telegram_kol_research/context_resolution_prompt.py tests/test_authoritative_recognition.py tests/test_context_resolution.py
git commit -m "fix: resolve management without dropping entries"
```

### Task 3: Project every accepted instruction to durable candidates and items

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Test: `tests/test_message_recognition.py`
- Test: `tests/test_message_instruction_items.py`

**Step 1: Write failing projection tests**

For `#4206`, assert one message produces:

```python
assert [(row.event_type, row.management_action) for row in candidates] == [
    ("close_signal", "cancel_pending_entry"),
    ("entry_signal", None),
]
assert [(row.sequence, row.instruction_kind) for row in items] == [
    (0, "management"),
    (1, "entry"),
]
```

Also test partial take profit plus a new entry, duplicate processing, candidate retirement after an edited message, and stable idempotency keys.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_message_recognition.py -k multi_instruction tests/test_message_instruction_items.py -k multi_instruction
```

Expected: only the message-level winner is projected.

**Step 3: Implement per-instruction projection**

Use role-specific candidate upserts keyed by raw message plus instruction identity. Canonically map cancellation to `close_signal` + `cancel_pending_entry`; keep replacement as `strategy_revision` + `replace_entry`; create a separate `entry_signal` for every validated independent entry. Pass the complete accepted candidate set to `_project_authoritative_instruction_items`.

**Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/test_message_recognition.py -k 'multi_instruction or authoritative' tests/test_message_instruction_items.py
git add src/telegram_kol_research/message_recognition.py src/telegram_kol_research/message_instruction_items.py tests/test_message_recognition.py tests/test_message_instruction_items.py
git commit -m "feat: persist all message instructions"
```

### Task 4: Enforce independent execution and correct revision confidence

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/models.py`
- Modify: `src/telegram_kol_research/db.py`
- Test: `tests/test_auto_trade_execution.py`
- Test: `tests/test_db_bootstrap.py`
- Test: `tests/test_trading_settings.py`

**Step 1: Write failing execution tests**

Cover:

- management is submitted before entry;
- definite management failure does not erase the entry result;
- unknown management submission is not retried and produces a separate entry evaluation;
- entry remains independently blocked by its own risk gates;
- `#4210` exact replacement with context confidence `0.72` passes a dedicated revision threshold when replacement fields and ownership are complete;
- a `0.72` ordinary new entry remains below the configured entry threshold.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_auto_trade_execution.py -k 'multi_instruction or revision_confidence or dabiaoke_4210' tests/test_trading_settings.py tests/test_db_bootstrap.py
```

Expected: revision still uses `min_ai_confidence` and the message-level result can hide item-specific outcomes.

**Step 3: Implement the minimal policy**

Add a separately named revision-target threshold defaulting to `0.70`. Require exact lifecycle/binding ownership and complete replacement entry/SL/TP before using it. Preserve the existing new-entry threshold. Return every ordered item result, including skipped/blocked states, and never convert “one item succeeded” into message-wide success.

**Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/test_auto_trade_execution.py tests/test_trading_settings.py tests/test_db_bootstrap.py
git add src/telegram_kol_research/auto_trade_execution.py src/telegram_kol_research/trading_settings.py src/telegram_kol_research/models.py src/telegram_kol_research/db.py tests/test_auto_trade_execution.py tests/test_trading_settings.py tests/test_db_bootstrap.py
git commit -m "fix: apply instruction-specific execution gates"
```

### Task 5: Repair exact Deepcoin cancellation confirmation

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Test: `tests/test_deepcoin_execution_actions.py`

**Step 1: Write failing readback tests**

Reproduce the production shape: both exact cancel calls succeed, the orders disappear from pending results, history contains the exact order IDs and economics but omits `state`, and no fill or position evidence exists. Assert cancellation succeeds and both legs become terminal.

Add refusal tests for missing successful cancel response, mismatched economics, fill evidence, a live exact position, and an unknown cancel response.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_deepcoin_execution_actions.py -k 'cancel_history_without_state or pending_entry_cancel'
```

Expected: success case raises `pending_entry_cancel_not_terminally_confirmed`.

**Step 3: Implement strict combined proof**

Allow a history row without state to prove cancellation only when all of the following hold: the same invocation recorded a definite successful cancel response, the exact order is absent from pending, exact history identity and economics match, and both fill and position evidence are absent. Keep every ambiguity fail-closed.

**Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/test_deepcoin_execution_actions.py -k 'cancel or revision'
git add src/telegram_kol_research/deepcoin_execution_actions.py tests/test_deepcoin_execution_actions.py
git commit -m "fix: confirm exact trigger-entry cancellations"
```

### Task 6: Add coverage contracts, notifications, and rollout mode

**Files:**
- Modify: `src/telegram_kol_research/message_operation_contracts.py`
- Modify: `src/telegram_kol_research/message_operation_supervisor.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`
- Modify: `src/telegram_kol_research/trading_settings.py`
- Test: `tests/test_message_operation_projection.py`
- Test: `tests/test_message_operation_supervisor.py`
- Test: `tests/test_system_operator_bot.py`

**Step 1: Write failing coverage and notification tests**

Require declared instruction count to equal candidate/item count, and classify missing entry projection, unevaluated sibling items, or a hidden failed item as severe violations. Require a bounded Telegram summary showing every instruction in sequence.

Add `disabled | shadow | live` multi-instruction mode tests. Shadow must persist comparison evidence without creating executable candidates/items.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_message_operation_projection.py tests/test_message_operation_supervisor.py tests/test_system_operator_bot.py -k multi_instruction
```

**Step 3: Implement coverage and rollout controls**

Use a future-only activation watermark. `disabled` keeps the current single-action path, `shadow` records normalized instruction projections and differences without execution, and `live` creates durable candidates/items. Notifications must sanitize exchange payloads and state every item outcome.

**Step 4: Verify GREEN and commit**

```bash
uv run pytest -q tests/test_message_operation_projection.py tests/test_message_operation_supervisor.py tests/test_system_operator_bot.py
git add src/telegram_kol_research/message_operation_contracts.py src/telegram_kol_research/message_operation_supervisor.py src/telegram_kol_research/system_operator_bot.py src/telegram_kol_research/trading_settings.py tests/test_message_operation_projection.py tests/test_message_operation_supervisor.py tests/test_system_operator_bot.py
git commit -m "feat: monitor multi-instruction completeness"
```

### Task 7: Full verification, review, and production rollout

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`
- Create or modify: focused rollout evidence under `docs/plans/`

**Step 1: Run full local verification**

```bash
uv run pytest -q tests/test_authoritative_instructions.py tests/test_authoritative_recognition.py tests/test_context_resolution.py tests/test_message_recognition.py tests/test_message_instruction_items.py tests/test_auto_trade_execution.py tests/test_deepcoin_execution_actions.py tests/test_message_operation_projection.py tests/test_message_operation_supervisor.py tests/test_system_operator_bot.py tests/test_trading_settings.py tests/test_db_bootstrap.py
python3 -m compileall -q src
git diff --check
```

**Step 2: Run side-effect-free historical replay**

Replay sanitized `#4206`, `#4210`, and `#4212` fixtures against a temporary database. Assert zero Deepcoin writes and expected normalized instructions/candidates/items.

**Step 3: Request independent code review**

Review the complete diff from the design commit through HEAD. Resolve every Critical and Important finding and rerun focused tests.

**Step 4: Update operational documentation and commit**

Document modes, watermark, rollback, monitoring, and the prohibition on historical live replay.

```bash
git add docs/runbook.md docs/migration-handoff.md docs/plans/
git commit -m "docs: add multi-instruction rollout runbook"
```

**Step 5: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

**Step 6: Deploy only in a proven safe window**

Use the documented server helper after two stable read-only snapshots and zero active recognition, context, management, revision, recovery, or exchange-write work:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Run focused server tests, verify services and HTTP, and confirm the exchange snapshot is unchanged by deployment.

**Step 7: Activate shadow for future messages only**

Set the watermark to the latest terminal raw message. Observe at least one natural future message or run a temporary-database canary. Confirm zero live writes and exact declared/projected instruction counts.

**Step 8: Activate live and verify rollback**

After explicit review of shadow evidence, switch to `live` in a safe window. Verify no historical claims, no duplicate candidates/items, and no activation-time exchange mutation. Test rollback by returning the mode to `disabled` without deleting audit rows.
