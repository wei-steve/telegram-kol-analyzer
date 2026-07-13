# Semantic AI Disagreement Review Design

## Goal

Replace field-by-field MiMo/DeepSeek disagreement alerts with an asynchronous semantic review that distinguishes material trading conflicts from harmless formatting or wording differences.

MiMo remains the sole production authority. A valid MiMo result proceeds through the existing lifecycle, attribution, binding, risk, and idempotency gates without waiting for DeepSeek. DeepSeek reviews the decision afterward and may affect only audit severity and operator notification.

## Confirmed Product Decisions

- Use two operator-visible levels: critical disagreements notify immediately; normal disagreements are persisted and shown in the Web detail view without sending a bot message.
- Run MiMo-authorized processing and automatic execution before DeepSeek semantic review.
- Use a hybrid reviewer: DeepSeek supplies an independent semantic interpretation, while deterministic code rules enforce risk floors and notification eligibility.
- DeepSeek must never authorize, block, modify, retry, cancel, compensate, or roll back a trade.

## Runtime Architecture

The synchronous message path is:

1. Run MiMo authoritative recognition.
2. Persist the MiMo decision with `comparison_status = pending`.
3. Apply the MiMo lifecycle or strategy result.
4. Pass the result through the existing trading safety gates.
5. Execute, skip, or fail the automatic action and persist the real automation outcome.
6. Return from the critical path without waiting for DeepSeek.

The asynchronous review path is:

1. A database-backed worker claims the oldest pending comparison.
2. DeepSeek independently interprets the original message and supplied safe context, then compares its interpretation with the persisted MiMo result.
3. Deterministic code normalizes the output and enforces severity floors.
4. The final review result is persisted as `none`, `normal`, or `critical`.
5. Only a critical result schedules an operator-bot notification.

The worker must use bounded concurrency. Startup recovery must resume pending work that survived a restart. A stale running claim must be recoverable without creating duplicate decision rows or duplicate notifications.

## DeepSeek Review Contract

The review prompt must require DeepSeek to interpret the original message before comparing it with MiMo. It must cite direct evidence from the current message and must not merely answer whether it agrees with MiMo.

The structured output is conceptually:

```json
{
  "independent_action": {
    "action_type": "none | entry | entry_confirm | cancel_entry | exit_full | exit_partial | position_update",
    "target_lifecycle_id": null,
    "symbol": null,
    "side": null,
    "stop_loss": null,
    "take_profit": null,
    "management_action": null
  },
  "evidence": ["direct evidence from the current message"],
  "conflict_types": [],
  "material_disagreement": false,
  "suggested_severity": "none | normal | critical",
  "confidence": 0.0,
  "reason": "short explanation"
}
```

Allowed conflict types must be a closed vocabulary so code can validate an attempted critical escalation. The prompt version and exact model invocation must remain auditable through the prompt registry and invocation records.

For an image-bearing message, DeepSeek may use the Telegram text and MiMo's persisted observation summary but must not claim to have inspected image pixels. Insufficient independent evidence cannot by itself create a critical disagreement.

## Severity Rules

### Critical

A disagreement is critical when it may materially change which real trading action or position is affected. Code must enforce a critical floor for:

- actionable versus no-action disagreement;
- entry versus exit, cancellation, or position-management disagreement;
- full exit versus partial exit;
- actionable symbol or side mismatch;
- target lifecycle mismatch that could act on the wrong strategy or position;
- opposite stop-loss or protection intent;
- a clear urgent exit that one interpretation misses;
- an authoritative action that was skipped or failed while the semantic review identifies a supported urgent risk-reduction instruction.

DeepSeek cannot downgrade a code-detected critical conflict. A DeepSeek-only critical escalation is accepted only when it uses an allowed material conflict type, supplies direct message evidence, and meets the configured confidence threshold. Otherwise it is limited to normal.

### Normal

Normal disagreements include non-critical entry or take-profit detail differences, management-detail uncertainty that does not reverse the action, and low-confidence semantic concerns. They are persisted and displayed but do not notify the operator bot.

### None

Equivalent numeric formats, capitalization, spacing, normalized synonyms, and reason-text differences are not material disagreements when the trading action is the same.

## Persistence

Extend the existing `recognition_decisions` audit record with semantic-review state equivalent to:

- `comparison_status`: `pending`, `running`, `completed`, or `failed`;
- `disagreement_severity`: `none`, `normal`, or `critical`;
- `comparison_model`;
- `comparison_payload_json`;
- `comparison_error`;
- `comparison_attempts`;
- `compared_at`.

The existing unique raw-message decision boundary remains. Re-recognition resets comparison state for the new authoritative result without making an older MiMo candidate executable again. Automation outcome is persisted before semantic review begins.

Notification state remains independently auditable. The implementation must claim notification work before making the network request and must prevent local retries or recovery scans from scheduling the same critical notification twice.

## Failure Handling

- MiMo failure remains fail-closed and independently notifies the operator; it does not wait for semantic review.
- DeepSeek timeout, transport failure, invalid JSON, or invalid schema never changes the MiMo or automation result.
- DeepSeek review uses a bounded retry policy, with a proposed maximum of three attempts and increasing delay.
- Exhausted work becomes `comparison_status = failed`; it is not reported as agreement.
- Individual comparison failures do not notify on every message. Repeated failures should feed a separate system-health signal to avoid notification floods.
- Existing lifecycle attribution, exact-position binding, risk, allowlist, confidence, and idempotency safeguards remain authoritative.

## Operator Notification

A critical notification includes:

- the Telegram source message and group identity;
- MiMo's authoritative action;
- the real automation outcome: submitted, executed, skipped, or failed;
- DeepSeek's independent interpretation;
- material conflict types and direct evidence;
- an explicit statement that processing already continued according to MiMo and is not waiting for approval.

The notification remains read-only. It must not add buttons that can accidentally repeat a trading action.

## Web Visibility

Message detail exposes one concise semantic-review state:

- AI review agreed;
- AI review normal difference;
- AI review critical disagreement;
- AI review pending;
- AI review failed.

Normal details are collapsed by default. Critical disagreements are visually prominent. The view must read from the authoritative `recognition_decisions` record rather than the legacy experiment-only comparison panel.

## Verification

Automated tests must prove:

1. A 60-second DeepSeek delay does not delay a MiMo-authorized exit reaching the automatic executor.
2. Full exit versus partial exit is critical.
3. Exit versus no action is critical.
4. Actionable symbol, side, or target-lifecycle mismatch is critical.
5. `62800` versus `62800.0`, capitalization, spacing, and normalized synonyms are none.
6. Same action with different reasoning does not notify.
7. A non-critical price/detail difference is normal, persisted, Web-visible, and not notified.
8. DeepSeek cannot downgrade a deterministic critical floor.
9. Unsupported or evidence-free DeepSeek escalation is limited to normal.
10. Timeout and invalid JSON retry, then fail without changing the trading outcome.
11. Startup recovery completes pending work.
12. Re-recognition and recovery do not duplicate a critical notification.
13. DeepSeek cannot claim independent image inspection or escalate without sufficient text evidence.
14. The Fengge `现价62800附近出局，空仓等待。` regression continues to use MiMo and reports only a material semantic conflict.
15. Existing entry, exit, cancellation, partial-take-profit, protective-stop, binding, and Deepcoin safety regressions remain green.

Production verification must run on the server after the reviewed implementation is pushed to `codex/deepcoin-auto-trading-v1` and deployed through the documented helper. It must verify schema migration, service health, execution-before-review ordering, critical-only bot delivery, normal Web visibility, retry/recovery behavior, and unchanged MiMo execution latency under a deliberately slow DeepSeek response.
