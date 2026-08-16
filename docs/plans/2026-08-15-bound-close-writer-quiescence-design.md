# Bound Close Writer Quiescence Design

## Purpose

The stopped-service read-only recovery window cannot start its Deepcoin capture
until the local database proves that no unrelated writer is still current. The
first production aggregate check reported 608 rows because the helper treated
every known historical active or unknown state as a current writer. That is
stricter than the existing deployment preflight and prevents a safe recovery
window without distinguishing stale residue from work that may still be
running.

This design corrects only the dedicated bound-close recovery helper and its
runbook. It does not change `deployment_preflight.py`, lower the deployment
gate, edit production data, replay messages, perform an exchange write, enable
MiMo v2, or reuse a prior capture authorization.

## Production evidence

The approved aggregate-only diagnosis found:

- 608 non-target rows under the old helper's broad rule;
- 513 rows in known active or unknown states older than the official ten-minute
  deployment-preflight window;
- 93 rows in reviewed non-writer states that the helper's closed safe-state map
  had omitted: 2 `restored`, 16 `missing`, and 75 `unbound`;
- 2 rows inside the ten-minute window: one management batch in `reconciling`
  and one management leg in `submitted`;
- 29 exact target `bound_position_close_reservations` rows in `submitted`,
  which are intentionally handled by the separate read-only capture.

No service was stopped and no exchange or database write occurred during this
diagnosis.

## Root cause

The helper currently divides every state into only two buckets: explicitly
safe, or blocking. It does not use each deployment-preflight work specification's
authoritative `time_column` and ten-minute freshness window. It therefore
mislabels known historical residue as evidence of a currently running writer.
Its independently maintained safe-state map also omitted three legitimate
persisted non-writer states.

## Considered approaches

### Mirror deployment-preflight freshness semantics

Selected. Known active and known unknown states use the same ten-minute cutoff
as deployment preflight. Fresh rows block; historical rows remain visible as an
aggregate warning. Unknown future state names, NULL states, malformed times, and
durable Deepcoin execution operations remain fail-closed.

### Prove quiescence only through database stability

Rejected. Two unchanged database snapshots cannot prove that an exchange or
database writer is absent, and a temporarily idle process could resume after
the sample.

### Exclude only Batch 119 or the 29 reservations

Rejected. The recovery window must account for every reviewed writer ledger,
not just the target rows or one prior recovery batch.

## Exact classification contract

The helper continues to read one coherent SQLite snapshot with `mode=ro`,
`PRAGMA query_only=ON`, and an explicit `BEGIN`. It mirrors the complete
`deployment_preflight._WORK_SPECS` table set and accepts only the audited
`_KNOWN_PRIOR_SCHEMA_MISSING_TABLE_SETS`. All five bound-close recovery source
tables remain mandatory.

For every existing work table:

1. A state in the reviewed non-writer set is safe and is not counted as work.
   The map is extended only with the repository-authoritative states
   `strategy_management_legs.restored`,
   `position_backup_stop_orders.missing`, and
   `source_message_deletion_exits.unbound`.
2. For `bound_position_close_reservations`, exactly the five target states
   `reserved`, `submitted`, `submit_unknown`, `unknown_exchange_outcome`, and
   `recovery_required` are excluded from unrelated-writer counts and are
   reported only through `target_reservation_count`. `confirmed` remains safe.
3. A state listed in the table's authoritative `active_states` or
   `unknown_states` is fresh when its authoritative time is greater than or
   equal to `checked_at - 10 minutes`. It contributes to
   `fresh_active_or_unknown_writer_count` and blocks.
4. The same known state strictly before the cutoff contributes to
   `historical_active_or_unknown_residue_count`. It is warning evidence and
   does not by itself block this stopped-service window.
5. `deepcoin_execution_operations` is the exception already declared by
   deployment preflight: any state outside its three exact safe terminal states
   blocks regardless of age, including NULL and a future status.
6. In every other table, NULL or an unrecognized state name blocks regardless
   of age and contributes to `unrecognized_or_null_state_count`. A future
   software state cannot silently age into safety.
7. A missing, non-text, malformed, or non-UTC authoritative timestamp on a
   known active/unknown row is fail-closed. The repository's canonical naive
   SQLite datetime is interpreted as UTC; an explicitly zoned value must be UTC.
   A future timestamp and a timestamp exactly at the cutoff are both fresh and
   block. Timestamp parsing and row inspection are bounded; overflow is an
   error, never a partial count.

The returned canonical JSON is aggregate-only and contains exactly bounded
integer counts plus `schema_version` and `status`. It includes:

- `checked_table_count`;
- `missing_table_count`;
- `target_reservation_count`;
- `fresh_active_or_unknown_writer_count`;
- `historical_active_or_unknown_residue_count`;
- `unrecognized_or_null_state_count`;
- `block_regardless_of_age_writer_count`;
- `blocking_writer_count`;
- `schema_version`;
- `status`.

No table name, state value, timestamp, database identifier, order/position id,
provider row, credential, path, or raw error is emitted. `status` is `ready`
only when the target count is between 1 and 64 inclusive and every blocking
count is zero.

## Stopped-window sequence

The reviewed inventory of legacy monitor, Phase One monitor, runtime, main,
socket, and dynamically discovered DB-stage units remains unchanged. The
runbook must:

1. save exact installed/absent and active/inactive state;
2. reject initially active transient oneshots;
3. stop timers first, then services and the socket;
4. repeat unit, dynamic-inventory, process, production-SHA, and database-identity
   checks before every quiescence sample;
5. poll the helper on a bounded interval until it returns `ready`, because the
   two legitimate fresh facts can become historical only after their writers
   have been proven stopped;
6. use a 12-minute absolute wall-clock deadline and a short bounded interval,
   rather than an unconditional ten-minute sleep;
7. record only the final aggregate helper projection; and
8. restore the exact original unit state on ready, refusal, timeout, signal, or
   any intermediate failure.

No Deepcoin request or recovery capture may start while the helper is refused or
errored. A timeout, changing unit inventory, reactivated process, changed
database identity, malformed timestamp, unknown/NULL state, or all-age blocker
restores services and ends the window without capture.

## Verification

TDD coverage must include:

- known active and known unknown states just before, exactly at, and just after
  the ten-minute cutoff;
- future, malformed, NULL, and unrecognized states;
- all-age Deepcoin operation blocking;
- the three newly reviewed non-writer states;
- the exact five target reservation states and target population limits;
- audited prior-schema missing sets and required-source-table refusal;
- a production-shaped aggregate fixture that classifies 513 historical rows,
  93 newly recognized safe rows, 2 fresh blockers, and 29 target rows;
- canonical aggregate-only output and error redaction;
- condition polling that reaches ready only after the cutoff;
- timeout and every failure path running postchecks and the restore trap before
  exit; and
- proof that exchange capture commands are unreachable until readiness.

After focused and full local verification and an independent Critical/Important
review, the revised SHA must be pushed. Because the executable SHA changes, the
previous stopped-service read-only authorization is consumed and cannot be
reused. Production work stops again and requests the exact authorization token
for the new reviewed SHA.
