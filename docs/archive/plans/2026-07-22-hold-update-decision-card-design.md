# Hold Update Decision Card Design

## Problem

The decision card currently treats every recognized lifecycle event as `manual_review`. Ordinary BTC position commentary such as “continue holding”, “watch the market”, or “prepare to add” is therefore shown as an orange manual-intervention item despite having no executable instruction.

## Decision

For a `position_update` whose `management_action` is `hold_update`, the card will show:

- state: `record_only`
- label: `仅记录`
- recommended action: `无需操作`

The source event, MiMo conclusion, DeepSeek review, and automation record remain visible under the same card. This is a display classification only; it does not alter stored recognition decisions, lifecycle records, or exchange requests.

## Guardrails

- A stop-management event without a new stop-loss price remains `需人工确认`.
- Recognition failures remain `获取失败`.
- Other lifecycle events retain their existing treatment until they receive their own explicit actionability rule.

## Verification

Add a focused query test for a `hold_update` decision and confirm it is `record_only`, while preserving the existing missing-stop test as the manual-review regression guard.
