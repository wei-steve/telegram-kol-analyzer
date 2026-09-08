"""End management batches that have been sitting in ``recovery_required``.

``recovery_required`` is an *active* batch status: it is not in
``('succeeded','blocked','resolved')``, so the partial unique index that allows
one live batch per strategy keeps counting it, and every later instruction for
that strategy is refused as "an unfinished batch already exists". Nothing in
the system ever re-claims that status on its own. Batch 158 therefore froze one
strategy from 2026-09-04 to 2026-09-07 while emitting no event at all.

Two endings, and only two:

* the position the batch was about is **provably gone** from the exchange, in
  which case the instruction has no subject any more and the batch is
  ``resolved`` (``position_closed_before_management``); or
* the timeout simply expired, in which case the batch is ``blocked``
  (``recovery_timeout``) and an always-notified incident hands it to a person.

**The batch is never re-run.** Blocking it releases the freeze; it does not
retry the instruction. Deciding to re-issue a three-day-old management
instruction is a person's decision, not this loop's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from telegram_kol_research.models import (
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.strategy_management_batches import transition_batch


logger = logging.getLogger(__name__)

RECOVERY_TIMEOUT_REASON = "recovery_timeout"
POSITION_GONE_REASON = "position_closed_before_management"


@dataclass(frozen=True, slots=True)
class ManagementRecoveryTimeoutResult:
    blocked: tuple[int, ...] = ()
    resolved: tuple[int, ...] = ()
    examined: int = 0
    details: tuple[dict[str, Any], ...] = field(default=())


def expire_stuck_management_recoveries(
    session_factory,
    *,
    now: datetime | None = None,
    timeout_minutes: float | None = None,
    positions: Iterable[Mapping[str, Any]] | None = None,
    position_loader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    capture: Callable[..., Any] | None = None,
) -> ManagementRecoveryTimeoutResult:
    """Block or resolve every management batch past its recovery timeout."""

    moment = now or datetime.now(UTC)
    if timeout_minutes is None:
        from telegram_kol_research.trading_settings import load_trading_settings

        timeout_minutes = float(
            load_trading_settings(session_factory).management_recovery_timeout_minutes
        )
    cutoff = _naive_utc(moment) - timedelta(minutes=float(timeout_minutes))
    candidates = _candidates(session_factory, cutoff=cutoff)
    if not candidates:
        return ManagementRecoveryTimeoutResult()

    live_pos_ids: set[str] | None = None
    if positions is not None:
        live_pos_ids = _position_ids(positions)
    elif position_loader is not None:
        try:
            live_pos_ids = _position_ids(position_loader())
        except Exception:
            # An unreadable position list is unknown, never "the position is
            # gone". Without it no batch is resolved; the timeout still runs.
            logger.warning(
                "management recovery timeout could not read positions",
                exc_info=True,
            )
            live_pos_ids = None

    blocked: list[int] = []
    resolved: list[int] = []
    details: list[dict[str, Any]] = []
    for candidate in candidates:
        batch_id = int(candidate["id"])
        leg_pos_ids = tuple(candidate["pos_ids"])
        position_gone = bool(
            live_pos_ids is not None
            and leg_pos_ids
            and not (set(leg_pos_ids) & live_pos_ids)
        )
        detail = {
            "management_batch_id": batch_id,
            "strategy_instance_id": candidate["strategy_instance_id"],
            "target_lifecycle_id": candidate["target_lifecycle_id"],
            "effective_action": candidate["effective_action"],
            "recovery_reason_code": candidate["reason_code"],
            "recovery_since": candidate["updated_at"].isoformat()
            if candidate["updated_at"] is not None
            else None,
            "leg_pos_ids": list(leg_pos_ids),
            "positions_readable": live_pos_ids is not None,
        }
        if position_gone:
            if transition_batch(
                session_factory,
                batch_id,
                expected_statuses=("recovery_required",),
                new_status="resolved",
                transitioned_at=moment,
                reason_code=POSITION_GONE_REASON,
            ):
                resolved.append(batch_id)
                detail["outcome"] = "resolved"
                details.append(detail)
            continue
        if not transition_batch(
            session_factory,
            batch_id,
            expected_statuses=("recovery_required",),
            new_status="blocked",
            transitioned_at=moment,
            reason_code=RECOVERY_TIMEOUT_REASON,
        ):
            continue
        blocked.append(batch_id)
        detail["outcome"] = "blocked"
        details.append(detail)
        _capture_timeout(
            session_factory,
            capture=capture,
            candidate=candidate,
            timeout_minutes=int(timeout_minutes),
            occurred_at=moment,
        )
    if blocked or resolved:
        logger.warning(
            "management recovery timeout blocked=%s resolved=%s",
            blocked,
            resolved,
        )
    return ManagementRecoveryTimeoutResult(
        blocked=tuple(blocked),
        resolved=tuple(resolved),
        examined=len(candidates),
        details=tuple(details),
    )


def _candidates(session_factory, *, cutoff: datetime) -> list[dict[str, Any]]:
    with session_factory() as session:
        batches = (
            session.query(StrategyManagementBatch)
            .filter(
                StrategyManagementBatch.status == "recovery_required",
                StrategyManagementBatch.updated_at <= cutoff,
            )
            .order_by(StrategyManagementBatch.id.asc())
            .all()
        )
        result: list[dict[str, Any]] = []
        for batch in batches:
            pos_ids = [
                str(pos_id).strip()
                for (pos_id,) in session.query(StrategyManagementLeg.pos_id)
                .filter(StrategyManagementLeg.management_batch_id == int(batch.id))
                .all()
                if str(pos_id or "").strip()
            ]
            result.append(
                {
                    "id": int(batch.id),
                    "strategy_instance_id": str(batch.strategy_instance_id or ""),
                    "target_lifecycle_id": int(batch.target_lifecycle_id),
                    "effective_action": str(batch.effective_action or ""),
                    "reason_code": batch.reason_code,
                    "updated_at": batch.updated_at,
                    "pos_ids": pos_ids,
                }
            )
        return result


def _capture_timeout(
    session_factory,
    *,
    capture: Callable[..., Any] | None,
    candidate: Mapping[str, Any],
    timeout_minutes: int,
    occurred_at: datetime,
) -> None:
    if capture is not None:
        capture(candidate=candidate, occurred_at=occurred_at)
        return
    from telegram_kol_research.runtime_incident_adapters import (
        capture_management_recovery_timeout,
        capture_runtime_incident_best_effort,
    )

    capture_runtime_incident_best_effort(
        capture_management_recovery_timeout,
        session_factory,
        management_batch_id=int(candidate["id"]),
        strategy_instance_id=str(candidate["strategy_instance_id"]),
        target_lifecycle_id=int(candidate["target_lifecycle_id"]),
        effective_action=str(candidate["effective_action"]),
        recovery_reason_code=candidate["reason_code"],
        timeout_minutes=int(timeout_minutes),
        occurred_at=occurred_at,
    )


def _position_ids(positions: Iterable[Mapping[str, Any]]) -> set[str]:
    """Live position ids, refusing to answer at all on an unreadable row.

    A row this function cannot identify could be the very position a batch is
    about, so one such row makes the whole snapshot unusable for the
    "position is gone" judgement -- which is why the caller treats ``None``
    and an empty set very differently.
    """

    ids: set[str] = set()
    for row in positions:
        if not isinstance(row, Mapping):
            raise ValueError("position row is not a mapping")
        found = ""
        for key in ("posId", "pos_id", "PositionID", "positionId", "position_id"):
            value = row.get(key)
            if value not in (None, ""):
                found = str(value).strip()
                break
        if not found:
            raise ValueError("position row carries no position id")
        ids.add(found)
    return ids


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
