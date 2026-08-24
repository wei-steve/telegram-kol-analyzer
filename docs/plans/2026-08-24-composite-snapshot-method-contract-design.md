# Composite Snapshot Method Contract Design

## Problem

The composite management executor calls
`list_trigger_orders_history()`, while the production Deepcoin client protocol
and implementation expose `list_trigger_order_history()`. Test doubles copied
the incorrect plural spelling, so focused tests did not exercise the production
interface. Every live composite preflight therefore fails before any exchange
write and exhausts its three attempts.

## Decision

Use the existing singular protocol method in the executor and make the composite
test double match the production protocol. Do not add an alias or dynamic
fallback: one canonical name keeps interface drift visible.

## Safety and acceptance

The change affects only a read-only exchange snapshot call. It does not alter
management intent, retry policy, idempotency, or exchange-write behavior. A
regression test must first fail with a production-shaped client that exposes
only `list_trigger_order_history()`, then pass after the one-line executor fix.
Run the focused composite-management tests before rebuilding a candidate.
