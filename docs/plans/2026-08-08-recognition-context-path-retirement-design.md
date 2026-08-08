# Recognition and Context Path Retirement Design

## Goal

Reduce recognition and contextual-resolution defects without stopping support
for newly discovered group message formats. Production will keep one
authoritative message path while historical records remain readable and new
formats continue to enter through authoritative evidence and contextual
resolution.

This work is architectural subtraction. It does not change strategy semantics,
replay historical messages, relax a trading gate, or grant the Runtime Incident
Agent any new authority.

## Problem

Production now intends to use this path:

```text
Telegram raw message
  -> MiMo authoritative evidence
  -> deterministic contextual-resolution trigger
  -> optional contextual decision
  -> authoritative projection
  -> message instruction item
  -> existing execution and reconciliation path
```

The repository still contains a second complete recognition family:

```text
Telegram raw message
  -> V1 DeepSeek/image OCR/local parser
  -> group-specific profile and lifecycle heuristics
  -> signal candidate
  -> automatic execution
```

The live listener and history-sync helpers retain fallback branches into that
family when an authoritative processor is absent. The V1 entry point,
group-specific profiles, local parser fallback, old direct MiMo comparison,
and legacy lifecycle hooks share a large module with
`apply_authoritative_mimo_payload()`, which the current production path still
needs. Therefore deleting the old module directly would also delete active
projection behavior.

This overlap has three costs:

- the same message can have more than one possible interpretation path;
- provider or wiring failure can silently change business semantics instead of
  failing closed;
- current projection changes are difficult to review because active and
  retired behavior are interleaved in one module and one very large test file.

## Decision

Use a strangler-style retirement in small reversible stages:

1. seal production entry points so missing authority cannot fall back;
2. characterize and extract the current authoritative projection without
   changing its behavior;
3. remove unreachable V1 execution branches and their production wiring;
4. narrow contextual resolution to one initial resolver and one durable
   reanalysis worker;
5. keep historical rows and legacy `parse_source` values readable.

Every stage is independently deployable. Removal follows a production
observation window in which the sealed entry point remains healthy. No stage
combines behavior removal with a new message-format feature.

## Alternatives

### Delete the V1 module immediately

Rejected. The active authoritative projector still imports from the same
module. A bulk deletion would mix code movement, behavior changes, and caller
removal, making a trading regression difficult to isolate.

### Disable fallback branches but leave all code indefinitely

Rejected as the final state. This is a useful first stage, but dead code,
configuration, and tests would continue to obscure the production authority
boundary and could be called again later.

### Build a new compatibility framework first

Deferred. It would add another abstraction before the existing paths are
understood and retired. New group formats can continue to be handled through
the current authoritative prompt/evidence contract while this cleanup runs.

## Authority Boundary

The only production authority after retirement is:

- `process_authoritative_message()` for message processing;
- immutable `MessageEvidenceVersion` for the first-pass evidence generation;
- `RecognitionDecision` for the authoritative execution generation and final
  automation outcome;
- `resolve_contextual_strategy()` only after a closed deterministic trigger;
- `ContextResolutionAttempt` plus the existing worker only for durable
  reanalysis after relevant state changes;
- `MessageInstructionItem` as the executable instruction boundary.

DeepSeek semantic review remains an asynchronous audit consumer. It may compare
or notify, but it cannot create, replace, or retire an executable instruction.

Unknown message formats fail closed as completed evidence with no executable
instruction, an exhausted contextual attempt, or a bounded recognition
failure. They never select the V1 parser because the authoritative provider was
unavailable.

## Scope

### Keep

- authoritative MiMo evidence extraction and prompt registry;
- recognition execution generation and compare-and-set ownership;
- strict authoritative payload parsing and projection;
- deterministic context-trigger classification;
- contextual candidate generation and exact target selection;
- multi-target management envelope and target projection;
- entry preamble and adjacent-entry fragment projection;
- instruction-item idempotency and retirement;
- semantic disagreement review as non-authoritative audit;
- historical UI, reporting, and database compatibility.

### Retire from production

- `recognize_message_now()` as a live or history-sync recognition entry point;
- the listener branch that runs V1 DeepSeek followed by direct MiMo comparison;
- automatic local-parser fallback when AI configuration is missing;
- OCR-to-local-parser automatic strategy creation;
- the Bitcoin Junzhang V1 group-profile entry point;
- V1 local lifecycle entry, cancellation, exit, and context-invalidation
  heuristics that directly mutate strategy state;
- the listener's legacy lifecycle-monitor callback;
- production imports, configuration, and tests that exist only for those
  retired callers.

### Preserve until separately proven redundant

- the current strict authoritative projector;
- exact management-scope validation;
- low-confidence exact risk-reduction checks used inside authoritative
  projection;
- symbol/side/position ownership validation;
- source deletion, message edit, and generation supersession behavior;
- any compatibility reader needed for existing rows or nonterminal exchange
  work.

The cleanup must classify helpers by call graph and behavior, not by names such
as `legacy`, `fallback`, or `heuristic`. A helper used by the authoritative
projector is active until extracted and covered by characterization tests.

## Target Module Shape

The active projection code will be split by responsibility:

```text
authoritative_recognition.py
  orchestration, evidence claim, context trigger, execution generation

authoritative_projection.py
  closed payload validation and top-level projection transaction

authoritative_entry_projection.py
  entry candidate, lifecycle, preamble, and fragment projection

authoritative_management_projection.py
  management scope, multi-target envelope, and instruction projection

context_resolution.py
  pure prompt contract, response validation, and exact decision

context_resolution_worker.py
  durable scheduling, claim, retry, and terminal exhaustion
```

The exact split may use fewer modules if extraction shows that a smaller
boundary is clearer. The non-negotiable property is that active authoritative
modules must not import the V1 recognition entry point or its provider/local
parser machinery.

## Stage 1: Seal Production Entry Points

The live listener and history-sync production runner must require an
authoritative processor whenever recognition is enabled.

If it is absent:

- persist raw intake normally;
- create no candidate, lifecycle, instruction item, trade signal, or exchange
  operation;
- return or log the fixed reason `authoritative_processor_required`;
- expose the condition to the existing production monitor;
- never call DeepSeek V1, direct MiMo comparison, a group profile, or the local
  parser.

Generic test helpers may accept explicit injected recognizers while tests are
migrated, but production construction cannot select the legacy branch. No new
long-lived feature flag is added. Structural wiring, tests, and architecture
checks enforce the boundary.

Deploy this stage without deleting V1. Observe normal natural intake for at
least seven days and require zero `authoritative_processor_required` events
before removing the unreachable branches. A time-sensitive incident during the
window resets the relevant observation, not the whole cleanup program.

## Stage 2: Characterize and Extract Active Projection

Before moving code, add production-shaped characterization tests for:

- complete new entries, text and image evidence;
- non-strategy messages;
- full exit, partial take profit, cancellation, and stop adjustment;
- exact single-target and multi-target management;
- context-resolved revision and exit;
- low-confidence exact risk reduction;
- source deletion and message edits;
- entry preambles and adjacent entry fragments;
- stale execution-generation rejection;
- duplicate processing and restart recovery;
- malformed or incomplete authoritative payloads.

Tests compare durable rows and bounded results before and after extraction.
The extraction is mechanical: no prompt, threshold, status name, candidate
selection, management policy, or transaction boundary changes in the same
commit.

Architecture tests then enforce that authoritative modules do not import:

- V1 provider invocation;
- `parse_signal_text`;
- group-specific recognition profiles;
- direct MiMo comparison;
- legacy lifecycle recognition entry points.

## Stage 3: Remove V1 Execution Paths

After the observation window and extracted-core deployment are healthy:

1. remove fallback branches from live and history listeners;
2. remove V1 provider orchestration and local-parser automatic candidate
   creation;
3. remove the group-specific V1 profile entry point and lifecycle hooks;
4. remove direct comparison notification code that is superseded by semantic
   review;
5. delete tests whose only contract is that production may fall back;
6. retain or replace historical serializers before deleting shared helpers.

CLI commands that intentionally inspect old logic must be either converted to
read-only historical tools with explicit names or removed. No command may
create a new executable candidate through the retired path.

## Stage 4: Narrow Contextual Resolution

Contextual resolution keeps exactly two invocation modes:

- initial synchronous resolution after `requires_context_resolution()` returns
  one or more closed trigger reasons;
- durable reanalysis after an allowlisted state-change event changes the
  context fingerprint.

The durable worker may reuse immutable first-pass evidence but must not invoke a
second first-pass business authority. Reanalysis of an unchanged fingerprint is
idempotent and does not create a new attempt, instruction, or notification.

Remove duplicate schedulers and compatibility retries that do not correspond
to an allowlisted state change. Terminal failed or exhausted attempts remain
historical and are never replayed merely because code was deployed.

## Historical Compatibility

No migration rewrites or deletes:

- `raw_messages`;
- `message_recognitions`;
- `recognition_decisions`;
- `message_evidence_versions`;
- `context_resolution_attempts`;
- `signal_candidates`;
- strategy threads, lifecycles, or message links;
- instruction items, execution bindings, orders, or protection ledgers.

Old `engine`, `parse_source`, reason, and status values remain displayable. A
historical row is not evidence that its creator must remain callable.

## Error Handling

- Missing authoritative wiring fails closed before business interpretation.
- Provider or schema failure produces no fallback candidate.
- Context contract failure uses the existing bounded retry and exhaustion path.
- Projection failure retires no previously submitted immutable instruction and
  creates no replacement unless the current generation owns the write.
- Auxiliary semantic-review failure never changes recognition automation.
- Unexpected top-level failures create bounded runtime evidence but do not
  enable another recognizer.

## Verification

### Local

- focused authoritative, evidence, context, listener, instruction, and
  architecture suites;
- production-shaped historical fixtures for known incidents;
- full repository regression;
- static import and caller checks proving no production legacy entry point;
- schema and historical-read compatibility tests.

### Production

Each stage follows the repository safe-window and GitHub deployment workflow.
Before restart, prove no recognition, context, management, entry revision,
position mutation, protection rescue, Runtime Agent, or notification claim is
in flight. After restart, verify:

- exact deployed commit and editable package;
- main service, listener, Runtime Agent, scanner, and monitor health;
- latest natural messages complete through the authoritative path;
- zero `authoritative_processor_required` events;
- no new legacy candidate `parse_source` values;
- no historical message replay;
- no database or exchange mutation caused by verification;
- stable complete protection and exchange readback.

The first natural examples of entry, management, contextual resolution, and
image evidence are checked end to end before the corresponding old code is
deleted.

## Rollback

Before code deletion, rollback restores the preceding reviewed commit. The
database requires no downgrade because cleanup adds no destructive migration.

After code deletion, rollback may restore old code only to read historical
state. Re-enabling a retired production authority requires a separate design,
review, and explicit operator approval; a deployment rollback alone is not
permission to run two authorities again.

Unknown exchange outcomes are reconciled by their existing owners. They are
never retried or rewritten as part of recognition-path rollback.

## Success Criteria

- every production message has exactly one first-pass business authority;
- missing provider or wiring cannot change recognition semantics;
- authoritative modules have no dependency on V1 provider/local-parser/group-
  profile execution code;
- contextual resolution has one initial path and one durable reanalysis path;
- new group formats remain supportable through evidence and prompt contracts;
- historical data remains readable and unchanged;
- no new legacy `parse_source` is created during the observation window;
- the deleted code and tests materially reduce branch and module size without
  weakening exact targeting, idempotency, protection, or fail-closed behavior.
