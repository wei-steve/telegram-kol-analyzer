# Entry preamble live verification

Deploy code while `entry_preamble_mode=disabled`. Deploying code does not
authorize a mode change. After the checks below, `live` applies explicit
entry-sizing preambles to every group already configured for automatic
trading; there is no separate chat allowlist.

## Pre-activation gate

1. Confirm there is no time-sensitive entry, cancellation, stop update, or
   position-management operation in progress.
2. In the Web prompt center, open `trading.analysis.shared`, copy the reviewed
   default change into a draft, validate it, run current historical tests for
   both MiMo and DeepSeek, and publish with a non-empty change note. Seeding at
   process startup will not replace an existing published prompt.
3. Run the focused server tests and controlled historical prompt comparisons.
   Do not submit a synthetic order to Deepcoin and do not fabricate normalized
   Telegram evidence.
4. Verify fixtures from at least two configured chats produce bounded evidence
   containing the mode, symbol, side, multiplier, configured/effective risk
   budgets, preamble message ID, strategy message ID, and assembly fingerprint.
5. Confirm the production safety monitor reports none of:
   `stale_entry_preamble_unresolved`, `entry_preamble_ambiguous`, or
   `live_entry_preamble_binding_evidence_missing`.

Historical messages created before this feature may not have an
`entry_preambles` row even when their raw text said “半仓操作”. Do not fabricate or
edit normalized evidence in SQLite. Use the read-only replay only against rows
that already contain authoritative preamble evidence.

## Live gate

After explicit approval and a second check that no time-sensitive operation is
active, change only `entry_preamble_mode` to `live`. This immediately affects
new matching entries in all configured trading groups. An entry without a
matching preamble continues to use its configured full risk budget.
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

Set `entry_preamble_mode=disabled`. This prevents new persistence and assembly
and does not alter existing orders or positions. Do not delete existing preamble,
assembly, recovery, draft, or execution-binding evidence. If a live order was
already submitted, manage it through the normal attributed-position workflow;
never replay the Telegram entry message.
