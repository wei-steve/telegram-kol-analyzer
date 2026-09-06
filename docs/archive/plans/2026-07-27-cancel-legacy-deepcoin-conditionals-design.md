# Cancel Legacy Deepcoin Conditional Stops Design

## Goal

Remove the six reviewed legacy BTC `Conditional` close orders without changing
positions, normal entry orders, or native TPSL protection:

- `1001124328936694`
- `1001124346855727`
- `1001124346876836`
- `1001124346889183`
- `1001124346896177`
- `1001124346908329`

Five orders are persisted legacy generic backup stops. One order
(`1001124328936694`) has no durable ownership record and is handled as an
explicitly reviewed orphan.

## Chosen Approach

Use a purpose-built, fail-closed operator command instead of browser clicks or
raw API calls. The command creates a fingerprinted dry-run from fresh Deepcoin
positions, pending trigger orders, native TPSL rows, and the production
database. Apply mode accepts only the exact reviewed fingerprint and cancels
one target at a time.

Each target must still match its reviewed immutable fields: instrument, order
ID, `Conditional` type, close side, position side, trigger price, and size.
Each mapped position must still exist and retain the separately identified
native stop before cancellation. The orphan must still match its complete
reviewed payload and its likely position must retain native stop protection.

After each cancellation, the command re-reads exchange state and requires:

- the exact legacy order is absent;
- the exact native stop remains pending;
- the exact position remains present unless it closed naturally during the
  operation, in which case execution stops for review;
- no other reviewed target was changed unexpectedly.

The five durable backup-stop rows are marked `cancelled` only after an explicit
successful cancellation response and confirming readback. An execution event
records the order ID, mapped position, reviewed fingerprint, and sanitized
response. The orphan cancellation is also recorded as an execution event.

## Alternatives Considered

1. Browser cancellation is easy to observe, but it is difficult to prove that
   each click selected the exact order and it does not reconcile database
   state.
2. Direct one-off API calls are quick, but they provide no reusable preflight,
   fingerprint, or durable audit update.
3. Replacing the five stops with native backup TPSL before cancelling them
   preserves redundant protection, but the reviewed positions already have
   separate native stops and the current migration planner is intentionally
   blocked by unresolved native-order attribution. Adding more stops would
   increase ambiguity.

## Failure Policy

Any snapshot error, payload mismatch, missing native stop, changed position,
unconfirmed cancellation response, or failed readback stops the run
immediately. No retry is made after an outcome-unknown write. Remaining
targets stay untouched.

## Verification

The final read-only snapshot must show all six legacy IDs absent, all six
expected native stop IDs still pending, all surviving mapped positions
unchanged, and the four reviewed BTC entry triggers plus the ETH entry trigger
untouched. Database audit rows must agree with the confirmed exchange outcome.
