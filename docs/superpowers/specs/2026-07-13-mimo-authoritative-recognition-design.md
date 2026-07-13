# MiMo Authoritative Recognition Design

## Goal

Make MiMo the authoritative recognizer for Telegram strategy messages because it can interpret both text and images. DeepSeek remains a text-only auxiliary validator. A disagreement must generate an operator notification but must not delay or block MiMo-authorized processing, especially temporary stop-loss, take-profit, or exit instructions.

## Decision Summary

- MiMo is the single authoritative recognition result for text-only, image-only, and combined text-and-image messages.
- The effective MiMo prompt must include all durable rules and examples from the DeepSeek entry-recognition prompt, lifecycle-event prompt, normalized strategy-field instructions, and price-normalization instructions.
- The effective MiMo prompt adds image-specific reading, grounding, quality, and anti-hallucination rules that do not apply to DeepSeek.
- DeepSeek runs only as an auxiliary validator for text-only messages.
- A MiMo/DeepSeek disagreement is observable but non-blocking. The system sends an operator notification and continues using MiMo's result.
- MiMo failure is different from model disagreement. Failure uses the fail-closed fallback rules in this document.

## Non-Goals

- Do not give DeepSeek image-recognition responsibility.
- Do not require human approval before acting on a valid MiMo result.
- Do not redesign position attribution, Deepcoin order construction, or position-close idempotency.
- Do not allow a recognition result alone to prove that an exchange position is closed.

## Prompt Architecture

The application must build the effective MiMo prompt at runtime from shared rule sections rather than relying on manually duplicated prompt text.

The shared sections are:

1. New-entry classification rules and counterexamples currently used by DeepSeek.
2. Strategy field normalization for symbol, side, entry, stop loss, take profit, leverage, and order type.
3. Lifecycle-event rules for `entry_confirm`, `cancel_entry`, `exit_position`, and `position_update`.
4. Active-strategy attribution rules, including the requirement to return no actionable lifecycle event when a target cannot be identified safely.
5. Price shorthand normalization, including BTC ten-thousand-unit forms.
6. Existing false-positive lessons covering commentary, reviews, advertising, contact details, historical screenshots, holding updates, partial exits, and protective-stop updates.

MiMo-only additions are:

- Read message text and attached images together as one current message.
- Read visible text, tables, exchange screenshots, annotations, arrows, labels, and chart callouts directly from the image.
- Report what was actually observed and an image-quality classification.
- Never invent a symbol, side, price, target, stop, or relationship that is not grounded in the current text/image or supplied active-strategy context.
- Treat blurry, cropped, obscured, or internally contradictory images as low confidence or recognition failure.
- Distinguish a historical result screenshot or promotional profit image from a current executable instruction.
- Prefer the current message over older context and never copy an old action into the current result.

Configuration may still expose editable DeepSeek and MiMo prompt fields, but the effective MiMo request must be composed with the shared authoritative rules automatically. Editing the DeepSeek experience rules must therefore affect the next MiMo request without requiring a second manual copy.

## Unified MiMo Result

MiMo returns one JSON document with these conceptual sections:

- `recognition_result`: new-entry classification (`是策略`, `非策略`, or `识别失败`).
- `strategy`: normalized new-entry fields.
- `lifecycle_event`: lifecycle action, target lifecycle, symbol, side, prices, management action, confidence, and reason.
- `input_reading`: observed text/image evidence and image quality.
- `confidence`: overall grounded confidence.

A lifecycle instruction such as “出局” may correctly have `recognition_result = 非策略` because it is not a new entry while also having `lifecycle_event.event_type = exit_position`. These values are not a conflict.

The persisted authoritative candidate and lifecycle interpretation must be derived from this unified MiMo document. The existing MiMo experiment record can remain available for diagnostics, but MiMo can no longer be only a side-channel experiment whose result is ignored by execution.

## Recognition Flow

### Messages containing an image

1. Build the effective MiMo prompt from shared strategy/lifecycle rules plus MiMo image rules.
2. Send current text, images, recent same-group context, and safely serialized active strategies to MiMo.
3. Validate and persist the unified MiMo result as authoritative.
4. Do not use DeepSeek as an image strategy judge.
5. Pass an actionable MiMo result through existing trading gates, attribution checks, binding checks, and idempotent execution paths.

### Text-only messages

1. Run MiMo with the same authoritative strategy and lifecycle rules.
2. Run DeepSeek as an auxiliary text validator.
3. Persist both results and compare their normalized new-entry and lifecycle interpretations.
4. Continue using MiMo whether the models agree or disagree.
5. On disagreement, send a non-blocking operator notification containing the source message, both normalized results, the chosen MiMo result, and whether an automatic action was attempted, submitted, skipped, or failed.

Notification delivery must not sit in front of trading execution. A slow or failed notification request must not delay or cancel a valid MiMo-authorized action. Notification failure must be logged for later diagnosis.

## Disagreement Semantics

The following differences count as a disagreement:

- One model identifies a new entry and the other does not.
- Both identify an entry but differ on symbol, side, entry mode, or material risk prices.
- One model identifies a lifecycle event and the other returns `none`.
- Both identify lifecycle events but differ on event type, target strategy, symbol, side, or full-versus-partial exit meaning.

The comparison must understand the unified schema. It must not treat `recognition_result = 非策略` and `lifecycle_event = exit_position` from the same model as contradictory.

On disagreement:

- MiMo remains authoritative.
- Existing risk, confidence, symbol allowlist, group automation, binding uniqueness, and exact-position safeguards still apply.
- The system must not introduce an artificial human-review gate.
- The notification must clearly state that MiMo was selected and state the execution outcome.

## MiMo Failure and Degradation

MiMo failure includes timeout, transport error, invalid JSON, unsupported or unreadable input, missing required fields, or confidence below the actionable threshold.

For text-only messages:

- DeepSeek may be displayed and persisted as a fallback analysis.
- DeepSeek must not directly authorize live opening, closing, partial closing, cancellation, or risk adjustment.
- Any actionable DeepSeek-only result is marked for manual confirmation.

For messages containing an image:

- Do not use DeepSeek as a replacement.
- Mark the message as MiMo recognition failure.
- Do not perform automatic trading actions.
- Send an operator notification with the failure reason.

## Lifecycle and Exchange-State Safety

Recognition state, requested action, submitted exchange action, and confirmed exchange state are separate facts.

- MiMo may identify `exit_position` and select the intended lifecycle.
- The execution layer may submit an exact bound-position close using the existing `pos_id` safeguards.
- The lifecycle must not be shown as finally exited merely because recognition succeeded or a close request was queued.
- A live-bound lifecycle becomes exited only after exchange reconciliation confirms that no active bound position or live entry order remains.
- If submission is skipped or fails, the lifecycle remains active and the operator notification/UI must expose the unresolved exit instruction.
- Duplicate close protection and fail-closed position attribution remain authoritative.

This prevents the observed failure mode where a KOL exit message changed the local strategy to exited while the real Deepcoin position stayed open.

## Persistence and Auditability

For each processed message, retain enough structured data to answer:

- Which MiMo model and effective prompt version produced the authoritative result?
- Was the message text-only or multimodal?
- What did MiMo observe in the input?
- Was DeepSeek used as auxiliary validation?
- Did the models agree, and on which fields did they differ?
- Which result was selected?
- What automation decision followed?
- Was the action submitted, skipped, failed, or later confirmed by reconciliation?
- Was the disagreement/failure notification delivered?

Secrets, API keys, and full provider authorization payloads must not be persisted.

## Operator Notifications

Send a notification when:

- MiMo and DeepSeek disagree on a text-only message.
- MiMo fails on any potentially actionable message.
- An authoritative MiMo lifecycle action cannot be attributed safely.
- Automatic execution is skipped or fails after an actionable MiMo result.

For disagreements, the notification includes both model summaries but labels MiMo as authoritative. It must not imply that execution is waiting for review. For exits and risk-reduction actions, include the execution outcome prominently so the user can immediately see whether the real position was handled.

## Testing Strategy

Regression tests must cover:

1. The Fengge text message `现价62800附近出局，空仓等待。`: MiMo identifies `exit_position`; DeepSeek auxiliary output differs; execution still reaches the exact active BTC-short binding; disagreement notification is sent without blocking execution.
2. A temporary stop-loss exit and a temporary take-profit exit: both remain time-sensitive MiMo-authorized actions under disagreement.
3. A text-only new entry where MiMo and DeepSeek disagree: MiMo remains authoritative while normal risk gates still decide whether it can auto-trade.
4. An image-only entry: MiMo can authorize it; DeepSeek is not called as an image judge.
5. A combined text-and-image lifecycle update: MiMo uses both inputs and emits grounded evidence.
6. MiMo timeout on text-only input: DeepSeek result is visible but no live action is authorized.
7. MiMo failure or unreadable image: no automatic action and a failure notification.
8. Notification timeout or failure: valid MiMo-authorized execution is not blocked.
9. Close submission failure: lifecycle stays active and unresolved.
10. Close submission success without reconciliation: lifecycle is not yet final-exited.
11. Reconciliation confirms the bound position is gone: lifecycle transitions to exited exactly once.
12. Prompt composition: the effective MiMo prompt contains the current DeepSeek experience rules, lifecycle rules, normalization rules, and MiMo-only image instructions.

## Deployment and Verification

Implementation is developed and tested locally. After review, commits are pushed to `codex/deepcoin-auto-trading-v1`. Production is updated by pulling from GitHub, reinstalling the editable package, and restarting `telegram-kol.service` through the existing server update helper.

Because real Telegram, MiMo, DeepSeek, system-bot notification, Deepcoin credentials, and exchange identity are available only on the server, final verification must include a server-side dry or controlled recognition check followed by database, execution-event, reconciliation, notification, and service-log inspection. No unapproved live order may be submitted as a verification shortcut.
