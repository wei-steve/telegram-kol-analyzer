# AI Prompt Registry Design

**Date:** 2026-07-13

## Goal

Make every prompt used by an AI-powered project workflow visible and manually editable in the Web application, while preventing an untested edit from immediately changing live-trading behavior.

The prompt system must also eliminate drift between the DeepSeek and MiMo trading-analysis instructions. Shared trading semantics live in one template, and MiMo adds only a separate image-understanding template.

## Approved Product Decisions

- Every AI call must reference a registered prompt or an explicitly registered composition of prompts.
- Shared trading analysis uses template **A**.
- MiMo image understanding uses template **B**.
- Runtime message and strategy context is dynamic input **C**, not an editable prompt.
- DeepSeek receives `A + C`.
- MiMo receives `A + B + C`.
- A saved edit is a draft and does not affect production.
- A draft must be tested before it can be published.
- Publishing changes the active production version.
- A previous published version can be restored with one rollback action.

## Prompt Inventory

The first migration must register all currently known AI prompt entry points:

1. Shared trading analysis (A)
   - New-entry recognition.
   - Existing-strategy lifecycle recognition.
   - Common output schema, field definitions, confidence rules, price normalization, and accumulated trading-language examples.
2. MiMo image supplement (B)
   - Image text, tables, exchange screenshots, annotations, arrows, chart levels, image quality, and text-image conflict handling.
3. Telegram group research chat system prompt.
4. Strategy-alert classification prompt.
5. Per-group research instruction, scoped by `chat_id`.

MiMo/DeepSeek comparison experiments must not own another copy of the trading rules. They reference A or A+B through the same composition service used in production.

If another AI call is discovered during implementation, it must be added to the registry before the migration is considered complete. Business prompt text must not remain embedded at an HTTP call site.

## Template Boundaries

### A: Shared Trading Analysis

A is model-neutral and contains every text-semantic trading rule used by both DeepSeek and MiMo:

- new strategy versus non-strategy classification;
- entry confirmation and entry cancellation;
- partial take profit, reduction, protective stop, stop-loss/take-profit adjustment, hold updates, and full exit;
- strategy/lifecycle association instructions;
- symbol, side, entry, stop loss, take profit, leverage, order type, lifecycle event, target lifecycle, and confidence fields;
- one canonical JSON output contract;
- BTC shorthand and other price normalization rules;
- historical screenshot, recap, education, advertisement, and reference-strategy distinctions;
- no-hallucination and low-confidence/failure behavior.

A must not mention a provider name or contain image-reading instructions.

### B: MiMo Image Supplement

B contains only capabilities unique to multimodal input:

- directly read visible image text, tables, labels, arrows, annotations, exchange screenshots, and chart levels;
- combine caption/text and image evidence;
- report observed image content and image quality;
- fail or lower confidence for blurry, cropped, occluded, unreadable, or internally inconsistent images;
- never infer fields that are not visible in the text or image.

B must not redefine the trading decision rules or JSON schema from A.

### C: Runtime Context

C is generated for each request and is displayed as a read-only composition preview:

- current Telegram message;
- relevant recent messages;
- active strategy lifecycle records and lifecycle IDs;
- relevant execution-binding summary;
- user question and selected source context for research chat.

C is never stored as part of a prompt version and cannot be edited globally.

## Registry Data Model

Use a database-backed registry. Provider URLs, API keys, and other secrets remain in server configuration and are not exposed by the prompt UI.

### Prompt definition

- stable prompt ID;
- Chinese display name and description;
- functional category;
- scope (`global` or a specific `chat_id`);
- consumers/models;
- required template variables;
- validation profile;
- enabled/disabled state.

### Prompt version

- immutable version ID and version number;
- prompt ID;
- content;
- status (`draft`, `published`, or `superseded`);
- change note;
- created and updated timestamps;
- published timestamp;
- source version for rollback/audit.

Each prompt has at most one editable draft and exactly one active published version once seeded. Publishing atomically changes the active version. Rollback creates or activates an auditable published version rather than deleting history.

### Runtime audit

Every AI recognition/experiment/chat decision must record the prompt IDs and exact version IDs used for the request. A result must remain explainable after a later prompt publication.

## Web Experience

Add an `AI 提示词中心` destination to the existing responsive workbench.

### Registry list

Show:

- prompt name and purpose;
- scope;
- consuming feature and models;
- active version;
- whether an unpublished draft exists;
- last edited and published times;
- enabled/validation state.

### Prompt detail

Allow the user to:

- inspect the active content;
- create or edit a draft;
- add a change note;
- inspect required variables and validation rules;
- preview the final composition for DeepSeek or MiMo;
- select historical messages for an isolated test;
- compare active and draft results field by field;
- publish after validation succeeds;
- inspect version history;
- roll back to a previous published version.

The A/B detail view must visibly show:

```text
DeepSeek = A + C
MiMo     = A + B + C
```

The older per-group prompt editor is migrated into the registry as a scoped prompt. The new draft/test/publish lifecycle supersedes the former immediate-on-save behavior.

## Isolated Draft Testing

Draft tests use historical text, image, or text-image messages but must have no production side effects:

- do not create executable signal candidates;
- do not mutate a strategy lifecycle;
- do not create or submit a Deepcoin order;
- do not send a production strategy alert;
- do not overwrite the published recognition result;
- store test results separately from production decisions.

The comparison view shows:

- recognition result;
- strategy fields;
- lifecycle event and target lifecycle ID;
- confidence and reason;
- observed image text and image quality;
- raw JSON;
- structured active-versus-draft differences;
- request duration and error details.

Seed the regression set with representative new-entry, Fengge full-exit, partial-profit, protective-stop, strategy-correction, historical-recap, and image-quality cases.

## Publish Validation and Safety

Prompt content is editable, but deterministic program safeguards are not prompts and remain outside the editor.

Before publication, validate:

- non-empty content;
- only registered template variables are referenced;
- every required variable is present;
- A contains the canonical output contract required by the parser;
- B does not introduce a conflicting output contract;
- representative draft tests return parseable responses;
- image regression tests actually include readable and failure-quality images where required;
- production composition contains each selected template exactly once.

Publishing requires a visible diff, successful validation, a change note, and explicit confirmation. Validation failure leaves the draft unchanged and production continues using the active version.

MiMo authority, DeepSeek auxiliary comparison, disagreement notification, lifecycle safety checks, execution binding, and Deepcoin reconciliation remain unchanged.

## Configuration Migration

Current YAML prompt fields are legacy inputs:

- `recognition_prompt`;
- `lifecycle_event_prompt`;
- `mimo_direct_prompt`.

Migration behavior:

1. Seed the registry from current effective prompt content, preserving accumulated instructions.
2. Combine legacy recognition and lifecycle rules into A.
3. Move image-only content into B.
4. Prefer an active registry version at runtime.
5. If the registry is empty during a transitional startup, compose the legacy fields and emit a deprecation warning.
6. Web edits write only prompt versions, never provider credentials or legacy YAML prompt fields.
7. Remove the legacy fallback only after production has a verified active version for every required prompt.

## Failure Handling

- Missing required published prompt: fail the affected AI workflow closed and raise an operator-visible configuration error.
- Invalid draft: keep the active version unchanged.
- Test model/network failure: record the failure without enabling publish based on that run.
- Concurrent publish: use an atomic active-version update and reject stale edits.
- Rollback failure: preserve the current active version.
- Unknown AI prompt at a call site: fail tests and block completion of the migration.

## Verification

### Unit and integration tests

- DeepSeek prompt is A+C and never contains B.
- MiMo prompt is A+B+C and contains each template exactly once.
- Both models share the same output contract.
- Updating A affects both model compositions.
- Updating B affects only MiMo.
- every AI call site resolves registered prompt IDs and version IDs;
- draft saves do not change active runtime output;
- isolated tests create no signal, lifecycle, execution, or alert side effects;
- publishing atomically changes the active version;
- rollback restores an older version with audit history;
- legacy configuration seeds equivalent effective prompts;
- Fengge exit, disagreement notification, MiMo failure, partial management, and image cases remain green;
- Web list, detail, draft, test, publish, history, and rollback routes enforce their state transitions.

### Production verification

Follow the normal local commit and GitHub deployment path, then on the server:

- install the updated editable package and restart `telegram-kol.service`;
- verify registry seeding and active versions;
- verify the prompt center loads without exposing secrets;
- run side-effect-free text and image draft comparisons;
- confirm new live recognition records contain exact prompt version IDs;
- confirm MiMo remains authoritative and DeepSeek remains auxiliary for text messages;
- inspect service logs for missing-prompt, migration, or reconciliation errors.

## Success Criteria

- Every AI-powered feature has a visible prompt definition in the Web UI.
- No AI business prompt remains hidden at a request call site.
- Trading rules have one shared source A and one image-only supplement B.
- Draft edits cannot affect production before explicit publication.
- Every production AI result is traceable to immutable prompt versions.
- Published prompts can be restored without editing server files or redeploying code.
