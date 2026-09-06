# Context Resolution Target Contract Repair Design

## Problem

Raw message 9758 completed first-pass MiMo evidence extraction and was
classified as commentary about an existing ETH short strategy, with no new
entry, management, cancellation, or exit instruction. Because the message
matched an existing strategy, it correctly entered contextual resolution.

The contextual model then returned a non-target decision together with one or
more `target_thread_ids`. The parser correctly rejected that combination as
`target_not_allowed`. Two attempts in each of two authoritative runs exhausted
with the same error, so the authoritative pipeline failed closed and skipped
automation. The invalid provider response is not persisted; only the closed
error code is durable.

The parser contract is intentional: `new_thread`, `hold`, and `unresolved`
have no executable target, while `revise_thread`, `manage_thread`,
`cancel_thread`, and `exit_thread` require targets. The system prompt lists the
fields but does not state this target-cardinality rule. This prompt/validator
mismatch makes an intuitive response such as “hold strategy 122” invalid even
though the safe representation is `hold` with an empty target list.

## Scope

- Repair future contextual resolutions only.
- Do not replay, reclassify, or execute raw message 9758.
- Preserve the existing first-pass recognition and contextual candidate
  selection as authoritative.
- Preserve strict parsing and fail-closed automation.
- Do not add or widen any trading, notification, or Runtime Agent authority.

## Considered Approaches

### 1. Strengthen the prompt contract and add targeted corrective retry

State the exact target-cardinality rules in the system prompt, include a
non-executable commentary example, and add a bounded corrective instruction
only when the first response fails with `target_not_allowed`.

This is the selected approach. It aligns the provider with the existing safe
contract without weakening validation.

### 2. Normalize invalid target lists in application code

The application could clear targets automatically for `hold`, `unresolved`,
or `new_thread`. This was rejected because it silently changes model output
and can hide a genuine semantic contradiction.

### 3. Require provider-native structured output

A provider JSON Schema could encode decision-specific target rules. This may
be useful later, but requires separate compatibility and fallback work for the
configured provider and is larger than the current repair.

## Design

### Prompt contract

Increment the contextual prompt version and state these rules explicitly:

- `new_thread`, `hold`, and `unresolved` require
  `target_thread_ids: []`.
- `revise_thread`, `manage_thread`, `cancel_thread`, and `exit_thread` require
  one or more IDs drawn only from `candidate_strategy_threads`.
- Mentioning or discussing an existing strategy does not itself authorize a
  target-bearing decision.
- Commentary matching an existing strategy but containing no actionable
  instruction is represented as `hold`, an empty target list, and no
  management action.

Include one compact valid example based on redacted 9758 semantics. Do not put
production IDs or message text into the prompt.

### Corrective retry

Keep the existing maximum of two provider calls. If the first parsed response
fails with `target_not_allowed`, the second call receives the original system
prompt plus a deterministic correction explaining that non-target decisions
must use an empty target list. Other failures retain the existing retry
behavior; no additional calls are introduced.

The parser remains the final authority. The second response must pass the same
closed decision, candidate-ID, action, fan-out, confidence, and evidence-ID
validation. The application never edits provider output to make it pass.

### Durable diagnostics

Persist a bounded, non-sensitive diagnostic summary for a rejected response:

- parsed `decision`, when it is a known closed value;
- `target_thread_count` only, not the target IDs;
- closed `error_class`;
- attempt number and prompt version already present in the attempt row.

Do not persist the raw provider response, message text, credentials, or full
request. The summary exists only to distinguish a prompt-contract mismatch
from network or malformed-JSON failures.

This requires one nullable bounded JSON column on
`context_resolution_attempts`, introduced additively through the existing
SQLite compatibility migration path. Older rows remain valid with `NULL`.

### Failure behavior

If the corrected response still fails validation, retain the existing
`exhausted` attempt, runtime incident capture, authoritative failure, and
automation skip. No management batch, mutation intent, execution event, or
exchange write may be created.

The unrelated Strategy Alert provider failure recorded after 9758 is outside
this repair.

## Verification

Use strict test-driven development.

- Prompt tests require the explicit target-cardinality language, redacted
  commentary example, and new prompt version.
- Resolver tests prove `target_not_allowed` adds the corrective instruction
  only on the second bounded call.
- Resolver tests prove a corrected `hold` response succeeds with an empty
  target list.
- Persistence tests prove the rejected diagnostic contains only the closed
  decision, target count, and error code.
- Safety tests prove two invalid responses remain exhausted and create no
  execution or business-write artifacts.
- An authoritative-recognition test built from a redacted 9758-shaped fixture
  proves the final result is non-strategy/hold and automation remains skipped.
- Existing context-resolution, authoritative-recognition, live-listener,
  runtime-incident, and architecture suites remain green.

## Rollout

Deploy only after local review and a fresh production safe-window proof. The
change has no feature activation and does not replay historical messages.
After deployment, observe only future natural messages and verify that:

- valid contextual decisions continue to pass;
- `target_not_allowed` does not recur for non-actionable commentary;
- any remaining invalid output still fails closed;
- no historical recognition decision, strategy lifecycle, management batch,
  or exchange state changes because of deployment.

Rollback is a reviewed code revert followed by the normal deployment helper.
The nullable diagnostic column may remain; do not drop it or delete attempt
history during rollback.
