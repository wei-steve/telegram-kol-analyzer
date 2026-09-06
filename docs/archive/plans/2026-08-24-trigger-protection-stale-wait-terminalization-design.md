# Trigger Protection Stale-Wait Terminalization Design

## Goal

Repair two bounded reconciliation defects without changing trading or exchange-write semantics:

1. A protection-history failure for one instrument must not mark an intent for another instrument as `snapshot_incomplete`.
2. The three audited stale intents (`138`, `141`, and `147`) must become terminal when their exact, verified entry legs are already terminal.

This design does not authorize production database apply, push, deployment, restart, cutover, Telegram traffic, or exchange mutation.

## Confirmed Root Cause

`load_deepcoin_execution_reconciliation_snapshot_read_only()` reads every instrument referenced by Deepcoin bindings. Instrument-scoped failures are stored as keys such as `trigger_history:ETH-USDT-SWAP`.

When any snapshot error exists, `_apply_reconcile_snapshot()` enters its global fail-closed branch. `_retry_saved_trigger_protection_intents_for_unavailable_snapshot()` currently copies every protection-related error key to every due, verified intent without checking the intent leg's instrument. This is why the three BTC intents recorded only an ETH history source.

The same retry helper does not exclude terminal legs. The normal adoption path does exclude them, so a terminal saved intent has no path to `resolved` and can remain `retrying / wait` indefinitely.

## Scope Boundary

A generic terminal-leg cleanup would currently match 72 production rows. That historical backfill is explicitly out of scope.

Automatic terminalization is therefore limited to an exact stale-wait predicate:

- intent `recovery_state == "retrying"`;
- intent `recovery_disposition == "wait"`;
- intent `last_reason_code == "snapshot_incomplete"`;
- entry leg is in `TERMINAL_ENTRY_LEG_STATES`;
- leg attribution is `verified`;
- leg has a non-empty `pos_id`;
- intent and leg binding IDs match;
- intent parent trigger ID exactly equals the leg order ID.

The current production database contains exactly three rows matching this predicate: intents `138`, `141`, and `147`. Pending historical terminal intents, failed intents, adopted intents, unverified legs, and terminal legs without the exact stale-wait state remain unchanged.

## Runtime Design

### 1. Instrument-scoped snapshot errors

Add one helper that derives a leg's canonical instrument from `request_json.instId`, with the owning binding symbol as a fail-closed fallback. Add one helper that selects protection snapshot errors relevant to that instrument:

- generic `pending_trigger_orders` or `trigger_history` errors affect every instrument;
- `pending_trigger_orders:<instrument>` and `trigger_history:<instrument>` affect only the exact normalized instrument;
- malformed or unknown scoped keys are not silently reassigned to a different instrument.

The global reconciliation remains fail-closed: any snapshot error still prevents partial adoption during that pass. The change only prevents an unrelated instrument failure from mutating another intent's retry evidence or unavailable metric.

### 2. Exact stale-wait terminalization

Before the global snapshot-error early return, invoke a narrow helper over saved intents and their entry legs. For each exact predicate match, transition the intent to:

- `recovery_state = "resolved"`;
- `recovery_disposition = "terminal"`;
- `last_reason_code = "entry_leg_terminal_after_snapshot_wait"`;
- `next_attempt_at = NULL`;
- bounded versioned evidence containing intent/leg/binding identity, instrument, leg status, terminal reason, and the prior stale reason.

`retry_attempts`, immutable submit evidence, parent order identity, and `adopted_order_id` remain unchanged. A second run is a no-op because `resolved` is outside the predicate.

The helper performs local database convergence only. It never calls a Deepcoin mutation method and does not create a rescue or worker command.

### 3. Error-path retry behavior

The unavailable-snapshot retry helper will:

- skip terminal intents already resolved by the narrow helper;
- select error sources for each remaining leg's exact instrument;
- leave an intent untouched when all errors belong to other instruments;
- preserve current `wait / snapshot_incomplete` behavior and retry-budget rules when the target instrument or a generic source is incomplete.

## RED-to-GREEN Tests

Focused tests will prove, in order:

1. A BTC exact stale-wait intent with a terminal verified leg resolves even when only ETH trigger history fails.
2. Resolution clears `next_attempt_at`, preserves retry attempts and immutable identities, records bounded terminal evidence, and is idempotent.
3. An active BTC intent is not changed and receives no refusal audit when only ETH protection history fails.
4. A BTC-scoped or generic protection snapshot error still produces the existing `retrying / wait / snapshot_incomplete` result.
5. Counterexamples—pending intent, failed/adopted intent, nonterminal leg, unverified leg, missing `pos_id`, parent mismatch, or binding mismatch—do not terminalize.
6. The unavailable metric counts only intents exposed to relevant protection errors.

Each production-code edit follows a witnessed focused RED before the minimal GREEN implementation. After focused compatibility checks, the frozen final production-code candidate receives one complete suite. Any later production-code edit invalidates that suite and requires a new final complete run.

## Production-Copy Rehearsal

After the candidate is frozen:

1. Create a fresh immutable SQLite online backup without pausing production.
2. Verify mode `0600`, SHA-256, `PRAGMA quick_check`, foreign-key count, and before counts/digests for critical tables.
3. On an independent rehearsal copy, invoke the same narrow terminalization helper with a fixed timestamp.
4. Require exactly three changed rows: intents `138`, `141`, and `147`; all other business rows and counts must remain exact.
5. Reapply and require an idempotent zero-row change.
6. Restore the three exact before rows on the copy and require the logical digest and `quick_check` to match the starting state.
7. Preserve the private raw evidence and a concise manifest. Do not build or execute a production apply plan.

Any extra matching row, CAS drift, integrity failure, or unexpected table change invalidates the candidate and stops the turn.

## Rejected Alternatives

### Terminalize every historical terminal intent

This would currently affect 72 rows. It may be semantically desirable, but requires a separate historical audit and L3 authorization.

### One-off exact production CAS for the three rows

This is narrower operationally but leaves the instrument-fanout defect in production code and permits recurrence.

### Per-intent live position-history queries inside reconciliation

This duplicates existing terminal-state authority, increases authenticated GET volume and rate-limit exposure, and creates new incomplete-query failure modes. The audited terminal leg is already the canonical local authority for whether protection recovery remains actionable.

## Acceptance

The local candidate is acceptable only if:

- the exact predicate matches the intended three-row production-copy set;
- unrelated-instrument failures no longer rewrite other intents;
- relevant and generic failures remain fail-closed;
- no exchange-write method is reachable from the new path;
- focused tests, compile checks, diff checks, and one final complete suite pass;
- copy apply/idempotence/restore evidence is exact;
- no push, deployment, restart, production mutation, cutover, or exchange write occurs.
