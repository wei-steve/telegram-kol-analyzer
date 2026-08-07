# Runtime Incident Notification Watermark Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a dormant incident-ID watermark that prevents historical runtime incidents from becoming Telegram-claimable when a new notification type is enabled.

**Architecture:** Parse one optional non-negative incident-ID watermark into `RuntimeIncidentConfig`, fail closed for malformed configured values, and apply `RuntimeIncident.id > watermark` to both the notification selection and compare-and-set claim predicates. Deploy with the setting absent and selectors unchanged; activation remains a later separately approved turn.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, pytest, systemd environment files.

---

## Constraints

- Use strict test-driven development.
- Do not edit or suppress historical runtime incidents.
- Do not add `severe_protection_incident` to Telegram or Agent selectors in this implementation turn.
- Do not restart production until a fresh safe-window gate passes.
- Deploy the watermark code dormant: setting absent, selectors unchanged.
- Rollback must restore the narrow selector before ever removing an active watermark.

### Task 1: Parse a fail-closed optional notification watermark

**Files:**
- Modify: `tests/test_runtime_incident_adapters.py`
- Modify: `src/telegram_kol_research/config.py`
- Modify: `config/runtime_incident_agent.env.example`

**Step 1: Write the failing configuration tests**

Add parameterized tests that load `RuntimeIncidentConfig` with:

```python
assert load_runtime_incident_config(
    environ={}, env_file_paths=[]
).telegram_notification_after_incident_id is None

assert load_runtime_incident_config(
    environ={
        "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID": "256",
    },
    env_file_paths=[],
).telegram_notification_after_incident_id == 256
```

Require `0` to remain valid. Require `""`, `"abc"`, `"-1"`, and values above
`2**63 - 1` to produce `2**63 - 1`, which disables every positive SQLite ID.

**Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_runtime_incident_adapters.py \
  -k 'notification_watermark'
```

Expected: FAIL because the configuration field does not exist.

**Step 3: Implement the minimal parser**

Add to `RuntimeIncidentConfig`:

```python
telegram_notification_after_incident_id: int | None = None
```

Add a private parser with these exact semantics:

```python
_SQLITE_MAX_INTEGER = 2**63 - 1

def _notification_after_incident_id(env: dict[str, str]) -> int | None:
    key = "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID"
    if key not in env:
        return None
    try:
        value = int(env[key])
    except (TypeError, ValueError):
        return _SQLITE_MAX_INTEGER
    if not 0 <= value <= _SQLITE_MAX_INTEGER:
        return _SQLITE_MAX_INTEGER
    return value
```

Pass the result into `RuntimeIncidentConfig`. Document the dormant setting in
`config/runtime_incident_agent.env.example` without assigning a value.

**Step 4: Run the focused configuration tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_runtime_incident_adapters.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/config.py \
  config/runtime_incident_agent.env.example \
  tests/test_runtime_incident_adapters.py
git commit -m "feat: parse runtime notification watermark"
```

### Task 2: Enforce the watermark at the notification claim boundary

**Files:**
- Modify: `tests/test_system_operator_bot.py`
- Modify: `src/telegram_kol_research/system_operator_bot.py`

**Step 1: Write the failing claim tests**

Create historical and new pending runtime incidents of the same selected type.
Call:

```python
claim = claim_next_runtime_incident_notification(
    session_factory,
    notification_types=frozenset({"severe_protection_incident"}),
    after_incident_id=historical.id,
)
assert claim["incident"].id == new.id
```

Add separate tests proving:

- a database containing only rows at or below the watermark returns `None` and
  leaves every notification status `pending`;
- the watermark combines with the exact type allowlist;
- failed and stale-delivering historical rows remain ineligible;
- `after_incident_id=None` preserves the existing oldest-first behavior.

**Step 2: Run the claim tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_system_operator_bot.py \
  -k 'runtime_incident and watermark'
```

Expected: FAIL because the claim helper does not accept or enforce the
watermark.

**Step 3: Implement the claim predicate**

Add `after_incident_id: int | None = None` to
`claim_next_runtime_incident_notification`. When non-`None`, combine:

```python
RuntimeIncident.id > int(after_incident_id)
```

with the existing status/type predicate. Reuse the complete combined predicate
in both the oldest-row query and the compare-and-set update.

Pass `feature_config.telegram_notification_after_incident_id` from
`deliver_runtime_incident_notifications` into every claim attempt.

**Step 4: Add the failing delivery test**

Configure Telegram enabled with the severe-protection type and a watermark at
the only historical row. Assert `deliver_runtime_incident_notifications`
returns zero, the fake sender is never called, and the historical row remains
pending. Then add one later row and assert exactly that row is delivered.

**Step 5: Run the tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_system_operator_bot.py \
  tests/test_runtime_incident_adapters.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/system_operator_bot.py \
  tests/test_system_operator_bot.py
git commit -m "fix: exclude historical runtime notifications"
```

### Task 3: Document rollback and verify the dormant boundary

**Files:**
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`
- Modify: `tests/test_runtime_agent_architecture_boundary.py`

**Step 1: Write the failing architecture-boundary assertion**

Require the runbook to contain the exact setting name and the rollback order:
restore the narrow Telegram selector before removing the watermark.

**Step 2: Run the boundary test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_runtime_agent_architecture_boundary.py
```

Expected: FAIL because the runbook does not document the watermark.

**Step 3: Update operational documentation**

Document:

- absent preserves existing behavior;
- valid values permit only larger incident IDs;
- malformed configured values fail closed;
- the dormant deployment leaves the key absent;
- activation records the current maximum ID before adding the exact type;
- rollback restores the narrow selector before removing the watermark.

Record local tests and the dormant rollout state in the canonical status file.

**Step 4: Run focused and adjacent regressions**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_runtime_incident_adapters.py \
  tests/test_system_operator_bot.py \
  tests/test_runtime_agent_architecture_boundary.py \
  tests/test_runtime_incidents.py \
  tests/test_runtime_agent_cli.py \
  tests/test_web_live_listener_startup.py
```

Expected: PASS.

Run:

```bash
git diff --check
```

Expected: no output.

**Step 5: Review and commit**

Review only the watermark-related diff. Confirm no selector value, production
configuration, or business-write path changed.

```bash
git add docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md \
  tests/test_runtime_agent_architecture_boundary.py
git commit -m "docs: add runtime notification watermark runbook"
```

### Task 4: Push and deploy dormant

**Files:**
- Modify only if production verification reveals a code defect.

**Step 1: Push the reviewed commits**

```bash
git push origin HEAD:codex/deepcoin-auto-trading-v1
```

Expected: fast-forward push succeeds.

**Step 2: Prove a fresh production safe window**

Confirm service health, latest completed recognition, listener checkpoints,
zero recognition/context/management/mutation/recovery work in flight, and a
complete exchange snapshot. Confirm no runtime notification claim is active.

**Step 3: Deploy through the existing helper**

Use `scripts/server_git_update.sh` when PowerShell is unavailable. Keep:

```text
TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES=management_partial_failed
TELEGRAM_KOL_RUNTIME_AGENT_TYPES=management_partial_failed
TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID absent
```

Expected: code deploys with no notification behavior change.

**Step 4: Verify dormant production behavior**

Confirm deployed SHA, active service, HTTP 200, unchanged selectors, absent
watermark, all 256 historical severe-protection rows still pending, zero new
notification claim/delivery, unchanged exchange fingerprint, and no business
write caused by verification. Run the focused deployed suites from Tasks 1-3.

**Step 5: Record the deployment checkpoint**

Update `docs/runtime-incident-agent-status.md`, commit, and push the
documentation-only checkpoint. Do not activate the severe-protection selector
in this turn.
