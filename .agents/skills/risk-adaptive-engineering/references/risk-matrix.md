# Risk Matrix

## Authorization comes first

- Answer, explain, and status requests remain read-only.
- Diagnose and review requests may use read-only delegation but do not authorize fixes.
- Change, build, fix, and deploy requests may enter L0, L1, or L2.

## L0 — Main Sol only

Use only when every condition is true: isolated, well understood, locally verifiable,
no shared interface change, no persistent or production impact, and readily reversible.

## L1 — Compact team

Use for ordinary multi-file work, internal interface changes, ordinary features,
uncertain non-trading defects, or meaningful regression risk when no L2 trigger applies.

## L2 — Full safety team

Any trading decision, order, cancellation, position, protection, TP/SL, partial-close,
breakeven, recognition, context, targeting, attribution, group-isolation, risk-limit,
contract-specification, quantity, price, leverage, persistent-state, migration, backfill,
state-repair, concurrency, retry, idempotency, recovery, compensation, private Deepcoin
write, production write path, deployment, reinstall, restart, current-position,
unknown-root-cause, unclear-impact-boundary, or explicit highest-safety request forces L2.

## Tie breakers

- Any L2 trigger wins over L0 or L1 characteristics.
- Uncertainty raises the level; it never lowers it.
- The main agent may increase risk based on repository evidence but may not downgrade a hard trigger.
- Routing does not expand the user's authorization or production permissions.

## Representative classifications

| Request | Expected route |
| --- | --- |
| Correct a documentation typo | L0 |
| Small isolated non-trading UI correction | L0 or L1, with evidence |
| Add an ordinary multi-file reporting feature | L1 |
| Change order sizing or leverage calculation | L2 |
| Repair strategy attribution | L2 |
| Restart the production service | L2 |
| Diagnose an ambiguous current-position defect | L2 read-only investigation |
