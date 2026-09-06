# Current Position Protection Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Repair the one proven live protection gap without mutating the already protected position, then make incident convergence reflect exact current exchange evidence.

**Architecture:** Treat the complete Deepcoin snapshot and exact `posId` ownership as authoritative. Use the existing fingerprinted single-position backup-stop repair for the only exchange mutation. Handle stale incident convergence as a separate, non-exchange-writing change; never rewrite or delete historical incidents.

**Tech Stack:** Python, Typer, SQLAlchemy, SQLite, Deepcoin private read-only/write gateways, pytest, systemd.

---

## Reviewed production evidence

The stable read-only audit classified five incident rows as `current_risk`, but
those rows do not represent five independent missing protections.

- `position:33ace41d1f86` is BTC long, size 6, average price 64289.7. The
  exchange exposes a verified size-6 primary stop at 64100, a size-0 stop at
  the same price, and a size-6 take profit at 67000. Its old specialized backup
  row at 62574.6 is `missing`. The existing dry-run planner produced exactly one
  conflict-free action: create an exact-position size-0 backup stop at 63971.8,
  20 bps beyond the 64100 primary stop. The five-incident audit contributes
  three rows for this position, all from the same protection episode:
  `backup_management_in_progress`, `protection_missing`, and
  `protection_leg_conflict`.
- `position:51017350265e` is BTC short, size 12, average price 64259.5. Current
  exchange readback and verified ledgers agree on primary stop 65300, backup
  stop 65430.6, and take profits 63600×6, 62900×3, and 62700×3. All five order
  IDs are visible in a complete pending-TPSL response. Its two audit rows came
  from transient `live_position_snapshot_unavailable` and
  `backup_stop_readback_unavailable` observations after the active revision;
  no exchange repair is currently justified.

The reviewed dry-run fingerprints in this document are evidence only and must
not be reused for apply. Production state can change at any time.

### Task 1: Apply one exact backup-stop repair to `position:33ace41d1f86`

**Files:**

- No source file changes.
- Update after verification: `docs/runtime-incident-agent-status.md`

**Step 1: Prove a fresh safe execution window**

Confirm the latest Telegram message is terminal, live checkpoints match,
evidence/context/management/component/mutation/rescue/runtime claims are zero,
and two complete exchange snapshots have the same fingerprint. Stop without an
exchange write if any check fails.

**Step 2: Generate a fresh dry-run**

Run on production:

```bash
.venv/bin/telegram-kol-research repair-backup-stops \
  --database-path data/research.db \
  --deepcoin-contract-specs-path config/deepcoin_contract_specs.yaml
```

Require exactly one action for the exact long position, no target conflict,
primary stop 64100, and a backup trigger exactly 20 bps farther from the market
than the primary. Record the newly returned plan fingerprint and action ID.

**Step 3: Apply only the freshly reviewed action**

Generate one new, single-use operator confirmation token. Run exactly one:

```bash
.venv/bin/telegram-kol-research repair-backup-stops \
  --database-path data/research.db \
  --deepcoin-contract-specs-path config/deepcoin_contract_specs.yaml \
  --pos-id <exact-pos-id> \
  --action-id <fresh-action-id> \
  --expected-fingerprint <fresh-plan-fingerprint> \
  --confirmation-token <new-single-use-token> \
  --apply
```

Expected result: `status=active` with one returned exchange order ID. A changed
fingerprint, ambiguous readback, unknown submission outcome, or missing order
ID is a hard stop; never retry blindly.

**Step 4: Verify the exchange and ledgers**

Re-read the exact position and all pending TPSL rows. Require the original
primary stop and take profit to remain visible, the new backup to be visible at
the planned trigger, a new active `position_backup_stop_orders` row, and a
verified exact `position_protection_ledger` row. Confirm the other three live
positions and all existing order IDs were unchanged.

**Step 5: Re-run the dry-run and no-notify safety audit**

The target must disappear from `repair-backup-stops` actions. Run the
independent no-notify monitor diagnostic and require `healthy=true`,
`monitor_error=null`, and `notification_status=disabled`.

### Task 2: Preserve `position:51017350265e` without an exchange mutation

**Files:**

- No source or production data changes.

**Step 1: Repeat exact current-state readback**

Require primary 65300, backup 65430.6, and take profits 63600×6, 62900×3, and
62700×3 to be present with a complete pending-TPSL observation.

**Step 2: Refuse unnecessary repair**

Require both `repair-backup-stops` and
`recover-position-management-liveness` to return no action for this exact
position. Do not cancel, replace, or duplicate any of its five current orders.

### Task 3: Separate stale incident convergence from exchange repair

**Files:**

- Update: `docs/runtime-incident-agent-status.md`

**Step 1: Re-run the read-only incident audit after Task 1**

Compare the incident classifications with the exact current exchange orders.
The old rows may remain `current_risk` because the current audit intentionally
requires a newer active protection revision than the incident; do not treat a
remaining count by itself as proof that another exchange write is needed.

**Step 2: Preserve historical rows**

Do not mark an incident delivered, resolved, or not-needed merely to reduce the
count. Do not insert a synthetic revision or refresh a timestamp. Record the
exact current exchange proof next to the unchanged incident history.

**Step 3: Open a separate code plan only if convergence remains misleading**

If the exact exchange state is healthy but the audit still reports current
risk, diagnose `protection_incident_convergence.py`, protection revisions, role
legs, and management replacement finalization as a separate bug. That change
must start with focused failing tests and a new design review; it must not be
bundled into the authorized single-position exchange repair.

### Task 4: Final production verification

**Files:**

- Update: `docs/runtime-incident-agent-status.md`

**Step 1: Re-run the protection incident audit**

Verify the repaired long position is fully protected and the already-safe
short position remains unchanged. Incident convergence work must never be used
as a substitute for checking current exchange orders.

**Step 2: Verify notification boundaries**

Confirm `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID=272`, historical
severe runtime incidents remain unclaimed/unnotified, and only new severe
incident IDs are Telegram-eligible. Keep the Runtime Agent selector exactly
`management_partial_failed`.

**Step 3: Record evidence and stop**

Record the final exchange fingerprint, exact repaired position reference,
new backup trigger and order-ID hash, no-notify monitor result, incident audit
counts, production commit, and service health. Do not proceed to another
position or cleanup action without a new reviewed plan and explicit approval.
