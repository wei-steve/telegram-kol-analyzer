# Entry Assembly Fingerprint Synchronization Design

## Goal

Fix the isolated fingerprint divergence in the recovery-trigger entry path
without replaying a Telegram message, changing an exchange order, or weakening
the production safety monitor.

The forward path must persist one finalized entry-assembly fingerprint in both
the pending trade signal and the later immutable execution binding before any
exchange submission. The one already-submitted production mismatch is handled
by exact append-only reconciliation evidence rather than rewriting historical
execution payloads.

## Incident Summary

Natural message `raw_messages.id = 9882` exercised the recovery-trigger branch
where `auto_draft` is absent. The branch performed these writes in this order:

```text
enqueue recovery trade signal with pre-finalization assembly evidence
  -> finalize entry assembly with bounded order draft
  -> submit the already-persisted trade signal
  -> create execution binding from the stale payload
```

The finalized assembly has fingerprint
`314f10a32e2225779ccafabdff6ffb3e836b02188b3c3177c4dee4abdb38468d`, while
trade signal `398` and execution binding `266` retain the exact
pre-finalization fingerprint
`a58c3227245cb691257321bd193a499a6e896b02dc0142c16a14e1497eb07f10`.
The production monitor correctly reports
`live_entry_preamble_binding_evidence_missing`.

The normal `auto_draft` branch already finalizes the assembly before enqueueing
the trade signal and is not defective. This change is deliberately limited to
the recovery-trigger branch and shared safety primitives needed to prove its
write ordering.

## Constraints

- Do not replay or re-recognize the source Telegram message.
- Do not submit, cancel, replace, or modify any Deepcoin order.
- Do not rewrite an already-submitted trade signal or execution binding.
- Do not suppress, baseline, or globally ignore the monitor invariant.
- Do not broaden strategy recognition, targeting, or contextual resolution.
- Fail closed before the first exchange write if the final evidence cannot be
  proven in durable storage.
- Keep the repair bounded to an explicitly named assembly and binding.

## Decision

Use two related but separate mechanisms:

1. **Forward synchronization:** after the recovery draft is known, finalize the
   assembly and compare-and-set the still-pending trade signal payload to the
   final assembly evidence. Reload and validate the signal before calling the
   live submitter.
2. **Historical reconciliation:** create one deterministic append-only
   `ExecutionEvent` proving that the immutable binding fingerprint is the exact
   pre-finalization form of the current finalized assembly. The monitor accepts
   a mismatch only when this complete evidence is present and valid.

This preserves immutable execution history while restoring a mechanically
checkable relationship between the old snapshot and the final assembly.

## Forward Synchronization

### Final Evidence Source

`finalize_adjacent_entry_assembly_draft()` remains the sole writer and hasher of
the finalized assembly. It will return a bounded result containing:

- assembly ID;
- strategy instance ID;
- original fingerprint;
- final fingerprint;
- finalized evidence needed by the trade-signal payload.

The function remains idempotent. If the same bounded draft has already been
attached, it returns the persisted final result. If the draft differs, it raises
`entry_assembly_draft_conflict`.

### Pending Signal Compare-and-Set

Add a helper in `trade_signals.py` that accepts:

- exact trade-signal ID;
- exact strategy instance ID;
- expected serialized payload read at enqueue time;
- expected pre-finalization assembly fingerprint;
- final assembly evidence and fingerprint;
- update timestamp.

Inside one database transaction it must:

1. load the exact row;
2. require `status == "pending"`;
3. require the strategy instance ID to match;
4. require the durable `payload_json` to equal the expected serialized payload;
5. require the top-level and nested draft assembly evidence, when present, to
   carry the expected pre-finalization fingerprint;
6. write the same final evidence to both
   `payload.entry_preamble_assembly` and
   `payload.deepcoin_order_draft.entry_preamble_assembly`;
7. update by ID, pending status, strategy ID, and exact old `payload_json`;
8. require exactly one affected row;
9. reload the row and prove both fingerprints equal the final fingerprint.

Any missing evidence, malformed payload, concurrent status transition, payload
change, strategy mismatch, or row-count mismatch raises a fixed synchronization
error. The caller must not invoke `process_trade_signal_live()` after that
error. This compare-and-set boundary matters because the exchange submitter
loads the signal in a new transaction.

### Recovery-Trigger Ordering

The corrected branch is:

```text
enqueue recovery trade signal (pending)
  -> derive and validate recovery draft
  -> finalize assembly using that exact draft
  -> CAS both pending-signal evidence locations to final evidence
  -> reload/assert pending signal
  -> process trade signal live
```

The finalization and signal synchronization are separate transactions because
the existing recovery gate creates the trade signal first. Safety does not
depend on atomicity across them: an interruption leaves a pending, unsubmitted
signal, and retry either completes the idempotent synchronization or fails
closed. Exchange submission occurs only after both durable rows agree.

The existing `auto_draft` branch remains structurally unchanged except for any
necessary adaptation to the richer finalization return type.

## Historical Reconciliation

### Why History Is Not Rewritten

Execution binding `266` is a snapshot of the payload that was actually used to
submit orders. Replacing its fingerprint would make the record look as though
the final evidence existed at submission time. The submitted trade signal is
retained for the same reason.

### Repair Planner

Create a pure bounded planner that receives exact assembly and binding IDs. It
loads the assembly, binding, and linked trade signal, then validates:

- the assembly is a V2 adjacent-entry assembly with finalized
  `order_draft_snapshot` and `final_entry_leg_count`;
- the binding and assembly have the same non-empty strategy instance ID;
- the binding contains a non-empty old assembly fingerprint;
- removing only `order_draft_snapshot` and `final_entry_leg_count` from the
  finalized assembly evidence and hashing canonical JSON reproduces the old
  binding fingerprint exactly;
- hashing the full canonical assembly evidence reproduces the current assembly
  fingerprint exactly;
- the binding identity, source message, side, symbol, strategy instance, and
  bounded submitted entry-leg identity agree with the finalized draft;
- the linked trade signal, when present, carries the same old fingerprint;
- no conflicting reconciliation event exists.

The planner emits a redacted action with IDs, old and final fingerprints, and a
deterministic repair fingerprint. It contains no API credentials, Telegram
content, or full exchange response.

The repair fingerprint is SHA-256 over canonical JSON containing:

```json
{
  "policy_version": "entry-assembly-fingerprint-reconciliation-v1",
  "assembly_id": 2,
  "execution_binding_id": 266,
  "trade_signal_id": 398,
  "strategy_instance_id": "...",
  "old_fingerprint": "...",
  "final_fingerprint": "..."
}
```

### Append-Only Event

Explicit apply inserts exactly one `ExecutionEvent` with:

- `action = "entry_assembly_fingerprint_reconciled"`;
- `status = "resolved"`;
- exact binding, trade-signal, and strategy IDs;
- `reason = "pre_finalization_payload_preserved"`;
- `before_json` containing bounded old evidence;
- `after_json` containing bounded final evidence and policy version;
- the deterministic repair fingerprint in `notification_fingerprint` solely as
  the table's existing unique idempotency key;
- `notification_status = NULL`, so the event is never a notification job.

Apply rebuilds the plan from the live database and requires an operator-supplied
expected repair fingerprint. A unique-key race is treated as idempotent only if
the existing row matches every expected field; otherwise it is a conflict.

No schema migration is needed. Reusing the existing unique fingerprint column
keeps this isolated repair append-only and race-safe, while null notification
status prevents notification delivery.

### CLI Contract

Add a command shaped as:

```text
telegram-kol repair-entry-assembly-fingerprint \
  --database-path <path> \
  --assembly-id <id> \
  --execution-binding-id <id> \
  [--apply --expected-plan-fingerprint <sha256>]
```

Without `--apply`, it is read-only and prints one redacted JSON plan. Apply is
refused unless the exact current plan contains one action and its fingerprint
matches the supplied value. The command contains no Deepcoin client, Telegram
client, or recognition dependency.

## Monitor Semantics

`read_entry_preamble_invariants()` continues to compare finalized assemblies
with binding payload evidence. For a mismatch, it may query
`execution_events` only if that table is present.

The mismatch is considered reconciled only when exactly one event:

- has the fixed action and `resolved` status;
- identifies the exact assembly, binding, trade signal, and strategy instance;
- carries the observed old and current final fingerprints;
- carries the expected policy version;
- has a repair fingerprint that recomputes exactly;
- corresponds to an old fingerprint that can be recomputed from the current
  assembly evidence by removing only the two finalization fields.

Missing, malformed, duplicate, or conflicting evidence leaves
`live_entry_preamble_binding_evidence_missing` active. A repair event cannot
authorize a different assembly or hide a future mismatch.

Older test databases without `execution_events` retain the current strict
behavior.

## Error Handling

- Forward payload synchronization fails closed before exchange submission.
- A finalized assembly plus an unsynchronized pending signal is recoverable by
  idempotent retry; it is never auto-submitted on uncertain evidence.
- Historical dry-run fails if any relationship cannot be derived exactly.
- Historical apply fails on plan drift, wrong expected fingerprint, non-unique
  scope, or conflicting prior evidence.
- The monitor remains unhealthy until a valid event is durably committed.
- No error path invokes Telegram replay or an exchange write.

## Testing

### Forward Path

- reproduce the recovery-trigger stale-fingerprint defect with a production-
  shaped test;
- prove finalization happens before the mocked first exchange call;
- prove top-level and nested pending payload fingerprints are identical;
- prove the execution binding receives the final fingerprint;
- prove pending-status drift, payload drift, wrong old fingerprint, and strategy
  mismatch each prevent the exchange call;
- preserve existing normal `auto_draft` behavior and retry idempotency.

### Repair

- exact valid mismatch produces one deterministic plan;
- old fingerprint not derivable from final evidence produces no actionable
  plan and a fixed reason;
- identity or entry-leg mismatch is rejected;
- apply requires the exact fingerprint and creates one event;
- repeated apply is idempotent;
- conflicting event is rejected;
- dry-run and planner perform no writes;
- CLI smoke tests cover dry-run, apply refusal, and exact apply.

### Monitor

- current mismatch still alerts without an event;
- an exact valid event clears only that mismatch;
- wrong binding, assembly, strategy, old fingerprint, final fingerprint,
  policy version, event status, or repair fingerprint still alerts;
- a future unrelated mismatch is not hidden;
- monitor access remains query-only.

## Deployment and Repair Sequence

1. Run focused and full local tests.
2. Review and push the forward fix and repair tooling.
3. Prove a safe production window with no time-sensitive strategy operation.
4. Pull the reviewed commit, reinstall the editable package, and restart the
   service.
5. Run focused server tests and verify service/monitor identities.
6. Run the repair command in dry-run mode for assembly `2` and binding `266`.
7. Compare the returned IDs and both fingerprints with the incident evidence.
8. Request explicit approval for the production append-only write.
9. Apply using the exact dry-run fingerprint.
10. Rerun the no-notify monitor and verify the invariant is healthy.
11. Observe subsequent natural recovery-trigger entries and verify assembly,
    signal, and binding fingerprints match.

Steps 6 through 9 do not restart services or call Deepcoin. If a safe restart
window cannot be proven, local work may finish but deployment waits.

## Rollback

Code rollback returns to the preceding reviewed commit. It does not delete the
append-only repair event and does not require a database downgrade.

Before the repair event is applied, rollback leaves the monitor alert active.
After apply, older monitor code ignores the event and will report the original
mismatch again; this is safe and visible. No rollback path rewrites an order,
binding, trade signal, or assembly.

## Success Criteria

- every new recovery-trigger entry has one final fingerprint in the assembly,
  pending signal, submitted signal, and execution binding;
- synchronization failure results in zero exchange calls;
- the submitted production history remains byte-for-byte unchanged;
- one exact append-only event explains the known historical divergence;
- the production monitor clears only after verifying that event;
- no replay, duplicate order, notification, or exchange mutation occurs.
