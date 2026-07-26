# Exchange Position Tab Persistence Design

## Problem

The positions panel is replaced with freshly fetched HTML when the browser
regains focus or becomes visible. The server-rendered fragment always marks the
`positions` subtab as active. The browser currently restores only the list
versus grouped display mode, so a user reading current orders, order history, or
position history is moved back to the positions subtab after a refresh.

## Design

Persist the selected exchange-position subtab in browser local storage, using a
key scoped to this workbench. The supported values are:

- `positions`
- `open-orders`
- `order-history`
- `position-history`

Clicking a subtab applies the selection and saves it. Binding a newly inserted
positions fragment restores the saved selection after event handlers are
attached. A shared setter updates tab classes, `aria-selected`, and the matching
panel so click and restore paths cannot diverge.

If local storage is unavailable or contains an unsupported value, the page
falls back to `positions`. If a future or partial fragment does not contain the
saved tab, it also falls back to `positions`. Existing refresh behavior and
Deepcoin data fetching remain unchanged.

## Verification

Add a focused asset regression test that proves the script:

- defines a dedicated storage key;
- saves a valid tab on click;
- restores the saved tab after a partial panel reload;
- validates persisted values and falls back safely.

Run the focused test locally, then the complete web asset smoke test file.
After pushing, deploy through the existing server update helper and verify in
production that selecting `历史委托` survives a refresh/focus recovery.
