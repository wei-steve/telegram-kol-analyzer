# Position Protection Ledger Design

## Goal

Reduce recurring management-message blocks by preserving and repairing verifiable Deepcoin position TPSL ownership evidence from entry through later strategy management actions.

## Problem

Current protection attribution is intentionally fail-closed. It can use exact TPSL position identity, or a globally unique instrument/side/time/size match. Deepcoin pending TPSL rows often omit `posId` / `closePosId`, so a later management message such as Miya 501 may see live positions and inline SL/TP prices but still lack cancellable, uniquely-owned TPSL order IDs. The planner then blocks with `target_protection_not_verified`.

Blocking is correct for exchange safety, but it is operationally weak when many Telegram management messages arrive. The system needs to keep stronger evidence from the moment it creates protection, repair that evidence over time, and surface unresolved management work explicitly.

## Safety Principles

- Never use this ledger to prove strategy position ownership. `execution_order_legs` and position-attribution audits remain the authority for `posId` ownership.
- Never mutate TPSL from ledger evidence alone. A management action still requires a fresh Deepcoin snapshot, unique current TPSL order IDs, and the existing recheck before cancel/replace.
- Do not replay historical management messages automatically after deployment.
- Ledger repair may change local evidence and queue status only; it must not place, cancel, or replace exchange orders.
- Ambiguous global assignment remains blocked. The improvement is to preserve exact evidence earlier and explain unresolved cases better.

## Architecture

Add a durable `position_protection_ledger` table keyed by venue and protection order ID. Each row stores the current known relationship between a verified position leg and one TPSL order: `execution_binding_id`, `execution_order_leg_id`, `pos_id`, `order_id`, purpose, trigger price, size, status, evidence source, evidence JSON, first/last seen timestamps, and last verification time.

The ledger is written in two places:

1. Entry protection creation and management TPSL replacement persist the returned order IDs and the exact position leg context.
2. Read-only repair scans live positions plus pending TPSL rows and upgrades ledger rows only when current exchange rows match prior ledger identity or a globally unique exact-price/time/size relationship.

The planner asks protection attribution for verified current rows. Attribution may include ledger-backed rows only when all of these hold:

- The position itself is verified by existing ownership rules.
- The ledger row belongs to the same binding, leg, and posId.
- The ledger order ID is present in the fresh pending TPSL snapshot.
- The fresh row matches instrument, side, purpose, price, and compatible size.
- No other live position can plausibly claim that order group.

## Operator Visibility

Keep terminal `blocked` semantics for execution safety, but make unresolved management actionable by adding specific reason codes and queryable status:

- `protection_ambiguous_global_assignment`
- `protection_missing_cancellable_order_id`
- `protection_price_or_size_mismatch`
- `protection_ledger_stale`
- `protection_evidence_unavailable`

Existing strategy record and execution views should show unresolved management batches as attention items, with group, entry message, management message, target positions, and the exact reason.

## Phased Delivery

Phase 1 builds the ledger schema, write/read helpers, and reason-code split. It is safe and local.

Phase 2 records ledger rows from successful TPSL set/adjust paths and proves that planner can use ledger-backed pending rows without weakening exchange rechecks.

Phase 3 adds read-only repair and unresolved queue visibility. Existing historical blocked messages are not replayed automatically; they become visible and may be reprocessed only by a separate, explicit operator workflow.

## Validation

- Unit tests cover schema bootstrap, ledger upsert, ledger-backed protection attribution, and ambiguous cases.
- Existing TPSL mutation tests must continue passing, especially ambiguous pending TPSL rejection and pre-cancel recheck.
- Server verification is deploy-only plus read-only ledger repair dry run first. Any exchange write requires a fresh natural message or explicit operator action outside this repair task.
