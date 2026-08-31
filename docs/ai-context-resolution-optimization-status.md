# AI Context Resolution Optimization Status

```yaml
workstream: ai_context_resolution_observability_and_network_backoff
phase_state: in_progress
current_phase: immutable_deployment
claimed_by: codex_current_session
base_sha: 387f638ba4afec26c106795724dcb27becdf30a7
change_a_sha: b1385ba4ab305d1406bea28bf12f987cbf5db546
change_b_sha: self
pushed_sha: null
production_sha_before: 6e2321cecbb3adf61d7a5972d391e662d4aea300
production_sha_after: null
source_mode: immutable
entry_admission_frozen_expected: false
auto_trade_enabled_expected: true
```

## Scope contract

- Add direct nullable observability to `context_resolution_attempts`.
- Replace only immediate provider network-error retries with durable backoff and a provider-keyed circuit.
- Do not change triggers, words, thresholds, context windows, whitelist/settings, prompts, first-pass inputs or trading semantics.
- Do not replay messages, reconcile/terminate lifecycle state or write to Deepcoin.

## Pre-implementation timing evidence

- Durable `next_attempt_at` uses UTC; the context worker runs once per default 60-second lifecycle-monitor cycle.
- Initial provider bound is two requests. Durable reanalysis defaults to three attempts and exhausts when the stored count has reached that bound.
- The message-processing/recovery 15-minute maximum age applies to raw messages missing authoritative decisions and expired message-job claims. A context failure leaves a fail-closed decision and its retry is handled independently; context retry has no age TTL.
- Exact per-provider-call timestamps were not historically persisted. For 174 completed rows with `attempts=2`, row insert-to-update proxy P50/P90/max is 8.777/56.444/47,124.634 seconds. Four have later reanalysis events. The 170-row no-trigger-event cohort is 8.362/50.230/153.399 seconds, with zero over 15 minutes.
- Default initial backoff is 5 seconds. With 60-second polling, uncontended due-to-send latency is approximately 5-65 seconds. No context retry age window exists, so the configured default puts zero rows outside such a window. This is a scheduler-contract conclusion; exact historical request intervals are unavailable.

## Rollback contract

Code rollback returns all services to the control release while leaving the five additive nullable columns in place. Old code ignores them. Physical column removal is not part of normal rollback and would require a stopped-service production-copy rehearsal and restore.

## Evidence log

- Design approved with an independent status file; the completed recovery ledger remains untouched.
- No production code, settings, database, service or exchange state had been changed when this file was created.
- Change A RED: the four exact observability tests failed at collection because `ContextProviderResult` and the five schema/write paths did not exist.
- Change A GREEN: the four exact tests passed; the focused migration/context/authority/replay/worker regression set passed `115 passed in 6.15s`.
- Change A exact-base review used base `387f638ba4afec26c106795724dcb27becdf30a7`. It confirmed the change is additive, no decision branch reads a new field, trigger order is passed from the already-computed tuple, provider usage is never inferred from bytes, and null telemetry retains cached-decision behavior. No Critical, Important or Minor finding remained before commit.
- Change A was committed independently as `b1385ba4ab305d1406bea28bf12f987cbf5db546`.
- Change B RED proved two pre-implementation gaps: a legacy retry row with nullable observability columns collapsed its provider request count from two to one, and a reanalysis network failure left both the source generation and the new generation queued. The exact tests failed with those two mismatches before the production fixes.
- Change B GREEN preserves legacy request numbering with an explicit `legacy_provider_usage_unavailable` entry and supersedes the source generation when the resolver has already persisted a new durable retry. The exact two tests passed, followed by the focused context/worker/replay/authority/migration set: `157 passed in 8.53s`.
- The required isolated-network-error case schedules request two durably, sends it on the next worker cycle at 65 seconds in the deterministic test, completes successfully, and yields the same decision object as the no-error baseline. It remains below 15 minutes even though context retry has no dependency on that message-recovery age gate.
- Change B exact-base review used Change A commit `b1385ba4ab305d1406bea28bf12f987cbf5db546`. The review confirmed provider-key isolation, a single half-open probe, no provider-call/count increment while the circuit is open, no conversion of unknown to no-action, no silent durable-row deletion, and unchanged immediate retry behavior for non-network contract correction. No Critical, Important or Minor finding remained.
- Final candidate full suite after the last production-code edit: `6749 passed, 4 skipped, 32 warnings in 413.26s`. The warnings are existing deprecation warnings; there were no failures.
- `change_b_sha: self` means the commit containing this status record; it avoids a circular self-hash while preserving the required two-commit boundary. Deployment evidence must replace `pushed_sha` and `production_sha_after` only if a later documentation-only status commit is explicitly permitted; otherwise the exact pushed/deployed SHA remains authoritative in the immutable receipt and final handoff.
