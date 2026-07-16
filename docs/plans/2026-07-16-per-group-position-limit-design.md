# Per-Group Effective Position Limit Design

## Goal

Limit automatic new entries to four effective Deepcoin positions per Telegram
group. The limit is a simple operational guardrail, not a strict distributed
reservation system. Position-management messages must remain executable when a
group is at the limit.

## Decision

Reuse `max_concurrent_positions`, change its default from `3` to `4`, and define
it as the maximum effective positions for one `chat_id`. Update the Web label so
operators do not interpret it as an account-wide limit.

Count distinct `execution_order_legs.pos_id` values joined through
`execution_bindings.chat_id` when all of the following are true:

- the binding belongs to Deepcoin and the incoming message's exact `chat_id`;
- the leg is an entry leg;
- the leg has `attribution_status=verified`;
- the leg is active and has a non-empty `pos_id`.

This preserves group isolation and counts split positions individually. Pending
limit orders without a confirmed position do not count.

## Entry Flow

Run the check only after the incoming entry message has resolved to an enabled
`auto_trade` group and before ticker reads, draft construction, queueing, or any
Deepcoin write. If the count is at least the configured limit, persist the
existing skipped-execution event with reason
`group_position_limit_reached`, including `current_position_count` and
`max_concurrent_positions`, then return without submitting an order.

Management messages take their existing earlier branch and are not subject to
the entry limit. Partial take profit, full exit, stop changes, and temporary exit
therefore continue to reduce or manage risk even when the group has four
positions.

## Consistency Boundary

No new database reservation or exchange reconciliation is added. The service
normally processes messages serially, but simultaneous entry attempts can
briefly exceed the limit. This accepted trade-off keeps the guardrail simple as
requested. Existing attribution and order-submission fail-closed checks remain
unchanged.

## Configuration And Compatibility

Existing persisted production value `3` is not overwritten by the code-default
change. Deployment must explicitly update the supported trading-settings API to
`max_concurrent_positions=4` while preserving every other setting, including
`auto_trade_enabled=true` and `management_execution_mode=live` if the production
preflight remains safe.

## Verification

Tests must prove:

- the default and Web label are four positions per group;
- zero through three verified active positions allow the normal entry path;
- four verified active positions in the same chat skip before any exchange
  read or write;
- positions from another chat do not consume the current group's limit;
- unverified, terminal, non-entry, or missing-`pos_id` legs do not count;
- split verified positions count by distinct `pos_id`;
- management execution is unaffected.

Production verification is read-only except for the approved settings update:
confirm the reviewed SHA, service health, persisted value `4`, live gates,
management-batch audit completeness, and absence of new service errors. Never
place a test order.
