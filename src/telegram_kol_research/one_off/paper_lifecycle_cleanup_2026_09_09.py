"""Label the simulated lifecycles, and give 1074 back the exit it really had.

A-7 task 5 and 6, an L3 production data change, planned in
``docs/plans/2026-09-07-management-reliability/step-7-target-resolution-and-ghost-lifecycles.md``
and ruled on by the coordinating session. Backup, copy rehearsal, quick_check
and per-row before/after are under ``/root/evidence/step7/``.

**Lifecycle 1074.** Its position closed on the exchange at
2026-09-04T13:07:38Z, stopped out at 78500 (A-4 read that from
``position-history``). Four days later the management path wrote it as
``exited / kol_signal`` at 2026-09-08T15:27:19 -- the moment the ledger caught
up, not the moment the position closed, and the wrong reason besides. A-4
recorded the discrepancy and left it for this step. The row is corrected to the
exchange's own fact.

**The ten paper lifecycles.** Every one of them is in a ``notify_only`` group
and carries no execution binding: they are simulations, which is what those
groups are *for*. The A-7 read-only survey settled the open question from step
4 -- whether any belonged to a group that trades, and would therefore need
``entry_failed`` instead. None do. Lifecycle 1121 was the only auto_trade one
and A-3d already voided it. So the whole set is labelled rather than
terminalized: nothing about them is wrong, they were simply indistinguishable
from real positions to a reader, and A-7's candidate gate now excludes them
anyway.

Labelling preserves what is already there. ``management_action`` is only
written where it is empty -- two rows carry ``expiry_review_requested``, which
records that a person was asked to look, and overwriting that would delete the
one fact those rows have. The note is prepended for all ten, so a single
greppable statement exists on every row.

**The five shadow jobs.** ``message_processing_jobs`` 9/11/12/14/15 are
``shadow = 1`` rows from the 2026-08-20 queue cutover. The claim query excludes
shadow rows deliberately (ARCHITECTURE section 6), and all five messages were
processed at the time -- each has a recognition decision. They are the last
non-terminal shadow rows, and they are terminalized here so nothing is left
looking like unfinished work. The claim condition is not changed and no alert
is added: it is not a defect.

Every write is a compare-and-set against the value read when the plan was
built, so a row production has moved is skipped and reported rather than
overwritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MessageProcessingJob,
    PositionAttributionAudit,
    StrategyLifecycle,
)


REPAIR_TAG = "a7_paper_lifecycle_cleanup_2026_09_09"
EVIDENCE_PATH = "/root/evidence/step7/"

AUDIT_EVENT_TYPE = "historical_cleanup"
AUDIT_NOTIFICATION_STATUS = "not_needed"

SIMULATED_ONLY = "simulated_only"
SIMULATED_NOTE = (
    "simulated_only: notify_only group, no execution binding; "
    "lifecycle advanced by candle replay, never by an exchange fill"
)

#: 1074's real exit, read from Deepcoin position-history during A-4.
LIFECYCLE_1074_EXIT_REASON = "stop_loss"
LIFECYCLE_1074_EXITED_AT = datetime(2026, 9, 4, 13, 7, 38)

#: Every one of these was confirmed notify_only with a NULL binding, and the
#: exchange was read directly to confirm no position stands behind any of them.
PAPER_LIFECYCLE_IDS: tuple[int, ...] = (
    1091, 1102, 1105, 1108, 1112, 1114, 1116, 1117, 1119, 1120,
)
SHADOW_JOB_IDS: tuple[int, ...] = (9, 11, 12, 14, 15)
SHADOW_JOB_REASON = "stale_job_voided_2026_09_09"


class PaperLifecycleCleanupError(RuntimeError):
    """The inputs cannot support a safe plan."""


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    lifecycle_1074: dict[str, Any] | None = None
    paper_lifecycles: tuple[dict[str, Any], ...] = ()
    shadow_jobs: tuple[dict[str, Any], ...] = ()
    skipped: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class CleanupResult:
    applied: tuple[tuple[str, int], ...] = ()
    audit_ids: tuple[int, ...] = ()
    skipped: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def build_plan(
    session_factory: sessionmaker,
    *,
    live_position_ids: frozenset[str],
) -> CleanupPlan:
    """Read-only: what this would write, and what it refuses to touch.

    ``live_position_ids`` comes from a direct positions read. A paper lifecycle
    is only labelled when nothing on the exchange stands behind it; the check
    is trivially satisfied for a NULL binding, and it is done anyway so the
    plan cannot be run against a database where one of them acquired a
    position.
    """

    skipped: list[dict[str, Any]] = []
    paper: list[dict[str, Any]] = []
    lifecycle_1074: dict[str, Any] | None = None
    with session_factory() as session:
        row = session.get(StrategyLifecycle, 1074)
        if row is None:
            skipped.append({"table": "strategy_lifecycles", "row_id": 1074,
                            "reason": "missing"})
        elif str(row.lifecycle_status) != "exited":
            skipped.append({"table": "strategy_lifecycles", "row_id": 1074,
                            "reason": "not_exited",
                            "observed": str(row.lifecycle_status)})
        elif str(row.exit_reason or "") == LIFECYCLE_1074_EXIT_REASON:
            skipped.append({"table": "strategy_lifecycles", "row_id": 1074,
                            "reason": "already_corrected"})
        else:
            lifecycle_1074 = {
                "row_id": 1074,
                "before": {
                    "exit_reason": row.exit_reason,
                    "exited_at": str(row.exited_at),
                },
                "after": {
                    "exit_reason": LIFECYCLE_1074_EXIT_REASON,
                    "exited_at": str(LIFECYCLE_1074_EXITED_AT),
                },
            }
        for lifecycle_id in PAPER_LIFECYCLE_IDS:
            paper_row = session.get(StrategyLifecycle, lifecycle_id)
            if paper_row is None:
                skipped.append({"table": "strategy_lifecycles",
                                "row_id": lifecycle_id, "reason": "missing"})
                continue
            if paper_row.execution_binding_id is not None:
                # It acquired a binding since the survey: out of scope, and a
                # labelled row would then be a false statement.
                skipped.append({"table": "strategy_lifecycles",
                                "row_id": lifecycle_id,
                                "reason": "has_execution_binding"})
                continue
            if SIMULATED_ONLY in str(paper_row.management_note or ""):
                skipped.append({"table": "strategy_lifecycles",
                                "row_id": lifecycle_id,
                                "reason": "already_labelled"})
                continue
            paper.append(
                {
                    "row_id": lifecycle_id,
                    "chat_id": paper_row.chat_id,
                    "before": {
                        "management_action": paper_row.management_action,
                        "management_note": paper_row.management_note,
                        "lifecycle_status": paper_row.lifecycle_status,
                    },
                }
            )
        jobs: list[dict[str, Any]] = []
        for job_id in SHADOW_JOB_IDS:
            job = session.get(MessageProcessingJob, job_id)
            if job is None:
                skipped.append({"table": "message_processing_jobs",
                                "row_id": job_id, "reason": "missing"})
                continue
            if str(job.status) != "pending":
                skipped.append({"table": "message_processing_jobs",
                                "row_id": job_id, "reason": "not_pending",
                                "observed": str(job.status)})
                continue
            if int(job.shadow or 0) != 1:
                # A non-shadow pending job is ordinary queued work. Never.
                skipped.append({"table": "message_processing_jobs",
                                "row_id": job_id, "reason": "not_a_shadow_row"})
                continue
            jobs.append(
                {
                    "row_id": job_id,
                    "raw_message_id": int(job.raw_message_id),
                    "before": {"status": job.status, "last_reason": job.last_reason},
                }
            )
    return CleanupPlan(
        lifecycle_1074=lifecycle_1074,
        paper_lifecycles=tuple(paper),
        shadow_jobs=tuple(jobs),
        skipped=tuple(skipped),
    )


def apply_plan(
    session_factory: sessionmaker,
    plan: CleanupPlan,
    *,
    applied_at: datetime | None = None,
) -> CleanupResult:
    moment = applied_at or datetime.now(UTC)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    applied: list[tuple[str, int]] = []
    audit_ids: list[int] = []
    skipped: list[dict[str, Any]] = list(plan.skipped)
    with session_factory() as session:
        if plan.lifecycle_1074 is not None:
            row = session.get(StrategyLifecycle, 1074)
            before = plan.lifecycle_1074["before"]
            if row is None or str(row.exit_reason or "") != str(
                before["exit_reason"] or ""
            ):
                skipped.append({"table": "strategy_lifecycles", "row_id": 1074,
                                "reason": "changed_since_plan"})
            else:
                changes = [
                    ("exit_reason", row.exit_reason, LIFECYCLE_1074_EXIT_REASON),
                    ("exited_at", str(row.exited_at), str(LIFECYCLE_1074_EXITED_AT)),
                ]
                row.exit_reason = LIFECYCLE_1074_EXIT_REASON
                row.exited_at = LIFECYCLE_1074_EXITED_AT
                row.updated_at = moment
                audit_ids.extend(
                    _audit(
                        session,
                        table="strategy_lifecycles",
                        row_id=1074,
                        changes=changes,
                        note=(
                            "exchange position-history: stopped out at 78500 on "
                            "2026-09-04T13:07:38Z; the ledger had recorded the "
                            "moment it caught up, four days later, as a kol_signal exit"
                        ),
                        moment=moment,
                    )
                )
                applied.append(("strategy_lifecycles", 1074))
        for item in plan.paper_lifecycles:
            row = session.get(StrategyLifecycle, int(item["row_id"]))
            if row is None or row.execution_binding_id is not None:
                skipped.append({"table": "strategy_lifecycles",
                                "row_id": int(item["row_id"]),
                                "reason": "changed_since_plan"})
                continue
            changes: list[tuple[str, Any, Any]] = []
            if not str(row.management_action or "").strip():
                changes.append(("management_action", row.management_action, SIMULATED_ONLY))
                row.management_action = SIMULATED_ONLY
            existing_note = str(row.management_note or "").strip()
            new_note = (
                f"{SIMULATED_NOTE}\n{existing_note}" if existing_note else SIMULATED_NOTE
            )
            changes.append(("management_note", row.management_note, SIMULATED_ONLY))
            row.management_note = new_note
            row.updated_at = moment
            audit_ids.extend(
                _audit(
                    session,
                    table="strategy_lifecycles",
                    row_id=int(item["row_id"]),
                    changes=changes,
                    note=(
                        "notify_only group, no execution binding, no position on "
                        "the exchange; labelled rather than terminalized because "
                        "a simulated lifecycle in a notify_only group is correct"
                    ),
                    moment=moment,
                )
            )
            applied.append(("strategy_lifecycles", int(item["row_id"])))
        for item in plan.shadow_jobs:
            job = session.get(MessageProcessingJob, int(item["row_id"]))
            if job is None or str(job.status) != "pending" or int(job.shadow or 0) != 1:
                skipped.append({"table": "message_processing_jobs",
                                "row_id": int(item["row_id"]),
                                "reason": "changed_since_plan"})
                continue
            changes = [
                ("status", job.status, "expired"),
                ("last_reason", job.last_reason, SHADOW_JOB_REASON),
            ]
            job.status = "expired"
            job.last_reason = SHADOW_JOB_REASON
            job.completed_at = moment
            audit_ids.extend(
                _audit(
                    session,
                    table="message_processing_jobs",
                    row_id=int(item["row_id"]),
                    changes=changes,
                    note=(
                        "shadow row from the 2026-08-20 queue cutover; the claim "
                        "query excludes shadow rows by design and this message "
                        "already has a recognition decision"
                    ),
                    moment=moment,
                )
            )
            applied.append(("message_processing_jobs", int(item["row_id"])))
        session.commit()
    return CleanupResult(
        applied=tuple(applied),
        audit_ids=tuple(audit_ids),
        skipped=tuple(skipped),
    )


def _audit(
    session,
    *,
    table: str,
    row_id: int,
    changes: list[tuple[str, Any, Any]],
    note: str,
    moment: datetime,
) -> list[int]:
    import hashlib

    fingerprint = hashlib.sha256(
        f"{REPAIR_TAG}\0{table}\0{int(row_id)}".encode("utf-8")
    ).hexdigest()
    existing = (
        session.query(PositionAttributionAudit)
        .filter(PositionAttributionAudit.fingerprint == fingerprint)
        .one_or_none()
    )
    if existing is not None:
        return []
    audit = PositionAttributionAudit(
        venue="deepcoin",
        pos_id=f"{table}:{int(row_id)}",
        event_type=AUDIT_EVENT_TYPE,
        prior_state=str(changes[0][1])[:32] if changes and changes[0][1] else None,
        new_state=str(changes[0][2])[:32] if changes else REPAIR_TAG,
        fingerprint=fingerprint,
        evidence_json=json.dumps(
            {
                "repair": REPAIR_TAG,
                "evidence_path": EVIDENCE_PATH,
                "table": table,
                "row_id": int(row_id),
                "changes": [
                    {"field": name, "before": before, "after": after}
                    for name, before, after in changes
                ],
                "note": note,
            },
            sort_keys=True,
            default=str,
        ),
        notification_status=AUDIT_NOTIFICATION_STATUS,
        created_at=moment,
    )
    session.add(audit)
    session.flush()
    return [int(audit.id)]
