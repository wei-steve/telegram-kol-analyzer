"""Unseal lanes held by source-deletion exits that stopped making progress.

``recovery_required`` is not one of the deletion worker's ``_ACTIVE_STATES``,
so the worker never claims such an exit again. The deferral barrier, however,
holds on anything that is not ``succeeded`` -- so the exit keeps sealing its
whole "chat + symbol + side" lane forever. A-3 found five of them (109, 128,
201, 209, 231) holding 28 instructions, the oldest since 2026-08-14; the 陈哥
BTC-long lane had been sealed since 08-29, which is why that group's entries
went silently missing.

The timeout has two possible outcomes and they are not symmetric:

* **Alert always.** Past ``source_deletion_exit_timeout_minutes`` an
  always-notified ``source_deletion_exit_stuck`` incident is filed, whatever
  else happens. That alone is a change: before this, nothing was emitted ever.
* **Unseal only on exchange proof.** The lane is released -- exit set to
  ``succeeded`` with reason ``position_gone_confirmed`` -- only when a direct
  exchange read shows the binding's positions *and* its resting orders are all
  gone. If the read is unavailable, incomplete, or shows anything still live,
  the lane stays sealed and the alert is the whole outcome. A sealed lane is a
  visible problem; an unsealed lane over a position that still exists is an
  invisible one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from telegram_kol_research.models import (
    ExecutionOrderLeg,
    SourceMessageDeletionExit,
)


logger = logging.getLogger(__name__)

STUCK_STATE = "recovery_required"
POSITION_GONE_REASON = "position_gone_confirmed"


@dataclass(frozen=True, slots=True)
class SourceDeletionExitTimeoutResult:
    alerted: tuple[int, ...] = ()
    released: tuple[int, ...] = ()
    held: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ExchangeAbsenceProof:
    """Whether the exchange proved this exit's position and orders are gone."""

    proven: bool
    reason: str
    pos_ids: tuple[str, ...] = ()


def expire_stuck_source_deletion_exits(
    session_factory,
    *,
    now: datetime | None = None,
    timeout_minutes: float | None = None,
    exchange_reader: Callable[[tuple[str, ...], tuple[str, ...]], ExchangeAbsenceProof]
    | None = None,
    capture: Callable[..., Any] | None = None,
) -> SourceDeletionExitTimeoutResult:
    """Alert on every timed-out exit; release only the provably empty ones."""

    moment = now or datetime.now(UTC)
    if timeout_minutes is None:
        from telegram_kol_research.trading_settings import load_trading_settings

        timeout_minutes = float(
            load_trading_settings(
                session_factory
            ).source_deletion_exit_timeout_minutes
        )
    cutoff = _naive_utc(moment) - timedelta(minutes=float(timeout_minutes))
    candidates = _candidates(session_factory, cutoff=cutoff)
    if not candidates:
        return SourceDeletionExitTimeoutResult()

    alerted: list[int] = []
    released: list[int] = []
    held: list[int] = []
    for candidate in candidates:
        exit_id = int(candidate["id"])
        proof = ExchangeAbsenceProof(False, "exchange_read_unavailable")
        if exchange_reader is not None:
            try:
                proof = exchange_reader(
                    tuple(candidate["pos_ids"]), tuple(candidate["order_ids"])
                )
            except Exception:
                logger.warning(
                    "source deletion exit exchange read failed exit_id=%s",
                    exit_id,
                    exc_info=True,
                )
                proof = ExchangeAbsenceProof(False, "exchange_read_failed")
        released_now = False
        if proof.proven:
            released_now = _release(
                session_factory, exit_id=exit_id, released_at=moment
            )
        if released_now:
            released.append(exit_id)
            _resume_behind_exit(session_factory, exit_id=exit_id, now=moment)
        else:
            held.append(exit_id)
        _capture_stuck(
            session_factory,
            capture=capture,
            candidate=candidate,
            timeout_minutes=int(timeout_minutes),
            lane_released=released_now,
            release_reason=(POSITION_GONE_REASON if released_now else proof.reason),
            occurred_at=moment,
        )
        alerted.append(exit_id)
    logger.warning(
        "source deletion exits stuck alerted=%s released=%s held=%s",
        alerted,
        released,
        held,
    )
    return SourceDeletionExitTimeoutResult(
        alerted=tuple(alerted),
        released=tuple(released),
        held=tuple(held),
    )


def build_exchange_absence_reader(
    *,
    positions_loader: Callable[[], Iterable[Mapping[str, Any]]],
    resting_orders_loader: Callable[[], Iterable[Mapping[str, Any]]],
) -> Callable[[tuple[str, ...], tuple[str, ...]], ExchangeAbsenceProof]:
    """A reader that refuses to prove absence from an unreadable snapshot.

    Both exchange lists are read at most once per pass and shared across every
    exit judged in it: the snapshot is the same for all of them, and reading it
    per exit would spend the worker's whole read quota on a check that usually
    finds nothing.
    """

    snapshot: dict[str, Any] = {}

    def load() -> tuple[set[str], set[str], set[str]]:
        if "value" not in snapshot:
            positions = list(positions_loader())
            orders = list(resting_orders_loader())
            snapshot["value"] = (
                _identity_set(
                    positions,
                    ("posId", "pos_id", "PositionID", "positionId", "position_id"),
                ),
                _identity_set(orders, ("ordId", "orderId", "order_id", "id")),
                _identity_set(
                    orders,
                    ("posId", "pos_id", "closePosId", "positionId"),
                    allow_missing=True,
                ),
            )
        return snapshot["value"]

    def read(
        pos_ids: tuple[str, ...], order_ids: tuple[str, ...]
    ) -> ExchangeAbsenceProof:
        if not pos_ids:
            # No known position identity means there is nothing to prove gone.
            # Refusing here is what keeps the release from being a guess.
            return ExchangeAbsenceProof(False, "exit_has_no_known_position")
        try:
            live_positions, live_orders, live_order_positions = load()
        except ValueError as exc:
            return ExchangeAbsenceProof(False, f"snapshot_unreadable:{exc}"[:96])
        remaining_positions = set(pos_ids) & live_positions
        remaining_orders = (set(order_ids) & live_orders) | (
            set(pos_ids) & live_order_positions
        )
        if remaining_positions:
            return ExchangeAbsenceProof(False, "position_still_open")
        if remaining_orders:
            return ExchangeAbsenceProof(False, "orders_still_resting")
        return ExchangeAbsenceProof(True, POSITION_GONE_REASON, tuple(sorted(pos_ids)))

    return read


def _candidates(session_factory, *, cutoff: datetime) -> list[dict[str, Any]]:
    with session_factory() as session:
        rows = (
            session.query(SourceMessageDeletionExit)
            .filter(
                SourceMessageDeletionExit.state == STUCK_STATE,
                SourceMessageDeletionExit.updated_at <= cutoff,
            )
            .order_by(SourceMessageDeletionExit.id.asc())
            .all()
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            pos_ids: list[str] = []
            order_ids: list[str] = []
            if row.execution_binding_id is not None:
                legs = (
                    session.query(ExecutionOrderLeg)
                    .filter(
                        ExecutionOrderLeg.execution_binding_id
                        == int(row.execution_binding_id)
                    )
                    .all()
                )
                for leg in legs:
                    pos_ids.extend(_split_ids(leg.pos_id))
                    order_ids.extend(_split_ids(leg.order_id))
            result.append(
                {
                    "id": int(row.id),
                    "state": str(row.state),
                    "last_reason": row.last_reason,
                    "execution_binding_id": row.execution_binding_id,
                    "target_lifecycle_id": row.target_lifecycle_id,
                    "updated_at": row.updated_at,
                    "pos_ids": sorted(set(pos_ids)),
                    "order_ids": sorted(set(order_ids)),
                }
            )
        return result


def _release(session_factory, *, exit_id: int, released_at: datetime) -> bool:
    with session_factory() as session:
        updated = (
            session.query(SourceMessageDeletionExit)
            .filter(
                SourceMessageDeletionExit.id == exit_id,
                SourceMessageDeletionExit.state == STUCK_STATE,
            )
            .update(
                {
                    SourceMessageDeletionExit.state: "succeeded",
                    SourceMessageDeletionExit.last_reason: POSITION_GONE_REASON,
                    SourceMessageDeletionExit.claim_token: None,
                    SourceMessageDeletionExit.completed_at: released_at,
                    SourceMessageDeletionExit.updated_at: released_at,
                },
                synchronize_session=False,
            )
        )
        session.commit()
        return updated == 1


def _resume_behind_exit(session_factory, *, exit_id: int, now: datetime) -> None:
    from telegram_kol_research.deferred_instruction_recovery import (
        resume_instructions_deferred_by_exit,
    )

    try:
        resume_instructions_deferred_by_exit(
            session_factory, deletion_exit_id=exit_id, now=now
        )
    except Exception:
        # The release is already committed and is the durable fact; the
        # deletion worker's own resume pass will pick the messages up.
        logger.exception(
            "deferred instruction resume failed after release exit_id=%s", exit_id
        )


def _capture_stuck(
    session_factory,
    *,
    capture: Callable[..., Any] | None,
    candidate: Mapping[str, Any],
    timeout_minutes: int,
    lane_released: bool,
    release_reason: str,
    occurred_at: datetime,
) -> None:
    if capture is not None:
        capture(
            candidate=candidate,
            lane_released=lane_released,
            release_reason=release_reason,
            occurred_at=occurred_at,
        )
        return
    from telegram_kol_research.runtime_incident_adapters import (
        capture_runtime_incident_best_effort,
        capture_source_deletion_exit_stuck,
    )

    capture_runtime_incident_best_effort(
        capture_source_deletion_exit_stuck,
        session_factory,
        deletion_exit_id=int(candidate["id"]),
        state=str(candidate["state"]),
        reason_code=candidate["last_reason"],
        timeout_minutes=int(timeout_minutes),
        lane_released=bool(lane_released),
        release_reason=release_reason,
        occurred_at=occurred_at,
    )


def _identity_set(
    rows: Iterable[Mapping[str, Any]],
    keys: tuple[str, ...],
    *,
    allow_missing: bool = False,
) -> set[str]:
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("row_not_mapping")
        value = ""
        for key in keys:
            candidate = row.get(key)
            if candidate not in (None, ""):
                value = str(candidate).strip()
                break
        if not value:
            if allow_missing:
                continue
            raise ValueError("row_without_identity")
        found.add(value)
    return found


def _split_ids(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
