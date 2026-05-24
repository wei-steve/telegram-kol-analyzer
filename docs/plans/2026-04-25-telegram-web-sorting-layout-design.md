# Telegram Web Sorting and Layout Design

## Goal

Make the web workbench feel closer to Telegram: active groups rise to the top, messages read oldest-to-newest with the newest at the bottom, and the message panel keeps a fixed context area above the scrolling timeline.

## Design

- Keep the backend as the source of truth for group ordering. `load_group_rows()` already sorts by latest `posted_at`; the UI should be able to re-render the left group list after refreshes and live update events instead of relying only on the initial page load.
- Keep message timelines chronological within the currently loaded page. The query fetches the newest window efficiently, then reverses it for display so older messages sit above newer messages. Loading history should prepend older rows and preserve scroll position.
- Split the middle panel into a sticky header and a scrollable message body. The sticky header holds group title, freshness, refresh status, and filters; the message list below remains Telegram-like with the latest message at the bottom.

## Testing

- Add route-level coverage for a group-list partial ordered by latest activity.
- Add render coverage for the sticky header marker.
- Add static asset smoke coverage for group-list refresh hooks and sticky header CSS.
