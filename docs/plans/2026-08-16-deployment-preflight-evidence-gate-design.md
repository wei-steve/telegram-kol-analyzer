# Deployment Preflight Evidence Gate Design

**Date:** 2026-08-16

**Status:** Approved by the operator on 2026-08-16

## Objective

Replace the deployment preflight's global `updated_at` recency heuristic with
an explicit, fail-closed work-evidence model. The corrected gate must allow a
reviewed `code` or `schema_compatible` deployment to restart around historical,
read-only reconciliation residue while continuing to block real exchange-write
critical sections, unknown exchange outcomes, malformed evidence, and unsafe
writer changes.

The first intended consumer is the reviewed MiMo v2 retirement candidate based
on `7813150b7b33cd8ce3d90a6145889c6fef192dc7`. The gate repair is not a Batch
119 recovery and must not change recognition, trading, reconciliation, database
history, or exchange state.

## Root Cause

The current collector gives every registered work table one set of active
states and one timestamp column. It classifies a row as fresh work whenever its
state is active and `updated_at` falls within a ten-minute window. The artifact
then maps any non-empty fresh-work count to `fresh_active_exchange_work` and
blocks every change class.

That model confuses two different events:

- a durable state transition or exchange-write attempt; and
- a read-only reconciler checking the same unresolved row again.

Production Batch 119 is the concrete reproduction. It was created on
2026-08-12 and remains `reconciling`; leg 103 remains `submitted`. The leg has
no client order ID, exchange order ID, request, or response, and the batch has
no `last_progress_at`. Reconciliation repeatedly records
`management_close_order_not_found`, preserves the non-retryable state, and
refreshes both `updated_at` values. Read-only observations on 2026-08-17 saw the
timestamp move from `01:15:28` to `01:15:53` to `01:16:28` while the durable
safety facts did not change.

Waiting cannot open a deployment window while the service continues this
heartbeat. A Batch ID exception, a reason-code exception, manual database edit,
or wider time window would hide the defect rather than repair it.

## Boundaries

The implementation must:

- remain fail closed for true writes and unknown outcomes at every age;
- make no Batch ID, message ID, strategy, symbol, or reason-code exception;
- make no production database edit or historical replay;
- make no exchange write and send no test notification;
- leave MiMo v1 authoritative and MiMo v2 retired;
- avoid changing writer retry rules, reconciliation state machines, or trading
  safety policy;
- preserve exact-commit, expiring-artifact, backup, migration-dry-run, and
  rollback guarantees;
- require separate approval before push and before deployment.

## Options Considered

### Use `last_progress_at` everywhere

This is small but incomplete. Several registered tables do not have a
semantically reliable `last_progress_at`, and falling back to `updated_at`
recreates the same defect.

### Add a global writer lease immediately

An exclusive deployment lease around every Deepcoin POST would provide a strong
quiescence primitive, but safely extending the lease through durable result
persistence touches the whole execution stack. It is disproportionate unless
fault injection proves that an exchange write can start without first creating
durable pre-submit evidence.

### Explicit work evidence plus two-phase deployment

Adopt this option. Each table has a bounded adapter that classifies durable
facts by safety meaning. The existing preliminary-preflight, service-stop, and
final-preflight skeleton becomes phase aware and binds both observations. A
global writer lease remains a mandatory follow-up only if fault injection finds
an unjournaled POST path.

## Standard Work Classifications

Every registered execution-related table must map each relevant row to exactly
one classification:

| Classification | Meaning |
| --- | --- |
| `in_flight_write` | Durable evidence says a write is reserved, submitting, cancelling, or inside another non-restart-safe critical section. |
| `unknown_outcome` | A request may have reached the exchange but its outcome is not known. |
| `restart_safe_wait` | The row may remain nonterminal, but restart handling is read-only or idempotently reconciles a durable exchange identity without resubmission. |
| `historical_residue` | Old or superseded nonterminal state has no active lease and cannot be admitted by the unchanged candidate writer surface. |
| `terminal` | No execution work remains. |
| `malformed` | Required columns, states, deadlines, or evidence cannot be interpreted completely. |

`updated_at` is retained for audit display but is never evidence of exchange
progress. An adapter may use `last_progress_at` only when the owning component
defines it as changing solely for a valid state transition or new exchange
evidence. Otherwise the adapter must use explicit state, lease/deadline,
attempted-write flag, mutation intent, request identity, response identity, and
terminal evidence.

Unknown states, a newly discovered work table without an adapter, or required
columns missing from an otherwise expected schema produce `malformed` rather
than being skipped.

## Decision Matrix

| Fact | `code` / `schema_compatible` | `execution_writer` / `live_promotion` |
| --- | --- | --- |
| `in_flight_write` | BLOCK | BLOCK |
| `unknown_outcome` | BLOCK | BLOCK |
| `restart_safe_wait` | WARN | BLOCK |
| `historical_residue` | WARN | BLOCK |
| `terminal` only | PASS | PASS |
| `malformed` | BLOCK | BLOCK |
| protected open position | WARN | WARN |
| unprotected open position | BLOCK | BLOCK |
| incomplete exchange snapshot | WARN | BLOCK |

The classifications are counts and bounded fingerprints, not row payloads.
Artifacts must not expose raw messages, strategy text, position IDs, order IDs,
request/response bodies, credentials, or secrets.

## Table Adapter Authority

Replace the generic `_TableWorkSpec(active_states, unknown_states,
time_column)` model with explicit adapters. An adapter declares:

- the exact table and state column;
- required and optional evidence columns;
- every recognized state;
- the classification predicate for each state and evidence combination;
- the safe timestamp source, when one exists;
- the bounded fields that participate in the safety fingerprint.

The registry is the only authority used by collection, decision reporting,
tests, and deployment documentation. Tests enumerate all registered states so
producer and gate allowlists cannot silently drift.

For management rows, `submitted` or `reconciling` is not automatically safe.
The adapter must prove that the candidate retains read-only reconciliation or
idempotent reconciliation semantics and that there is no unknown mutation
intent. Batch 119 qualifies without a special case because its durable evidence
matches that generic rule. The original retirement-only candidate did not
change the management writer/reconciler surface. During Task 6 review, however,
the terminal-entry cleanup path was proved to lack durable pre-submit ownership;
closing that prerequisite changes an exchange writer boundary. The combined
candidate is therefore `execution_writer`, not `schema_compatible`, and must
BLOCK on restart-safe/history residue rather than inherit the retirement-only
exception.

## Candidate Change-Surface Validation

The requested change class is a lower bound, not an assertion the operator can
use to downgrade risk. Before preflight, compare the exact production commit to
the exact staged candidate and classify changed paths through one versioned
registry:

- deployment-guard-only changes may remain `code` or `schema_compatible`;
- additive/reader-compatible model and migration changes require at least
  `schema_compatible`;
- Deepcoin clients, mutation gateways, executors, reconcilers, retry/state
  transitions, or their execution settings require `execution_writer`;
- activation or authority changes require `live_promotion`.

The artifact binds the requested and effective change classes, production SHA,
candidate SHA, registry version, and change-surface fingerprint. An
underdeclared change is `malformed`/BLOCK. The class may be upgraded
automatically but never downgraded.

## Two-Phase Deployment Protocol

### Stage and validate

Fetch the exact remote commit, verify it matches the approved SHA, and create a
detached candidate worktree. Extract the candidate updater to a mode-0700
temporary directory and verify its SHA-256 against the reviewed local copy.
Do not overwrite `/usr/local/bin/telegram-kol-update` before the final gate.

### Preliminary phase A

While the production service is active, collect one query-only SQLite snapshot
and bounded exchange-cache facts. Block immediately for `in_flight_write`,
`unknown_outcome`, `malformed`, unprotected positions, or an underdeclared
change class. For `code` and `schema_compatible`, restart-safe waits and
historical residue produce WARN.

The preliminary artifact binds the database watermark and normalized work fact
fingerprint. It can authorize only an attempt to stop the service; it cannot
authorize checkout, installation, or restart on the candidate.

### Graceful stop

Stop `telegram-kol.service` through systemd and wait for it to become inactive
within a bounded timeout. Failure to stop completely aborts the deployment.
No checkout, package installation, database migration, or updater installation
has occurred yet.

### Final phase B

With the service inactive, collect a second query-only database snapshot and
bind it to phase A. Heartbeat-only timestamp movement is excluded from the work
fingerprint. A safe wait may remain unchanged or become terminal. Any new
in-flight write, unknown outcome, malformed fact, unprotected position, or
otherwise unsafe transition blocks deployment.

For `schema_compatible`, create the authoritative online backup and candidate
migration dry-run only after the service stops. Both must match the phase-B
watermark and pass `quick_check` and candidate-model validation. The final
artifact includes the preliminary fingerprint and is the only artifact allowed
to authorize mutation.

### Install and restart

Verify the unexpired final artifact immediately before mutation. Fast-forward
the production checkout to the exact candidate, install the editable package,
install the candidate updater, and start the service. Verify exact SHA, active
service, HTTP recovery, clean tracked tree, MiMo v1 authority, and database
audit invariants.

## Race And Failure Handling

A writer that starts after phase A must persist its reservation/submitting
state before the Deepcoin POST. Phase B therefore sees either a completed safe
transition, an in-flight write, or an unknown outcome. Tests must inject work
between the two phases and prove the latter two block.

Fault injection must enumerate every production Deepcoin POST entry point and
prove durable pre-submit evidence exists before network dispatch. If any path
cannot meet that invariant, this design must not deploy. The next design must
introduce a global writer lease that remains owned until the outcome is durably
persisted; no path or reason-code exception is permitted.

If phase B, backup validation, migration dry-run, artifact verification,
checkout, installation, or service startup fails, the updater restores the
previous checkout and editable package and starts the previous service. It
never restores an old database. Temporary migration copies are deleted; the
validated backup remains. Failure before checkout leaves production code
untouched.

## Artifact Contract And Observability

Introduce an artifact schema version that includes:

- `phase` (`preliminary` or `final`);
- production and candidate commits;
- requested and effective change classes;
- policy and change-surface versions/fingerprints;
- database watermark;
- bounded counts by standard work classification;
- exchange-snapshot completeness/protection facts;
- backup and migration facts when required;
- preliminary artifact fingerprint on the final artifact;
- creation, expiry, decision, reason codes, and final fingerprint.

Reason codes identify the actual class, including
`deployment_in_flight_write`, `deployment_unknown_outcome`,
`deployment_restart_safe_wait`, `deployment_historical_residue`,
`deployment_change_class_underdeclared`, `deployment_phase_drift`, and
`deployment_evidence_malformed`. `fresh_active_exchange_work` is retired.

## TDD And Verification

RED tests must cover:

- the exact Batch-119-shaped heartbeat reproduction without production IDs;
- identical fingerprints when only `updated_at` changes;
- `schema_compatible` WARN versus `execution_writer` BLOCK for the same
  restart-safe residue;
- fresh and stale true write critical sections;
- fresh and stale unknown exchange outcomes;
- unknown states, missing columns, and unregistered tables;
- underdeclared candidate writer changes;
- phase-A/phase-B linking and tamper resistance;
- a new write or unknown outcome appearing between phases;
- candidate updater not being installed before final authorization;
- rollback at every post-stop failure boundary.

GREEN must be the minimum implementation of those policies. It must not modify
management state, trading retry behavior, or historical data.

Run focused deployment-preflight, CLI, updater, reconciliation, fault-injection,
MiMo v1, configuration, and retirement-boundary tests; then compile checks,
`git diff --check`, and the full suite. Obtain an independent review with zero
Critical or Important findings before requesting push approval.

## Server Shadow And Deployment Acceptance

Before push or deployment, stage the exact candidate beside production and run
focused server tests. A read-only shadow classification of the production
database must show:

- Batch-119-shaped residue classified generically as restart-safe/history;
- zero `in_flight_write`;
- zero `unknown_outcome`;
- policy-only `schema_compatible` WARN and `execution_writer` BLOCK for the
  same sanitized facts;
- exact combined candidate classified `execution_writer` and BLOCK;
- zero database writes, notifications, and exchange writes.

Verify that production SHA, service, tracked files, and database remain
unchanged. Push requires explicit approval. Deployment requires a second,
independent approval and may proceed only through the two-phase updater.

Successful deployment additionally requires exact candidate SHA, active
service, clean tree, HTTP recovery, MiMo v1 as the only recognition path, no
lost audit rows, no non-v1 recognition run, no orphaned references, no
historical replay, no notification, and no deployment-attributed exchange
write.

## Rollback

Before checkout, a failure simply restarts the unchanged production service.
After checkout, restore the previous SHA, reinstall its editable package, and
restart it. Never restore an older database, delete business history, rewrite
Batch 119, or treat rollback as authorization to retry an unknown exchange
request.
