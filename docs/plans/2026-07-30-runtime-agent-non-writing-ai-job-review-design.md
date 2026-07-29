# Runtime Agent Non-Writing AI Job Reschedule Review

## Review Outcome

**Rejected before implementation.** The current production system has no
durable AI-job source that is both outside recognition/contextual resolution
and proven unable to initiate a business write. The playbook
`reschedule_non_writing_ai_job` therefore remains
`executor_not_configured`.

No runtime code, feature flag, database row, or production service was changed
by this review.

## Reachable Source Analysis

### Context-resolution exhaustion

`capture_context_worker_state` emits `context_worker_exhausted` only after the
authoritative worker has exhausted its retry budget. The emitted summary
contains the worker kind, exhausted status, error type, reason code, and raw
message label. It does not contain `business_write_owned: false`, so the
deterministic execution policy already refuses this source.

More importantly, the production Web worker reprocesses a scheduled context
attempt through `process_authoritative_message` with the live
`auto_trade_executor`. Requeuing an exhausted attempt can therefore reach
strategy automation and a business write. It is not a non-writing AI job.
Giving the Runtime Agent a parallel requeue path would also duplicate the
authoritative contextual worker and weaken the permanent contextual-resolution
boundary.

### Semantic-review provider exhaustion

The only production call to `capture_provider_failure` is the semantic
disagreement review loop. Its incident source is `semantic_review` and its
source record ID is the raw message ID. When the retry budget is exhausted,
the durable `RecognitionDecision` is left with:

- `comparison_status: failed`;
- no claim token;
- no next-attempt timestamp;
- the completed attempt count.

This is a genuine terminal source state. Production read-only inspection found
two such historical rows, both with three attempts and no active claim or
schedule. However, rescheduling them requires changing
`RecognitionDecision.comparison_status` and its retry fields. That is a
recognition-state mutation, which Phase 6 explicitly prohibits. The provider
incident adapter also does not emit `business_write_owned: false`, and the
current `get_worker_state` projection has no semantic-review source resolver.

Historical reachability is therefore not sufficient to authorize this action.

## Considered Approaches

### 1. Requeue exhausted contextual-resolution attempts

Rejected. The authoritative callback is configured with the live auto-trade
executor and can reach business execution. This approach would duplicate
contextual-resolution authority and violate the no-business-write proof.

### 2. Reset failed semantic reviews to pending

Rejected for Phase 6. The source is durable and terminal, but the update would
mutate the recognition ledger and re-enter a recognition component. Runtime
Agent Phase 6 is expressly barred from recognition mutations.

### 3. Introduce a dedicated non-writing AI-job queue

Potentially safe in a future design. Such a queue would need its own durable
model, exact ownership field, retry contract, source adapter, verification
projection, and proof that its callback cannot reach recognition, contextual
resolution, or a business mutation.

This is not selected now because no such production queue exists. Creating it
solely to make the catalog playbook executable would expand the architecture
and operational surface without a current runtime source.

## Deterministic Safety Result

The existing fail-closed behavior remains authoritative:

- the production action-handler registry does not inject
  `reschedule_non_writing_ai_job`;
- missing handler authority returns `executor_not_configured` before a recovery
  reservation;
- the execution policy also refuses current production adapter summaries
  because `business_write_owned: false` is absent;
- the Runtime Agent does not reset a contextual attempt or
  `RecognitionDecision`.

The reviewed corpus may continue to exercise hypothetical shadow selection.
Shadow nomination is not proof that a production handler is safe or reachable.

## Phase 6 Conclusion

Phase 6 has four reviewed, deployed handlers with positive isolated canaries:

- `build_read_only_reconciliation_plan`;
- `refresh_read_only_exchange_snapshot`;
- `rerun_production_audit`;
- `fetch_missing_telegram_evidence`.

The other two catalog candidates have now been explicitly rejected:

- `recover_stale_side_effect_free_claim` duplicates an authoritative stale
  claim path and depended on an unreachable source state;
- `reschedule_non_writing_ai_job` has no current source that satisfies both the
  non-writing proof and the recognition/contextual-resolution boundary.

No further low-risk Phase 6 handler is approved. Optional Phase 7 remains
blocked until the user gives the separate explicit approval required by the
canonical rollout plan.
