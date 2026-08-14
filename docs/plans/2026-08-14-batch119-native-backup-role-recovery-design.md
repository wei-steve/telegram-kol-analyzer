# Batch 119 Native Backup-Stop Role Recovery Design

## Problem

The first reviewed batch-119 production dry-run stopped before its first
Deepcoin GET with `exact_history_scope_invalid`. Production has three verified
protection-ledger rows for the exact batch-owned position: two native
`stop_loss` rows and one `take_profit` row. The exact-history loader currently
requires one ledger row whose literal purpose is `stop_loss` and one whose
literal purpose is `backup_stop`, so it rejects this durable shape.

The second native `stop_loss` is not an ambiguous duplicate. It has the same
exact exchange order identity as all of the following durable authorities:

- one confirmed `PositionMutationIntent` for an exact position SL write;
- one active `PositionBackupStopOrder`;
- one verified `PositionProtectionLeg` whose logical role is `backup_stop`;
- the exact execution binding, entry leg, position, instrument, side, and
  trigger economics owned by batch 119.

Deepcoin represents both the primary and backup protection as native SL orders.
The backup writer therefore persisted the exchange-native type `stop_loss` in
the generic protection ledger while the dedicated backup tables retained the
logical role. The batch-specific exact-history loader incorrectly treated the
generic ledger purpose as the only logical-role authority.

A separate operational problem also exists. The currently deployed production
version does not have or maintain the account write-generation table required
by the reviewed running-service dry-run. Creating an empty table would not make
the old writers increment it, so it would not provide a real generation fence.

## Safety Boundary

This change remains allowlisted to batch `119`. It does not rewrite historical
production ledger rows, change generic ledger semantics, infer arbitrary
duplicate stops, alter protected-entry reconciliation, replay a message, grant
automatic-trading authority, or authorize an exchange writer.

The dedicated loader may map an exchange-native `stop_loss` to the logical
`backup_stop` role only when independent durable authorities close the complete
identity and economics chain. Missing, duplicate, malformed, stale, or
conflicting evidence remains a pre-network refusal.

## Alternatives Considered

### Rewrite the production ledger

An audited migration could change the backup row's purpose from `stop_loss` to
`backup_stop`. This would mutate historical production evidence and require a
new production-write authorization. It also risks changing generic consumers
whose documented native-stop semantics currently use `stop_loss`.

### Keep the recovery permanently refused

This preserves the narrowest safety boundary but leaves batch 119 in
`reconciling` even though the logical backup role is already durably represented
by the dedicated backup authorities.

### Resolve the role from closed durable authority (selected)

Keep the historical ledger unchanged. Add a batch-119-only resolver that
accepts the generic native-SL row as the logical backup only when the ledger,
mutation intent, active backup row, and verified protection leg all identify
the same exact order and economics. This is the smallest compatibility surface
and preserves the original evidence.

## Exact Role Authority

The resolver must produce exactly one primary stop and exactly one backup stop.
The two exchange order identities must be distinct and safe. The take-profit
row remains outside the stop scope but must not conflict with either identity.

### Primary stop

The primary role requires all of these facts:

- one exact-owner verified `PositionProtectionLedger` native stop row;
- one adopted `TriggerProtectionIntent` whose adopted order is that ledger
  order;
- one verified `PositionProtectionLeg` with role `primary_stop`, index `1`, and
  the same exchange order;
- exact binding, entry leg, position, venue, strategy, instrument, and side;
- matching positive trigger economics and valid size semantics; and
- canonical, bounded evidence linking the adoption to the exact durable owner.

### Backup stop

The backup role requires all of these facts:

- one exact-owner verified `PositionProtectionLedger` native stop row;
- one exact-owner confirmed `PositionMutationIntent` whose response and
  persisted order identity match the ledger order;
- one active `PositionBackupStopOrder` with the same order and exact owner;
- one verified `PositionProtectionLeg` with role `backup_stop`, index `1`, and
  the same exchange order;
- exact binding, entry leg, position, venue, strategy, instrument, and side;
- equal positive trigger prices across the ledger, request, backup row, and
  protection leg; and
- exact size agreement, except that an omitted native request size is accepted
  only when the dedicated backup row's strict request is an exact-position SL
  with no `sz`, the verified backup leg has `planned_size=0`, and the dedicated
  backup-order contract identifies that combination as whole-position
  protection.

The mutation intent must also have the exact canonical operation,
idempotency identity, request fingerprint, response identity, timestamps, and
confirmed state expected for the dedicated backup submission. JSON is parsed
with existing strict bounded helpers. A caller-provided evidence field cannot
substitute for an ORM identity.

### Fingerprints and CAS

The exact-scope fingerprint must bind:

- the logical role;
- hashed exchange order identity;
- hashes of every durable authority row used for that role;
- canonical trigger and size markers;
- source/state markers; and
- the existing immutable batch, binding, entry-leg, and position authority.

Apply and resume rebuild the same role authority inside their existing locked
database transaction. Any row insertion, deletion, state change, identity
drift, or economics drift invalidates the reviewed snapshot and plan before a
database or exchange mutation.

## Data Flow

1. Load and validate the fixed batch-119 durable identity.
2. Load the bounded candidate ledger population for the exact owner.
3. Resolve the primary and backup roles from their separate closed authorities.
4. Reject unless the resulting role set is exactly
   `{primary_stop, backup_stop}` with two unique order identities.
5. Build the redacted exact scope and its authority fingerprints.
6. Only after the scope is complete may the dedicated loader perform exact
   `posId` and `ordId` reads.
7. The existing natural-stop proof must still show exactly one owned terminal
   stop caused the full position close and that the other stop cannot claim the
   close.
8. A `position_absent` plan remains local-only: zero exchange writers and zero
   production writes during dry-run.

## Read-Consistency Operation

The previous running-service procedure is not valid on the currently deployed
version because its writers do not maintain the candidate generation table.
The replacement procedure requires a separately approved stopped-service,
read-only diagnostic window:

1. Complete local RED/GREEN work, full regression, and independent review.
2. Obtain a separate approval to stop `telegram-kol.service` for diagnosis.
3. Stop the service and prove there are no in-flight durable operations, other
   local Deepcoin writer processes, or related writer timers.
4. Use a detached worktree at the exact reviewed SHA, a mode-0700 temporary
   directory, a mode-0600 SQLite backup, and the dedicated read-only client.
5. Execute only the allowlisted exact position and protection-history GETs.
6. Create two fresh captures and require identical durable scope, collection
   digests, role authority, natural-stop proof, and semantic fingerprints.
7. Stop on any transport, pagination, identity, chronology, ownership, or
   fingerprint difference.
8. Run no apply, deployment, setting change, production bootstrap, or exchange
   mutation. Restore the unchanged production service before returning the
   redacted result.

Any future apply requires another explicit approval and a new stopped-service
final snapshot. It cannot be run in the same turn as the diagnostic captures.

## Error Handling and Redaction

All failures return bounded safe reason codes. No error, plan, log, test
fixture, or operator record may contain a raw order ID, position ID, client
order ID, provider response, request payload, strategy identifier, or
credential-shaped value.

The resolver fails closed for:

- missing, duplicate, malformed, or noncanonical authority rows;
- wrong binding, leg, position, venue, strategy, instrument, or side;
- wrong or duplicate exchange order identity;
- intent, backup-row, protection-leg, or ledger state conflicts;
- trigger or size mismatch, including an omitted size without explicit
  whole-position authority;
- invalid, oversized, deep, duplicate-key, non-finite, or hostile JSON;
- source or evidence drift after capture;
- incomplete pagination, unavailable exact readers, or transport failure;
- conflicting current pending and terminal history state; or
- any new close mutation, execution event, management submission, or account
  exposure evidence.

## Test Strategy

Development starts with a real RED fixture matching the production topology:
two verified native `stop_loss` ledger rows, where the second is independently
bound to the exact backup intent, active backup row, and verified backup leg.
The old loader must fail and the new resolver must produce the two logical
roles without rewriting the database.

Attack coverage includes:

- each backup authority missing, duplicated, or linked to another owner;
- mismatched order, binding, leg, position, strategy, instrument, or side;
- trigger and size drift in every source;
- omitted size without whole-position `0` authority;
- forged intent/evidence JSON and wrong status combinations;
- duplicate primary or backup role and order reuse;
- post-capture insert, delete, identity, state, and economics drift;
- incomplete exact pagination and both snapshots differing;
- hostile exchange identities, malformed decimals, and credential-shaped text;
- generic reconciliation, protected-entry, backup-stop, and position-mutation
  behavior remaining unchanged; and
- dry-run and `position_absent` apply paths reaching no exchange writer.

Verification requires the focused batch-119 suite, protection-ledger and backup
stop suites, position-mutation and protected-entry suites, CLI regressions, the
complete repository test suite, compileall, diff check, and an independent
review with zero Critical and zero Important findings.

## Completion Boundary

Completing the code and documentation does not authorize a server stop,
production dry-run, apply, deployment, restart, setting change, database
bootstrap, or exchange mutation. Each operational step remains separately
approved, and any refusal returns control without advancing automatically.
