# Management Symbol Guard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent a narrative mention of BTC from blocking an explicitly targeted ETH partial take-profit instruction.

**Architecture:** `_apply_lifecycle_event_decision` currently derives an `explicit_symbol` from the complete message and rejects a target with another symbol. Narrow that check so an authoritative `target_lifecycle_id` with a matching model symbol is accepted. The existing guard remains for inferred targets and contradictory model output.

**Tech Stack:** Python 3.12, SQLAlchemy, pytest.

---

### Task 1: Reproduce the mixed-symbol management message

**Files:**

- Modify: `tests/test_message_recognition.py`
- Test: `tests/test_message_recognition.py`

**Step 1: Write the failing test**

Create an entered ETH-short lifecycle and apply an authoritative `position_update` with `target_lifecycle_id`, `symbol="ETH"`, and `management_fraction=0.5`. Use the production message shape that mentions BTC before saying the ETH 1940 position should be half closed. Assert a management `SignalCandidate` targets ETH and one management `MessageInstructionItem` is created.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_message_recognition.py -k mixed_symbol -v`

Expected: FAIL because the current full-text symbol extraction returns BTC and rejects the ETH target.

### Task 2: Narrow the text-symbol safety guard

**Files:**

- Modify: `src/telegram_kol_research/message_recognition.py:1058-1067`
- Test: `tests/test_message_recognition.py`

**Step 1: Write minimal implementation**

Retain text-symbol rejection when the target was inferred. For an explicit target ID, accept only when the decision symbol is absent or equals the resolved lifecycle symbol; otherwise reject. This lets the explicit immutable target override an unrelated narrative symbol, without accepting a model/lifecycle mismatch.

**Step 2: Run the focused regression test**

Run: `uv run pytest tests/test_message_recognition.py -k mixed_symbol -v`

Expected: PASS.

### Task 3: Verify the recognition suite

**Files:**

- Test: `tests/test_message_recognition.py`

**Step 1: Run focused module tests**

Run: `uv run pytest tests/test_message_recognition.py -v`

Expected: PASS.

**Step 2: Review the diff**

Run: `git diff --check && git diff -- src/telegram_kol_research/message_recognition.py tests/test_message_recognition.py`

Expected: only the regression test and minimal guard adjustment.
