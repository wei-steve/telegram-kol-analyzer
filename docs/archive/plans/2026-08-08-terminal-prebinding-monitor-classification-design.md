# Terminal Pre-Binding Monitor Classification Design

## Status

Approved by the operator on 2026-08-08 as the narrow prerequisite for
resuming the Phase 8R.5 future-only shadow canary.

## Problem

The independent production monitor reports
`live_entry_preamble_binding_evidence_missing` for entry assembly 3, created
from raw message 9955. Durable evidence shows that the instruction failed
closed before enqueue with `missing_ready_confirmation` and
`contract_size_unverified`. No trade signal, execution binding, execution
event, order, or position was created.

The current monitor applies the binding-evidence invariant to every recent
entry assembly. It therefore classifies a terminal pre-binding safety refusal
as though it were a live entry with missing ownership evidence. This is a
monitor classification defect, not evidence that the refused entry executed.

The underlying SOL configuration mismatch is separate: global trading
settings allow SOL while the reviewed static contract-spec file contains only
BTC and ETH. This design does not add a SOL contract spec, change the allowed
symbol list, replay raw 9955, or enable SOL trading.

## Approved Scope

Change only the read-only entry-preamble invariant classification. An assembly
without a binding may be classified as a known terminal pre-binding refusal
only when all required durable evidence agrees:

1. the assembly has exactly one linked message-instruction item through its
   signal candidate;
2. that item is terminal `failed` and is not retired;
3. its bounded error document has type `RecoveryLiveSubmitError`;
4. the error message is exactly a `signal_enqueue_blocked:` refusal containing
   only closed pre-submit safety reason codes;
5. no trade signal exists for the assembly strategy identity or source
   chat/message;
6. no execution binding exists for the assembly strategy identity;
7. no execution event exists for the source chat/message or strategy identity.

When every condition holds, the assembly is non-live and does not emit
`live_entry_preamble_binding_evidence_missing`. Any missing, duplicate,
malformed, unbounded, unknown, retired, nonterminal, submitted, or conflicting
evidence continues to fail closed with the existing reason code.

The closed refusal reason set initially contains only the two production-proven
pre-submit reasons:

- `missing_ready_confirmation`
- `contract_size_unverified`

The match must require both reasons for the raw-9955 case. It must not accept a
substring, arbitrary exception message, provider text, or a general `failed`
status.

## Data Flow

`read_entry_preamble_invariants` continues to open SQLite in read-only,
query-only mode. For an assembly whose binding evidence neither matches nor
has an exact reconciliation, it calls one bounded helper that evaluates the
terminal pre-binding refusal proof. The helper reads only the linked
instruction, raw-message identity, trade-signal existence, binding existence,
and execution-event existence. It returns a boolean and persists nothing.

The existing binding fingerprint and reconciliation checks remain
authoritative for every assembly that has a binding. The new helper cannot
create or repair a binding and cannot reinterpret an exchange outcome.

## Safety Boundaries

- No order, position, protection, strategy, recognition, context, instruction,
  assembly, binding, event, incident, notification, Agent, or configuration
  write.
- No Deepcoin, provider, Telegram, web, listener, or service-control call from
  the classifier.
- No historical deletion, update, replay, or synthetic reconciliation event.
- No change to normal auto-trading or message-processing code.
- No Phase 8R.5 contract projection, outcome supervision, Stage 1
  notification, or Agent eligibility change in this prerequisite fix.
- The monitor remains unhealthy for any evidence shape other than the exact
  closed terminal refusal.

## Verification

Focused tests must first reproduce the current false classification. They then
prove:

- the exact terminal pre-binding refusal is treated as non-live;
- a real assembly/binding fingerprint mismatch still reports the invariant;
- missing or multiple instruction items fail closed;
- pending, executing, unknown, succeeded, retired, or malformed instruction
  evidence fails closed;
- unknown or partial refusal reasons fail closed;
- any trade signal, binding, or execution event defeats the exception;
- the database bytes remain unchanged;
- architecture tests continue to prohibit provider, Telegram, exchange, and
  business-write dependencies.

After review and push, deploy only in a new proven quiet window with the
message-operation supervisor still disabled. Synchronize the independent
monitor expected HEAD, run the no-notify full diagnostic, and require
`healthy=true` with no reason codes. Only then resume the separately gated
Phase 8R.5 future-only watermark and one-shot shadow canary.

## Rollback

Rollback is a reviewed forward revert of this classifier change. It requires
no database cleanup because the implementation is read-only and creates no
state. If verification is incomplete or the diagnostic reports any other
reason, keep the Phase 8R.5 supervisor disabled and leave the phase
`in_progress`.
