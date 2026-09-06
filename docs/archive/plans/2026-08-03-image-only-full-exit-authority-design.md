# Image-Only Full-Exit Authority Design

## Problem

Telegram message `4125` in the Feiyang group contained only an image. MiMo read
the image correctly as `BTC空单，目前成本价附近，出局吧`, and contextual
resolution selected the exact active BTC-short thread with
`decision=exit_thread`, `management_action=exit_full`, confidence `0.95`, and
lifecycle `688`. No management candidate or durable instruction was created,
so automation stopped with `mimo_authoritative_not_safely_applied`.

The failure is deterministic. `raw_messages.text` is empty for an image-only
message. The exit-downgrade guard checks full-exit wording only in that empty
field, then reads the explanatory model reason, sees `成本价`, and treats the
exit as a possible break-even update. After that downgrade,
`resolve_management_directive` does not recognize `management_action=exit_full`
when `event_type` has already become `position_update`, so safe target
resolution refuses the instruction.

## Goals

- Preserve an explicit, high-confidence, exact `exit_full` decision through
  candidate projection and live execution.
- Let image-observed text prevent a false exit downgrade without letting OCR
  text independently expand trading authority.
- Exclude explanatory AI prose from deterministic keyword classification.
- Preserve exact Telegram source, lifecycle, binding, verified-leg, and fresh
  exchange-position gates.
- Add a regression test that matches the production message shape: empty raw
  text, image-observed exit text, exact target, and a verified live position.

## Non-goals

- Replaying historical management messages.
- Reopening or modifying the already closed Feiyang position.
- Broadly changing entry recognition, risk sizing, or strategy targeting.
- Treating OCR text alone as sufficient authority to close a position.
- Relaxing conflict, ownership, freshness, or final write-boundary checks.

## Considered Approaches

### 1. Add only an `exit_full` alias

Teach the directive resolver that `management_action=exit_full` means full
exit. This is small and provides a useful defense, but it leaves the earlier
image-only downgrade bug intact and can still misclassify other image exits.

### 2. Use OCR text for every management decision

Replace raw message text with OCR/observed text throughout the management
pipeline. This covers more cases, but changes stop-loss, partial-close, and
position-sizing behavior at the same time. It is too broad for this incident.

### 3. Structured authority first, bounded image evidence second

This is the selected approach. An explicit structured full-exit event or action
cannot be downgraded by explanatory wording. For the narrow downgrade check,
the current-message evidence is raw text plus `input_reading.observed_text`.
The model's explanatory `reason` is not instruction text. The directive layer
also recognizes full-exit action aliases as a defense in depth.

## Design

### Current-message evidence

Add a small helper in `message_recognition.py` that returns normalized current
instruction evidence from:

1. `raw_message.text`, and
2. authoritative `payload.input_reading.observed_text`.

The helper must not include `payload.reason`, lifecycle-event `reason`, context
resolution rationale, prior messages, or exchange-state prose. It is only a
bounded view of what the current Telegram message visibly said.

Pass this evidence into the authoritative deterministic-management path. Keep a
default that uses `raw_message.text` for existing non-authoritative callers.

### Exit downgrade precedence

Change `_exit_decision_looks_like_management_update` to use this precedence:

1. If `event_type` is `exit_full`, `full_exit`, or `close_position`, do not
   downgrade. A generic `exit_position` still requires either a structured
   full-exit action or current-message exit evidence because models sometimes
   use that event for partial/protective management.
2. If `management_action` is `exit_full`, `full_exit`, or `close_position`, do
   not downgrade.
3. If current-message evidence contains an explicit full-exit phrase, do not
   downgrade.
4. Only then may current-message evidence and a structured management action
   indicate partial take profit or protective-stop management.

Explanatory `reason` text must not participate in steps 3 or 4. In particular,
the word `成本价` inside an explanation cannot override `exit_full`.

### Directive normalization

Update `resolve_management_directive` so these structured action aliases map to
the existing `full_exit` directive even if an upstream event was normalized to
`position_update`:

- `exit_full`
- `full_exit`
- `close_position`

This is a secondary guard. The primary fix is preventing the invalid downgrade.

### Existing safety gates

After projection, the existing path remains authoritative:

`raw message -> candidate -> instruction item -> management batch -> verified
entry leg -> exact posId -> fresh exchange preflight -> close write boundary`

The change must not create a candidate when the target lifecycle is absent,
belongs to another chat, lacks a unique active binding, lacks a verified entry
leg, or cannot be confirmed in the current Deepcoin snapshot. Conflicting
evidence continues to fail closed through the existing contextual-resolution
and ownership gates.

### Failure handling and observability

- A rejected event remains non-executable and records the existing bounded
  automation reason.
- A successfully projected image exit must create one active management
  instruction for the exact lifecycle.
- Exchange submission remains idempotent and exact-position scoped.
- Submitted close is not treated as terminal until exchange reconciliation
  confirms position absence.

## Tests

Add focused tests for:

1. Empty raw text plus observed image text `BTC空单，目前成本价附近，出局吧`,
   exact lifecycle, `exit_position`, and `exit_full`: one close candidate and
   one management instruction must be projected.
2. An explicit `exit_full` action cannot be downgraded merely because the
   explanation contains `成本价`.
3. `management_action=exit_full` normalizes to the full-exit directive even if
   an upstream caller supplies `event_type=position_update`.
4. `成本价附近继续拿着` does not become a full exit.
5. `减仓一半并保护成本` remains partial-close/protection management.
6. Missing or unverified position ownership still creates no executable work.
7. Existing text-only full exits, cancellations, partial exits, and stop changes
   continue to pass.

Run focused recognition and directive tests first, then management planning and
execution tests, then the full suite.

## Rollout and rollback

Implement locally with TDD, review the diff, push
`codex/deepcoin-auto-trading-v1`, and deploy only after a read-only check proves
there is no active time-sensitive management operation or unknown exchange
outcome. Production verification is passive: service health, deployed SHA,
settings read-back, and the next naturally arriving qualifying message. Do not
create a Telegram exit signal or real position as a test fixture.

Rollback is a reviewed Git revert followed by the normal server update helper.
The fix introduces no schema change, so rollback does not require data repair.
