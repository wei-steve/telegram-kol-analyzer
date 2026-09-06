# Expired Recognition-Gap Alert Noise Reduction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop Telegram operator-alert floods from historical messages whose authoritative-recognition recovery window has already expired, while retaining their durable audit trail and the fail-closed no-trade behavior.

**Architecture:** Keep the existing 15-minute recovery boundary and `recovery_guard` terminal decision unchanged. When reconciliation finds an older raw message with no decision, it will save the same `authoritative_gap_recovery_expired` decision and recognition result, but mark its notification as a deliberate suppression instead of scheduling `send_ai_recognition_conflict_review`. Real-time MiMo failures and recent recoverable gaps retain their current notification paths.

**Tech Stack:** Python 3.11, asyncio, SQLAlchemy, SQLite, pytest, Telegram Bot API adapter.

---

## Scope and safety contract

- Do not retry, recognize, create candidates for, or execute a message older than `AUTHORITATIVE_GAP_RECOVERY_MAX_AGE`.
- Preserve the visible `RecognitionDecision` and `MessageRecognition` entries for every expired gap; the Web audit view must continue to show `authoritative_gap_recovery_expired`.
- Do not change `_handle_authoritative_failure_notification`, live-ingestion notifications, recent-gap recovery, or semantic-disagreement notifications.
- Use the explicit audit state `suppressed_expired_recovery`, rather than `sent`, `failed`, or a misleading generic suppression state.
- This change prevents new spam. It does not delete past Telegram messages and does not mutate existing production decisions.

### Task 1: Specify the expired-gap notification contract with failing tests

**Files:**

- Modify: `tests/test_reconcile_live_history.py:292-334`
- Modify: `tests/test_reconcile_live_history.py` (add a second-pass regression test adjacent to the expired-gap test)

**Step 1: Replace the current outbound-alert expectation with a no-alert expectation**

Rename `test_reconcile_notifies_operator_for_expired_external_market_gap` to `test_reconcile_suppresses_operator_notification_for_expired_gap`. Keep the stale `RawMessage`, configured operator bot, and fake sender, then assert the sender received nothing and the row contains the terminal recovery decision:

```python
assert sent == []
with session_factory() as session:
    decision = session.query(RecognitionDecision).one()
assert decision.authoritative_model == "recovery_guard"
assert decision.automation_status == "skipped"
assert decision.automation_reason == "authoritative_gap_recovery_expired"
assert decision.notification_status == "suppressed_expired_recovery"
```

**Step 2: Add an idempotency regression test**

Run `run_reconcile_once()` twice against the same stale, undecided message with a configured operator bot. Assert `sent == []`, exactly one `RecognitionDecision` exists, and its notification state remains `suppressed_expired_recovery`. This protects against future changes that accidentally reintroduce a notification during periodic passes.

**Step 3: Run the focused tests and confirm they fail before implementation**

Run:

```bash
uv run pytest tests/test_reconcile_live_history.py -k 'expired_gap' -v
```

Expected: the modified no-alert assertion fails because the current expired-gap branch still calls `_schedule_authoritative_notification()` and records `notification_status="sent"`.

**Step 4: Commit the test-only red state**

```bash
git add tests/test_reconcile_live_history.py
git commit -m "test: specify expired recognition gap alert suppression"
```

### Task 2: Persist an explicit suppressed-notification audit outcome

**Files:**

- Modify: `src/telegram_kol_research/telegram_live_listener.py:445-499`
- Test: `tests/test_reconcile_live_history.py:292-334`

**Step 1: Add a narrow helper for the terminal expired-gap outcome**

Extend `_record_expired_authoritative_recovery_gap()` so its final `update_recognition_execution_outcome()` call writes the explicit notification state:

```python
update_recognition_execution_outcome(
    session_factory,
    raw_message_id=raw_message.id,
    automation_status="skipped",
    automation_reason=reason,
    notification_status="suppressed_expired_recovery",
    notification_error=None,
)
```

Do not add a database migration: `RecognitionDecision.notification_status` is already a nullable string field and accepts this new audit value.

**Step 2: Remove the outbound scheduling branch for expired gaps**

In the `for raw_message in expired_messages` loop in `run_reconcile_once()`, retain the `_record_expired_authoritative_recovery_gap()` call and `expired_recovery_messages += 1`, but remove the `system_operator_bot_enabled()` condition and `_schedule_authoritative_notification()` call. `_build_expired_authoritative_recovery_payload()` becomes unused; remove it rather than leave a dead message template.

**Step 3: Run the focused tests and confirm they pass**

Run:

```bash
uv run pytest tests/test_reconcile_live_history.py -k 'expired_gap' -v
```

Expected: PASS. The fake sender records no calls; one durable no-trade audit row has `suppressed_expired_recovery`.

**Step 4: Commit the implementation**

```bash
git add src/telegram_kol_research/telegram_live_listener.py tests/test_reconcile_live_history.py
git commit -m "fix: suppress stale recognition recovery alerts"
```

### Task 3: Prove adjacent notification behavior is unchanged

**Files:**

- Test: `tests/test_telegram_live_listener.py:563-634`
- Test: `tests/test_reconcile_live_history.py:220-290`

**Step 1: Run the real-time authoritative-failure regression**

Run:

```bash
uv run pytest tests/test_telegram_live_listener.py::test_authoritative_mimo_failure_still_alerts_position_management_text -v
```

Expected: PASS; an immediate high-risk MiMo failure still sends one operator alert and retains `scheduled` then `sent` audit progression.

**Step 2: Run the recent-gap and stale-gap recovery regression set**

Run:

```bash
uv run pytest tests/test_reconcile_live_history.py -k 'recover or expired' -v
```

Expected: PASS; recent gaps still invoke the authoritative processor, while stale gaps still create `recovery_guard` decisions without an outbound alert.

**Step 3: Run the related suite**

Run:

```bash
uv run pytest tests/test_reconcile_live_history.py tests/test_telegram_live_listener.py tests/test_recognition_decisions.py -q
```

Expected: PASS.

**Step 4: Commit the verified change if Task 2 required test refinements**

```bash
git add src/telegram_kol_research/telegram_live_listener.py tests/test_reconcile_live_history.py
git commit -m "test: cover stale recovery notification boundary"
```

### Task 4: Deploy and verify the production persistence hypothesis

**Files:**

- No code changes required.
- Reference: `AGENTS.md`, `scripts/server_git_update.ps1`, `docs/runbook.md`

**Step 1: Review the branch and run local checks**

Run:

```bash
git diff origin/codex/deepcoin-auto-trading-v1...HEAD --check
uv run pytest tests/test_reconcile_live_history.py tests/test_telegram_live_listener.py tests/test_recognition_decisions.py -q
```

Expected: no whitespace errors and a passing suite.

**Step 2: Push the reviewed commits to the required production branch**

Run:

```bash
git push origin HEAD:codex/deepcoin-auto-trading-v1
```

Expected: the remote branch advances successfully.

**Step 3: Update the server through the existing helper**

Run from the project workspace in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the same Git SHA, reinstalls the editable package, and restarts `telegram-kol.service`.

**Step 4: Verify server identity and durable state**

On the server, inspect the active service status/logs and the configured database path. Confirm exactly one active `telegram-kol.service` process, then query the production `recognition_decisions` row for chat `-1003344714145`, message `399` (joining `raw_messages`) and verify it has a persistent terminal decision. Its desired values after deployment are:

```text
authoritative_model = recovery_guard
automation_status = skipped
automation_reason = authoritative_gap_recovery_expired
notification_status = suppressed_expired_recovery
```

If the row is absent after it was previously recorded, stop deployment investigation and resolve the database-path/volume or duplicate-service problem before treating the software fix as complete.

**Step 5: Observe one normal reconciliation interval**

Confirm no new `【AI识别分歧告警】` is emitted for expired historical gaps. Do not use a live trade signal as a test; confirmation is via service logs and database audit rows only.

**Step 6: Commit any deployment-only documentation update separately, if needed**

```bash
git add docs/runbook.md
git commit -m "docs: record stale recovery alert verification"
```
