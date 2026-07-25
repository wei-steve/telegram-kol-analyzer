# Ledger Protection Display State Design

## Problem

DeepCoin can return a pending TPSL order without `posId`. The positions page
already uses a verified local `orderId -> posId` record to place that order on
the correct live position card, but the renderer re-derives the display state
from the raw exchange order and labels it `无法归属`.

## Decision

When the display splitter resolves a pending order through the verified local
order-to-position map, it will pass a display-only copy marked with the exact
live `posId` into the existing renderer. The renderer will therefore label it
`已验证归属`, exactly as it does when DeepCoin supplies `posId` directly.

The raw exchange payload remains unchanged. Orders without an exact verified
mapping remain in the unattributed section, and conflicting local mappings
remain fail-closed and unattributed.

## Validation

An integration test will render an unscoped order resolved through both the
protection ledger and persisted take-profit order records, asserting that its
card shows `已验证归属`. Unknown orders will continue to show `无法归属` only in
the unattributed summary. Production verification will include a server-side
page request and an in-app-browser navigation attempt.
