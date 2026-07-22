# Prudent Exit and Multi-target Compatibility Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Execute a full exit for a uniquely attributable KOL strategy when it says “求稳可走”, while accepting an empty multi-target list only when a valid single lifecycle target is present.

**Architecture:** Keep the existing exact-leg full-exit path unchanged. Clarify the lifecycle model contract, then normalize the single-target compatibility case before strict multi-target validation. Ambiguous targetless and malformed multi-target payloads remain fail-closed.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest, MiMo lifecycle-recognition prompts.

---

### Task 1: Lock the lifecycle-target contract with tests

**Files:**
- Modify: `tests/test_message_recognition.py`
- Modify: `tests/test_authoritative_recognition.py`

**Step 1: Write a failing single-target regression test**

Add an `exit_position` payload with one valid `target_lifecycle_id` and `targets: []`. Use a “求稳可走” raw message and an entered lifecycle with a live execution binding. Assert persistence of one `close_signal` candidate, `management_action == "full_exit"`, and one management instruction item.

**Step 2: Run it to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_message_recognition.py -k empty_targets -v`

Expected: FAIL because an empty `targets` list currently aborts lifecycle application.

**Step 3: Write a targetless fail-closed test**

Use `targets: []` without a valid scalar `target_lifecycle_id`. Assert no candidate or instruction is created and recognition is `识别失败`.

**Step 4: Commit the test change**

Run: `git add tests/test_message_recognition.py tests/test_authoritative_recognition.py && git commit -m "test: cover prudent exit target compatibility"`

### Task 2: Normalize only safe single-target empty lists

**Files:**
- Modify: `src/telegram_kol_research/message_recognition.py:1585-1617`
- Test: `tests/test_message_recognition.py`

**Step 1: Implement the minimal normalizer change**

In `_expand_lifecycle_event_targets`, keep the absent-list and non-empty-list paths. Add a branch that returns `[decision]` only when `raw_targets == []` and `_int_or_none(decision.get("target_lifecycle_id"))` is valid. Keep targetless empty lists returning `None`; never infer from symbol, side, or all active strategies.

**Step 2: Run focused checks**

Run: `./.venv/bin/python -m pytest tests/test_message_recognition.py -k "empty_targets or multi_target or exits_entered_position" -v`

Expected: PASS.

**Step 3: Verify the authoritative path**

Run: `./.venv/bin/python -m pytest tests/test_authoritative_recognition.py -k "exit or cancel" -v`

Expected: PASS; recognition produces a full-exit candidate but does not itself submit an exchange order.

**Step 4: Commit the implementation**

Run: `git add src/telegram_kol_research/message_recognition.py tests/test_message_recognition.py && git commit -m "fix: accept single lifecycle target with empty targets"`

### Task 3: Clarify the MiMo lifecycle contract and prudent-exit rule

**Files:**
- Modify: `src/telegram_kol_research/ai_recognition_config.py:89-121`
- Modify: `src/telegram_kol_research/prompt_defaults.py:35-90`
- Test: `tests/test_ai_recognition_config.py`

**Step 1: Write prompt-contract assertions**

Assert that “求稳可走” is a full exit only for a uniquely attributable entered strategy, and that a single target omits `targets` while an explicit multi-strategy action requires a non-empty list.

**Step 2: Update both prompt sources**

Remove the default `"targets": []` example from the single-target schema. State that “求稳可走/稳健者可走” is `exit_position` only with unique attribution; ambiguous cases return `none` or low confidence.

**Step 3: Run prompt and recognition checks**

Run: `./.venv/bin/python -m pytest tests/test_ai_recognition_config.py tests/test_message_recognition.py -v`

Expected: PASS.

**Step 4: Commit the prompt change**

Run: `git add src/telegram_kol_research/ai_recognition_config.py src/telegram_kol_research/prompt_defaults.py tests/test_ai_recognition_config.py && git commit -m "feat: treat prudent KOL exits as full exits"`

### Task 4: Review and verify before production rollout

**Files:**
- Verify: `src/telegram_kol_research/strategy_management_executor.py`
- Verify: `src/telegram_kol_research/deepcoin_execution_actions.py`

**Step 1: Review the exact-leg boundary**

Confirm a `full_exit` candidate creates a batch only for the resolved strategy and closes all of its exact verified live legs; do not add symbol-wide closing.

**Step 2: Run final local checks**

Run: `./.venv/bin/python -m pytest tests/test_message_recognition.py tests/test_authoritative_recognition.py tests/test_ai_recognition_config.py tests/test_strategy_management_planner.py tests/test_deepcoin_execution_actions.py -v && git diff --check`

Expected: PASS with no whitespace errors.

**Step 3: Reviewed deployment and server verification**

Push `codex/deepcoin-auto-trading-v1`, deploy with `scripts/server_git_update.ps1`, then confirm the server SHA and active `telegram-kol.service`. Do not replay 4068 or submit a test close; verify the next natural matching message through its recognition decision, management batch, exact-leg records, and exchange reconciliation.

### Task 5: Publish the production trading-prompt version

**Files:**
- Verify/update: production `trading.analysis.shared` prompt registry version

**Step 1: Preserve the current active prompt content**

Load the active shared trading prompt from the production prompt registry. Apply only the approved lifecycle wording changes: add uniquely attributable “求稳可走/稳健者可走” full exits and clarify single-target versus non-empty multi-target output. Preserve all other active content and customizations.

**Step 2: Create and validate a draft**

Create the registry draft with a change note, then run the built-in validation. Run required historical tests for both MiMo and DeepSeek against a real DBK prudent-exit message; inspect that the draft identifies the uniquely bound BTC short as `exit_position`.

**Step 3: Publish only after successful validation and historical tests**

Publish the draft with optimistic active/draft version IDs. Confirm the new active version contains the prudent-exit wording and no longer instructs a single target to emit `targets: []`.

**Step 4: Commit the plan update**

Run: `git add docs/plans/2026-07-22-prudent-exit-multitarget-compatibility.md && git commit -m "docs: add production prompt rollout"`
