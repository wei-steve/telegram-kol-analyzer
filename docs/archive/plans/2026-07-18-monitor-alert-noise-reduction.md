# Production Monitor Alert Noise Reduction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop repeated Telegram pushes for unchanged low-priority production-monitor anomalies while keeping fail-closed health evaluation.

**Architecture:** Reuse the existing monitor result fingerprint and four-field state file. Add a small repeat-policy classifier in `production_safety_monitor.py`; apply it only inside notification decision logic, not snapshot evaluation.

**Tech Stack:** Python, pytest, systemd monitor runbook docs.

---

### Task 1: Add Notification Policy Tests

**Files:**
- Modify: `tests/test_production_safety_monitor.py`

**Step 1: Write failing tests**

Add tests near the existing `decide_monitor_notification()` tests:

```python
def test_same_low_priority_monitor_notification_stays_suppressed_after_six_hours():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    result = MonitorResult(
        healthy=False,
        reason_codes=("audit_abnormal", "head_drift"),
        details={
            "head": OTHER_HEAD,
            "expected_head": REVIEWED_HEAD,
            "audit_abnormal_count": 4,
            "audit_abnormal": True,
        },
    )
    fingerprint = fingerprint_monitor_result(result)
    state = MonitorState(
        anomaly_fingerprint=fingerprint,
        last_notification_at=(now - timedelta(hours=24)).isoformat(),
    )

    decision = decide_monitor_notification(result, state, now=now)

    assert decision.should_notify is False
    assert decision.next_state == state
```

Add a second test proving a changed low-priority fingerprint notifies, and a
run-level test proving `run_production_safety_monitor()` returns
`notification_status="suppressed"` while persisting `last_window_at`.

**Step 2: Run test to verify it fails**

Run:

```bash
./.venv/bin/python -m pytest -q tests/test_production_safety_monitor.py::test_same_low_priority_monitor_notification_stays_suppressed_after_six_hours
```

Expected: fail because unchanged anomalies currently notify again after six hours.

### Task 2: Implement Repeat Policy

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py`

**Step 1: Add constants and helper**

Add:

```python
_LOW_REPEAT_REASON_CODES = frozenset({"head_drift", "audit_abnormal"})


def _uses_low_repeat_policy(result: MonitorResult) -> bool:
    reason_codes = set(code for code in result.reason_codes if code in _FIXED_REASON_CODES)
    return bool(reason_codes) and reason_codes <= _LOW_REPEAT_REASON_CODES
```

**Step 2: Use helper in `decide_monitor_notification()`**

When fingerprint is unchanged and `last_notification_at` exists, return
suppressed immediately for low-repeat results. Otherwise retain the six-hour
policy.

**Step 3: Run focused tests**

Run:

```bash
./.venv/bin/python -m pytest -q tests/test_production_safety_monitor.py
```

Expected: all tests pass.

### Task 3: Update Operator Docs

**Files:**
- Modify: `docs/runbook.md`
- Modify: `docs/migration-handoff.md`

**Step 1: Document notification semantics**

Record that unchanged `head_drift` + `audit_abnormal` residue sends once per
fingerprint, then remains log-only until the fingerprint changes. State that the
health result and process exit still fail closed.

**Step 2: Run doc-adjacent focused tests**

Run:

```bash
./.venv/bin/python -m pytest -q tests/test_production_safety_monitor.py tests/test_server_monitor_installation.py
```

Expected: all tests pass.
