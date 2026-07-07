# Exchange Strategy Attribution Design

## Goal

The exchange position dashboard should show which Telegram group strategy each real Deepcoin position and order belongs to. The page must support two switchable views:

- Real list view: Deepcoin positions and orders remain the primary list, with strategy attribution shown on each card.
- Group view: the same attributed items are grouped by Telegram group, with an explicit unassigned group for items that cannot be matched.

This first version is display-only. It should not add manual binding or correction actions yet.

## User Experience

Add a compact view switch near the top of the existing exchange positions panel:

- Real list
- Grouped by group

The existing tabs stay in place: positions, open orders, order history, and position history.

In real list view, each card adds an attribution block:

- Group name, when known.
- Strategy summary, such as symbol, side, entry range, stop loss, take profit, and a short source text.
- Attribution state:
  - Bound: matched through an existing execution binding or lifecycle record.
  - Candidate: inferred from symbol, side, price, and time proximity.
  - Unassigned: no reasonable strategy candidate found.
- Current orders also show an order role when inferable: entry order, take-profit order, stop-loss order, trigger order, or regular order.

In grouped view, the selected exchange tab still controls which item type is shown. Items are grouped into sections:

- One section per Telegram group with matching items.
- One unassigned section for unassigned items.

Each group section shows the group name, item count, and the same cards as the real list view.

## Data Flow

Build an attribution layer after the Deepcoin snapshot is loaded.

Inputs:

- `exchange_snapshot.positions`
- `exchange_snapshot.open_orders`
- `exchange_snapshot.order_history`
- `exchange_snapshot.position_history`
- `holding_positions`
- `pending_entry_signals`
- `exited_positions`
- existing execution bindings already surfaced by `_load_deepcoin_live_position_rows`

Outputs:

- Each exchange snapshot item gets an `attribution` dictionary:
  - `state`: `bound`, `candidate`, or `unassigned`
  - `label`: localized display text for the attribution state
  - `chat_id`
  - `group_name`
  - `strategy_id` or lifecycle id when available
  - `strategy_summary`
  - `source_excerpt`
  - `score`
  - `reasons`
  - `order_role` for orders when inferable
- `exchange_snapshot.grouped` contains grouped lists for each exchange tab:
  - `positions`
  - `open_orders`
  - `order_history`
  - `position_history`

The grouping structure should be plain dictionaries/lists so the Jinja templates stay simple.

## Attribution Rules

Use a conservative ranking:

1. Bound lifecycle data wins. If a live position already has group/source fields from an existing binding, mark it `bound`.
2. Match by normalized symbol and side against active strategy rows.
3. Improve confidence when entry price, order price, stop loss, or take profit is close to the strategy values.
4. Improve confidence when the exchange timestamp is near the strategy creation or execution timestamp.
5. If the best score is clearly above the threshold and not tied, mark as `candidate`.
6. Otherwise mark as `unassigned`.

For orders:

- Pending entry signals are the main candidates for entry orders.
- Holding strategies are the main candidates for stop-loss and take-profit orders.
- Historical order rows may match exited strategies, but uncertain matches should remain `unassigned`.

The first version should prefer being explicit about uncertainty over forcing a wrong group.

## UI Details

Use the existing exchange card layout and avoid a major redesign.

Add small attribution elements inside each card:

- A group chip or unassigned chip.
- A short one-line strategy summary.
- A muted reason line for candidate matches, such as "inferred from BTC long side, close price, and close time".

For unassigned items, keep the card visible and style the chip neutrally. This is important because unassigned real positions or orders are operationally important.

The group view should not duplicate tabs. It should reuse the existing selected tab and only change the body layout.

## Error Handling

If attribution fails, the Deepcoin snapshot should still render. Cards should fall back to unassigned, and the page should show a small non-blocking warning if needed.

If a group name is missing, display "Group <chat_id>". If both group name and chat id are missing, use "Unknown group".

## Testing

Add focused tests for:

- A bound live position shows its group attribution.
- An inferred current order shows a candidate attribution and appears under the group in grouped view.
- Unmatched orders remain visible under the unassigned group.
- The existing exchange tab counts still use real Deepcoin snapshot counts.

Server verification remains required for live Deepcoin data because the API credentials and IP allowlist only work on the server.
