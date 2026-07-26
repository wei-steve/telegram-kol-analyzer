# Message Panel Progressive Loading Design

## Goal

Make group switching feel immediate by reducing the initial message payload, loading older messages progressively, and preventing obsolete group requests from consuming browser and server work.

The change keeps the existing FastAPI, Jinja, vanilla JavaScript, and CSS architecture. It does not change Telegram ingestion, AI recognition, strategy lifecycle handling, or Deepcoin execution behavior.

## Current Findings

The current message routes load 50 complete message records for every initial group detail request. Each rendered message can include the original text, media, decision card, execution result, semantic review, authoritative model summary, and historical AI details.

On representative local data, a 50-message detail fragment is approximately 330 KB before browser DOM construction. The warm database query and server render are comparatively fast; most avoidable delay comes from transferring, parsing, inserting, laying out, and binding a large HTML fragment.

The existing UI already supports cursor pagination through `before_message_id`, but:

- the initial batch and every later batch contain 50 messages;
- pagination requires clicking `Load more`;
- the footer is rendered whenever a page contains any messages, even if no older page exists;
- group switching waits for the middle strategy panel before starting the right-side message request;
- stale responses are ignored by `groupSwitchRequestId`, but their network requests are not cancelled.

## Confirmed Product Behavior

- An initial group selection renders at most 20 newest messages.
- Older history loads in batches of 20.
- Scrolling to within 320 pixels of the bottom requests the next batch automatically.
- A visible `加载更多` control remains available as an accessibility and retry fallback.
- Only one older-history request may run for a message panel at a time.
- Search and sender filters use the same 20-message cursor pagination.
- The strategy and message requests start concurrently during a group switch.
- Selecting another group cancels the previous group-switch requests.
- A stale response can never replace the current group, message panel, filters, or scroll state.

## Approaches Considered

### 1. Reduce the existing manual page size only

Change 50 to 20 and keep the current `Load more` button.

This is the smallest change and immediately reduces the first response, but it leaves unnecessary clicks and does not address sequential group-switch loading or obsolete requests.

### 2. Progressive loading with parallel, cancellable group switching

Load 20 messages initially, automatically fetch 20 more near the bottom, retain the fallback button, start the strategy and detail requests concurrently, and abort superseded group requests.

This is the recommended approach. It addresses the largest measured costs while preserving the current server-rendered design and message-card behavior.

### 3. Lazy-load every message's AI detail or virtualize the list

Render only message summaries and fetch historical AI details on expansion, or maintain a virtual DOM window for visible cards.

This could reduce the payload further, but it adds new per-message routes, expansion loading states, caching, and more complex refresh behavior. It should be considered only if the recommended approach does not meet the production target.

## Backend Design

Introduce a single `MESSAGE_PAGE_SIZE = 20` constant for Web message timelines. Chat analysis keeps its existing independently selected message limits.

Add a page-oriented query helper that:

1. applies the existing group, cursor, text, and sender filters;
2. requests `page_size + 1` raw rows;
3. uses the extra row only to calculate `has_more`;
4. serializes at most `page_size` rows.

The initial detail route, messages-tab route, and standalone messages route all use the same helper and pass `has_more` to the template. This prevents route behavior from drifting.

The cursor remains the oldest rendered Telegram `message_id`. The response contract remains an HTML partial, so the frontend can continue appending the returned message-list markup without introducing a JSON rendering layer.

The footer renders only when `has_more` is true. It carries the next cursor and contains the manual fallback control.

## Frontend Data Flow

### Initial group switch

1. Increment `groupSwitchRequestId`.
2. Abort the previous group-switch controller.
3. Create a controller for the new selection.
4. Start the strategy-panel and detail-panel requests immediately.
5. Commit each fragment only when its request ID is current and its signal was not aborted.
6. Bind controls to each committed fragment.

The request-ID guard remains in place even with cancellation because a response can complete immediately before an abort is observed.

### Loading older history

The scrollable element is `[data-message-list]`. Its scroll handler checks:

`scrollHeight - scrollTop - clientHeight <= 320`

When the threshold is reached and a `data-load-more` control exists, the shared `loadMoreMessages(panel)` function:

- returns immediately when the current footer is already loading;
- reads the current group and filter state;
- requests the next cursor page;
- verifies that the original panel is still connected and still represents the same group;
- appends older cards to the end of the current list;
- replaces the footer with the returned footer;
- rebinds message controls;
- clears the loading state.

The fallback button calls the same function. It is disabled and labelled `加载中…` during the request. A failure preserves the existing messages, restores an enabled button labelled `加载失败，点击重试`, and does not advance the cursor.

## Scroll And Refresh Semantics

- Initial group loads continue to start at the newest message and scroll to the top.
- Appending older history does not change `scrollTop`.
- Manual recognition refresh preserves the existing scroll position as it does today.
- Applying or clearing filters replaces the panel, resets to the newest filtered result, and binds a new pagination handler.
- Live-message refresh behavior remains unchanged; it must not be confused with loading older history.

## Loading And Failure States

- The existing strategy panel remains visible until its replacement is ready.
- The existing message panel remains visible until its replacement is ready.
- Aborted requests do not show a user-facing error.
- A genuine active-request failure retains the last successful content and exposes the existing group-switch failure status.
- Pagination failure affects only the footer and never removes already rendered messages.
- When `has_more` is false, no automatic or manual pagination request can be started.

## Performance Targets

The production acceptance targets are:

- at most 20 message cards in an initial group detail response;
- at most 20 newly appended cards per history request;
- no duplicate cursor request while a page is in flight;
- strategy and detail requests begin during the same group-switch operation;
- selecting a second group aborts the first group's active fetches;
- initial message-fragment size is materially lower than the existing 50-message response on the same data;
- no regression in message ordering, filtering, refresh, recognition controls, or strategy navigation.

Exact elapsed-time targets are observed in production rather than enforced as timing-sensitive unit tests.

## Validation

Automated tests cover:

- the 20-message initial boundary;
- correct `has_more` behavior for 19, 20, and 21 matching messages;
- no pagination footer on the final page;
- preservation of search and sender filters across pages;
- newest-first ordering and cursor continuity;
- one in-flight history request per panel;
- automatic threshold loading and manual fallback using the same function;
- retry behavior after a pagination error;
- appending older cards without changing scroll position;
- concurrent strategy/detail request creation;
- cancellation and stale-response guards;
- existing manual recognition and live-refresh scroll preservation.

Local verification runs the focused Web query, route, JavaScript asset, and rendering tests. Production verification runs after the reviewed commit is pushed, pulled through the established server update script, the editable package is reinstalled, and `telegram-kol.service` is restarted.

## Out Of Scope

- Changing Telegram collection or database retention.
- Changing AI recognition, model authority, or recognition payloads.
- Changing trading strategy, sizing, order, protection, or Deepcoin behavior.
- Replacing server-rendered message cards with a frontend framework.
- Virtual scrolling.
- Per-message lazy loading of historical AI details in the first implementation.
- Prefetching all remaining message history.
