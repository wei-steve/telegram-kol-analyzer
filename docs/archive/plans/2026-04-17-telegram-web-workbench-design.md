# Telegram Web Workbench Design

**Date:** 2026-04-17

**Goal**

Add a local-first web workbench to the existing Telegram KOL research project so the user can browse group messages, inspect mixed media content, ask grounded AI questions about the current data, and receive near-real-time updates when new Telegram messages arrive.

**Scope**

- Add a server-rendered web UI for browsing Telegram groups and messages
- Display text, images, OCR text, and fallback metadata for unsupported media
- Add a NotebookLM-style grounded AI chat panel inside the web UI
- Integrate AI requests through a backend-only proxy connection to CLIProxyAPI
- Add near-real-time message updates in the browser
- Preserve idempotent ingestion so duplicate events do not create duplicate rows
- Add reconciliation logic to reduce the risk of missed messages after disconnects or restarts

**Non-Goals**

- Full SPA frontend architecture in v1
- Multi-user auth or team collaboration in v1
- Browser-direct model API access
- Vector database or semantic retrieval infrastructure in v1
- Perfect live ordering guarantees across all reconnect edge cases in v1
- Full production deployment hardening in v1

## Recommended Approach

Use a thin web layer built on the existing Python package and SQLite database:

- `FastAPI` for HTTP routes and server-sent events
- `Jinja2` templates plus light JavaScript for page behavior
- Existing SQLAlchemy models and session factory for persistence
- Existing `dataset_export`-style serialization patterns as the basis for AI grounding context
- Existing `sync_checkpoints` plus periodic reconciliation for listener safety

This gives the project a usable internal workbench without introducing a second application stack. It also keeps the AI integration server-side so the proxy credentials remain private.

## Product Overview

The web workbench has three coordinated surfaces:

1. **Group Navigator**
   - Shows tracked Telegram groups
   - Sorted by latest message time descending
   - Displays message count and last activity time

2. **Message Timeline**
   - Shows the selected group's messages in reverse chronological order
   - Renders message text, reply context, downloaded images, OCR text, and fallback media metadata
   - Supports loading more history without leaving the page

3. **AI Analysis Panel**
   - Lets the user ask grounded questions about the current group or selected messages
   - Shows source chips describing the current analysis scope
   - Returns answers with source references that can jump back to cited messages

## User Experience Design

### 1. Layout

Use a three-column layout on desktop:

- Left: group list
- Center: selected group timeline
- Right: AI analysis panel

On narrower screens, collapse the AI panel below the timeline.

### 2. Group List

Each group item should show:

- Group title
- Latest message time
- Message count
- Optional badge if the group has media-rich posts

Ordering:

- Most recently active groups first

Selection behavior:

- Clicking a group loads that group's timeline
- The selected group remains highlighted

### 3. Message Cards

Each message card should show:

- Sender name
- Posted time
- Telegram message id
- Text body
- Reply context snippet if present
- Media preview area
- OCR text in a collapsible block when available
- Candidate / trade summary badge if that message has parsed signal data

Media handling:

- Images render inline from local paths through a safe static-file route
- Unsupported media renders a structured placeholder with media type and path
- Empty text plus media-only messages must still be visible

### 4. AI Panel Pattern

Use a compact NotebookLM-inspired but server-rendered pattern:

- Header with model selector and current scope label
- Source chips describing grounding scope, such as:
  - Current group, latest 50 messages
  - Selected messages
  - Time window
- Chat transcript area
- Input box with submit button
- Each AI answer includes source references `[1] [2]`
- Clicking a source reference scrolls to or highlights the corresponding message card

### 5. Scope Controls

The first version should support three scopes:

- Entire current group, recent N messages
- User-selected messages only
- Time-window slice within current group

This provides meaningful control without needing vector search.

## Web Architecture

### 1. New Web Modules

Recommended additions:

- `src/telegram_kol_research/web_app.py`
  - FastAPI application factory and route registration
- `src/telegram_kol_research/web_queries.py`
  - Group list and message timeline query helpers
- `src/telegram_kol_research/web_serialization.py`
  - Serialization helpers for messages, media, and source references
- `src/telegram_kol_research/llm_chat.py`
  - Grounded prompt assembly and CLIProxyAPI client wrapper
- `src/telegram_kol_research/live_updates.py`
  - Event publication and SSE helpers

Template/static structure:

- `src/telegram_kol_research/templates/base.html`
- `src/telegram_kol_research/templates/index.html`
- `src/telegram_kol_research/static/app.css`
- `src/telegram_kol_research/static/app.js`

### 2. HTTP Routes

Recommended v1 routes:

- `GET /` — render main workbench page
- `GET /groups` — optional partial route for group list refresh
- `GET /groups/{chat_id}/messages` — fetch paginated messages for one group
- `GET /media/{media_asset_id}` or `/local-media/...` — safely serve downloaded local media
- `POST /api/chat` — grounded AI question/answer endpoint
- `GET /api/events` — SSE stream for new messages and updates

## Data Query Design

### 1. Group List Query

Group rows should be derived from `raw_messages` aggregated by `chat_id`, enriched with config titles where possible.

Needed fields:

- `chat_id`
- display title
- `last_posted_at`
- `message_count`
- `has_media`

### 2. Message Timeline Query

The message timeline should reuse the same conceptual structure already present in `dataset_export.py`, but filtered by one `chat_id` and sorted by `posted_at DESC, message_id DESC`.

Each returned record should include:

- raw message fields
- reply context
- related media assets
- optional candidate summary
- optional trade idea summary

### 3. Grounding Context Assembly

The AI context assembler should not query the whole database ad hoc. It should build a bounded, structured context packet using the same message-centered approach already used for export:

- message id and timestamp
- sender
- text
- reply context
- OCR text
- candidate metadata
- trade idea metadata

This packet can then be rendered into a prompt block for the model.

## CLIProxyAPI Integration

Research confirms CLIProxyAPI is OpenAI-compatible and commonly exposes endpoints such as `/v1/chat/completions`.

Integration rules:

- The browser never talks to CLIProxyAPI directly
- The FastAPI backend keeps the proxy base URL, auth token, and model defaults in environment variables
- The web AI route translates the grounded request into the proxy-compatible chat payload
- If streaming is added later, the same backend route can proxy streamed responses

Suggested environment variables:

- `TELEGRAM_KOL_LLM_BASE_URL`
- `TELEGRAM_KOL_LLM_API_KEY`
- `TELEGRAM_KOL_LLM_MODEL`
- `TELEGRAM_KOL_LLM_TIMEOUT_SECONDS`

## Real-Time Update Design

### 1. Transport Choice

Use **Server-Sent Events (SSE)** for v1 rather than WebSockets.

Reasons:

- Simpler server implementation
- Fits one-way push from server to browser
- Adequate for message arrival notifications
- Easy to consume with light JavaScript

### 2. Event Flow

1. Telegram listener receives a new message or edit
2. Message is normalized and persisted through the existing ingestion pipeline
3. Parse/merge stages update related candidate and trade tables if applicable
4. The listener publishes a lightweight in-process event for the affected `chat_id`
5. SSE endpoint fans the event out to connected browsers
6. Browser prepends the new message card or refreshes the affected message row

### 3. Avoiding Duplicates

The source of truth remains `(chat_id, message_id)`.

Required rules:

- Writes must be idempotent for the same `(chat_id, message_id)`
- Edits update the existing `raw_messages` row rather than creating a new row
- Media rows should be de-duplicated for the same `raw_message_id`, `kind`, and `local_path`
- Frontend event handlers should ignore already-rendered message ids

### 4. Avoiding Missed Messages

Real-time listeners alone are not sufficient. The design should combine:

- **Push path**: Telethon listener for fast delivery
- **Recovery path**: periodic reconcile fetch using `sync_checkpoints`

Recommended strategy:

- On listener startup, load the last checkpoint for each tracked chat
- Backfill a small safety window after the checkpoint
- On reconnect after failure, replay from the last saved checkpoint
- Periodically run a lightweight reconciliation fetch to cover network interruptions

This means duplicates may arrive, but the database layer should absorb them safely.

## Reliability Model

### Idempotency

All ingestion paths must be safe to replay:

- history fetch
- live listener
- reconnect recovery
- reconcile job

### Ordering

Display order should prefer:

- `posted_at DESC`
- fallback `message_id DESC`

### Edits

The system should surface edits by updating the stored text and `edit_date`, and the frontend can mark edited messages with a subtle badge.

## Testing Strategy

### 1. Web Query Tests

Add tests for:

- group list ordering
- message timeline ordering
- media attachment serialization
- reply context inclusion

### 2. AI Grounding Tests

Add tests for:

- scope-to-context conversion
- bounded prompt assembly
- source reference generation
- backend-only proxy request building

### 3. SSE / Live Update Tests

Add tests for:

- event publication on new persisted messages
- client payload structure
- duplicate event suppression in serialization layer

### 4. Reconciliation Tests

Add tests for:

- checkpoint-driven fetch windows
- no duplicate rows when replaying overlapping history
- edited messages updating existing rows

## Risks and Mitigations

### Risk: AI answers become ungrounded or too expensive

Mitigation:

- Keep context bounded by message count and scope
- Expose scope clearly in the UI
- Require source references in response formatting

### Risk: Browser cannot render local media safely

Mitigation:

- Serve media through a controlled FastAPI route rooted in approved local directories
- Never expose arbitrary file reads

### Risk: Listener disconnects create silent gaps

Mitigation:

- Persist checkpoints
- Add safety-window replay on startup/reconnect
- Schedule reconcile passes

### Risk: Web layer duplicates business logic

Mitigation:

- Extract reusable query/serialization helpers rather than embedding SQL in routes
- Reuse existing message-centered serialization concepts from `dataset_export.py`

## Technical Recommendation

For v1 of the workbench:

- FastAPI
- Jinja2 templates
- Vanilla JavaScript plus EventSource for SSE
- Existing SQLAlchemy session factory
- `httpx` for CLIProxyAPI calls
- Existing Telethon-based ingestion pipeline plus a lightweight listener/reconcile wrapper

This is the most direct path to a useful local workbench while preserving the project's current architecture and keeping future upgrades open.
