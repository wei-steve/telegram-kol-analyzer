# Management Symbol Guard Design

## Goal

Allow an authoritative, explicitly targeted lifecycle-management instruction to
apply when its message also mentions another symbol in narrative or future-plan
text.

## Decision

Keep the existing text-derived symbol guard for inferred targets.  When the
authoritative result supplies a valid `target_lifecycle_id`, trust that
immutable target only if its lifecycle symbol also matches the result's
explicit `symbol`.  Do not reject it merely because a different symbol appears
earlier in the message text.

This preserves the guard against an inferred or model/lifecycle symbol mismatch
while fixing messages such as: a BTC market comment followed by an explicit
instruction to take half profit on an ETH position.

## Verification

Add a regression test using the missed ETH message shape. It must create the
ETH partial-take-profit candidate and its management instruction item.  Keep
the existing mismatch protection covered by the current test suite.
