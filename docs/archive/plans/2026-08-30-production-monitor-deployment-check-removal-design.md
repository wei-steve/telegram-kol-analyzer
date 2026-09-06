# Production Monitor Deployment-Check Removal Design

## Goal

Remove deployment-self-observation from the production safety monitor while
preserving its business, trading-risk, and operational-health checks. The
activation controller remains the sole authority for immutable-release,
process-identity, and systemd deployment proof.

## Boundary

The monitor will stop producing or collecting evidence for these deployment
reason codes:

- `runtime_release_invalid`
- `runtime_release_mixed`
- `runtime_identity_unproven`
- `runtime_capability_unproven`
- `runtime_unit_hash_drift`

The monitor will also stop validating runtime topology, querying systemd for
`MainPID`, reading other processes under `/proc`, comparing installed unit
files or release drop-ins, and checking unit enablement state.

The following remain unchanged:

- activation-controller exact-SHA, immutable-release, process-identity,
  authority, restart, and rollback gates;
- business and trading-risk checks, including settings drift, order and
  position invariants, protection failures, message-operation coverage,
  contract-spec ownership, abnormal execution events, and management audits;
- Telegram anomaly and recovery notifications;
- the activation-time monitor diagnostic as a hard business-safety gate;
- the immutable release path used to load the monitor's own code.

`audit_command_failed` remains a monitor failure because its command audits
trading-management data rather than deployment identity.

## Architecture

`ProductionSafetyAdapters` will retain only the sources required by business
and operational checks. Service liveness will be proven by the existing
loopback settings endpoint. Journal inspection will use a fixed bounded list
containing both the legacy monolith unit and the split runtime units, instead
of probing runtime roles to choose a topology.

The monitor snapshot and evaluator will no longer contain a runtime-release
scope. Monitor CLI and systemd command lines will no longer accept or pass
expected release commit, manifest, release path, or deployment-identity
endpoint options. The systemd units may still use the installer-provided
release path in `PYTHONPATH`; that selects the monitor code and is not a
deployment assertion made by the monitor.

All three monitor service sandboxes will block access to the system bus. With
deployment checks removed, the regular monitor and diagnostic no longer need
D-Bus access; the test-notification unit already exits before adapter
construction and remains isolated.

## Failure Semantics

Missing or malformed business evidence remains fail-closed through the
existing adapter-failure and invariant reason codes. Removing deployment
checks must not suppress failures from settings, journal, database, exchange
snapshot, contract-spec, message-operation, or management-audit sources.

Old deployment reason codes found in persisted monitor state will no longer be
part of the fixed active-reason set and will age out through the normal state
transition. No production database or settings mutation is part of this
change.

## Verification

Tests will prove, in order:

1. a monitor run does not request runtime-release or deployment-identity
   evidence and cannot emit the removed reason codes;
2. journal collection does not probe topology and covers the fixed bounded
   unit list;
3. monitor CLI and all systemd units omit deployment-check arguments;
4. the regular, diagnostic, and test-notification units all block the system
   bus;
5. representative business failures still fail the monitor and the activation
   diagnostic remains a hard gate.

Development uses focused tests for each edit and one full suite for the final
candidate. Deployment may push and stage the exact reviewed candidate, but the
existing `entry_preambles.id=13` business anomaly remains an intentional
activation blocker until separately repaired under an authorized production
data-mutation scope.
