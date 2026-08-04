# Human-Readable Production Monitor Alerts Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace developer-oriented production-monitor dumps with deterministic Chinese alerts that tell a non-technical operator what failed, the known impact, and the required action without weakening monitoring safety.

**Architecture:** Preserve the existing read-only collection and safety-evaluation path. Extend the management audit with a bounded actionable-batch projection, translate validated `MonitorResult` data into a pure `MonitorAlertPresentation`, and make dedupe depend only on operator-relevant facts. Add backward-compatible notification state so recovery is announced only after the same safety source has been fully rechecked.

**Tech Stack:** Python 3.12, Typer, dataclasses, SQLite read-only snapshots, systemd oneshot/timer, pytest.

---

### Task 1: Project actionable management batches for operator messages

**Files:**
- Modify: `src/telegram_kol_research/cli.py:1064-1215`
- Test: `tests/test_cli_smoke.py:560-980`

**Step 1: Write failing audit-projection tests**

Add focused fixtures with terminal history, an actionable batch status, an
actionable leg status, and more than ten actionable batch IDs. Assert the new
closed shape:

```python
assert payload["actionable_batches"] == {
    "total": 2,
    "returned": 2,
    "truncated": False,
    "items": [
        {"batch_ref": "batch:17", "states": ["recovery_required"]},
        {"batch_ref": "batch:22", "states": ["recovery_required"]},
    ],
}
```

Also prove:

```python
assert payload["actionable_batches"]["returned"] == 10
assert payload["actionable_batches"]["truncated"] is True
assert payload["counts"]["terminal_blocked"] == 1
assert all(
    item["batch_ref"] != terminal_ref
    for item in payload["actionable_batches"]["items"]
)
```

Cover a batch whose batch status is non-actionable but one leg is
`submit_unknown`, and verify its effective state list includes
`submit_unknown`. Verify stable ascending batch-ID ordering and no raw
`reason_code`, `last_error`, request, response, symbol, or position value in the
new projection.

**Step 2: Run the new tests and verify RED**

Run:

```bash
uv run pytest tests/test_cli_smoke.py -k "actionable_batches" -q
```

Expected: FAIL because `actionable_batches` is absent.

**Step 3: Add the bounded read-only projection**

In `_audit_management_snapshot`, initialize the shape even when the schema is
unavailable:

```python
"actionable_batches": {
    "total": 0,
    "returned": 0,
    "truncated": False,
    "items": [],
},
```

Build one shared SQL definition for actionable batch membership so counts and
the projection cannot drift. It must:

- match `blocked`, `partial_failed`, `recovery_required`, or `submit_unknown`
  on the batch or a leg;
- apply the existing informational-noop and terminal-blocked exclusions only
  to `blocked`;
- select distinct batch IDs;
- order by `b.id ASC`;
- read at most eleven IDs so the public list is capped at ten;
- derive a sorted, deduplicated state list from only the four fixed alert
  states.

Render each ID through the existing `_identity_ref("batch", ...)` validator.
Set `total` from the same membership predicate rather than from the length of
the bounded list. Keep `output_complete` fail-closed if any selected reference
or state is malformed.

Add the same empty `actionable_batches` shape to the error payload in
`audit_management_batches` so every result has one schema.

**Step 4: Run the focused CLI audit tests**

Run:

```bash
uv run pytest tests/test_cli_smoke.py -k "management_audit or actionable_batches or terminal_blocked" -q
```

Expected: PASS.

**Step 5: Commit the audit projection**

```bash
git add src/telegram_kol_research/cli.py tests/test_cli_smoke.py
git commit -m "feat: project actionable management audit batches"
```

---

### Task 2: Validate audit details for presentation and fingerprinting

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py:1316-1378`
- Test: `tests/test_production_safety_monitor.py:430-510`

**Step 1: Write failing evaluator tests**

Add a complete audit with two recovery-required references and assert:

```python
assert result.reason_codes == ("audit_abnormal",)
assert result.details["audit_state_counts"] == {
    "blocked": 0,
    "partial_failed": 0,
    "recovery_required": 2,
    "submit_unknown": 0,
}
assert result.details["actionable_batch_refs"] == (
    ("batch:17", ("recovery_required",)),
    ("batch:22", ("recovery_required",)),
)
assert result.details["actionable_batches_total"] == 2
assert result.details["actionable_batches_truncated"] is False
```

Parameterize malformed totals, booleans used as counts, invalid references,
unknown states, duplicate references, more than ten items, mismatched returned
counts, and a false truncation flag. Every malformed case must add
`malformed_snapshot` and/or `audit_incomplete`; none may reduce the existing
abnormal count or severity.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "actionable_batch_refs or audit_state_counts" -q
```

Expected: FAIL because the details are not projected.

**Step 3: Implement strict allowlisted validators**

Add private validators that accept only:

```python
_AUDIT_ALERT_STATES = (
    "blocked",
    "partial_failed",
    "recovery_required",
    "submit_unknown",
)
_MAX_ACTIONABLE_BATCH_REFS = 10
```

Validate the entire `actionable_batches` mapping before copying any item into
`MonitorResult.details`. Store immutable tuples, not the mutable audit payload.
Keep `audit_abnormal_count` for compatibility, but compute it from the validated
four-state counts. Treat missing projections from older deployed audit code as
incomplete rather than inventing batch IDs.

**Step 4: Run evaluator and audit-policy regressions**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "audit" -q
```

Expected: PASS.

**Step 5: Commit the validated projection**

```bash
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git commit -m "feat: validate operator audit details"
```

---

### Task 3: Build deterministic Chinese alert presentations

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py:248-278,1381-1453`
- Test: `tests/test_production_safety_monitor.py:500-650`

**Step 1: Write failing mapping and rendering tests**

Add one parameterized case for every member of `_FIXED_REASON_CODES`. Each case
must assert a fixed severity, Chinese problem sentence, impact sentence, and
operator action. Add exact contract tests for:

```python
text = format_monitor_alert(
    MonitorResult(
        healthy=False,
        reason_codes=("audit_abnormal",),
        details={
            "audit_abnormal_count": 2,
            "audit_state_counts": {
                "blocked": 0,
                "partial_failed": 0,
                "recovery_required": 2,
                "submit_unknown": 0,
            },
            "actionable_batch_refs": (
                ("batch:17", ("recovery_required",)),
                ("batch:22", ("recovery_required",)),
            ),
            "actionable_batches_total": 2,
            "actionable_batches_truncated": False,
        },
    ),
    checked_at=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
)
assert text.startswith("【🟡稍后核查：2条历史交易管理记录无法确认】")
assert "你需要做什么：" in text
assert "不要手动重复平仓" in text
assert "管理批次 17、22" in text
assert "系统定时安全检查，不是 AI Agent" in text
assert "2026-08-04 09:00（北京时间）" in text
```

Also prove:

- `submit_unknown=1` makes an audit alert critical;
- a critical reason wins over review reasons;
- only three problems render and the remainder count is stated;
- a truncated batch list states both the total and display limit;
- unknown injected reason codes use the generic critical fallback;
- Git hashes do not appear in an audit-only message;
- required title, impact, and action survive maximum-length handling;
- token-shaped secrets, newlines, huge integers, and raw mappings never render.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "formatter or presentation or readable_alert" -q
```

Expected: existing fixed-label formatter assertions fail.

**Step 3: Add a pure presentation model and fixed mapping**

Introduce:

```python
@dataclass(frozen=True, slots=True)
class MonitorAlertPresentation:
    severity: str
    title: str
    problems: tuple[str, ...]
    impact: str
    operator_action: str
    technical_codes: tuple[str, ...]
    actionable_batch_ids: tuple[int, ...] = ()
    additional_problem_count: int = 0
    additional_batch_count: int = 0
```

Add `build_monitor_alert_presentation(result)` with a closed mapping for every
fixed reason. Use fixed severity order `critical > review`, fixed reason
priority within a severity, and at most three problem strings. Convert only
validated `batch:<positive-int>` references to display IDs.

Replace the old label dump in `format_monitor_alert` with six fixed sections:
title, `发生了什么`, `当前影响`, `你需要做什么`, `通知来源`, and
`排查信息`. Convert aware datetimes to `Asia/Shanghai`. Invalid string
timestamps render `时间无法确认`; do not echo them.

Implement bounded composition by dropping extra diagnostic references before
truncating prose. If even the fixed core exceeds `MAX_ALERT_LENGTH`, return a
short fixed critical fallback that still contains impact and operator action.

**Step 4: Run formatter and security tests**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "formatter or presentation or readable_alert or sensitive" -q
```

Expected: PASS.

**Step 5: Commit the readable formatter**

```bash
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git commit -m "feat: render readable production monitor alerts"
```

---

### Task 4: Make fingerprints ignore irrelevant deployments

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py:184-210,1053-1128`
- Test: `tests/test_production_safety_monitor.py:760-840,1720-1872`

**Step 1: Write failing fingerprint-policy tests**

Prove two audit-only results with identical actionable facts but different
`head` and `expected_head` values have the same fingerprint:

```python
assert fingerprint_monitor_result(first) == fingerprint_monitor_result(deployed_later)
```

Prove fingerprints differ when any operator-relevant fact changes:

- severity changes because `submit_unknown` appears;
- one actionable batch ID changes;
- one actionable state changes;
- total or truncation changes;
- a relevant service or setting value changes for its matching reason.

Also prove an irrelevant setting detail does not change a pure journal-error
fingerprint.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "fingerprint and (head or relevant or actionable)" -q
```

Expected: the Git-head test fails because heads are globally fingerprinted.

**Step 3: Fingerprint reason-scoped facts**

Replace the global `_FINGERPRINT_DETAIL_KEYS` loop with a closed reason-to-detail
mapping. Build the presentation first and include its severity. Include only
validated details used by active reasons. Audit fingerprints include the four
counts, bounded batch references, total, and truncation flag. Do not include
`head` or `expected_head` because no current fixed reason represents deployment
integrity.

Keep canonical JSON, sorted codes, bounded values, and SHA-256. Unknown reason
codes must canonicalize to the generic critical fallback instead of being
silently discarded.

**Step 4: Run fingerprint and dedupe tests**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "fingerprint or low_priority or notification" -q
```

Expected: PASS, including unchanged low-priority suppression after a deployment.

**Step 5: Commit the fingerprint correction**

```bash
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git commit -m "fix: fingerprint only relevant monitor facts"
```

---

### Task 5: Persist active causes and send only proven recovery notices

**Files:**
- Modify: `src/telegram_kol_research/production_safety_monitor.py:255-274,396-477,571-735,1082-1128,1521-1585`
- Test: `tests/test_production_safety_monitor.py:650-760,840-1185,1560-1872,2062-2130`

**Step 1: Write failing state-compatibility and recovery tests**

Add tests proving:

```python
legacy = MonitorState(
    anomaly_fingerprint="a" * 64,
    last_notification_at="2026-08-04T01:00:00+00:00",
)
assert legacy.active_reason_codes == ()
```

Cover these sequences:

1. A service failure is delivered, the next complete service check is healthy,
   and exactly one blue recovery message is delivered.
2. Recovery delivery fails, so active reasons remain and the next run retries.
3. An audit abnormality is delivered after 09:00; a pre-09:00 run that skips
   audit sends no recovery and preserves the active audit reason.
4. A later complete healthy audit sends one recovery and clears the cause.
5. A legacy state lacking active reasons silently clears rather than claiming a
   recovery it cannot prove.
6. A partial recovery replaces active causes with the remaining/current set and
   sends one readable update.

Assert recovery text starts with `【🔵状态提醒：生产安全监控已恢复正常】`,
states that no action is required, and identifies the system timer rather than
the AI Agent.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "recovery_notice or active_reason or legacy_state" -q
```

Expected: FAIL because state does not retain active causes and healthy runs
never notify.

**Step 3: Extend monitor state backward compatibly**

Add:

```python
@dataclass(frozen=True, slots=True)
class MonitorState:
    last_window_at: str | None = None
    last_full_audit_date: str | None = None
    anomaly_fingerprint: str | None = None
    last_notification_at: str | None = None
    active_reason_codes: tuple[str, ...] = ()
```

The loader must accept the exact old four-field schema and the new five-field
schema. Validate a list of unique fixed reason-code strings, sort it into an
immutable tuple, cap it at the number of fixed reasons, and reject unknown or
duplicate values. The writer always emits the new field. Do not treat a valid
legacy file as `state_invalid`.

**Step 4: Make notification decisions recovery-aware**

Pass an explicit `audit_rechecked_healthy` boolean into the decision layer. On
an abnormal result, save current active reason codes only after successful
delivery. On a healthy result:

- if no active causes are known, send nothing;
- if an active audit cause exists and no full healthy audit ran, preserve the
  active fingerprint and send nothing;
- otherwise return a recovery decision, but clear state only after successful
  delivery.

Give `MonitorNotificationDecision` an explicit kind (`anomaly`, `recovery`, or
`none`) so `run_production_safety_monitor` does not infer it from `healthy`.
Render recovery with a fixed formatter. Preserve the current rule that config
or delivery failure never acknowledges a notification.

**Step 5: Run state, delivery, scheduling, and recovery tests**

```bash
uv run pytest tests/test_production_safety_monitor.py -k "state or notification or recovery or daily_audit" -q
```

Expected: PASS.

**Step 6: Commit recovery state**

```bash
git add src/telegram_kol_research/production_safety_monitor.py tests/test_production_safety_monitor.py
git commit -m "feat: notify only proven monitor recovery"
```

---

### Task 6: Update operator documentation and run full local verification

**Files:**
- Modify: `docs/runbook.md:315-365`
- Modify: `docs/migration-handoff.md:150-185`
- Modify: `tests/test_server_monitor_installation.py`
- Test: `tests/test_production_safety_monitor.py`
- Test: `tests/test_cli_smoke.py`

**Step 1: Write the operator-contract regression**

Add a documentation assertion or focused installation test proving the runbook
contains all three severity labels, states that the monitor is deterministic
and not AI-authored, documents the six-hour critical reminder, and documents
the audit recovery gate.

**Step 2: Run and verify RED**

```bash
uv run pytest tests/test_server_monitor_installation.py -k "readable or notification" -q
```

Expected: FAIL because the readable contract is undocumented.

**Step 3: Update the runbook and handoff**

Document:

- the human-readable message sections and three severities;
- reason-to-action behavior without requiring operators to interpret codes;
- why Git drift is excluded from audit-only display and dedupe;
- the ten-reference audit bound;
- recovery proof requirements;
- the safe generic fallback;
- that the system timer, not the Runtime Incident AI Agent, authors these
  notifications.

Do not paste credentials, live chat IDs, raw production events, or unredacted
database values.

**Step 4: Run focused suites**

```bash
uv run pytest tests/test_production_safety_monitor.py tests/test_cli_smoke.py tests/test_server_monitor_installation.py -q
```

Expected: PASS.

**Step 5: Run broader safety regressions**

```bash
uv run pytest tests/test_runtime_incident_adapters.py tests/test_runtime_incidents.py tests/test_system_operator_bot.py tests/test_web_app.py -q
```

Expected: PASS, with only already-documented environment-specific skips or
warnings.

**Step 6: Run the complete local suite**

```bash
uv run pytest -q
```

Expected: PASS except for a failure already documented before this change. If a
new failure appears, stop and diagnose it before deployment.

**Step 7: Request code review**

Use `@requesting-code-review` to review the full diff against
`docs/plans/2026-08-04-human-readable-production-monitor-alerts-design.md`.
Address every Critical or Important finding and rerun each affected suite.

**Step 8: Commit documentation and final review fixes**

```bash
git add docs/runbook.md docs/migration-handoff.md tests/test_server_monitor_installation.py src/telegram_kol_research/production_safety_monitor.py src/telegram_kol_research/cli.py tests/test_production_safety_monitor.py tests/test_cli_smoke.py
git commit -m "docs: explain readable production monitor alerts"
```

Skip the commit if review produced no uncommitted changes.

---

### Task 7: Push and perform staged production verification

**Files:**
- Verify: `scripts/server_git_update.ps1`
- Verify: `scripts/install_server_monitor.sh`
- Verify: `deploy/systemd/telegram-kol-monitor-diagnostic.service`
- Verify: `deploy/systemd/telegram-kol-monitor.timer`

**Step 1: Confirm the reviewed local scope**

Run:

```bash
git status --short --branch
git log --oneline --decorate -10
git diff --stat origin/codex/deepcoin-auto-trading-v1...HEAD
```

Expected: only reviewed commits for this feature are ahead. Preserve unrelated
existing working-tree files and never stage them.

**Step 2: Push the reviewed branch**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds.

**Step 3: Prove a production safe window**

Use the existing read-only server checks to prove no recognition decision,
context resolution, management submission, position mutation, stop rescue,
runtime claim, or incomplete recovery attempt is in flight. Confirm the latest
raw message has a completed decision and no message arrived during the final
bounded quiet window.

Expected: every in-flight count is zero. If not, do not restart or deploy; keep
the phase in progress and record the exact pending verification.

**Step 4: Disable and verify the monitor timer**

On the server:

```bash
systemctl disable --now telegram-kol-monitor.timer
systemctl is-enabled telegram-kol-monitor.timer
systemctl is-active telegram-kol-monitor.timer
```

Expected: disabled and inactive. Also prove the monitor, diagnostic, and test
oneshots are inactive before installation.

**Step 5: Deploy through the normal Git workflow**

From the approved workstation, prefer:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: production pulls the reviewed branch, reinstalls the editable
package, restarts `telegram-kol.service` in the proven safe window, and returns
healthy.

**Step 6: Synchronize the monitor installation and expected head**

With the timer still disabled, run from `/opt/telegram-kol-analyzer`:

```bash
./scripts/install_server_monitor.sh
```

Expected: monitor files and root-owned environment are installed for the exact
deployed HEAD while the timer remains disabled. Do not print the credential
file.

**Step 7: Run no-notify and simulated-delivery verification**

Start only the static no-notify diagnostic unit and inspect its bounded JSON
summary. Separately invoke the deployed pure formatter against a fixed synthetic
`MonitorResult` with batches 17 and 22 and capture stdout locally; do not call
Telegram and do not mutate the production database.

Expected:

- `monitor_error` is null;
- the only known production reason is the stable management-audit baseline,
  unless a new real reason is present;
- the simulated text matches the approved yellow contract;
- no current/expected Git hashes appear;
- the system-source and non-AI statement is present.

**Step 8: Re-enable monitoring and verify services**

```bash
./scripts/install_server_monitor.sh --enable
systemctl is-enabled telegram-kol-monitor.timer
systemctl is-active telegram-kol-monitor.timer
systemctl is-active telegram-kol.service
```

Expected: timer enabled/active, main service active, loopback HTTP health returns
200, monitor state is owned by the dedicated identity at mode 0600, and the
Runtime Agent/scanner settings and authority are unchanged.

Do not force a real anomaly notification during rollout. Let the next natural
eligible monitor result exercise delivery; this respects the project's
single-stop-notification rule while the no-notify diagnostic and simulated
formatter prove content before activation.

**Step 9: Record production evidence**

Update the appropriate handoff/status documentation with the reviewed and
deployed commit, focused and full test totals, safe-window evidence, no-notify
diagnostic result, simulated message contract, service/timer state, and rollback
command. Never record credentials or raw production payloads.

If the timer cannot be safely re-enabled or any real abnormality beyond the
known baseline appears, stop, leave the feature in progress, and report the
exact server verification still required.
