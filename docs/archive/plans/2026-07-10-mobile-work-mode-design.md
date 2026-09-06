# Mobile Work Mode Design

## Goal

Make the existing trading console practical to use in a phone browser without creating or maintaining a separate mobile application.

## Decision

Use one responsive application with a dedicated mobile work mode at viewport widths of 760px and below. The desktop three-column workbench remains unchanged. The mobile mode uses the same server-rendered data, API endpoints, and JavaScript actions as the desktop console.

## Information Architecture

The phone layout presents one primary view at a time and provides a fixed bottom navigation bar:

- **概览** is the default view. It shows listener health, data freshness, four strategy KPIs, and the current group’s important strategy cards.
- **策略** focuses the existing strategy list and filters.
- **消息** focuses the selected group’s message/detail area.
- **持仓** opens the existing Deepcoin position, order, and history view.
- **更多** provides access to logs and existing settings panels.

The navigation changes the visible workbench region without loading a new page. Refreshing the page returns the user to 概览.

## Actions and Safety

Mobile users retain the same functionality as desktop users, including Deepcoin synchronization, live-position binding, and marking a position manually closed. Existing server-side behavior and authorization remain authoritative.

For actions that can affect live trading state, the client presents a confirmation dialog that includes the instrument, direction, source group where available, and the intended action. Buttons meet a practical touch target size, and successful or failed responses use a persistent, accessible status message with retry context.

## Implementation Boundaries

- Do not add a separate `/mobile` route or duplicate backend dashboard data.
- Add mobile navigation and semantic data hooks in `templates/index.html`.
- Extend `static/app.js` with mobile view switching and live-action confirmation handling.
- Add mobile-only layout, fixed navigation, touch targets, and safe-area support in `static/app.css`.
- Reuse the existing exchange-position dashboard rather than recreating position cards.

## Validation

Add render and static-asset tests for the mobile navigation hooks, default view behavior, responsive CSS, and confirmation logic. Run the focused web tests and the full local test suite. Real Telegram and Deepcoin verification runs on the production server after the reviewed commit is deployed.
