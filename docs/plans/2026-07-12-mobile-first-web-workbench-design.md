# Mobile-First Web Workbench Redesign

## Goal

Redesign the existing web workbench around fast mobile monitoring while preserving complete desktop functionality. The first screen must answer two questions quickly: whether the account is safe and what important event happened most recently.

The redesign keeps the existing Flask/Jinja application, backend routes, and trading controls. It reorganizes information and interaction instead of creating a separate mobile frontend or adopting a new client framework.

## Confirmed Product Decisions

- Mobile is the primary usage environment; desktop is an enhanced workspace.
- The home screen combines a compact risk summary with a unified event timeline.
- Low-risk actions such as refresh, filtering, and navigation are available directly from the home screen.
- Position closing, live-position binding, and trading-setting changes require entering a detail view and confirming the action.
- Mobile and desktop use the same information hierarchy and server data.

## Information Architecture

The primary navigation contains five destinations:

1. **首页**: account risk, service health, and the unified event timeline.
2. **持仓**: open positions, pending orders, risk controls, and position history.
3. **策略**: executing, pending-confirmation, pending-entry, completed, and abnormal strategies.
4. **消息**: KOL/group navigation, chronological Telegram messages, and AI recognition results.
5. **更多**: trading settings, AI configuration, prompts, recognition profiles, and logs.

The mobile view uses a fixed bottom navigation bar. The desktop view uses the same destinations in a left navigation rail, a primary content area, and an optional right-side detail drawer. It does not return to the current three equally weighted columns.

## Home Screen

### Risk Summary

The top section contains four high-priority metrics:

- Current position count and total unrealized profit or loss.
- Number of risk exceptions, including missing stops and unmatched positions.
- Number of strategies awaiting execution or confirmation.
- Independent Telegram, database, and Deepcoin connection states.

The summary uses large values and subdued supporting labels. It can collapse into a compact status row while the user scrolls through events.

When a risk exception exists, the highest-priority exception appears immediately below the summary with a direct link to its detail view. The home screen does not expose the dangerous corrective action itself.

### Unified Event Timeline

The timeline joins the operational story across existing domains:

`KOL message -> AI recognition -> strategy creation or update -> order submission -> fill -> position management -> exit`

Events are ordered newest first on the home screen. Each compact card shows only the fields needed to identify and assess it:

- Time and event type.
- KOL or source group.
- Instrument and direction where applicable.
- Short human-readable event summary.
- Current status and destination indicator.

The timeline provides filters for all events, messages, strategies, fills, and exceptions. New live events do not steal the user's scroll position; a persistent “new events” control lets the user reveal them deliberately.

Selecting an event opens the relevant message, strategy, position, or execution detail rather than rendering every technical field inside the timeline.

## Positions

The positions destination contains `持仓`, `挂单`, and `历史` tabs. Its summary shows total unrealized profit or loss, margin usage when available, and the number of risky positions.

An open-position card prioritizes:

- Instrument and long/short direction.
- Unrealized profit or loss.
- Entry and mark prices.
- Position size.
- Stop-loss and take-profit state.
- Strategy and KOL attribution.

Missing stops, missing attribution, or stale exchange data appear as persistent warnings at the top of the card. Closing and binding actions live in the position detail view. Confirmation content includes instrument, direction, quantity, source KOL when available, and the intended effect.

## Strategies

The strategy destination provides `执行中`, `待确认`, `待入场`, `已结束`, and `异常` filters. Cards show KOL, instrument, direction, entry range, stop, take-profit levels, and lifecycle status.

The strategy detail view combines:

- Original Telegram content.
- AI recognition output and confidence.
- Strategy lifecycle changes.
- Related order, fill, and position events.

Strategies requiring human intervention state the exact blocking reason, such as a missing stop, insufficient confidence, or failure to match a live position.

## Messages

The first level lists KOLs and groups ordered by latest activity, with unread count and last-message preview. The second level presents messages chronologically in a Telegram-like conversation view.

Message filters include all messages, recognized strategies, unrecognized messages, image messages, and exceptions. Selecting a message reveals its AI recognition and linked strategy in the detail view. The main message list does not expand every recognition field inline.

## Visual System

- Retain a dark trading-console theme while reducing border density and competing accent colors.
- Use green for profit, red for loss, orange for actionable risk, and neutral colors for ordinary state.
- Use large numeric typography for decision-critical values and lower contrast for supporting metadata.
- Identify event types with a narrow accent rather than fully colored cards.
- Use spacing and touch targets suitable for thumb operation.
- Keep persistent labels alongside status colors so meaning never depends on color alone.
- Preserve existing content terminology where it is already familiar to operators.

## Interaction and Safety

- Refresh, filtering, navigation, and detail opening are direct actions.
- Closing a position, binding a live position, and changing trading configuration occur only from a detail view.
- Dangerous actions use an explicit confirmation dialog and disable repeat submission while pending.
- Results remain visible after completion. Failures explain the cause and a useful next step.
- Backend authorization, validation, reservation, and idempotency remain authoritative.

## Loading, Empty, and Failure States

- Initial loads use layout-preserving skeletons instead of blank panels.
- A refresh failure preserves the last successful data and displays its timestamp.
- Telegram, database, and Deepcoin health are reported independently.
- Empty states distinguish a legitimate absence of records from a synchronization failure.
- Pending mutations show progress and prevent duplicate taps.
- Live updates surface through a non-disruptive new-event indicator.

## Architecture and Data Flow

The redesign continues to use server-rendered Jinja templates plus the existing CSS and JavaScript assets. It reuses current backend routes and action handlers wherever possible.

The home timeline may add a lightweight query/view model that normalizes messages, recognition results, strategy lifecycle events, execution events, and risk exceptions into a common presentation shape. It must link to source records rather than duplicating their authoritative state.

Desktop and mobile templates share semantic markup and data hooks. Responsive CSS changes layout and navigation presentation without maintaining separate page implementations.

## Validation

Automated validation covers:

- Mobile and desktop render hooks.
- Bottom navigation and desktop navigation state.
- Summary metrics and independent service-health states.
- Timeline ordering, filters, event destinations, and the new-event indicator.
- Position, strategy, and message empty/error/stale states.
- Touch target sizing and scroll behavior.
- Dangerous-action detail routing, confirmation, pending state, and duplicate-submit protection.
- Existing position close and binding behavior to prevent regressions.

Local checks cover deterministic rendering, queries, JavaScript behavior, and CSS/static-asset assertions. Verification involving the production Telegram session, Deepcoin IP allowlist, account state, or live credentials runs on the server after reviewed changes are pushed and deployed through the established GitHub update workflow.

## Out of Scope

- A separate `/mobile` application or duplicated backend API.
- A new JavaScript framework.
- Changing trading strategy, risk sizing, or execution semantics.
- Moving production secrets or live identity to the development Mac.
