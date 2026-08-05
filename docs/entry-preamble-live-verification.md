# Entry preamble live verification

This feature must be deployed dormant. The initial production setting is
`entry_preamble_mode=disabled` with an empty
`entry_preamble_live_chat_ids` list. Deploying code does not authorize a mode
change.

## Shadow gate

1. Confirm there is no time-sensitive entry, cancellation, stop update, or
   position-management operation in progress.
2. In the Web prompt center, open `trading.analysis.shared`, copy the reviewed
   default change into a draft, validate it, run current historical tests for
   both MiMo and DeepSeek, and publish with a non-empty change note. Seeding at
   process startup will not replace an existing published prompt.
3. Set only `entry_preamble_mode=shadow`; keep the live chat allowlist empty.
4. Verify a known pair produces bounded evidence containing the mode, symbol,
   side, multiplier, configured/effective risk budgets, preamble message ID,
   strategy message ID, and assembly fingerprint. The effective execution
   multiplier must remain `1` in shadow.
5. Confirm the production safety monitor reports none of:
   `stale_entry_preamble_unresolved`, `entry_preamble_ambiguous`, or
   `live_entry_preamble_binding_evidence_missing`.

Historical messages created before this feature may not have an
`entry_preambles` row even when their raw text said “半仓操作”. Do not fabricate or
edit normalized evidence in SQLite. Use a newly received shadow-only pair, or
run the read-only replay against rows that already contain authoritative
preamble evidence.

## Live gate

Live promotion requires separate explicit approval after reviewed shadow
evidence. Add only the approved chat ID to
`entry_preamble_live_chat_ids`, then change `entry_preamble_mode` to `live`.
For a 20 USDT configured risk budget and a 50% multiplier, verify the operator
record says:

```text
基础风险预算 20 USDT × 仓位倍率 50% = 实际风险预算 10 USDT
前置消息 9901 / 策略消息 9902
```

The persisted execution binding must contain the same assembly fingerprint as
the immutable `entry_strategy_assemblies` row. Ambiguity, invalid multiplier,
missing evidence, edits, or deletions must fail closed.

## Immediate rollback

Set `entry_preamble_mode=disabled` and clear
`entry_preamble_live_chat_ids`. This prevents new persistence and assembly and
does not alter existing orders or positions. Do not delete existing preamble,
assembly, recovery, draft, or execution-binding evidence. If a live order was
already submitted, manage it through the normal attributed-position workflow;
never replay the Telegram entry message.
