# Phase 6A Safe Sync Shadow Probe Design

## Context

Phase 6A Candidate A is deployed in `worker_command_mode=shadow`, but Task 12
cannot collect its mandatory real sync sample under the owner's no-exchange-write
authorization. The normal sync orchestration is intentionally write-capable: it
can run trigger-protection rescue, submit verified backup stops, and cancel
terminal or deferred entry legs. Production currently has eligible work in those
lanes, so an ordinary sync cannot be proven exchange-read-only merely because no
write is already in flight.

The existing normal route remains authoritative. This design adds a temporary,
strictly gated Candidate A probe that exercises the real HTTP/shadow durability
path while allowing normal database reconciliation and Deepcoin reads but making
exchange mutation structurally unreachable. Candidate B removes the probe after
queue evidence is complete.

## Decision and rejected alternatives

Use the migration-only request header
`X-Worker-Command-Probe: reconcile-only`. The header is accepted only for the
sync route, only in `worker_command_mode=shadow`, and only while the notification
bot is effectively disabled. The server records the probe policy in the shadow
job request so the durable fingerprint describes the operation that actually
ran. Requests without the header retain the exact existing `{}` command payload,
domain calls, response, notification ordering, and exchange-write semantics.

Rejected alternatives:

- A permanent public read-only sync option would leave an operator-visible way
  to bypass normal protection maintenance after Phase 6A.
- Temporarily changing global position-management liveness would weaken unrelated
  live protection and was explicitly rejected.
- A CLI-only or synthetic shadow row would not prove the real HTTP enqueue and
  response-fingerprint path required by Task 12.

## Runtime boundary

The probe carries an internal `effects_policy=reconcile_only` through the Web
compatibility executor and worker adapter. The normal default is
`effects_policy=full`; no caller can obtain reconcile-only behavior by omitting
or misspelling the header.

The reconcile-only orchestration:

1. Creates the existing Deepcoin client and exposes only the read methods needed
   by reconciliation. Any attempted submit, cancel, amend, or close method fails
   before a request can leave the process.
2. Calls `reconcile_deepcoin_execution_bindings_read_only`, which may persist
   normal local reconciliation observations/state but does not run protection
   rescue or backup-stop submission.
3. Calls `sync_manual_closed_deepcoin_positions` with a new keyword
   `allow_exchange_mutations=False`. The default remains `True`. The false path
   skips terminal/deferred-entry cleanup cancellation while retaining the
   existing read-based manual-close and local reconciliation behavior.
4. Does not call attribution, protection, or terminal-entry notification
   delivery functions. The pre-call gate still requires the notification bot to
   be effectively disabled; the explicit skip is defense in depth and is limited
   to the temporary probe.
5. Returns the same six-key `200` body shape and existing bounded error mapping.
   The shadow job settles with the exact HTTP status/body fingerprint.

The probe is refused before enqueue or reconciliation when the header value is
unknown, the runtime mode is not `shadow`, the notification bot is enabled, or
the request has any other payload. Operational preflight additionally requires
zero active management/write state and complete evidence. These conditions are
not silently downgraded.

## Data and compatibility

No schema or business-table shape changes. The existing `worker_command_jobs`
table stores the probe request as `{"effects_policy":"reconcile_only"}`;
ordinary sync remains `{}`. Existing idempotency and fingerprint rules therefore
distinguish a probe from a normal sync without a second command type.

The probe may perform the database writes already permitted by the owner:
exchange snapshot observations, binding/leg reconciliation, manual-close state,
and the shadow job lifecycle. It must not claim, deliver, suppress, delete, or
otherwise mutate any pre-existing notification row. Pre/post evidence compares
the exact old-row state, not counts alone.

Recognition, contextual strategy resolution, position ownership, order
parameters/order, `message_lock_mode=global`, `message_pipeline_mode=queue`, and
normal exchange-write semantics are unchanged.

## Failure and rollback behavior

- Any attempted exchange mutation in reconcile-only mode is a hard failure; it
  is never retried or converted into a normal sync.
- Incomplete Deepcoin evidence receives at most one reasoned retry at the
  operational layer, then fails closed with Phase 6A left `in_progress`.
- Shadow enqueue/settlement failure retains the existing fail-closed behavior.
- Deployment rollback is the currently recorded Candidate A through the gated
  updater. Runtime remains shadow until the safe sample and subsequent Task 12
  gates pass.
- Candidate B deletes the header branch and reconcile-only Web reachability, so
  the hardened production interface contains no permanent bypass.

## Verification

Every production edit starts with a focused failing test. Tests must prove:

- requests without the header call the existing full functions and preserve the
  frozen contract byte-for-byte;
- invalid/mis-scoped probes fail before enqueue, DB reconciliation, notification,
  or exchange access;
- reconcile-only uses the read-only reconciler, disables manual-sync cleanup,
  rejects every mutation method, skips all notification delivery, and settles one
  shadow row with the actual response fingerprint;
- `allow_exchange_mutations=True` remains the default and preserves current
  cleanup ordering;
- database reconciliation still occurs with exchange mutation disabled.

After focused GREEN, run the affected Phase 6A acceptance slice and exactly one
new final full suite for the new Candidate A. Because schema is unchanged, the
existing schema/physical-rollback rehearsal remains valid; add a production-copy
probe rehearsal with a mutation-trap Deepcoin adapter, `PRAGMA quick_check`, and
before/after evidence for affected and critical tables.

Deploy only the exact reviewed SHA through the gated updater. Before and after
the one real probe, verify notification-bot disabled state, zero active
management/write state, exact old notification-row state, worker-job parity,
SQLite_BUSY, and complete read-only Deepcoin order/fill/trigger/position history.
Any mismatch stops the phase in shadow. No order or position is manufactured.
