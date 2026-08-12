# MiMo v2 Safe Rebuild Design

## Status

Approved on 2026-08-11. The rebuild starts from Task 5 commit `5a5f9fa` in an
isolated worktree. The production branch and server remain unchanged and keep
`mimo_contract_mode=v1` until the later isolated replay and activation gates
are separately approved.

## Goal

Reimplement Tasks 6 through 11 in order, preserving the useful MiMo v2
contract and attempt audit from Tasks 1 through 5 while closing the safety and
observability gaps found in the Hermes implementation.

## Boundaries

- Do not reset, rewrite, or redeploy the production branch while rebuilding.
- Do not enable MiMo v2 or change the production activation watermark.
- Keep the existing authoritative generation and execution claim as the only
  automatic-trading ownership mechanism.
- `MimoRecognitionRun.became_authoritative` records result selection only; it
  never grants permission to execute.
- Do not change established executor behavior merely to make v2 pass a test.
  A demonstrated executor defect requires a separate decision.
- Make each task test-first, independently reviewable, and independently
  revertible.

## Architecture

### 1. Immutable analysis input

The v2 analysis input fingerprint covers both:

1. the current message and media fingerprint; and
2. the exact composed context sent to MiMo.

The coordinator owns the context snapshot. After MiMo returns and before
evidence persistence, it rebuilds the context from current database state and
compares the resulting joint fingerprint with the run fingerprint.

If the context changed for the first time, the result is discarded and the
whole v2 analysis is retried once with the newly built context. The retry is a
new audited run linked to the stale run. A stale run cannot become evidence,
create candidates, or claim execution.

If the context changes again during the one allowed whole-run retry, processing
ends with `input_changed_during_analysis`. It persists only terminal audit
facts and produces no automatic-trading side effect.

Immediately before `claim_authoritative_execution`, the coordinator rebuilds
and checks the same joint fingerprint again. This closes the window between
evidence finalization and execution ownership. A mismatch retires/refuses the
generation and safely stops; it does not apply the stale candidate.

Dynamic context includes every database fact actually used by the prompt,
including prior messages, current evidence and active strategy lifecycle data.
The comparison must use the exact prompt composition context, not a separately
approximated subset.

### 2. Source-separated evidence

Task 6 persists validated v2 evidence without mixing source types:

- text evidence remains text evidence;
- every image has an immutable media asset reference, observed text, summary,
  extracted fields, quality and confidence;
- the normalized intent result remains separate from raw source evidence;
- no image bytes, provider credentials, or complete provider response are
  stored in evidence rows.

Evidence persistence is claim-bound and atomic. It verifies message ownership,
media ownership, the completed run, canonical fingerprints and the current
message/media fingerprint. Context freshness remains a coordinator gate because
it depends on the complete prompt input rather than only the evidence row.

### 3. Future-only activation and atomic circuit breaker

The default mode stays `v1`. The only accepted modes are `v1` and
`v2_live_adapter`; shadow mode is not introduced. v2 applies only to messages
strictly above an explicit future watermark.

Circuit updates use a SQLite write transaction that serializes singleton
read-modify-write operations. Three simultaneous transport failures must be
recorded as three failures and open the circuit. Contract or adapter failures
open it immediately. Business outcomes and safety refusals do not count as
technical failures. An open circuit routes future eligible messages through
the existing v1 path and never replays an already processed message.

### 4. Existing authority and fallback

Task 8 connects v2 to the current authoritative coordinator without adding a
second execution path. Strict v2 parsing and adaptation finish before evidence
or candidates are written. Eligible technical failures may fall back once to
v1 only before execution ownership is claimed. There is no fallback after a
claim, and no v1/v2 double execution.

The coordinator records the joint input fingerprint used by the selected
result and carries it into the authoritative assessment so the final claim can
recheck it.

### 5. Execution equivalence

Task 9 proves behavior through the established execution path for:

- entry;
- cancel pending entry;
- full exit;
- partial exit and partial take profit;
- move stop to protect;
- hold/no-op updates;
- strategy revision/replacement; and
- a supported multi-action message.

For v1 and v2-adapted inputs, tests compare candidates, instruction ordering,
lifecycle and binding ownership, risk budgets, drafts, idempotency keys, fake
Deepcoin request bodies, and skip/block/defer reasons. Only nondeterministic IDs
and timestamps may be normalized. Safety refusals must remain refusals.

### 6. Web truth model

The Web projection presents two independent records:

- **Current authoritative result:** the canonical result and evidence actually
  selected by the system.
- **Latest MiMo call:** the newest run and attempt status, including failures,
  retries and error reasons even when an older authoritative result remains
  current.

The page does not infer intent from source prose. It displays validated stored
enums and structured evidence. Invalid stored v2 payloads are quarantined from
semantic rendering. Current v1 and historical pre-audit v1 records remain
clearly labeled. MiMo analysis and image evidence appear before system
acceptance, automatic-trading outcome and the collapsed DeepSeek review.

## Failure Handling

- First joint-input change: discard and retry the whole analysis once.
- Second joint-input change: terminal safe failure, no candidate or execution.
- Missing/unreadable image: terminal `image_unavailable`; no orphan running run.
- Provider transport/JSON failure: bounded provider attempts, audited failure,
  then guarded v1 fallback if still before execution claim.
- Contract/adapter failure: audited failure, immediate circuit open and guarded
  pre-claim fallback.
- Evidence claim/fingerprint mismatch: refuse persistence.
- Final pre-claim joint fingerprint mismatch: refuse execution ownership and
  return a safe non-executing result.
- Web projection failure: show the projection error and raw runtime facts, not
  unvalidated semantic content.

## Verification Strategy

Each task begins with a regression that fails for the expected reason. Focused
tests run after the minimal implementation, followed by the relevant executor
and Web suites. Before integration, run the full local suite and an independent
code review.

Server work remains read-only until the rebuilt Tasks 6 through 11 are reviewed.
Task 12 is a later, separately approved isolated replay. Production continues
using v1 throughout this rebuild.

## Rollback

The rebuild branch is isolated from the production branch. Abandoning it
requires no production change. After eventual integration, rollback remains a
future-only setting change to `mimo_contract_mode=v1`; it does not replay,
delete or reinterpret messages and does not disturb existing position
management state.
