"""Close out the 29 convergences frozen on ``partial_position_unexplained``.

A-5b, an L3 production data change, planned in
``docs/plans/2026-09-07-management-reliability/step-5b-frozen-convergence-backlog.md``
and ruled on by the coordinating session (see the ``step-5b`` evidence entry in
``docs/management-reliability-status.md``). Backup, copy rehearsal, quick_check
and per-row before/after values are recorded under ``/root/evidence/step5b/``.

**Why these rows are stuck.** ``reconcile_trigger_take_profit_order_history``
only visits convergences whose status is ``submitted`` (or a
rejected-without-submission ``conflicted``). Every one of these 29 is
``conflicted / convergence_partial_position_unexplained``, so the loop
``continue``s past them forever -- their positions closed weeks ago and both the
convergence row and its take-profit rows still read as live. A-5 added the
explanation that stops *new* freezes; it deliberately did not reach backwards,
which is what this module is for.

**What it does, and only that.**

* For a convergence whose position is **absent from a complete exchange
  positions read**: every still-``active`` take-profit row becomes ``expired``
  with ``terminalization.reason_code = "position_terminal_order_absent"``, and
  the convergence becomes ``completed / convergence_position_terminal``. These
  are exactly the writes the online path performs for a terminal position --
  same field, same reason code, same shape -- applied to the rows it cannot
  reach.
* For a convergence whose position is **still live**: nothing changes except
  ``error_json``, which gains the full judgement scene. The freeze stays.

**What it must never do.** It writes no exchange order, changes no price or
size, and touches no protection ledger row. A position that is live keeps its
freeze: this module has no authority to decide that a live position's reduction
was explained -- that judgement belongs to A-5's three criteria running online.

**Fail-closed inputs.** The set of live position ids must come from a positions
read in which *every* row carried an identifiable position id. One unreadable
row and the module refuses to build a plan at all, because "absent from the
snapshot" would then no longer mean "closed".

Every write is a compare-and-set against the value read when the plan was
built, so a row production has moved in the meantime is skipped and reported
rather than overwritten. A second run finds the terminalized rows already past
their ``before`` values and plans nothing for them; the one live-position row
is still frozen by design, so it is re-planned, rewrites the same scene with a
fresh ``observed_at``, and writes no second audit (the fingerprint already
exists). Verified on a production copy on 2026-09-08: 95 rows and 95 audits on
the first run, 1 row and 0 audits on the second.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    PositionAttributionAudit,
    PositionTakeProfitOrder,
    TriggerTakeProfitConvergence,
)


REPAIR_TAG = "a5b_frozen_convergence_backlog_2026_09_09"
EVIDENCE_PATH = "/root/evidence/step5b/"

FROZEN_REASON_CODE = "convergence_partial_position_unexplained"
TERMINAL_REASON_CODE = "convergence_position_terminal"
ORDER_TERMINAL_REASON = "position_terminal_order_absent"

#: Reuses the existing ``historical_cleanup`` value rather than inventing a new
#: one, exactly as A-4's repair did; ``REPAIR_TAG`` in the evidence is what
#: identifies this batch.
AUDIT_EVENT_TYPE = "historical_cleanup"
#: The repair was decided and reviewed out of band, and A-5 landed a delivery
#: gate at ``position_attribution_audits`` id 3844 -- rows written here are
#: newer than the gate, so leaving them ``pending`` would page a person about a
#: change that person already approved.
AUDIT_NOTIFICATION_STATUS = "not_needed"

_POSITION_ID_KEYS = ("posId", "pos_id", "PositionID", "positionId", "position_id")


class FrozenConvergenceRepairError(RuntimeError):
    """The inputs cannot support a safe plan."""


@dataclass(frozen=True, slots=True)
class RowAction:
    table: str
    row_id: int
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    note: str
    execution_binding_id: int | None
    execution_order_leg_id: int | None
    pos_id: str


@dataclass(frozen=True, slots=True)
class RepairPlan:
    actions: tuple[RowAction, ...] = ()
    live_frozen_convergence_ids: tuple[int, ...] = ()
    skipped: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RepairResult:
    applied: tuple[tuple[str, int], ...] = ()
    audit_ids: tuple[int, ...] = ()
    skipped: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def live_position_ids(positions: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Position ids from a snapshot, refusing to answer on an unreadable row.

    A row this cannot identify could be one of the positions the plan is about
    to declare closed, so a single such row invalidates the whole snapshot.
    """

    found: set[str] = set()
    for row in positions:
        if not isinstance(row, Mapping):
            raise FrozenConvergenceRepairError("positions_row_not_mapping")
        value = ""
        for key in _POSITION_ID_KEYS:
            candidate = row.get(key)
            if candidate not in (None, ""):
                value = str(candidate).strip()
                break
        if not value:
            raise FrozenConvergenceRepairError("positions_row_without_id")
        found.add(value)
    return frozenset(found)


def build_plan(
    session_factory: sessionmaker,
    *,
    live_pos_ids: frozenset[str],
    judged_at: datetime,
) -> RepairPlan:
    """Read-only: what this repair would write, row by row."""

    actions: list[RowAction] = []
    live_frozen: list[int] = []
    skipped: list[dict[str, Any]] = []
    stamp = judged_at.isoformat()
    with session_factory() as session:
        convergences = (
            session.query(TriggerTakeProfitConvergence)
            .filter(
                TriggerTakeProfitConvergence.status == "conflicted",
                TriggerTakeProfitConvergence.reason_code == FROZEN_REASON_CODE,
            )
            .order_by(TriggerTakeProfitConvergence.id.asc())
            .all()
        )
        for convergence in convergences:
            pos_id = str(convergence.pos_id or "").strip()
            if not pos_id:
                skipped.append(
                    {
                        "convergence_id": int(convergence.id),
                        "reason": "convergence_without_position_id",
                    }
                )
                continue
            orders = (
                session.query(PositionTakeProfitOrder)
                .filter(
                    PositionTakeProfitOrder.trigger_take_profit_convergence_id
                    == int(convergence.id)
                )
                .order_by(PositionTakeProfitOrder.id.asc())
                .all()
            )
            if pos_id in live_pos_ids:
                # The position is still open. The freeze stays; only the
                # judgement scene is recorded, so a person reading this row
                # later sees what was compared rather than a bare verdict.
                live_frozen.append(int(convergence.id))
                actions.append(
                    _live_position_action(
                        convergence=convergence,
                        orders=orders,
                        judged_at=stamp,
                    )
                )
                continue
            for order in orders:
                if str(order.status or "") != "active":
                    continue
                actions.append(
                    RowAction(
                        table="position_take_profit_orders",
                        row_id=int(order.id),
                        before={"status": str(order.status)},
                        after={"status": "expired"},
                        note=(
                            f"take-profit {order.order_id} sz={order.size_text} "
                            f"@{order.trigger_price}: its position {pos_id} is "
                            "absent from a complete positions read, so the order "
                            f"is gone too ({ORDER_TERMINAL_REASON})"
                        ),
                        execution_binding_id=int(order.execution_binding_id),
                        execution_order_leg_id=int(order.execution_order_leg_id),
                        pos_id=pos_id,
                    )
                )
            actions.append(
                RowAction(
                    table="trigger_take_profit_convergences",
                    row_id=int(convergence.id),
                    before={
                        "status": "conflicted",
                        "reason_code": FROZEN_REASON_CODE,
                    },
                    after={
                        "status": "completed",
                        "reason_code": TERMINAL_REASON_CODE,
                    },
                    note=(
                        f"position {pos_id} absent from a complete positions "
                        "read; there is no ladder left to rebuild"
                    ),
                    execution_binding_id=int(convergence.execution_binding_id),
                    execution_order_leg_id=int(convergence.execution_order_leg_id),
                    pos_id=pos_id,
                )
            )
    return RepairPlan(
        actions=tuple(actions),
        live_frozen_convergence_ids=tuple(live_frozen),
        skipped=tuple(skipped),
    )


def _live_position_action(
    *,
    convergence: TriggerTakeProfitConvergence,
    orders: list[PositionTakeProfitOrder],
    judged_at: str,
) -> RowAction:
    return RowAction(
        table="trigger_take_profit_convergences",
        row_id=int(convergence.id),
        before={
            "status": "conflicted",
            "reason_code": FROZEN_REASON_CODE,
        },
        after={
            # Unchanged on purpose: the freeze is the outcome.
            "status": "conflicted",
            "reason_code": FROZEN_REASON_CODE,
        },
        note=(
            f"position {pos_id_of(convergence)} is still open; the freeze "
            "stays and only the judgement scene is recorded"
        ),
        execution_binding_id=int(convergence.execution_binding_id),
        execution_order_leg_id=int(convergence.execution_order_leg_id),
        pos_id=pos_id_of(convergence),
    )


def pos_id_of(convergence: TriggerTakeProfitConvergence) -> str:
    return str(convergence.pos_id or "").strip()


def apply_plan(
    session_factory: sessionmaker,
    plan: RepairPlan,
    *,
    applied_at: datetime | None = None,
    live_position_scene: Mapping[int, Mapping[str, Any]] | None = None,
) -> RepairResult:
    """Apply every action whose row still matches the value the plan read."""

    moment = applied_at or datetime.now(UTC)
    applied: list[tuple[str, int]] = []
    audit_ids: list[int] = []
    skipped: list[dict[str, Any]] = list(plan.skipped)
    models = {
        "trigger_take_profit_convergences": TriggerTakeProfitConvergence,
        "position_take_profit_orders": PositionTakeProfitOrder,
    }
    with session_factory() as session:
        for action in plan.actions:
            model = models[action.table]
            row = session.get(model, action.row_id)
            if row is None:
                skipped.append(
                    {"table": action.table, "row_id": action.row_id, "reason": "missing"}
                )
                continue
            mismatch = {
                name: getattr(row, name)
                for name, value in action.before.items()
                if str(getattr(row, name, None)) != str(value)
            }
            if mismatch:
                # Production moved this row after the plan was built. Refusing
                # is the whole point of the compare-and-set.
                skipped.append(
                    {
                        "table": action.table,
                        "row_id": action.row_id,
                        "reason": "changed_since_plan",
                        "observed": {k: str(v) for k, v in mismatch.items()},
                    }
                )
                continue
            changes: list[tuple[str, Any, Any]] = []
            for name, value in action.after.items():
                current = getattr(row, name)
                if str(current) != str(value):
                    changes.append((name, current, value))
                    setattr(row, name, value)
            if action.table == "position_take_profit_orders":
                evidence = _load_json(row.evidence_json)
                evidence["terminalization"] = {
                    "reason_code": ORDER_TERMINAL_REASON,
                    "observed_at": moment.isoformat(),
                    "repair": REPAIR_TAG,
                }
                row.evidence_json = _dump_json(evidence)
                row.completed_at = moment
                changes.append(
                    ("evidence_json.terminalization", None, ORDER_TERMINAL_REASON)
                )
            elif int(action.row_id) in set(plan.live_frozen_convergence_ids):
                scene = dict((live_position_scene or {}).get(int(action.row_id), {}))
                error = _load_json(row.error_json)
                error["partial_position_unexplained"] = {
                    **scene,
                    "repair": REPAIR_TAG,
                    "observed_at": moment.isoformat(),
                }
                row.error_json = _dump_json(error)
                changes.append(("error_json.partial_position_unexplained", None, "recorded"))
            else:
                row.completed_at = moment
                changes.append(("completed_at", None, moment.isoformat()))
            row.updated_at = moment
            fingerprint = _audit_fingerprint(action.table, action.row_id)
            existing = (
                session.query(PositionAttributionAudit)
                .filter(PositionAttributionAudit.fingerprint == fingerprint)
                .one_or_none()
            )
            if existing is None:
                audit = PositionAttributionAudit(
                    execution_binding_id=action.execution_binding_id,
                    execution_order_leg_id=action.execution_order_leg_id,
                    venue="deepcoin",
                    pos_id=action.pos_id,
                    event_type=AUDIT_EVENT_TYPE,
                    prior_state=(
                        str(changes[0][1])[:32]
                        if changes and changes[0][1] is not None
                        else None
                    ),
                    new_state=str(changes[0][2])[:32] if changes else REPAIR_TAG,
                    fingerprint=fingerprint,
                    evidence_json=json.dumps(
                        {
                            "repair": REPAIR_TAG,
                            "evidence_path": EVIDENCE_PATH,
                            "table": action.table,
                            "row_id": action.row_id,
                            "changes": [
                                {"field": name, "before": before, "after": after}
                                for name, before, after in changes
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
        applied=tuple(applied),
        audit_ids=tuple(audit_ids),
        skipped=tuple(skipped),
    )


def _audit_fingerprint(table: str, row_id: int) -> str:
    return hashlib.sha256(
        f"{REPAIR_TAG}\0{table}\0{int(row_id)}".encode("utf-8")
    ).hexdigest()


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _dump_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
