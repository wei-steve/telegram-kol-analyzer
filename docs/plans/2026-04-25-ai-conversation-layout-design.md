# AI Conversation Layout Design

## Goal

Make the AI analysis area easier to read as a research workspace instead of a narrow chat log.

## Approved Approach

Use a focused first pass:

- Widen the AI column so long model answers have a human-readable line length.
- Collapse the group default prompt behind a compact preferences panel.
- Render conversation entries as report cards, where the user's question is metadata and the assistant answer is the main content.
- Keep existing localStorage history, group prompt persistence, source citations, and message-jump behavior unchanged.

## Testing

Cover the server-rendered page structure with HTML assertions, then verify the browser manually after restarting/reloading the local app.
