"""Repair the ledger rows the 2026-09-07 management incident left behind.

A-4, an L3 production data change, approved in
``docs/management-reliability-status.md`` (evidence entry ``step-4-approval``).
Every row below was re-verified against Deepcoin ``position-history`` /
``trigger-orders-history`` / ``positions`` before this module was written; the
raw JSON is in ``/root/evidence/step4/``.

What is being repaired, and why each row is stuck:

* **binding 337 / legs 579-580 / lifecycle 1074.** Both entry positions were
  closed on the exchange at ``2026-09-04T13:07:38Z`` (pos 1001125123045253:
  ``closePos=10 closeAvgPx=80149.8``, which is exactly TP1 filling 5 @81100
  plus the stop filling 5 @79200; pos 1001125126414222: ``closePos=14
  closeAvgPx=79200``). The ledger never followed, so ``lifecycle_monitor``
  re-derives a stop-loss exit every minute and declines to write it.
* **take-profit orders 195-197.** TP1 (195) triggered at
  ``2026-09-04T08:34:43Z``; TP2/TP3 never triggered (``triggerTime="0"``) and
  disappeared when the position closed. They are still ``active`` because
  ``position_take_profit_orders.reconcile`` only visits convergences whose
  status is ``submitted`` (or a rejected-without-submission ``conflicted``),
  and convergence 222 is ``conflicted /
  convergence_partial_position_unexplained`` -- so it is skipped forever.
* **protection rows.** The leg-579 stop ledger row and the three leg-580
  take-profit legs (852-854, ``exchange_order_id`` NULL: never created on the
  exchange) belong to positions that no longer exist.
* **lifecycle 1081.** Entered on paper with ``execution_binding_id`` NULL after
  a failed entry; no binding, no legs, no exchange exposure.
* **five ``source_message_deletion_exits``.** 109/128/201/231 froze on
  ``frozen_ledger_identity_unverified`` because the worker's ``hazardous_event``
  probe treats *any* ``execution_events`` row carrying ``request_json`` as
  evidence of an exchange action -- and the rows it found (3501/3589/3796/3967)
  are ``action='auto_trade_skipped'``, ``status='skipped'``, with
  ``order_id``/``client_order_id``/``pos_id`` all NULL. Those are records of
  *not* trading. 209 froze on ``exact_lifecycle_missing``. None of the five
  ever had an ``execution_bindings`` row, so none ever had a position; each one
  nevertheless pins its ``(chat_id, symbol, side)`` lane, because
  ``source_execution_barrier`` holds on ``state != 'succeeded'``.
  **The judgement defect itself is not fixed here** (that belongs to step 5);
  this module only releases the five rows it was approved to release.
* **convergences 230 and 231.** Both were pushed to the terminal
  ``convergence_exact_leg_not_verified`` by a transient condition (see the 1b
  and 1c evidence). 230's position was stopped out at 78500 on
  ``2026-09-08T06:00:24Z``, so it is terminalized. 231's position is still live
  (ETH long, 1.6 @2526.08, stop only, no take profit), so it is returned to the
  waiting state the online scan itself writes before promoting a row -- and the
  production code, not this module, re-derives readiness from there.

Every write is a compare-and-set against the value recorded when the manifest
was approved, so this module can only ever act on those exact rows in that
exact state. Run it once; a second run reports zero changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    PositionProtectionLeg,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    TriggerTakeProfitConvergence,
)

#: Marks every row this module touches, in the audit evidence.
REPAIR_TAG = "repair_2026_09_08"
#: Where the read-only exchange evidence for this repair lives on the server.
EVIDENCE_PATH = "/root/evidence/step4"

#: Exchange-confirmed moments, from ``position-history`` /
#: ``trigger-orders-history``. Stored as naive UTC, like every other datetime
#: column in this database.
TP1_FILLED_AT = datetime(2026, 9, 4, 8, 34, 43)
POSITIONS_CLOSED_AT = datetime(2026, 9, 4, 13, 7, 38)

#: One frozen row spec: the exact row, the exact fields, the value each field
#: must still hold, and the value it is moved to. ``before`` is the
#: compare-and-set guard -- a row whose current values differ is skipped and
#: reported, never forced.
@dataclass(frozen=True, slots=True)
class RowSpec:
    table: str
    row_id: int
    before: dict[str, object]
    after: dict[str, object]
    note: str
    #: For the audit row, when the repair concerns an exact binding/leg/pos.
    execution_binding_id: int | None = None
    execution_order_leg_id: int | None = None
    pos_id: str | None = None


#: SQLAlchemy model per ``RowSpec.table``.
_MODELS = {
    "execution_bindings": ExecutionBinding,
    "execution_order_legs": ExecutionOrderLeg,
    "strategy_lifecycles": StrategyLifecycle,
    "position_take_profit_orders": PositionTakeProfitOrder,
    "position_protection_ledger": PositionProtectionLedger,
    "position_protection_legs": PositionProtectionLeg,
    "source_message_deletion_exits": SourceMessageDeletionExit,
    "trigger_take_profit_convergences": TriggerTakeProfitConvergence,
}


#: The frozen manifest, in the order of the approved table. ``before`` holds
#: only fields whose value is part of the decision: ``updated_at`` and
#: ``last_verified_at`` are deliberately excluded because live reconciliation
#: loops still touch them (binding 337 was last stamped 2026-09-08T13:54:37Z
#: while this manifest was being built), and guarding on them would make the
#: repair fail for a reason that has nothing to do with its correctness.
ROW_SPECS: tuple[RowSpec, ...] = (
    RowSpec(
        table="execution_bindings",
        row_id=337,
        before={
            "status": "active",
            "last_exchange_status": "position_attribution_evidence_unavailable",
        },
        after={
            "status": "closed",
            "last_exchange_status": "stop_loss_confirmed_by_position_history",
        },
        note=(
            "both entry positions closed 2026-09-04T13:07:38Z; "
            "position-history closePos 10 and 14"
        ),
        execution_binding_id=337,
        pos_id="1001125123045253",
    ),
    RowSpec(
        table="execution_order_legs",
        row_id=579,
        before={"status": "active", "terminal_reason": None},
        after={
            "status": "closed",
            "terminal_reason": "historical_exchange_position_closed",
        },
        note="pos 1001125123045253 closePos=10 closeAvgPx=80149.8 (TP1 5 + stop 5)",
        execution_binding_id=337,
        execution_order_leg_id=579,
        pos_id="1001125123045253",
    ),
    RowSpec(
        table="execution_order_legs",
        row_id=580,
        before={"status": "active", "terminal_reason": None},
        after={
            "status": "closed",
            "terminal_reason": "historical_exchange_position_closed",
        },
        note="pos 1001125126414222 closePos=14 closeAvgPx=79200 (stop)",
        execution_binding_id=337,
        execution_order_leg_id=580,
        pos_id="1001125126414222",
    ),
    RowSpec(
        table="strategy_lifecycles",
        row_id=1074,
        before={
            "lifecycle_status": "entered",
            "exit_reason": None,
            "exited_at": None,
        },
        after={
            "lifecycle_status": "exited",
            "exit_reason": "stop_loss",
            "exited_at": POSITIONS_CLOSED_AT,
        },
        note=(
            "exit_price_actual deliberately left NULL: the two legs closed at "
            "80149.8 and 79200, and no single price is a fact of this ledger"
        ),
        execution_binding_id=337,
    ),
    RowSpec(
        table="position_take_profit_orders",
        row_id=195,
        before={"status": "active", "completed_at": None},
        after={"status": "filled", "completed_at": TP1_FILLED_AT},
        note=(
            "order 1001125123049529 triggerTime=1788510883 "
            "(2026-09-04T08:34:43Z), sz=5 @81100"
        ),
        execution_binding_id=337,
        execution_order_leg_id=579,
        pos_id="1001125123045253",
    ),
    RowSpec(
        table="position_take_profit_orders",
        row_id=196,
        before={"status": "active", "completed_at": None},
        after={"status": "expired", "completed_at": POSITIONS_CLOSED_AT},
        note=(
            "order 1001125123049649 triggerTime=0 (never triggered), "
            "uTime 2026-09-04T13:07:38Z: removed when the position closed"
        ),
        execution_binding_id=337,
        execution_order_leg_id=579,
        pos_id="1001125123045253",
    ),
    RowSpec(
        table="position_take_profit_orders",
        row_id=197,
        before={"status": "active", "completed_at": None},
        after={"status": "expired", "completed_at": POSITIONS_CLOSED_AT},
        note=(
            "order 1001125123049805 triggerTime=0 (never triggered), "
            "uTime 2026-09-04T13:07:38Z: removed when the position closed"
        ),
        execution_binding_id=337,
        execution_order_leg_id=579,
        pos_id="1001125123045253",
    ),
    RowSpec(
        table="position_protection_ledger",
        row_id=652,
        before={"status": "verified"},
        after={"status": "cancelled"},
        note=(
            "stop 1001125123045252 triggerTime=1788527258 (filled at "
            "2026-09-04T13:07:38Z); size_text stays '10' as the historical fact"
        ),
        execution_binding_id=337,
        execution_order_leg_id=579,
        pos_id="1001125123045253",
    ),
    *(
        RowSpec(
            table="position_protection_legs",
            row_id=leg_id,
            before={"status": "protection_recovery_pending", "exchange_order_id": None},
            after={"status": "cancelled"},
            note=(
                "leg 580 take profit never created on the exchange "
                "(exchange_order_id NULL) and its position closed"
            ),
            execution_binding_id=337,
            execution_order_leg_id=580,
            pos_id="1001125126414222",
        )
        for leg_id in (852, 853, 854)
    ),
    RowSpec(
        table="trigger_take_profit_convergences",
        row_id=230,
        before={
            "status": "conflicted",
            "reason_code": "convergence_exact_leg_not_verified",
            "completed_at": None,
        },
        after={
            "status": "completed",
            "reason_code": "convergence_position_terminal",
        },
        note=(
            "pos 1001125163581280 closePos=8 closeAvgPx=78500 "
            "uTime 2026-09-08T06:00:24Z: stopped out, no ladder to rebuild"
        ),
        execution_binding_id=341,
        execution_order_leg_id=586,
        pos_id="1001125163581280",
    ),
    RowSpec(
        table="trigger_take_profit_convergences",
        row_id=231,
        before={
            "status": "conflicted",
            "reason_code": "convergence_exact_leg_not_verified",
            "completed_at": None,
        },
        after={
            "status": "waiting_backup_stop",
            "reason_code": "convergence_waiting_backup_stop",
        },
        note=(
            "pos 1001125164628529 still live (ETH long 1.6 @2526.08, stop only, "
            "no take profit); returned to the waiting state the online scan "
            "writes itself, so production code re-derives readiness"
        ),
        execution_binding_id=342,
        execution_order_leg_id=588,
        pos_id="1001125164628529",
    ),
)


#: The five deletion exits, with the reason each one froze and the evidence
#: that it never had an exchange position. Kept apart from ``ROW_SPECS``
#: because each one also gets a ``flat_proof_json`` written from its own
#: evidence rather than a fixed value.
DELETION_EXIT_RELEASE_REASON = "repair_2026_09_08_position_gone"
DELETION_EXIT_SPECS: tuple[dict[str, object], ...] = (
    {
        "id": 109,
        "before_reason": "frozen_ledger_identity_unverified",
        "lane": {"chat_id": -1002960443256, "symbol": "BTC", "side": "short"},
        "target_lifecycle_id": 838,
        "raw_message_id": 10877,
        "blocking_execution_event_id": 3501,
    },
    {
        "id": 128,
        "before_reason": "frozen_ledger_identity_unverified",
        "lane": {"chat_id": -1002368892075, "symbol": "ETH", "side": "short"},
        "target_lifecycle_id": 886,
        "raw_message_id": 11530,
        "blocking_execution_event_id": 3589,
    },
    {
        "id": 201,
        "before_reason": "frozen_ledger_identity_unverified",
        "lane": {"chat_id": -1002337721508, "symbol": "BTC", "side": "long"},
        "target_lifecycle_id": 1031,
        "raw_message_id": 13776,
        "blocking_execution_event_id": 3796,
    },
    {
        "id": 209,
        "before_reason": "exact_lifecycle_missing",
        "lane": {"chat_id": -1002199068560, "symbol": "ETH", "side": "long"},
        "target_lifecycle_id": None,
        "raw_message_id": 14465,
        "blocking_execution_event_id": None,
    },
    {
        "id": 231,
        "before_reason": "frozen_ledger_identity_unverified",
        "lane": {"chat_id": -1002960443256, "symbol": "ZEC", "side": "short"},
        "target_lifecycle_id": 1079,
        "raw_message_id": 14780,
        "blocking_execution_event_id": 3967,
    },
)


@dataclass(frozen=True, slots=True)
class RepairAction:
    table: str
    row_id: int
    changes: tuple[tuple[str, object, object], ...]  # (field, before, after)
    note: str


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """Read-only: what would change, and what no longer matches the manifest."""

    actions: tuple[RepairAction, ...] = ()
    skipped: tuple[dict[str, object], ...] = field(default_factory=tuple)

    @property
    def action_count(self) -> int:
        return len(self.actions)


@dataclass(frozen=True, slots=True)
class RepairResult:
    applied_row_ids: tuple[tuple[str, int], ...] = ()
    audit_ids: tuple[int, int] | tuple[int, ...] = ()
    skipped: tuple[dict[str, object], ...] = field(default_factory=tuple)


#: ``position_attribution_audits.event_type`` reuses the existing
#: ``historical_cleanup`` value (55 rows already) rather than inventing a new
#: one; ``REPAIR_TAG`` inside the evidence is what identifies this batch.
AUDIT_EVENT_TYPE = "historical_cleanup"
#: These audits document a repair that was decided and reviewed out of band;
#: the two notification channels are disabled anyway (step-2 ruling), and
#: leaving them ``pending`` would add rows to a 2840-deep backlog that step 5
#: has to drain.
AUDIT_NOTIFICATION_STATUS = "not_needed"


def _audit_fingerprint(table: str, row_id: int) -> str:
    import hashlib

    return hashlib.sha256(
        f"{REPAIR_TAG}:{table}:{int(row_id)}".encode("utf-8")
    ).hexdigest()


def _matches_before(row: object, before: dict[str, object]) -> bool:
    return all(getattr(row, name, None) == value for name, value in before.items())


def _current(row: object, names) -> dict[str, object]:
    return {name: getattr(row, name, None) for name in names}


def plan_management_ledger_repair(
    session_factory: sessionmaker,
    *,
    specs: tuple[RowSpec, ...] = ROW_SPECS,
    deletion_exit_specs: tuple[dict[str, object], ...] = DELETION_EXIT_SPECS,
) -> RepairPlan:
    """Report what would change. Touches nothing."""

    actions: list[RepairAction] = []
    skipped: list[dict[str, object]] = []
    with session_factory() as session:
        for spec in specs:
            model = _MODELS[spec.table]
            row = session.get(model, spec.row_id)
            if row is None:
                skipped.append(
                    {"table": spec.table, "row_id": spec.row_id, "reason": "row_missing"}
                )
                continue
            if not _matches_before(row, spec.before):
                skipped.append(
                    {
                        "table": spec.table,
                        "row_id": spec.row_id,
                        "reason": "before_state_changed",
                        "expected": dict(spec.before),
                        "found": _current(row, spec.before),
                    }
                )
                continue
            actions.append(
                RepairAction(
                    table=spec.table,
                    row_id=spec.row_id,
                    changes=tuple(
                        (name, getattr(row, name, None), value)
                        for name, value in spec.after.items()
                    ),
                    note=spec.note,
                )
            )
        for exit_spec in deletion_exit_specs:
            row = session.get(SourceMessageDeletionExit, int(exit_spec["id"]))
            if row is None:
                skipped.append(
                    {
                        "table": "source_message_deletion_exits",
                        "row_id": int(exit_spec["id"]),
                        "reason": "row_missing",
                    }
                )
                continue
            if (
                row.state != "recovery_required"
                or row.last_reason != exit_spec["before_reason"]
            ):
                skipped.append(
                    {
                        "table": "source_message_deletion_exits",
                        "row_id": int(exit_spec["id"]),
                        "reason": "before_state_changed",
                        "expected": {
                            "state": "recovery_required",
                            "last_reason": exit_spec["before_reason"],
                        },
                        "found": {"state": row.state, "last_reason": row.last_reason},
                    }
                )
                continue
            actions.append(
                RepairAction(
                    table="source_message_deletion_exits",
                    row_id=int(exit_spec["id"]),
                    changes=(
                        ("state", row.state, "succeeded"),
                        ("last_reason", row.last_reason, DELETION_EXIT_RELEASE_REASON),
                        ("last_error", row.last_error, None),
                    ),
                    note=(
                        f"lane {exit_spec['lane']} released; no execution_bindings "
                        "row ever existed for this strategy"
                    ),
                )
            )
    return RepairPlan(actions=tuple(actions), skipped=tuple(skipped))


def apply_management_ledger_repair(
    session_factory: sessionmaker,
    *,
    expected_action_count: int,
    now: datetime | None = None,
    specs: tuple[RowSpec, ...] = ROW_SPECS,
    deletion_exit_specs: tuple[dict[str, object], ...] = DELETION_EXIT_SPECS,
) -> RepairResult:
    """Apply exactly the frozen rows that still match the manifest.

    ``expected_action_count`` is the count from the plan that was reviewed;
    a mismatch refuses the whole run rather than applying a subset nobody saw.
    """

    moment = now or datetime.now(UTC)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)

    plan = plan_management_ledger_repair(
        session_factory, specs=specs, deletion_exit_specs=deletion_exit_specs
    )
    if plan.action_count != int(expected_action_count):
        raise ValueError(
            "refusing apply: plan has "
            f"{plan.action_count} action(s), expected {int(expected_action_count)}"
        )

    applied: list[tuple[str, int]] = []
    audit_ids: list[int] = []
    spec_by_key = {(spec.table, spec.row_id): spec for spec in specs}
    exit_by_id = {int(item["id"]): item for item in deletion_exit_specs}

    with session_factory() as session:
        for action in plan.actions:
            if action.table == "source_message_deletion_exits":
                exit_spec = exit_by_id[action.row_id]
                row = session.get(SourceMessageDeletionExit, action.row_id)
                if row is None or row.state != "recovery_required":
                    continue
                row.state = "succeeded"
                row.last_reason = DELETION_EXIT_RELEASE_REASON
                row.last_error = None
                row.flat_proof_json = json.dumps(
                    {
                        "proved_at": moment.isoformat(),
                        "repair": REPAIR_TAG,
                        "evidence_path": EVIDENCE_PATH,
                        "lane": exit_spec["lane"],
                        "target_lifecycle_id": exit_spec["target_lifecycle_id"],
                        "raw_message_id": exit_spec["raw_message_id"],
                        "froze_on": exit_spec["before_reason"],
                        "blocking_execution_event_id": exit_spec[
                            "blocking_execution_event_id"
                        ],
                        "execution_bindings": 0,
                        "execution_order_legs": 0,
                        "position_protection_ledger": 0,
                        "note": (
                            "no exchange position ever existed for this strategy; "
                            "the blocking execution_event is action="
                            "'auto_trade_skipped' with order/client/pos ids NULL"
                        ),
                    },
                    sort_keys=True,
                    default=str,
                )
                row.last_reconciled_at = moment
                row.completed_at = moment
                row.updated_at = moment
                evidence_binding = None
                evidence_leg = None
                evidence_pos = None
            else:
                spec = spec_by_key[(action.table, action.row_id)]
                model = _MODELS[action.table]
                row = session.get(model, action.row_id)
                if row is None or not _matches_before(row, spec.before):
                    continue
                for name, value in spec.after.items():
                    setattr(row, name, value)
                if hasattr(row, "updated_at"):
                    row.updated_at = moment
                evidence_binding = spec.execution_binding_id
                evidence_leg = spec.execution_order_leg_id
                evidence_pos = spec.pos_id

            fingerprint = _audit_fingerprint(action.table, action.row_id)
            existing = (
                session.query(PositionAttributionAudit)
                .filter(PositionAttributionAudit.fingerprint == fingerprint)
                .one_or_none()
            )
            if existing is None:
                audit = PositionAttributionAudit(
                    execution_binding_id=evidence_binding,
                    execution_order_leg_id=evidence_leg,
                    venue="deepcoin",
                    pos_id=evidence_pos,
                    event_type=AUDIT_EVENT_TYPE,
                    prior_state=str(action.changes[0][1])[:32]
                    if action.changes
                    else None,
                    new_state=str(action.changes[0][2])[:32]
                    if action.changes
                    else REPAIR_TAG,
                    fingerprint=fingerprint,
                    evidence_json=json.dumps(
                        {
                            "repair": REPAIR_TAG,
                            "evidence_path": EVIDENCE_PATH,
                            "table": action.table,
                            "row_id": action.row_id,
                            "changes": [
                                {"field": name, "before": before, "after": after}
                                for name, before, after in action.changes
                            ],
                            "note": action.note,
                        },
                        sort_keys=True,
                        default=str,
                    ),
                    notification_status=AUDIT_NOTIFICATION_STATUS,
                    created_at=moment,
                )
                session.add(audit)
                session.flush()
                audit_ids.append(int(audit.id))
            applied.append((action.table, action.row_id))
        session.commit()

    return RepairResult(
        applied_row_ids=tuple(applied),
        audit_ids=tuple(audit_ids),
        skipped=plan.skipped,
    )
