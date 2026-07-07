# Exchange Position Tabs Design

## Goal

Add an isolated main dashboard tab for an exchange-style order and position view. The new tab should sit beside the existing dashboard settings entries and avoid changing the current KOL strategy workbench layout until the feature is stable.

The tab will mirror the Deepcoin mobile position area with four sub-tabs:

- 持仓
- 当前委托
- 历史委托
- 历史仓位

## Placement

The dashboard header settings menu will get a new entry named `交易持仓`. Clicking it switches to a new panel, `data-dashboard-panel="exchange-positions"`, using the existing dashboard tab switching mechanism.

This panel is separate from the current middle strategy panel. The existing `持仓 / 待入场 / 已离场` KOL strategy filters remain unchanged.

## Layout

The new panel uses a compact exchange workspace layout:

- A panel header with title `交易持仓` and a return button to the main dashboard.
- A horizontal Deepcoin-style sub-tab strip for the four order and position states.
- A card list for mobile-like readability, matching the Deepcoin mobile pattern of symbol, side, account/margin, leverage, price metrics, status, quantity, and timestamps.
- Independent empty states per sub-tab.

## Data Mapping

The first implementation uses existing local data so it is safe to ship before any new Deepcoin API integration.

`持仓` shows active live or bound positions from the existing execution overview data. Cards prioritize symbol, side, execution status, entry price, position size, stop loss, take profit, protection status, position id, and last checked time.

`当前委托` shows pending entry strategy records and execution previews that represent orders waiting to be submitted or filled. Cards prioritize symbol, side, planned entry, risk, blocking reasons, and available submit actions where those already exist.

`历史委托` shows locally known cancelled, expired, or exited order-related strategy records. If the current local model cannot cleanly distinguish order history from position history, it will render a clear empty state and keep the tab ready for future Deepcoin open/history order APIs.

`历史仓位` shows exited or expired strategy lifecycle records. Cards prioritize symbol, side, entry price, exit price, realized result when available, exit reason, entered time, and exited time.

## Interaction

Sub-tab switching happens client-side within the new dashboard panel. The active sub-tab gets a visible underline and accessible pressed/current state.

The first sub-tab defaults to `持仓`. The current dashboard tab switching code remains the top-level navigation controller.

## Error Handling

If a data source is empty or unavailable, the panel renders an empty state rather than blocking the dashboard.

Existing live sync or Deepcoin API failures should not prevent the new tab from loading because the first version depends on already available local query helpers.

## Testing

Render tests should verify:

- The dashboard includes the new `交易持仓` top-level menu entry.
- The dashboard includes `data-dashboard-panel="exchange-positions"`.
- The four sub-tabs render in the requested order.
- Existing KOL strategy filters still render unchanged.

Frontend smoke coverage should verify that the sub-tab buttons and panel markers are present in the static assets or rendered HTML.
