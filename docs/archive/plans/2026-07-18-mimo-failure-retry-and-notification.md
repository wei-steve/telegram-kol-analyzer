# MiMo Failure Retry and Notification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce MiMo transport/schema failure misses and suppress low-value non-crypto noise while preserving fail-closed trading safety.

**Architecture:** MiMo remains the only authoritative recognizer. The synchronous authority path retries one transient or schema-invalid MiMo response before writing a terminal failure. Failure notification is then classified: crypto or position-management risk still alerts; clearly external stock-only material is audited as suppressed.

**Tech Stack:** Python, SQLAlchemy, httpx, pytest, existing Telegram system-operator bot.

---

### Task 1: Add One Immediate MiMo Retry

**Files:**
- Modify: `src/telegram_kol_research/recognition_experiments.py`
- Test: `tests/test_recognition_experiments.py`

**Steps:**
1. Add a failing test where `_call_mimo_direct_model` raises `TimeoutError` once and returns a valid authoritative payload on the second call.
2. Run the focused test and verify it fails because only one call is made.
3. Add a small retry helper around MiMo authoritative calls only. Retry once for timeout/transport exceptions and validation/schema errors. Do not retry deterministic local failures such as missing model config, empty message, or unreadable declared image files.
4. Persist only the final result and one prompt invocation, with final status `completed` if retry succeeds.
5. Run the focused recognition tests.

### Task 2: Classify Failed Authority Notifications

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Test: `tests/test_telegram_live_listener.py`

**Steps:**
1. Add a failing live-listener test proving a MiMo failure for `美光MU...850和880分批走` records `notification_status="suppressed_low_value"` and does not send the operator bot message.
2. Add a failing live-listener test proving a MiMo failure for stop-loss/take-profit/exit text still sends the operator alert.
3. Implement a helper that classifies the failed message text. High-risk triggers include crypto symbols/names, active management words, exit words, TP/SL words, and position words. Low-value suppression requires obvious external-stock markers without crypto/risk context.
4. Use that helper in live realtime and reconcile authoritative failure scheduling.
5. Keep the existing `mimo_authoritative_failed` automation reason unchanged.

### Task 3: Apply Same Gate to Manual Recognition

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Test: existing focused web tests or live-listener helper tests where practical.

**Steps:**
1. Reuse the same notification decision helper in the manual `/api/messages/{id}/recognize` endpoint.
2. Suppressed low-value manual failures should be persisted as `notification_status="suppressed_low_value"` and return `notification_scheduled=false`.
3. High-risk failures should keep existing scheduling behavior.

### Task 4: Document Runtime Boundary

**Files:**
- Modify: `docs/migration-handoff.md`

**Steps:**
1. Document that MiMo authority now makes one immediate retry for transient/schema failures.
2. Document that low-value external-stock-only MiMo failures are audited but do not notify.
3. Document that high-risk failed authority still alerts and never falls back to DeepSeek execution.
4. Run focused tests and a broader relevant test set before reporting.
