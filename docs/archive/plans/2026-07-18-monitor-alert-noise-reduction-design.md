# Production Monitor Alert Noise Reduction Design

## Goal

Reduce low-value Telegram alerts from `telegram-kol-monitor` without weakening
the fail-closed health result or changing any trading behavior.

## Problem

The monitor currently sends the same continuing anomaly again after the global
six-hour suppression window. That is useful for urgent failures such as service
down, unknown execution status, missing settings evidence, or state-file damage.
It is noisy for stable low-priority residue such as an unchanged expected-HEAD
baseline mismatch or an unchanged management-audit abnormal count that is
already known and tracked.

## Approach

Keep evaluation strict: `head_drift`, `audit_abnormal`, and other abnormal
states still make `healthy=false` and the monitor process still exits non-zero.
Only notification eligibility changes.

Classify continuing anomalies into two notification repeat policies:

- High-repeat policy: retain the current six-hour reminder for service,
  settings, journal, adapter, state, malformed snapshot, abnormal event, and
  incomplete audit evidence.
- Low-repeat policy: send once per unique fingerprint, then suppress unchanged
  repeats indefinitely while preserving state progress. This applies only when
  all reasons are limited to stable low-priority residue: `head_drift` and
  `audit_abnormal`.

A changed fingerprint still notifies immediately. Examples include a different
observed or expected HEAD, a changed audit abnormal count, a newly added high
priority reason, or a resolved anomaly followed by a new anomaly.

## Data Flow

`evaluate_monitor_snapshot()` continues to produce the same `MonitorResult`.
`fingerprint_monitor_result()` continues to canonicalize reason codes and safe
details. `decide_monitor_notification()` uses the fingerprint plus reason class
to decide whether to notify:

1. Healthy result clears the active fingerprint as before.
2. New or changed fingerprint notifies immediately.
3. Unchanged high-repeat fingerprint follows the existing six-hour policy.
4. Unchanged low-repeat fingerprint remains suppressed.

The existing four-field state schema is unchanged, so server state files remain
compatible.

## Safety Boundaries

This does not place or cancel orders, alter TP/SL, write the production
database, restart services, change trading settings, or hide `healthy=false`.
The scheduled monitor still logs its JSON result through systemd, and the
non-zero exit status remains available to operators.

## Verification

Add focused tests proving:

- unchanged `head_drift + audit_abnormal` stays suppressed after six hours;
- changed low-priority fingerprints notify immediately;
- high-priority anomalies still repeat after six hours;
- `run_production_safety_monitor()` persists progress while suppressing a
  continuing low-priority anomaly.
