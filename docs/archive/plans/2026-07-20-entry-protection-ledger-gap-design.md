# Entry Protection Ledger Gap Design

## Goal

Close two verified protection-ledger gaps without weakening TPSL ownership rules: market-entry protection where Deepcoin returns one TPSL order id plus a same-time sibling, and trigger-entry protection whose TPSL rows appear only after the trigger entry fills.

## Approach

Market-entry online recording should keep requiring a verified entry leg and exact local request details. If Deepcoin returns one TPSL order id, the recorder may use that returned id as the anchor when the pending TPSL row still exists, matches instrument, side, trigger price, and event time. The missing sibling may be recorded only when it shares the anchor timestamp group, matches the remaining requested TP/SL exactly, and is unique.

Trigger-entry protection should remain a repair path rather than an immediate online write, because the position id is not known when the trigger order is submitted. A guarded dry-run/apply planner should match each verified trigger-limit entry leg to exactly one current pending TPSL row by instrument, side, size, time, and requested TP/SL prices. Ambiguity, missing current TPSL rows, or conflicting existing ledger rows must be refusals.

## Safety

No code path may attribute TPSL ownership by symbol, side, price, group label, or message text alone. Current production repairs must be dry-run first and apply only with the displayed fingerprint.
