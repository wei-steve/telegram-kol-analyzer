# Protection Incident Current-Evidence Convergence Design

## Problem

The production protection audit reports seven `current_risk` incident rows for
two positions even though a complete Deepcoin readback proves both positions
currently have verified primary stops, independent backup stops, and take
profits.

The existing convergence path has two production-shape mismatches:

1. It requires an active role-aware protection revision created after each
   incident. Current production revisions use an older payload that contains
   order IDs but no `roles` or `replacements`, so they can never satisfy that
   contract.
2. It filters pending TPSL rows by `posId`. Deepcoin's production pending-TPSL
   rows normally omit `posId`; their globally unique order IDs are instead
   assigned to an exact position by the canonical protection ledger. The
   filter therefore discards orders that are present on the exchange.

The result is a convergence failure, not an exchange-protection failure. The
historical rows must remain immutable and no additional position mutation is
justified.

## Decision

Extend the read-only audit with an exact current-state convergence path. Keep
the existing newer-revision path as a compatible fast path. For a live
position whose historical incident cannot use that path, derive current health
from one coherent exchange snapshot and exact local ownership records.

This change does not create or update a protection revision, incident, ledger,
notification, claim, order, or position. It changes only the classification
returned by the read-only audit.

## Evidence boundary

The current-state path resolves an incident only when every condition below is
true:

- the database snapshot is stable and the exchange snapshot has no errors;
- the exact `posId` is live and maps to the incident's venue, binding, and
  execution leg;
- the target instrument has an explicitly complete pending-TPSL observation;
- account-wide canonical ownership has no conflict for the position;
- an exact active specialized backup-stop row identifies one visible native
  backup order;
- after excluding that exact backup order from legacy `stop_loss` ledger rows,
  a distinct exact primary stop is visible;
- primary and backup order IDs are nonempty and different;
- at least one exact verified take-profit order is visible;
- every selected order matches its persisted instrument, side, price, size,
  and exact account-wide owner through the existing native TPSL matcher;
- no unowned native TPSL order can affect the position.

Pending rows do not need to contain `posId`. Their globally unique order ID is
matched to the exact canonical owner; symbol, side, price, size, or time alone
never grants ownership.

Any missing evidence, duplicate role identity, incomplete pagination,
conflicting owner, mismatched row, or exchange error remains fail-closed as
`current_risk` or `evidence_insufficient` under the existing classification
rules.

## Components

`protection_incident_convergence.py` will:

- retain the global complete-snapshot gate and redacted output;
- load account-wide ownership once for all live positions;
- cache one current protection result per exact
  `(venue, binding_id, leg_id, pos_id)` scope;
- load only ledger, backup-stop, take-profit, and revision rows belonging to
  that exact scope;
- remove an active specialized backup order from legacy `stop_loss` primary
  candidates;
- call the existing `build_position_protection_audit()` matcher with no
  historical freeze reasons, because the incident being classified must not
  circularly prevent observation of its current state;
- require `protected=true`, distinct primary/backup IDs, and at least one
  verified take profit before returning
  `resolved_by_current_exchange_evidence`.

`protection_snapshot.py` and `protection_ledger.py` remain the authoritative
matching and ownership implementations. They should not be duplicated in the
incident audit.

## Data flow

1. The CLI makes a stable private SQLite snapshot and one complete read-only
   Deepcoin reconciliation snapshot.
2. The convergence audit derives live positions and instrument completeness.
3. Newer role-aware replacements continue through the existing strict path.
4. Legacy or transient incidents use the cached exact current-state path.
5. Exact database ownership and current exchange visibility are combined by
   the existing protection snapshot matcher.
6. The audit returns only hashed incident, position, and type references plus
   aggregate counts.

No step performs a database commit or invokes a Deepcoin write method.

## Error handling

- Snapshot errors or incomplete target-instrument pagination produce
  `evidence_insufficient`.
- A live exact position with incomplete or conflicting protection remains
  `current_risk`.
- Malformed legacy revision JSON is ignored; it cannot authorize resolution.
- Unknown or stale ledger rows cannot authorize another position because all
  rows are exact-scope filtered and account-wide order ownership is checked.
- The current strict newer-revision behavior remains available and unchanged.

## Testing

Focused tests will reproduce the production shape: pending TPSL rows without
`posId`, legacy revisions without role payloads, backup ledger rows historically
stored as `stop_loss`, and multiple incidents for the same exact scope. The
healthy case must fail before implementation and resolve afterward.

Fail-closed tests cover missing take profit, missing or non-visible backup,
same primary/backup order ID, wrong binding or leg, ownership conflict,
incomplete target-instrument observation, unowned order, and exchange errors.
An immutability test compares all source-row counts and values before and after
the audit. Existing redaction, truncation, and strict newer-revision tests must
continue to pass.

## Rollout

Deploy through a proven quiet window. The change has no runtime feature flag
because it is a read-only audit correction, but production verification must
first run the audit manually with no notification. Expected current production
classification is that the seven live-position incident rows move to
`resolved_by_current_exchange_evidence`, while the evidence-insufficient and
historical-terminal totals remain unchanged. Verify identical exchange state,
unchanged database source rows, zero runtime notification claims, the existing
watermark at 272, and a healthy no-notify monitor result.

