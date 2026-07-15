# Deepcoin Order Management Notes

Last checked: 2026-07-05.

Deepcoin API documentation pages are CloudFront-blocked from this environment, so these notes combine the official endpoint names already captured in `docs/context/telegram-deepcoin-auto-trading-context.md`, current project tests, the observed live payload shape from the execution dashboard, and guarded live probes against the server account.

## Endpoint Roles

- `POST /deepcoin/trade/order`: regular market/limit order. Market entry can create a position immediately, then protection should be applied with `set-position-sltp`.
- `POST /deepcoin/trade/trigger-order`: trigger/conditional order. For our current limit-entry flow, the entry trigger order can include `tpTriggerPx`, `slTriggerPx`, `tpOrdPx=-1`, and `slOrdPx=-1`.
- `POST /deepcoin/trade/cancel-trigger-order`: cancel a pending trigger / conditional order by `instId` and `ordId`.
- `POST /deepcoin/trade/set-position-sltp`: add TP/SL on an existing position. Live testing showed repeated calls append additional TPSL rows instead of replacing old ones. In split position mode, always include the matched `posId`. For full-position protection omit `sz`; never send `sz: "0"`. Partial protection sends its exact positive canonical `sz`.
- `POST /deepcoin/trade/cancel-position-sltp`: cancel one exact position TPSL row. The required payload is `instType`, `instId`, and `ordId`. Position TPSL must never be cancelled through `cancel-trigger-order`.
- Both position TPSL write paths currently document default limits of 15 requests/second and 450 requests/minute. All set, cancel, compensation, and restoration writes share one process-level limiter per Deepcoin credential UID.
- `POST /deepcoin/trade/replace-order-sltp`: documented for open-order TP/SL replacement, but server live probes rejected both normal limit and trigger-limit cases. Treat it as unavailable until a working Deepcoin payload is confirmed.
- `GET /deepcoin/trade/orders-pending`: list pending regular orders. Use this to verify a regular limit order is still open or has disappeared after cancel.
- `GET /deepcoin/trade/orders-history`: list historical regular orders. Server live probes confirmed this returns filled and cancelled regular orders, including market open/close rows.
- `GET /deepcoin/trade/trigger-orders-pending`: list pending trigger and TPSL orders. This is the read path used to find the current stop-loss or take-profit trigger order before a KOL management update.
- `GET /deepcoin/trade/trigger-orders-history`: list historical trigger / TPSL orders. Server live probes confirmed this returns cancelled trigger-limit orders and cancelled TPSL rows, including historical TP/SL trigger prices.
- `GET /deepcoin/account/positions`: list current positions. This is the read path used to bind `posId` and detect manual closes.

## Matching Rules

Every Deepcoin order created for a KOL signal must keep the local binding:

- `strategy_instance_id`: `venue:chat_id:message_id:symbol:side`
- `client_order_id`: deterministic `clOrdId`
- `order_id`: Deepcoin entry / trigger order id
- `pos_id`: Deepcoin position id once available
- `margin_mode` and `position_mode`

`execution_bindings` stores the strategy-level attribution. `execution_order_legs`
stores each Deepcoin order leg separately, keyed by `execution_binding_id`,
`purpose`, and `leg_index`. Each leg records `order_kind`, `ordId`, `clOrdId`,
`posId`, status, and compact request/response JSON. This is the source of truth
for later actions like partial close, temporary exit, trigger-entry cancel, and
trigger-entry TP/SL recreation when one KOL signal created multiple Deepcoin
orders.

Legacy bindings can be backfilled with:

```bash
telegram-kol-research repair-execution-order-legs --database-path data/research.db
```

Limit / trigger entries are asynchronous. The initial order response may only
confirm the pending order or trigger order, while the eventual filled entry can
appear later as a different Deepcoin order id. The web service therefore runs a
background Deepcoin execution reconciliation loop. It scans open bindings,
regular order history, trade fills, trigger order history, and current
positions. When a trigger entry has fired, the loop can use the original
trigger order id plus filled price, size, side, and time evidence to bind the
resulting `posId` back to the original KOL strategy binding.

System-created Deepcoin orders should never be treated as anonymous KOL
positions. If a filled order cannot be safely attributed to exactly one local
strategy binding, it must remain an execution attribution problem rather than
being counted as a normal KOL holding.

For a later stop-loss update, resolve the target in this order:

1. Pending SL trigger order with matching Deepcoin order id or `clOrdId`.
2. Pending SL trigger order with matching `posId`.
3. A single pending SL trigger order matching instrument + side.
4. Active position fallback: cancel the matched position's existing TPSL trigger orders, then call `set-position-sltp` with `posId`.
5. Unfilled trigger-entry fallback: cancel the old trigger order and recreate it with the same entry fields and the new TP/SL.

If instrument + side has more than one candidate and there is no `posId` or order id match, fail closed and require manual review. This matters because multiple KOLs can hold BTC long at the same time.

Live probe findings:

- Repeated `set-position-sltp` calls append multiple TP/SL rows for the same position. They do not safely replace old protection.
- Deepcoin rejected `replace-order-sltp` for a normal open ETH limit order with `InvalidAction:TriggerOrder not found, OrderAction should be used instead`.
- Deepcoin rejected `replace-order-sltp` for a pending conditional trigger order with `OrderNotFound`.
- The reliable tested adjustment path for existing positions is cancel old matched TPSL rows, then add the new TPSL.
- The reliable tested adjustment path for unfilled trigger/limit entry is cancel-and-recreate.

## Current Code

- `src/telegram_kol_research/recovery_live_submit.py` builds Deepcoin order, trigger-order, position-SLTP, and order-SLTP payloads, and dispatches non-entry queued trade signals into the Deepcoin management action layer.
- `src/telegram_kol_research/auto_trade_execution.py` auto-bridges recognized `close_signal` and `position_update` candidates into queued management actions when there is exactly one matching active Deepcoin binding.
- `src/telegram_kol_research/deepcoin_execution_actions.py` executes high-risk management actions with explicit matching and event logging.
- `src/telegram_kol_research/deepcoin_order_matching.py` normalizes pending TPSL trigger orders and selects the exact stop-loss adjustment target.
- `src/telegram_kol_research/deepcoin_client.py` exposes pending/history read helpers for regular orders and trigger/TPSL orders.
- `src/telegram_kol_research/execution_bindings.py` persists the local strategy to Deepcoin id mapping.

Supported queue actions:

- `open_position`: existing entry path. Market entries place the order and then add position TP/SL. Limit entries use Deepcoin trigger orders with embedded TP/SL. Multi-stage take-profit now sorts targets by nearest-first (`long`: low to high, `short`: high to low), uses `50/50` for two targets and `40/30/30` for three targets, and caps live submission at the first three targets. Market entries set a full-position stop loss first, then append partial TP rows with `sz`. Limit trigger entries are split into child trigger orders per TP target because there is no `posId` before the entry fills.
- `set_position_tpsl`: first-time position protection only. This can add TP/SL when no old protection exists.
- `adjust_position_tpsl`, `adjust_stop_loss`, `adjust_take_profit`: position protection adjustment. These require a matched old TPSL row, cancel all old matched position TPSL orders, preserve the unchanged TP or SL side, then call `set-position-sltp` once.
- `close_position`, `exit_position`, `temporary_exit`, `temporary_close`: market close using the exact bound `closePosId`.
- `partial_close_and_move_stop_to_entry`: for an exact same-chat, same-KOL, same-symbol, same-side set of split positions, market-close the requested fraction of every bound position and then move each successful target's stop loss to its own live `avgPx`, preserving its existing take profit. Every target must have an exact binding, `posId`, and positive average entry price. A failed partial close never changes that target's stop; other exact targets report their own outcome independently.
- `cancel_entry`, `cancel_limit_entry`, `cancel_trigger_entry`: cancel a bound unfilled regular or trigger entry order after verifying it is still pending.
- `adjust_trigger_entry_tpsl`, `recreate_trigger_entry`: unfilled trigger-limit TP/SL adjustment by canceling the old trigger order and recreating it with the same entry fields plus the new TP/SL.

Safety rule: never use `adjust_*` as a blind `set-position-sltp` call. If old TPSL rows cannot be matched, the code fails closed with `no_existing_position_tpsl_to_adjust` so it does not append a second TP/SL pair.

## Historical Audit

Exchange history is necessary but not sufficient:

- Deepcoin `orders-history` can prove a market order filled, a close order filled, or a regular limit order was cancelled/filled.
- Deepcoin `trigger-orders-history` can prove an old trigger-limit order or old TPSL order was cancelled, and it preserves the old TP/SL prices.
- Deepcoin history does not know which Telegram KOL message caused the action, or whether an action was part of "move SL", "partial TP", or cleanup after a failed adjustment.

The project now has an append-only local execution-event ledger in `execution_events`. Each action should record:

- `strategy_instance_id`, `execution_binding_id`, `chat_id`, `message_id`, symbol, side
- action type: `open_market`, `set_position_tpsl`, `cancel_position_tpsl`, `adjust_position_tpsl`, `create_trigger_entry`, `cancel_trigger_entry`, `recreate_trigger_entry`, `close_position`
- Deepcoin ids: `ordId`, `clOrdId`, `posId`, and cancelled/recreated order ids
- before/after TP/SL prices and quantities
- request JSON and response JSON
- source reason: entry signal, stop-loss update, take-profit update, temporary exit, cleanup, manual action
- timestamps from local clock and Deepcoin `cTime/uTime/fillTime` when available

`src/telegram_kol_research/execution_events.py` provides the write/read helpers. Current live order submission records market opens, position TP/SL setup, and trigger-entry creation. The management action layer now records `cancel_position_tpsl`, `adjust_position_tpsl`, `set_position_tpsl`, `close_position_market`, `cancel_trigger_entry`, `cancel_regular_entry`, and `recreate_trigger_entry` through the same ledger.

This gives us a complete timeline for questions like "which TP was active before this KOL update?", "did the old stop-loss actually get cancelled?", and "which partial take-profit closed part of the position?".

## Verification

Focused offline tests cover:

- splitting one Deepcoin TPSL payload into stop-loss and take-profit legs
- matching a stop-loss order by `posId`
- matching by `clOrdId`
- rejecting ambiguous same-symbol same-side SL orders
- falling back to `set-position-sltp` for active positions
- falling back to explicit unfilled-entry handling instead of guessing a TP/SL order
- executing `adjust_stop_loss` without duplicating TP/SL rows
- refusing `adjust_stop_loss` when no old TPSL rows can be matched
- closing a bound position with `closePosId`
- canceling a bound trigger entry
- recreating an unfilled trigger entry with adjusted TP/SL
- auto-bridging `close_signal` into `close_position`
- auto-bridging `position_update` into `adjust_stop_loss`

## Live TP/SL Probe

`scripts/deepcoin_tpsl_probe.py` is the guarded integration probe for ETH long TP/SL management.

Dry-run:

```powershell
.\.venv\Scripts\python.exe scripts\deepcoin_tpsl_probe.py
```

Live mode is blocked unless both are present:

```powershell
$env:DEEPCOIN_API_KEY="..."
$env:DEEPCOIN_API_SECRET="..."
$env:DEEPCOIN_API_PASSPHRASE="..."
$env:DEEPCOIN_LIVE_TPSL_TEST_CONFIRM="ETH_0.1_TPSL_TEST"
.\.venv\Scripts\python.exe scripts\deepcoin_tpsl_probe.py --live
```

The probe sequence is:

1. Market ETH long for 0.1 ETH, currently `1` contract from `contract_value=0.1`.
2. Add position TP/SL with `set-position-sltp`.
3. Adjust that position TP/SL by cancelling the position's existing TPSL rows, then calling `set-position-sltp`.
4. Place far-away ETH long trigger-limit order at `1000` with embedded TP/SL.
5. Adjust the unfilled trigger-limit order by cancelling it and recreating it with new TP/SL.
6. Cancel the recreated far-away trigger-limit order.

The market-position probe attempts a market close at the end unless `--keep-position` is supplied. In split position mode, close requests must include the exact `closePosId`; a plain reverse market order can fail with `NotEnoughPositionToClose`.

Server live result on 2026-06-30:

- Market ETH long 0.1 opened successfully.
- `set-position-sltp` added TP/SL successfully.
- A second `set-position-sltp` without cancellation created duplicate TP/SL rows. This is unsafe.
- The corrected flow cancels existing matched TPSL rows before setting the new TP/SL.
- The test position was closed successfully with `closePosId`.
- Normal far-away ETH limit order at `1000` was accepted, but `replace-order-sltp` failed.
- Far-away ETH trigger-limit order at `1000` with embedded TP/SL was accepted.
- Trigger-limit TP/SL adjustment succeeded by cancelling the old trigger order and recreating it with new TP/SL.
- Test trigger orders were cancelled; only pre-existing account TPSL orders remained.
