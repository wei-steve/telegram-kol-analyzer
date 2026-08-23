# MiMo Context-Resolution Authority Cutover Design

## Status

Approved by the owner on 2026-08-22. This design creates a dedicated Phase 6C
between the currently deployed Phase 6 split topology and Phase 6's final L2
acceptance.

The owner made two explicit decisions:

1. Future contextual strategy resolution permanently uses `mimo-v2.5` and does
   not automatically fall back to DeepSeek.
2. Historical analysis repaired after the DeepSeek HTTP 402 incident must never
   submit, cancel, replace, or modify an exchange order and must never create a
   downstream path that can do so later.

## Problem

The first-pass authoritative recognizer already uses MiMo v2.5. A second,
contextual resolver handles ambiguous thread ownership and management intent,
including `new_thread`, `revise_thread`, `manage_thread`, `cancel_thread`,
`exit_thread`, `hold`, and `unresolved`. That second resolver currently selects
`deepseek-v4-flash` through `context_resolution_model_id`.

DeepSeek is returning HTTP 402. A read-only production census taken during
design found 162 exhausted DeepSeek attempts across 33 distinct messages from
2026-08-23 04:04:55 UTC through 06:36:41 UTC. After deduplication by raw
message, 30 active messages had failed jobs, two active messages had expired
jobs, and one source message had been deleted. The attempt count is not the
backfill count: changing evidence and context fingerprints caused repeated
attempt rows for the same raw message.

The existing normal replay path is not safe for historical repair. Even when
its immediate `auto_trade_executor` is absent, authoritative application can
create signal candidates, lifecycle state, message instruction items, and
execution-contract projections that other workers may consume later.

## Goals

- Make MiMo v2.5 the sole future context-resolution provider.
- Fail closed when MiMo context resolution is unavailable or invalid.
- Preserve the existing context prompt, request builder, closed decision
  contract, retry bound, candidate constraints, and confidence rules.
- Let Codex produce analysis-only decisions for the deduplicated HTTP 402
  incident using the exact persisted request and prompt contract.
- Preserve the original exhausted DeepSeek attempts unchanged.
- Display historical repair decisions and analytical target-thread references
  in the workbench without creating operational authority.
- Provide deterministic dry-run, production-copy rehearsal, apply, verification,
  and exact rollback.
- Keep the deployed ingest/worker/Web topology and all exchange-write semantics
  unchanged.

## Non-goals

- No DeepSeek fallback, circuit-based or otherwise.
- No re-enabling of semantic disagreement review.
- No replay of historical messages through normal recognition or automation.
- No requeue of failed or expired message-processing jobs.
- No creation or update of `RecognitionDecision`, `MessageRecognition`,
  `SignalCandidate`, `MessageInstructionItem`, `MessageOperation`,
  `StrategyMessageLink`, `StrategyLifecycle`, management batches, worker
  commands, execution events, notifications, or exchange state from historical
  repair.
- No attempt to turn a stale historical instruction into a current trading
  instruction. Any still-relevant live action must arrive through the normal
  future MiMo path or receive a separate, exact authorization.
- No repair of a deleted source message.
- No Phase 6 ingest-stall remediation inside Phase 6C.

## Architecture

### Future authority path

`AiRecognitionConfig.context_resolution_model_id` becomes `mimo-v2.5` while
`active_text_model_id` and `active_image_model_id` remain independently
configurable. `_select_provider` continues to require `supports_text=true` and
returns the configured MiMo provider. The request body and response parser stay
unchanged: temperature zero, the published context-resolution system prompt,
the existing rendered user payload, at most two attempts, and the same strict
closed-contract validation.

There is no fallback provider. Provider HTTP failures, malformed JSON, and
contract failures retain the current exhausted/fail-closed behavior. A model
switch changes provider authority only; it does not weaken target, fanout,
management-action, confidence, evidence-ID, or current-state validation.

The existing AI configuration API currently rebuilds an
`AiRecognitionConfig` without carrying `context_resolution_model_id`, so an
unrelated model save could silently return context authority to the active text
model. Phase 6C must preserve and expose the independent context selection and
test that unrelated saves cannot change it.

Production cutover uses a reviewed, atomic configuration updater with an exact
before hash, expected old model, expected new model, backup path, dry-run, and
rollback. It does not edit API keys. The worker loads the recognition
configuration on each authoritative message, so a successful atomic change does
not require a process restart. If live verification disproves that assumption,
the operation stops before changing authority and the plan must be corrected.

### Historical analysis-only store

Add a dedicated `context_analysis_backfills` table. No operational worker may
query this table. The table records:

- `raw_message_id`;
- the selected source `ContextResolutionAttempt.id`;
- source request hash and source state fingerprint;
- prompt version and analyst model (`codex-manual-context-v1`);
- the strict decision JSON, including analytical `target_thread_ids`;
- a status from `analysis_only_completed`, `skipped_deleted`, or
  `skipped_stale`;
- bounded skip reason and timestamps;
- a manifest/run identity used for idempotence and rollback.

The analytical target IDs are deliberately not copied into
`strategy_message_links`. They are non-authoritative references rendered by the
Web query layer. This prevents normal recognition, management, reconciliation,
or execution workers from discovering a new operational association.

The existing DeepSeek attempts remain exhausted and unchanged. The historical
message jobs remain failed or expired. The workbench renders the new record as
`historical_analysis_only`, visibly distinct from an authoritative completed
context resolution.

### Export and Codex analysis

The export command is read-only and selects one candidate record per distinct
raw message:

1. Match the approved incident window/provider/error classification.
2. Deduplicate by `raw_message_id`.
3. Select the newest attempt whose request JSON parses and whose evidence
   version is still identifiable.
4. Record deleted messages as excluded.
5. Include the source attempt ID, raw-message status, request payload, request
   SHA-256, state fingerprint, prompt version, allowed target-thread IDs, and
   allowed evidence-message IDs.
6. Exclude credentials, provider headers, Telegram session material, and raw
   exchange identifiers not already present in the bounded prompt payload.

The export is stored as a root-readable server evidence file, not committed to
Git. Codex evaluates each non-deleted record with the exact
`CONTEXT_RESOLUTION_SYSTEM_PROMPT` and the same request renderer. Every proposed
decision is then passed through the production
`parse_context_resolution_decision` validator. Free-form prose is not an
applyable result.

### Validation and apply

The CLI has distinct `export`, `validate`, `apply`, and `rollback` operations.
`export` and `validate` are read-only by default. `apply` requires all of:

- an explicit `--effects analysis-only` value;
- exact database identity and manifest SHA-256;
- exact expected record count;
- a successful production-copy rehearsal artifact;
- `PRAGMA quick_check=ok`;
- no active write, in-flight management batch, claimed message job, or claimed
  worker command;
- a manifest decision that passes the closed contract;
- current raw-message/source-attempt/evidence identity matching the manifest;
- target thread IDs still belonging to the same chat and still present;
- no existing row for the same manifest/raw-message pair.

Deleted messages become `skipped_deleted`. Identity, evidence, or target drift
becomes `skipped_stale`. Neither is forced to completed. The apply transaction
may insert only `context_analysis_backfills` rows. A SQL authorizer/test guard
rejects any write to any other table.

The production apply command does not import or construct a Deepcoin client,
Telegram client, notification client, authoritative processor, automation
executor, management executor, or worker-command executor. Static authority
tests guard that call graph.

### Rollback

Rollback accepts the exact run identity and expected inserted-row IDs from the
apply receipt. It deletes only those `context_analysis_backfills` rows inside one
transaction. It refuses if a row hash differs from the receipt or if any target
row is outside the run. Schema remains in place. Before and after rollback,
record `quick_check`, table count, targeted row hashes, and unchanged counts for
critical operational tables.

The MiMo authority rollback is the atomic restoration of the exact backed-up AI
configuration file after proving the expected current file hash. It does not
alter database data. A provider rollback is not automatic on one failed
message; runtime failures stay fail closed and require an operator decision.

## Safety Invariants

- Historical backfill has zero route to exchange-write methods.
- Historical backfill cannot make a failed/expired job pending, claimed, or
  succeeded.
- Historical backfill cannot create downstream instruction or operation rows.
- Historical backfill cannot emit Telegram notifications.
- Every inserted decision validates against the exact candidate and evidence ID
  sets from its source request.
- Original DeepSeek evidence is immutable.
- MiMo context failures do not fall back to DeepSeek.
- First-pass MiMo recognition, strategy parsing, position ownership, execution,
  and exchange-write rules are unchanged.
- `message_lock_mode=global`, `message_pipeline_mode=queue`,
  `worker_command_mode=queue`, and `semantic_review_enabled=false` stay fixed.

## Verification

Phase 6C is L3 because it adds a table and applies a bounded production data
repair. It also performs an authority cutover, so the final live observation
uses the L2 traffic criteria.

Local verification follows strict RED/GREEN TDD for:

- independent context-model selection and configuration preservation;
- MiMo provider selection and no-fallback behavior;
- migration/bootstrap idempotence;
- incident deduplication and newest-valid-source selection;
- deleted/stale classification;
- exact contract validation;
- analysis-only table write allowlist;
- idempotent apply and exact rollback;
- Web projection clearly marked non-authoritative;
- authority scanning proving no operational or exchange path;
- unchanged normal recognition and context application semantics.

Run focused tests while developing and one full suite on the final production
candidate. Any later production-code change creates a new final candidate and
requires affected focused tests plus one new final full suite.

Before production apply, use an online backup copy, preserve the immutable
backup, run `quick_check`, record before/after counts for the new table and
critical operational tables, hash the targeted rows, rehearse repeated apply,
and rehearse exact rollback. Do not hash the whole database without an anomaly.

After deployment and the atomic model switch, observe 30 continuous minutes and
at least five natural messages, trying to cover two chats. Verify each required
context resolution selects `mimo-v2.5`, passes the contract, creates one durable
job/decision path, and does not call DeepSeek. Check backlog, duplicates,
SQLite errors, loop health, monitor health, management activity when naturally
available, and direct exchange history if a natural path can affect execution.
Do not manufacture messages or exchange writes.

## Phase Boundary

Phase 6 remains safely deployed but incomplete. Phase 6C temporarily becomes
the canonical current phase. After Phase 6C completes, restore the exact Phase 6
checkpoint at final L2 acceptance. Phase 6 then resolves its independently
observed ingest `iter_dialogs` stall and runs a fresh qualifying split-topology
window. The Phase 6C model switch and historical repair must not be presented as
a fix for that stall.
