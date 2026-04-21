# Telegram AI Panel Simplification Design

**Date:** 2026-04-18

## Goal

Simplify the Telegram web workbench AI interaction so it behaves more like NotebookLM: the user asks natural-language questions without manually selecting scope controls or individual messages, and the system defaults to analyzing the current group's recent messages.

## Approved Direction

The user selected **Approach A**.

Approved product decisions:

- Remove per-message checkboxes from the message timeline
- Remove the AI scope dropdown from the right-side panel
- Remove explicit date/time range fields from the right-side panel
- Keep only a chat history area, a single input box, and a submit button in the AI panel
- Default AI grounding scope to the **current group's most recent 50 messages**
- If the user explicitly asks for a different recent-message count such as "最近100条" or "最近 200 条", respect the user's requested count instead of the default 50

## Problem Statement

The current AI panel reflects an analyst-control model rather than a natural chat model:

- The message timeline includes checkboxes for manual selection
- The AI panel exposes scope controls and optional time-window inputs
- Conversation history is rendered as a flat stack that becomes visually noisy after multiple rounds

This creates too much operator friction for the user's preferred workflow. The user wants the system to infer the normal scope automatically and only let the question text override the default when needed.

## Recommended Experience

### 1. Message Timeline

The center timeline should focus on browsing rather than manual AI scope selection.

Keep:

- Current group switching
- Reverse-chronological message rendering
- Search field
- Sender filter field
- Load more pagination
- Media preview and OCR blocks

Remove:

- Per-message selection checkboxes

This change makes the timeline feel like a reading surface instead of a control surface.

### 2. AI Panel

The right-side AI panel should become a lightweight grounded chat box.

Keep:

- Conversation history area
- Single question input
- Submit button
- Answer rendering with citations and source jump links

Remove:

- Scope selector
- Time window inputs

Add:

- Short helper text explaining the default behavior, for example:
  - "默认分析当前群最近 50 条消息；你也可以直接问‘总结最近 200 条’。"

### 3. Default Grounding Rules

The backend should stop depending on explicit UI scope controls for the common path.

Default rule:

- Analyze the selected group's latest 50 messages

Override rule:

- If the user explicitly requests a recent-message count in the question text, use that count instead

Examples:

- "总结这个群最近在讨论什么" → use latest 50 messages
- "总结最近100条消息" → use latest 100 messages
- "分析最近 200 条里最重要的观点" → use latest 200 messages

Non-goal for this iteration:

- Full natural-language parsing for time windows such as "昨晚" or "最近三小时"

Those time-based instructions can be added later, but they should not block the first simplification pass.

## Conversation History Redesign

The history area should be rendered as structured turns rather than a flat role/content list.

Each turn should contain:

- User question
- Assistant answer
- Optional source reference block

Recommended UI pattern:

- User message bubble
- Assistant response bubble beneath it
- Sources rendered as a compact expandable or clearly grouped section
- Better spacing between turns

This reduces clutter and makes multi-round sessions easier to scan.

## Backend Design Changes

### 1. Chat Request Shape

The frontend should send a much smaller payload for the normal path:

- `question`
- `chat_id`

Optional backend-derived values:

- `message_limit` extracted from question text, otherwise default 50

The backend should no longer depend on:

- `scope_mode`
- `selected_message_ids`
- `posted_after`
- `posted_before`

for the primary UI flow.

### 2. Limit Extraction

Add a small parsing helper that detects recent-message count requests from the question text.

Examples to support in v1:

- `最近100条`
- `最近 100 条`
- `recent 100 messages`

If no match is found, use 50.

### 3. Source References

Keep the existing citation and jump-link behavior.

This remains valuable even after the panel is simplified because it grounds the answer and lets the user inspect the referenced messages without scope micromanagement.

## Frontend Design Changes

### Templates

`index.html`

- Remove the scope label/select block
- Remove the time-window field block
- Replace the current textarea label arrangement with a simpler chat composer

`_messages.html`

- Remove checkbox markup and related label structure

### JavaScript

`app.js`

- Stop collecting selected message ids
- Stop reading scope mode and time-window fields
- Send only the simplified chat payload
- Store and render structured conversation turns
- Improve conversation rendering so each round stays grouped and readable

### CSS

`app.css`

- Remove styles used only by message selection controls
- Add clearer chat-turn spacing and grouping
- Improve readability for longer histories

## Risks and Mitigations

### Risk: The user sometimes wants precise scope control

Mitigation:

- Preserve a small natural-language override for recent message count
- Keep the message search and sender filters in the timeline so the user can narrow what they are reading even if the AI panel is simplified
- Reintroduce hidden advanced controls later only if needed

### Risk: Natural-language count extraction is too permissive

Mitigation:

- Only extract an explicit recent-message count from narrow, easy-to-test patterns
- Fallback safely to 50 if parsing fails

### Risk: Existing chat tests assume explicit scope controls

Mitigation:

- Update tests to reflect the simplified request shape
- Keep backend helpers modular so previous scoped behavior could still be reintroduced if needed

## Testing Strategy

Add or update tests for:

- Message timeline no longer rendering selection checkboxes
- AI panel rendering a single simplified input flow
- Recent-message count extraction from question text
- Chat API defaulting to 50 messages when no explicit count is present
- Chat API using the requested recent-message count when present in the question
- Conversation history rendering grouped turns instead of a flat noisy stack

## Recommendation

Implement the simplification in one focused slice:

1. Remove checkbox and scope/date UI
2. Add backend default-limit + count parsing
3. Restructure conversation history rendering
4. Update tests and docs

This gives the user a more natural, NotebookLM-style experience without expanding the product scope beyond the current architecture.
