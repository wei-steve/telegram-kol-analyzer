# AI Context Resolution Observability and Network Backoff Design

## Scope

This work adds direct context-resolution observability and changes only the timing of retrying provider network failures. It does not change `requires_context_resolution()`, its eight triggers, any prompt/window input, any whitelist or setting, or any decision/application rule.

## Pre-implementation timing audit

- `ContextResolutionAttempt.next_attempt_at` and worker comparisons use UTC datetimes. The durable worker is called once per `LifecycleMonitor` cycle; the production/default cycle is 60 seconds.
- Initial context resolution currently makes at most two provider requests and marks the row `exhausted` on the second failure. Durable context reanalysis currently defaults to `max_attempts=3` and exhausts when the stored attempt count is already greater than or equal to that bound before another worker retry.
- The 15-minute `authoritative_gap_recovery_max_age_minutes` gate belongs to raw messages that have no `RecognitionDecision`, and to expiry of message-processing claims. A context-resolution failure is persisted as an authoritative fail-closed decision, so a pending context retry is not selected or expired by that raw-message recovery query. The context retry worker has no age TTL. It separately checks current eligibility, terminal instructions and context fingerprints before reanalysis.
- Exact timestamps for each historical provider request were not persisted. Therefore the actual request-to-request interval for the 174 `completed, attempts=2` rows cannot be recovered exactly. The strongest available proxy is the attempt row's first insert (`created_at`) to last update (`updated_at`). Across all 174 rows that proxy is P50 8.777 seconds, P90 56.444 seconds and max 47,124.634 seconds. Four rows contain later reanalysis events and contaminate that envelope. Restricting to the 170 rows without `trigger_event_json`, the proxy is P50 8.362 seconds, P90 50.230 seconds and max 153.399 seconds; none exceeds 15 minutes.
- The conservative default initial backoff is 5 seconds. With the 60-second worker cadence, an uncontended retry is due and selected in approximately 5-65 seconds. Context retry has no age expiry, so zero of the 174 rows falls outside a context retry age window. The 15-minute raw-message recovery cutoff does not apply. Because exact per-request timestamps are unavailable, this is a scheduler-contract proof plus a row-timestamp proxy, not a reconstructed provider trace.

## Change A: additive observability

Add five nullable columns to `context_resolution_attempts`:

1. `invocation_triggers_json`: ordered closed-list JSON containing only the eight pre-call deterministic triggers.
2. `attempt_phase`: `initial_resolution` or `reanalysis` for the provider phase being recorded.
3. `provider_request_count`: actual full context provider calls, separate from legacy `attempts`.
4. `provider_usage_json`: one ordered entry per full provider call. Each entry contains the provider's original `usage` object when present, otherwise an explicit unavailable marker. Bytes are never reported as tokens.
5. `request_component_bytes_json`: UTF-8 canonical-JSON byte counts for total request, `message_context`, `reply_chain`, `active_strategies`, current message and remainder.

All columns are nullable and added through the existing SQLite compatibility migration map. Existing readers do not branch on them. Missing or null observability cannot fail recognition or alter a cached decision. Rollback to the control release leaves the nullable columns in place. Physical removal requires a stopped-service database-copy rehearsal/restore and is not the normal rollback.

## Change B: durable network retry and circuit

Only provider-call exceptions currently classified as `network_error` change timing. The first failure persists `retry_pending`, keeps the full request and records `next_attempt_at` using exponential backoff. The existing durable context worker claims the due row. An initial-resolution row has an effective maximum of two actual provider requests, preserving the existing bound; reanalysis keeps its existing bound.

Circuit state is process-local and keyed by provider base URL plus model. Consecutive failures open the circuit. While open, due rows remain durable and are rescheduled without incrementing `attempts` or `provider_request_count`. After cooldown, exactly one half-open request is admitted. Success closes the circuit; network failure reopens it. No circuit state produces `no_action`, a decision, or a silent terminal result.

Defaults are conservative and environment-configurable without modifying production settings in this phase:

- initial backoff: 5 seconds;
- maximum exponential backoff: 60 seconds;
- consecutive-failure threshold: 3;
- open interval: 120 seconds.

The isolated-error path therefore still issues the second full request, normally within 65 seconds including scheduler cadence. A retry uses the persisted request/fingerprint contract; if current context or execution state makes application unsafe, existing eligibility, terminal-instruction and fingerprint gates remain authoritative and fail closed.

## Verification

Change A uses RED/GREEN tests for migration compatibility, ordered triggers, phases, usage available/unavailable, six byte components, null compatibility and unchanged cached decisions. Change B uses deterministic clocks and no real waiting to prove isolated second-request success, circuit opening, exponential scheduling, half-open single admission, no request loss, unchanged decisions and unknown-not-no-action behavior.

The two commits receive separate exact-base reviews. One full repository suite runs after the final production-code change. Deployment uses a production-copy schema rehearsal, fresh immutable stage, exact installer rerun and exact unit-file hashes before one authorized activation.
