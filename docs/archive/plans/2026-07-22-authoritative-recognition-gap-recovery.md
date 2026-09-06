# Authoritative Recognition Gap Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure a persisted Telegram message that lacks an authoritative recognition decision is recovered on the next reconcile pass and traverses the existing, idempotent recognition/execution pipeline once.

**Architecture:** Extend `run_reconcile_once` with a bounded recovery pass before new-history ingestion. It will query only configured-chat `RawMessage` rows with no `RecognitionDecision`, process them chronologically via the existing `authoritative_processor`, and reuse the existing notification/summary helpers. No new execution path or checkpoint semantics are introduced.

**Tech Stack:** Python 3.12, SQLAlchemy, asyncio, pytest.

---

### Task 1: Lock the recovery contract with tests

**Files:**
- Modify: `tests/test_reconcile_live_history.py`
- Reference: `src/telegram_kol_research/telegram_live_listener.py:run_reconcile_once`

**Step 1: Write the failing test**

Add a test that persists a raw message for chat `9001`, persists a history
checkpoint at that same message ID, calls `run_reconcile_once` with an
authoritative processor, and supplies no fresh Telegram messages. The test
processor must add a `RecognitionDecision` for the raw ID, then assert the
processor was called once.

**Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_reconcile_live_history.py -k recovers_persisted_message_without_authoritative_decision -v`

Expected: FAIL because the existing reconcile path does not invoke the
processor for an already-persisted message.

**Step 3: Write the idempotency test**

Call reconciliation a second time after the first invocation. Assert the
processor call list is unchanged, proving a newly persisted decision removes
the message from the recovery scan.

**Step 4: Run the targeted tests**

Run: `python -m pytest tests/test_reconcile_live_history.py -k "recovers_persisted_message_without_authoritative_decision or processes_each_new_message_authoritatively_exactly_once" -v`

Expected: the new recovery test fails and the existing fresh-message test
passes.

### Task 2: Add the bounded recovery scan

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py:run_reconcile_once`
- Test: `tests/test_reconcile_live_history.py`

**Step 1: Implement the minimal query**

When `authoritative_processor` is present, derive the configured chat IDs from
the matched dialogs and query `RawMessage` rows whose chat ID is in that set,
whose Telegram `posted_at` time is within the 15-minute recovery window, and
which have no `RecognitionDecision`. Order by `posted_at`, `message_id`, and
`id`; cap the scan to the reconcile message limit. Handle older or timestamp-
less gaps separately: persist a terminal `recovery_guard` failure and operator
notification, never an automatic execution. Send this recovery-guard
notification directly rather than through low-value suppression, and do not
schedule an automatic execution retry.

**Step 2: Process through the existing boundary**

For each selected message, call `authoritative_processor` using
`asyncio.to_thread`, then reuse `_build_authoritative_notification_payload`,
`_handle_authoritative_failure_notification`, and
`_deliver_authoritative_instruction_summary`. Do not call an exchange client,
write a candidate, or update a checkpoint directly in the recovery code.

Pass the existing Web Telegram operation lock into the live listener as well as
the periodic reconcile runner, so the selection and processing window cannot
race a live message handler. Log and continue when one recovered message's
processor raises so another eligible gap is not starved.

**Step 3: Run the focused tests**

Run: `python -m pytest tests/test_reconcile_live_history.py -k "recovers_persisted_message_without_authoritative_decision or processes_each_new_message_authoritatively_exactly_once" -v`

Expected: PASS.

**Step 4: Commit**

```bash
git add src/telegram_kol_research/telegram_live_listener.py tests/test_reconcile_live_history.py docs/plans/2026-07-22-authoritative-recognition-gap-recovery-design.md docs/plans/2026-07-22-authoritative-recognition-gap-recovery.md
git commit -m "fix: recover missing authoritative recognition decisions"
```

### Task 3: Verify and deploy

**Files:**
- Verify: `tests/test_telegram_live_listener.py`
- Verify: `tests/test_authoritative_recognition.py`

**Step 1: Run focused regression suites**

Run:

```bash
python -m pytest tests/test_reconcile_live_history.py tests/test_telegram_live_listener.py tests/test_authoritative_recognition.py -q
```

Expected: PASS.

**Step 2: Push and update production**

Push the reviewed commit to `codex/deepcoin-auto-trading-v1`, then run:

```bash
./scripts/server_git_update.sh
```

**Step 3: Verify production read-only**

Confirm the deployed SHA, `telegram-kol.service=active`, and that no recent
configured-chat `raw_messages` lack a `recognition_decisions` row. Confirm the
current live holdings independently; do not submit a test trade.
