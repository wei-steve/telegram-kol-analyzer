# Batch 150 Controlled Volatile `updated_at` Design

## Goal

Keep the batch `150` terminalization fail-closed while allowing the production
worker's natural refresh of `execution_order_legs.id=553.updated_at` between a
read-only plan and a later CAS attempt. No other row or field becomes volatile.

## Approved Boundary

The only relaxed comparison coordinate is:

```text
table:  execution_order_legs
id:     553
field:  updated_at
```

The field remains part of the action's before/after evidence and is still
written to the plan's repair timestamp. It is omitted only from the starting
state comparison and the SQL `WHERE` predicate for that one row. The
transactional postcondition still requires the complete after row, including
the written `updated_at` value.

Every other field on leg `553`, every field on leg `554`, and every field on
the other six actions remains an exact full-row CAS predicate. Table counts,
target-set checks, unsafe-management checks, quick checks, database-path
binding, fingerprints, action count, timestamp spelling, and confirmation token
remain unchanged in force.

## Plan Contract

The plan schema advances and carries one canonical CAS-policy object naming the
single ignored-before field. The policy participates in the plan fingerprint
and confirmation token. Loading a plan with a missing, additional, or changed
policy fails before opening a writable database. Old full-row plans therefore
cannot silently acquire the new behavior.

## State Classification

- Apply may start when all seven ordinary actions exactly equal `before` and
  leg `553` equals `before` after excluding only `updated_at`.
- Apply is already complete when all seven ordinary actions exactly equal
  `after` and leg `553` equals `after` after excluding only `updated_at`.
- Rollback uses the symmetric rule.
- Any mixed state or any other changed field refuses with zero committed rows.
- A successful mutation writes the complete planned destination rows and then
  verifies them exactly before commit.

The rollback writes the plan's captured `updated_at` value. This is deliberate:
the field is declared non-authoritative runtime metadata, while every business
field is restored exactly. If the worker refreshes the timestamp again after
commit, that is ordinary runtime behavior rather than repair drift.

## Rollback SQL

The rendered rollback SQL omits only `execution_order_legs.id=553.updated_at`
from that row's `WHERE` clause. It retains `updated_at=<captured-before-value>`
in the `SET` clause and retains the one-row changes guard. The other seven SQL
updates remain full-row CAS statements.

## Alternatives Rejected

1. Stop or pause the worker before planning and applying. This needs production
   lifecycle authorization and adds avoidable operational risk.
2. Ignore all timestamps or all leg-553 metadata. This materially weakens CAS
   and could hide a business-state transition.
3. Generate and apply a plan in one production command. That conflates planning
   with mutation and prevents prior exact authorization.

## Verification

RED tests must prove the current tool rejects an `updated_at`-only drift. GREEN
must prove apply, idempotent reapply, rollback, and rendered rollback SQL accept
only the approved coordinate. Parameterized counter-tests change another field,
leg `554.updated_at`, or another action's `updated_at` and require zero writes.

The rebuilt tool is committed locally, copied only into a new private evidence
directory, and exercised only on a fresh independent SQLite backup. Production
remains read-only; no push, deployment, restart, cutover, replay, settings
mutation, or exchange write is authorized.
