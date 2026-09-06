# Exchange Tab Manual Refresh Design

## Goal

Give the `当前委托`, `历史委托`, and `历史仓位` tabs an explicit, reliable
manual refresh path without adding periodic Deepcoin polling or changing the
existing live-position snapshot behavior.

## Current Behavior and Root Cause

The three non-position exchange tabs are loaded lazily through
`GET /positions-panel/tabs/{tab_name}`. The browser marks a successful partial
with `data-exchange-tab-loaded="true"` and keeps it in memory. Subsequent tab
clicks return early from `loadExchangePositionTab()`, so they never read
Deepcoin again. The interface has no tab-specific refresh control, and the
global Telegram refresh control does not refresh exchange tabs.

This matches the performance goal of avoiding unnecessary Deepcoin reads, but
it leaves the operator unable to request a newer snapshot.

## Scope

The change covers:

- `当前委托` (`open-orders`);
- `历史委托` (`order-history`);
- `历史仓位` (`position-history`).

The `持仓` tab keeps its existing persisted-snapshot and bounded background
refresh mechanism. No tab gains interval polling, focus refresh, visibility
refresh, or refresh-on-selection behavior. Every refresh in this design is an
explicit operator action.

## Considered Approaches

### Shared active-tab refresh control — selected

Place one control next to the list/grouped view selector. Its label follows the
active tab, for example `刷新当前委托`. The control is hidden on `持仓` and
shown on the other three tabs.

This keeps the behavior discoverable while centralizing request state, status
text, accessibility attributes, and event binding.

### Refresh control inside every partial

Each lazy partial could render its own button. This is visually direct but
duplicates markup and binding behavior across three replaceable fragments.

### Re-click the active tab to refresh

This adds no new control but is not discoverable and can trigger unexpected
Deepcoin reads. It is rejected.

## User Interface

The exchange view row becomes a compact toolbar containing:

- the existing `真实列表` and `按群组` selector;
- an active-tab refresh button;
- a polite status area for refresh time or failure state.

The refresh control behaves as follows:

- hidden while `持仓` is active;
- labeled `刷新当前委托`, `刷新历史委托`, or `刷新历史仓位`;
- disabled and labeled `刷新中…` while its request is active;
- restored after success or failure;
- usable with keyboard navigation and announced through the status area.

A successful partial exposes its item count and capture time. The browser
updates the matching tab label, such as `当前委托(5)`, and reports
`更新于 HH:mm:ss UTC`.

## Browser Data Flow

`loadExchangePositionTab(root, tab, options)` gains an explicit `force`
option. Normal lazy loading keeps the existing loaded-state short circuit.
Manual refresh calls the same function with `force: true`, bypassing only that
short circuit.

The existing per-root, per-tab promise map remains the single-flight boundary.
If a request for the selected tab is already active, another refresh action
reuses that promise and never creates a second Deepcoin request.

During a forced refresh the current partial remains mounted. The browser marks
it busy but does not replace its contents with a loading placeholder. On
success it replaces only that tab partial, restores the selected tab and
list/grouped view, updates the tab count and capture time, and rebinds controls
owned by the new fragment.

Initial lazy loading keeps its existing placeholder because no prior successful
content exists.

## Error Handling

Initial-load and refresh failures have different presentation rules:

- An initial-load failure renders a retryable unavailable state with a
  `重新加载` action.
- A forced-refresh failure keeps the previous successful partial unchanged,
  removes its busy state, and reports
  `刷新失败，当前展示上次成功数据`. The refresh button becomes available
  again.

The browser must not set a previously successful partial to unloaded after a
failed forced refresh. A later manual attempt remains possible.

## Server Contract

The existing read-only tab route remains authoritative and continues to read
only the requested dataset. The response partial adds bounded display metadata:

- `data-exchange-tab-item-count`;
- `data-exchange-tab-captured-at` in UTC.

No new mutation route or cache is introduced. Current orders still call only
the normal open-order API plus relevant pending-trigger reads. Each history tab
continues to use only its existing history methods.

The refresh result is display evidence only. Close, cancel, bind, and TPSL
mutations continue to revalidate directly against Deepcoin and never consume
the rendered partial as authority.

## State Preservation

A successful refresh preserves:

- the active exchange tab;
- `真实列表` versus `按群组` mode;
- the surrounding positions workbench and its scroll context.

Open card details may be restored when the refreshed fragment still contains
the same stable order or position identifier. Missing identifiers are ignored;
refresh must not keep a detail open for a different record.

## Verification

Local deterministic tests will cover:

- refresh control visibility and active-tab labels;
- normal tab selection continuing to reuse a loaded partial;
- forced refresh bypassing the loaded-state short circuit;
- duplicate refresh actions sharing one in-flight request;
- successful replacement, item-count update, timestamp update, and UI-state
  restoration;
- forced-refresh failure preserving successful content and enabling retry;
- initial-load failure exposing an explicit retry action;
- all three route responses exposing bounded metadata;
- each route continuing to call only its requested Deepcoin methods.

Production verification must run after the reviewed commit is pushed and
deployed through the existing server update workflow. It will manually refresh
all three tabs, compare the visible results with Deepcoin, verify the failure
state without creating a trade mutation, and confirm no duplicate requests.
Deployment must wait for a proven safe window with no active time-sensitive
strategy operation.

## Rollback

The change is isolated to read-only partial metadata, browser behavior, styling,
and tests. Rolling back the implementation commit restores the existing lazy
load-once behavior. No database migration, persisted setting, exchange write,
or service-side state conversion is required.
