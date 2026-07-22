# Actionable Strategy Decision Card Design

## Goal

Separate “identified strategy” from “manual review” in the message decision card without expanding automatic trading authority.

## Card States

| Source evidence | Card state | Meaning |
| --- | --- | --- |
| Complete strategy fields: symbol, side, entry, stop loss, take profit | `strategy_identified` / 策略已识别 | A complete opening strategy was recognized; show the existing automation record. |
| Lifecycle event has a unique `target_lifecycle_id`, models agree, and no severe disagreement | `strategy_linked` / 已关联策略 | The message was linked to an existing strategy; show the recorded action and execution status. |
| Missing price, ambiguous target, or severe disagreement | `manual_review` / 需人工确认 | Do not imply a safe action. |
| `hold_update` | `record_only` / 仅记录 | Informational position commentary. |

## Safety

This design only changes display labels and suggested actions. It does not create lifecycle records, call the exchange, retry any request, or override `automation_status`.
