# Exchange Positions Loading Performance Design

## Goal

Reduce the time to first useful content on the exchange positions workbench
without weakening live-position attribution, TPSL ownership checks, or the
pre-mutation exchange revalidation path.

Production measurements on 2026-07-30 showed that `/positions-panel` required
8.4–9.6 seconds and returned about 573 KB of HTML. An isolated server-side
profile attributed 4.815 of 5.057 seconds to the Deepcoin snapshot while local
database queries, attribution, and template rendering together took about
0.24 seconds.

## Chosen Approach

Use progressive server-rendered partials.

The first positions request loads only live positions and their pending TPSL
evidence. Current orders, order history, and position history become separate
lazy-loaded partials fetched when their tab is selected. This preserves the
existing Jinja and vanilla JavaScript architecture while removing unrelated
history calls from the critical path.

Two alternatives were rejected:

1. Connection reuse and caching alone would leave the initial request coupled
   to all four datasets and would not remove the large hidden DOM.
2. A full JSON/client-rendered rewrite would reduce payload overhead further,
   but it would substantially increase implementation and regression risk
   without being necessary to reach the latency target.

## Server Architecture

The positions shell route returns:

- live positions and exact pending TPSL evidence;
- counts that are available without loading exchange history, when such counts
  are already part of the live response;
- empty lazy hosts for the other three tabs.

Dedicated read-only routes return one tab at a time:

- current orders;
- order history;
- position history.

The existing exchange snapshot loader is split into focused loaders. Shared
normalization and attribution helpers remain authoritative. No display cache or
partial response may be used as mutation authority.

Within one focused loader, a single Deepcoin client owns a reusable HTTP
connection. The live-position TPSL response is reused when current-order data
needs the same evidence, so the same instrument is not fetched twice in one
request.

## Browser Data Flow

Opening the positions workbench loads the positions shell and commits it
immediately. Selecting another exchange tab starts exactly one request for that
tab. Loaded tabs remain in memory until an explicit refresh or a lightweight
change signal invalidates them.

Refresh checks use a bounded live-position fingerprint rather than downloading
and comparing the complete positions panel. When the fingerprint changes, the
page keeps the user's current browsing state and offers the existing
non-disruptive update control.

The browser preserves:

- selected exchange tab;
- list/grouped view;
- open details state where practical;
- scroll position and exact-position focus behavior.

## Cache and Freshness

Caching is limited to read-only display data:

- live positions and TPSL: no long-lived cache; request coalescing only;
- current orders: up to 3 seconds;
- order history: 30 seconds;
- position history: 120 seconds.

Concurrent identical reads use a single-flight result. A stale history response
may be shown with an explicit timestamp if Deepcoin is temporarily unavailable.
Live positions must fail closed rather than silently presenting stale data as
current.

All trade mutations continue to perform their existing direct exchange
revalidation. Display caches never feed close, cancel, bind, or TPSL mutation
decisions.

## Rendering and Payload

Only the selected list or grouped representation is rendered for a loaded tab.
History tabs begin with 20 items and expose bounded pagination. The initial
positions response target is below 50 KB.

Nginx compression is supplementary. The main improvement comes from eliminating
unneeded exchange calls and hidden DOM, not from compressing the existing
oversized response.

## Error Handling

Each lazy tab has its own loading, retry, and error state. A history failure
does not remove already loaded live positions. Request identifiers prevent
slower obsolete responses from replacing newer tab content.

If the exchange positions request fails, the existing unavailable state is
rendered and no cached history is presented as proof of a live position.

## Observability

Add bounded timing data for:

- database preparation;
- live positions/TPSL;
- open orders;
- order history;
- position history;
- template rendering;
- response byte size and cache status.

Expose coarse `Server-Timing` values without request payloads, credentials, or
exchange response bodies.

## Verification

Local deterministic tests cover:

- the initial positions route does not call history methods;
- each lazy route calls only its required Deepcoin methods;
- TPSL evidence is not fetched twice within one request;
- selected tab/view survive partial replacement;
- stale or failed history cannot authorize a mutation;
- pagination and response bounds.

Production verification runs on the server after a reviewed commit is pushed
and deployed through the existing GitHub update helper. Deployment must wait
for a proven safe window with no active time-sensitive strategy operation.

Acceptance targets:

- live positions P50 below 1 second and P95 below 2 seconds;
- initial response below 50 KB for the measured production account;
- no more than two Deepcoin reads on the initial live-position critical path;
- cached history responses below 300 ms;
- unchanged position attribution and mutation safety tests.
