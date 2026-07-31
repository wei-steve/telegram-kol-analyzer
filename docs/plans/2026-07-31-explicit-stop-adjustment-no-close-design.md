# Explicit Stop Adjustment Must Never Become a Close

## Problem

The message `BTC市价62600附近，止损下移动500点，调整61900。` was correctly associated with the active BTC long position, but the deterministic directive layer treated the model action `move_stop_to_protect` as a break-even request. The extracted numeric stop was represented as `61900.0`, so the string-source check did not match the original text `61900`. The explicit price was discarded, and the break-even market policy converted an unavailable long break-even stop into a full market exit.

## Safety Invariant

An explicit stop price in the current message has authority over a generic protection label. It must be handled only as an exact stop-loss adjustment. A stop-loss adjustment may be applied or blocked, but it must never be converted into a partial or full close.

## Design

The directive resolver will identify an explicit stop price by numeric equivalence instead of raw string containment. When the current message contains the lifecycle event's stop price, the resolver will return `adjust_stop_loss` before interpreting `move_stop_to_protect` or other break-even labels.

Price-source validation remains conservative. A model-supplied price that cannot be proven to occur in the current message is not treated as an explicit adjustment. Existing planner-side validation remains authoritative for checking tick precision, position side, current protection, and whether the requested stop reduces risk.

Messages that contain only cost-protection language and no explicit price continue through `move_stop_to_break_even`. The existing market-aware break-even behavior is unchanged for that narrow intent. Explicit close instructions continue to use the full-exit path.

## Error Handling

- A proven explicit stop price becomes `adjust_stop_loss`.
- An explicit stop that is invalid, loosens risk, or cannot be safely applied is blocked by the existing stop-adjustment validation.
- A price that is not proven to originate in the current message is not silently promoted to an explicit adjustment.
- No failure in the explicit-stop path may fall back to `break_even_by_market` or `full_exit`.

## Verification

Regression coverage will include:

- the exact production wording with `61900` in text and `61900.0` in structured evidence;
- generic `move_stop_to_protect` plus an explicit stop resolving to `adjust_stop_loss`;
- cost-protection wording without a price continuing to resolve to break-even;
- planner/execution boundaries proving the explicit-stop path cannot create a close batch;
- focused management, recognition, planner, and execution regression suites.

Production verification will run after push and server deployment. It will confirm the deployed commit, active service, focused server tests, and a read-only database/exchange audit. No synthetic Telegram message or live trade mutation will be submitted for verification.
