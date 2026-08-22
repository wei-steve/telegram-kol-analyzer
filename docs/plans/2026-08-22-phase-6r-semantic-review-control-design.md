# Phase 6R Semantic Review Control Design

**Date:** 2026-08-22

**Status:** Approved design; implementation not started

## Goal

Stop the asynchronous DeepSeek semantic disagreement review from running by
default without changing MiMo authority, message recognition, context
resolution, strategy selection, position ownership, automation, or exchange
write semantics.

The feature remains available behind an explicit runtime switch. Existing
`pending` and `failed` semantic reviews are moved to a truthful, compatible
disabled terminal projection through a separately rehearsed L3 operation.

## Why this is a separate prerequisite phase

Phase 6A is already deployed at compatibility Candidate A and paused at its
Task 12 real-sync parity gate. DeepSeek review failures are not part of the
durable worker-command boundary, and the pending Telegram notification
outboxes that block the real sync remain a separate problem.

Phase 6R therefore pauses, but does not modify or reinterpret, Phase 6A. It
must preserve:

- deployed Phase 6A Candidate A
  `f257a93121ba1d547955f0b4dd5a270dd347904d`;
- `message_lock_mode=global`;
- `message_pipeline_mode=queue`;
- `worker_command_mode=shadow`;
- the monolith topology;
- the unresolved 2463 position-attribution and 331 position-protection
  notification gate.

After Phase 6R completes, the canonical pointer returns to the exact Phase 6A
Task 12 checkpoint.

## Current problem

The Web application starts the semantic review supervisor unconditionally.
The loop loads AI provider configuration, claims persisted review work, and
calls DeepSeek even though MiMo has already produced and executed the
authoritative result. An exhausted DeepSeek balance consequently produces
repeated `402 Payment Required` failures and retries.

Simply stopping the loop is unsafe. `RecognitionDecision.comparison_status`
currently carries both short-lived authoritative execution handoff states and
the later semantic review states. New authoritative decisions normally become
`pending` after automation finishes. If the worker is merely stopped, those
rows remain pending and downstream message-operation projection and supervisor
logic treat them as non-terminal.

The switch must therefore control both enqueue eligibility and the final
persisted state.

## Scope

Phase 6R owns only:

- a persisted `semantic_review_enabled` runtime setting, default `false`;
- the policy transition after authoritative automation completes;
- semantic review worker gating and notification suppression;
- the Web setting and disabled-state projection;
- a read-only-by-default CLI for exact historical terminalization;
- TDD, one final full suite, L3 rehearsal, exact deployment, guarded apply,
  rollback proof, and bounded production observation;
- canonical status bookkeeping needed to pause and resume Phase 6A.

It does not:

- disable DeepSeek in context resolution or any other feature;
- change the active MiMo or text/context model configuration;
- top up, rotate, or rewrite provider credentials;
- change recognition prompts or result schemas;
- change strategy, position, automation, notification-routing, or exchange
  semantics outside semantic disagreement review;
- process, send, delete, or mark the existing notification outboxes;
- advance Phase 6A, Phase 6 process separation, Monitor A, DeepSeek incident
  repair, or historical position-management repair.

## Selected architecture

### Runtime switch

Add the strict boolean `semantic_review_enabled` to `TradingSettings`, with a
safe default of `false`. A missing or unreadable settings row resolves through
the existing safe defaults and therefore leaves review disabled.

The existing trading-settings API and page expose one checkbox labelled
“开启 DeepSeek 辅助复核”. Saving unrelated settings must preserve the stored
value. Invalid non-boolean values fail with the existing settings validation
contract.

The semantic review task may remain part of the application lifespan so the
switch can change without a restart, but while disabled it must:

1. check the setting before loading AI provider configuration;
2. claim no review row;
3. call no reviewer;
4. schedule no retry;
5. emit no semantic-review notification.

This design controls only `semantic_disagreement_review`. It must not claim
that all DeepSeek traffic is disabled.

### Authoritative flow

The authoritative flow becomes explicit:

```text
MiMo authority
  -> persist exact authoritative generation
  -> claim and run existing automation
  -> persist exact automation outcome
  -> apply current semantic-review policy
       enabled  -> review pending
       disabled -> compatible disabled terminal state
```

MiMo and the existing automation run before review policy is applied. The
policy never authorizes, blocks, retries, cancels, compensates, or changes an
automation or exchange action.

### Logical and physical state

The application exposes the logical semantic review state
`review_disabled`. The database uses existing compatible columns:

| Meaning | `comparison_status` | `agreement_status` |
|---|---|---|
| Waiting for enabled review | `pending` | `pending` |
| Review executing | `running` | `pending` |
| Review completed | `completed` | existing reviewed value |
| Review failed after retries | `failed` | existing value |
| Review intentionally disabled | `completed` | `review_disabled` |

Using physical `comparison_status=completed` is deliberate:

- existing message-operation consumers already accept it as terminal;
- the semantic worker already ignores it;
- a code rollback to the pre-Phase-6R candidate cannot turn it into a pending
  blocker;
- the new UI can distinguish disabled from agreement or failure using
  `agreement_status`;
- no schema change is necessary.

The old UI may render a disabled row as an unclassified completed review after
a code rollback, but it remains terminal and does not claim agreement. That is
the accepted compatibility degradation.

### New and repeated recognition

When review is disabled, finalizing a newly executed authoritative generation
sets `comparison_status=completed` and
`agreement_status=review_disabled` atomically with the automation outcome.

An authoritative failure remains `authoritative_failed`; it is not relabelled
as review-disabled.

Re-recognition of an unchanged historical disabled decision remains disabled.
Re-enabling the switch does not silently requeue historical rows. A changed
authoritative candidate follows the current switch at the time its automation
outcome is finalized.

### Runtime transition races

The worker reads the switch before claiming and again after a claim but before
calling the provider. If it observes disabled after claiming, it completes the
claimed row as review-disabled through the claim token and does not call
DeepSeek.

An external request already sent to DeepSeek cannot be reliably cancelled. If
the switch is disabled while such a request is in flight, the request may
return, but the worker must re-read the switch before claiming or delivering a
critical notification. It records no new critical notification after observing
disabled.

The initial production cutover is stricter: it requires zero `running` reviews
before deployment/apply. If the count is non-zero after one bounded recheck,
the phase fails closed.

The switch is not a promise that a remote request already in flight is erased;
it is a durable policy preventing future review work and review notifications.

## Historical terminalization

Historical cleanup is a production data mutation and therefore L3 even though
it does not change schema.

Add a read-only-by-default CLI that builds a bounded canonical plan for exactly
the current `pending` and `failed` rows. The plan contains:

- database identity and quick-check result;
- cutoff time;
- counts by status;
- ordered target raw-message ids;
- each target's current status, update timestamp, claim fields, retry fields,
  and a canonical row fingerprint;
- a SHA-256 plan fingerprint;
- `running` count;
- explicit `provider_call_count=0`, `notification_count=0`, and
  `exchange_write_count=0` declarations.

Dry run is the default. Apply requires the exact expected plan fingerprint and
uses one `BEGIN IMMEDIATE` transaction. It refuses when:

- semantic review is enabled;
- any row is `running`;
- the database or target set differs from the plan;
- a target status, timestamp, or claim field drifted;
- the expected plan fingerprint is missing or mismatched.

For each exact target, apply:

- sets `comparison_status=completed`;
- sets `agreement_status=review_disabled`;
- clears `comparison_next_attempt_at`, `comparison_started_at`, and
  `comparison_claim_token`;
- updates `updated_at` to the single apply timestamp;
- preserves authoritative payload, automation outcome, comparison error,
  comparison attempts, any existing auxiliary/comparison audit payload,
  prompt versions, differences, and notification audit fields.

Completed reviews and authoritative failures are untouched. Repeating apply
with the original plan must report zero eligible targets rather than mutating
anything again.

The immutable online backup and the canonical plan are the audit record for
the previous status of every changed row.

## Rollback

There are three separate rollback boundaries:

1. **Runtime off switch:** set `semantic_review_enabled=false`. This stops new
   review work without changing recognition or automation.
2. **Code rollback:** deploy the exact pre-Phase-6R Candidate A. Disabled rows
   remain physically `completed`, so the old worker ignores them and downstream
   consumers do not block.
3. **Historical data rollback:** use a rehearsed targeted rollback plan built
   from the immutable preimage. Restore only rows whose id and post-apply
   fingerprint still match; any drift refuses the whole transaction. Never
   restore the whole live database.

Re-enabling the switch reviews only future eligible generations. Requeueing
historical disabled rows is a separate operation requiring separate explicit
approval.

## UI and observability

The message decision card shows disabled review as:

- label: `辅助复核已关闭`;
- logical status: `review_disabled`;
- no DeepSeek model assertion;
- no “一致”, “失败”, or “等待中” implication;
- no critical alert role.

Production evidence distinguishes semantic-review DeepSeek calls by the
feature/source marker. Acceptance is zero new calls and zero new 402 entries
from `semantic_disagreement_review`; it does not claim zero DeepSeek calls from
other configured consumers.

Record review counts by physical status and logical projection, oldest pending
age, provider calls, 402 errors, notifications, and runtime modes.

## Error handling

- Invalid setting input returns the existing bounded validation error and
  leaves the stored setting unchanged.
- A settings read failure resolves to the safe default: disabled.
- Disabled worker ticks do not load provider configuration, so missing or
  invalid DeepSeek credentials cannot create review-loop errors.
- Enabled review retains the existing bounded retry and terminal failure
  behavior.
- A claimed row that observes the switch disabled is terminalized only through
  its exact claim token.
- Any L3 plan drift, non-zero running count, incomplete evidence, failed backup,
  failed quick check, or unexpected table change fails closed.

## Verification strategy

Development uses focused TDD for every production-code edit. The assembled
production candidate receives exactly one final full-suite run. If production
code changes afterward, affected focused tests and one new final full suite are
required.

Because historical rows are mutated, production preparation includes:

- SQLite online backup;
- rehearsal on a production database copy;
- quick check before/after apply, repeated apply, and rollback;
- before/after counts for all tables;
- targeted hashes for recognition decisions and critical business tables;
- proof that only the exact planned recognition rows change;
- exact targeted rollback rehearsal.

After exact-SHA gated deployment and guarded apply, observe 30 continuous
minutes and at least five real messages, trying to cover two chats. If five
messages do not arrive within 30 minutes, stop at 30 minutes, keep Phase 6R
`in_progress`, and record limited traffic.

Acceptance requires:

- `semantic_review_enabled=false` persisted and readable;
- new successful authoritative decisions project `review_disabled` only after
  their unchanged automation outcome is finalized;
- MiMo authority, recognition payloads, strategy targeting, automation, and
  exchange history show no semantic drift;
- no new semantic-review provider call, retry, notification, or 402;
- historical target counts match the exact plan and `pending=0`, `failed=0`,
  `running=0` for the planned set;
- quick check remains `ok`;
- no unrelated table count/hash change;
- rollback remains evaluable;
- the three runtime modes and monolith topology remain unchanged.

## Security prerequisite

During the preceding read-only investigation, DeepSeek, GLM, and MiMo provider
keys were exposed in local tool output. Local development may proceed, but no
production deployment is allowed until the owner confirms all three keys were
rotated. Phase 6R does not itself rotate or print credentials.

## Completion boundary

On success, Phase 6R records exact design, plan, code, test, backup, rehearsal,
deployment, apply, observation, and rollback evidence, then restores the
canonical pointer to Phase 6A `in_progress`, `claimed_by=null`, at its existing
Task 12 blocker.

Phase 6R completion does not authorize Phase 6A continuation in the same turn.

