# Exchange protection display design

## Goal

On the live-position page, show every pending DeepCoin take-profit and
stop-loss order that can affect a position, without representing uncertain
ownership as verified strategy protection.

## Chosen approach

Extend each position card with an exchange-protection detail list.  It will
show every directly position-bound TPSL order, including multiple take-profit
levels and multiple stop-loss orders.  Each entry will show its type, trigger
price, size, exchange order ID, and ownership state.

The existing summary fields remain conservative: they continue to represent
only verified protection.  The detail list is the complete exchange view and
uses these states:

- `已验证归属`: the order is backed by the persisted exact order-to-position
  evidence or by an exchange `posId` matching the displayed position.
- `未验证归属`: the order can affect the displayed position but lacks the
  durable strategy ownership evidence.
- `无法归属`: a same-instrument, same-side order could plausibly belong to the
  position but the exchange did not provide enough evidence to associate it
  safely.  It is presented separately and never included in verified totals.

## Data flow

The live-position loader already fetches pending TPSL rows.  Add a pure
normalizer that derives individual display rows from them, preserving every
non-zero TP and SL side of a combined TPSL row.  Associate direct `posId` rows
to a card.  Keep remaining same-instrument/same-side rows as candidates rather
than silently discarding them.  The template renders the normalized list below
the existing metric grid.

## Safety and tests

The change is read-only.  It must not change matching used for order mutation
or the conservative verified-protection summary.  Tests will cover multiple
TPs, primary and backup stops, a direct-but-unverified exchange row, and an
unscoped candidate.  A page-render test will verify the labels and all prices
are visible.
