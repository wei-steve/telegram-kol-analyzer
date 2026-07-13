# Lazy Workbench Loading Design

## Problem

The authenticated `GET /` request currently builds every workbench destination before returning the home page. It loads message history, strategy lifecycle data, recovery state, settings, and a live Deepcoin exchange snapshot. The live exchange snapshot performs several remote API calls, so it blocks first paint. After `DOMContentLoaded`, the browser also forces a second group, message, and strategy refresh even though the same data was embedded in the initial HTML.

Production measurements on 2026-07-13 showed:

- `GET /`: about 1.66-1.70 seconds and 715 KB.
- Group list partial: about 47 ms and 49 KB.
- Message detail partial: about 19 ms and 332 KB.
- Strategy partial: about 4 ms and 2 KB.

## Chosen Approach

Render a lightweight workbench shell first, then load each data destination independently. This keeps the existing FastAPI/Jinja/vanilla-JavaScript application and existing URLs instead of introducing a second frontend or a client-side framework.

Two alternatives were rejected:

1. Cache the entire current page. This shortens repeat visits but still couples unrelated destinations, risks stale trading data, and does not reduce the large duplicate response.
2. Parallelize all existing server work. This may reduce wall time but still transfers hidden content and creates unnecessary Deepcoin traffic whenever the user only needs the home page.

## Architecture

`GET /` returns the navigation, current group context, settings forms, and loading containers. It must not call Deepcoin and must not render the 50-message timeline, exchange-position panel, or selected-group strategy cards.

Three read-only partial routes own the deferred data:

- Home dashboard partial: loads the current Deepcoin position summary and home event feed after first paint.
- Positions partial: loads the complete exchange position/order view only when the user first opens `持仓`.
- Existing selected-group detail and strategy partials: load only when the user first opens `消息` or `策略`.

The browser tracks loaded and in-flight destinations. A destination is fetched once on first entry, can still be refreshed by existing explicit refresh actions, and shows an inline error with a retry path if its request fails.

## Initial Browser Flow

1. Render the lightweight shell and selected group context.
2. Start the home dashboard request asynchronously; it does not delay the shell or navigation.
3. Fetch only the freshness snapshot needed to establish the SSE/polling baseline. Do not force-refresh message, strategy, or group partials on startup.
4. Restore the persisted group selection as UI state only. Do not simulate a group click while the active destination is `首页`.
5. On the first navigation to `持仓`, `策略`, or `消息`, fetch that destination and bind its existing controls.

Focus and visibility recovery continue to check freshness, but concurrent recovery calls are coalesced so a focus event and a visibility event cannot start duplicate refresh batches.

## Safety and Errors

- All deferred routes are read-only; live trading mutations are unchanged.
- A failed Deepcoin call affects the home/positions partial only and no longer prevents the workbench shell from opening.
- Existing request IDs continue to prevent stale group responses from overwriting a newer selection.
- Loading placeholders must never display zero as though it were confirmed live data.

## Verification

- Route tests prove `GET /` does not construct a Deepcoin client and does not embed message cards or exchange cards.
- Partial-route tests prove home and positions still render their expected data.
- JavaScript asset tests prove startup does not use `force: true`, persisted selection does not click a group, and workbench destinations use an in-flight/loaded guard.
- Production verification compares time-to-first-byte and response size before and after deployment, then verifies lazy routes, service health, and HTTP 200.
