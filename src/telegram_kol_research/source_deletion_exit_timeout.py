"""Unseal lanes held by source-deletion exits that stopped making progress.

``recovery_required`` is not one of the deletion worker's ``_ACTIVE_STATES``,
so the worker never claims such an exit again. The deferral barrier, however,
holds on anything that is not ``succeeded`` -- so the exit keeps sealing its
whole "chat + symbol + side" lane forever. A-3 found five of them (109, 128,
201, 209, 231) holding 28 instructions, the oldest since 2026-08-14; the 陈哥
BTC-long lane had been sealed since 08-29, which is why that group's entries
went silently missing.

The timeout has three possible outcomes and they are not symmetric:

* **Alert always** (but at most once every
  :data:`STUCK_EXIT_CAPTURE_MIN_INTERVAL`, see below). Past
  ``source_deletion_exit_timeout_minutes`` an always-notified
  ``source_deletion_exit_stuck`` incident is filed, whatever else happens.
  That alone is a change: before this, nothing was emitted ever.
* **Unseal on exchange proof.** The lane is released -- exit set to
  ``succeeded`` with reason ``position_gone_confirmed`` -- only when a direct
  exchange read shows the binding's positions *and* its resting orders are all
  gone. If the read is unavailable, incomplete, or shows anything still live,
  the lane stays sealed and the alert is the whole outcome. A sealed lane is a
  visible problem; an unsealed lane over a position that still exists is an
  invisible one.
* **Unseal when we never had anything to cancel** (reason
  ``released_no_exchange_footprint``). The proof above needs a position
  identity, and an exit with no ``execution_binding_id`` has none, so
  ``exit_has_no_known_position`` used to be a permanent verdict: 陈哥's exits
  310 and 311 sealed the BTC-long lane from 2026-09-15 to 09-26, and the seal
  expired eleven messages -- four of them entry strategies. For that shape the
  question "is *this* position gone" is unanswerable and the answerable one is
  "is anything in this lane ours at all": see
  :func:`_no_exchange_footprint_verdict`.

**Capture throttle.** The deletion worker ticks every five seconds
(``source_message_deletion_worker_interval_seconds``), and this pass used to
capture every stuck exit on every tick: exits 310/311 reached a combined
``repeat_count`` of 356933 and roughly 69000 log lines a day, which is how a
real alarm became wallpaper. The same exit is now captured at most once per
:data:`STUCK_EXIT_CAPTURE_MIN_INTERVAL` -- unless its ``state`` or
``last_reason`` changed since the last capture, or the pass released it, in
which case it is captured immediately, because a state change is news and the
throttle must not sit on news. The throttle also gates the lane-footprint read
below, so the new judgement costs one exchange read per interval rather than
one every five seconds. The throttle lives in this process's memory
(:data:`_LAST_STUCK_CAPTURE`) and is deliberately not persisted: after a
restart each stuck exit states its situation once more, which is better than a
restart inheriting somebody else's silence. ``runtime_incidents`` coalescing is
untouched -- that is a global mechanism; this is one caller calling less often.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import or_

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    SourceMessageDeletionExit,
)


logger = logging.getLogger(__name__)

STUCK_STATE = "recovery_required"
POSITION_GONE_REASON = "position_gone_confirmed"
#: Released because nothing in the lane was ever ours -- not because a known
#: position was proven gone. The two paths are deliberately distinguishable in
#: ``last_reason`` afterwards.
NO_EXCHANGE_FOOTPRINT_REASON = "released_no_exchange_footprint"
#: How long the same unchanged stuck exit stays quiet between captures.
STUCK_EXIT_CAPTURE_MIN_INTERVAL = timedelta(minutes=30)

#: ``exit_id -> (state, last_reason, last capture moment)``, process memory
#: only. See the module docstring for why it is not persisted.
_LAST_STUCK_CAPTURE: dict[int, tuple[str, str, datetime]] = {}

_POSITION_ID_KEYS = ("posId", "pos_id", "PositionID", "positionId", "position_id")
_ORDER_ID_KEYS = ("ordId", "orderId", "order_id", "id")
_ORDER_POSITION_ID_KEYS = ("posId", "pos_id", "closePosId", "positionId")
_SYMBOL_KEYS = ("instId", "inst_id", "instrumentId", "instrument_id", "symbol")
_SIDE_KEYS = ("posSide", "pos_side", "positionSide", "side")


def reset_stuck_exit_capture_throttle() -> None:
    """Forget every throttle decision. For tests and for explicit restarts."""

    _LAST_STUCK_CAPTURE.clear()


@dataclass(frozen=True, slots=True)
class SourceDeletionExitTimeoutResult:
    """What one pass did.

    ``alerted`` is every timed-out exit the pass judged -- the scope of the
    sweep, unchanged by the throttle. ``captured`` is the subset that actually
    filed an incident this pass; the rest were judged identically and stayed
    quiet because :data:`STUCK_EXIT_CAPTURE_MIN_INTERVAL` had not elapsed.
    """

    alerted: tuple[int, ...] = ()
    released: tuple[int, ...] = ()
    held: tuple[int, ...] = ()
    captured: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ExchangeAbsenceProof:
    """Whether the exchange proved this exit's position and orders are gone."""

    proven: bool
    reason: str
    pos_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExchangeLaneFootprint:
    """Everything the exchange is carrying in one symbol+side lane.

    ``read`` separates "the snapshot says the lane is empty" from "the snapshot
    could not be read", which :class:`ExchangeAbsenceProof` cannot express: its
    ``proven=False`` means both "still live" and "unknown", and a release must
    never be granted on "unknown".
    """

    read: bool
    reason: str
    pos_ids: tuple[str, ...] = ()
    order_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _LaneIdentity:
    """The lane a deletion exit seals, named the way the barrier names it."""

    chat_id: int
    message_id: int
    symbol: str
    side: str


@dataclass(frozen=True, slots=True)
class _NoFootprintVerdict:
    release: bool
    reason: str


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
    captured: list[int] = []
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
        # The lane-footprint judgement below costs an exchange read for an exit
        # the old code answered from memory alone, so it only runs on a pass
        # that is going to speak anyway -- at most once per throttle interval
        # instead of once every five seconds. A lane that has been sealed for
        # days can wait another half hour; the venue's rate limit cannot.
        speak = _should_capture(candidate=candidate, moment=moment)
        released_now = False
        release_reason = proof.reason
        if proof.proven:
            released_now = _release(
                session_factory,
                exit_id=exit_id,
                released_at=moment,
                reason=POSITION_GONE_REASON,
            )
            if released_now:
                release_reason = POSITION_GONE_REASON
        elif speak and _has_no_execution_credentials(candidate):
            verdict = _no_exchange_footprint_verdict(
                session_factory,
                candidate=candidate,
                exchange_reader=exchange_reader,
            )
            release_reason = verdict.reason
            if verdict.release:
                released_now = _release(
                    session_factory,
                    exit_id=exit_id,
                    released_at=moment,
                    reason=NO_EXCHANGE_FOOTPRINT_REASON,
                )
                if not released_now:
                    release_reason = "release_lost_the_race"
        if released_now:
            released.append(exit_id)
            _resume_behind_exit(session_factory, exit_id=exit_id, now=moment)
        else:
            held.append(exit_id)
        if released_now and not speak:
            # A release is the one outcome the throttle may never swallow: the
            # exit's state changes under it, and "the lane just reopened" is
            # the single most newsworthy thing this pass can say.
            speak = True
        if speak:
            _capture_stuck(
                session_factory,
                capture=capture,
                candidate=candidate,
                timeout_minutes=int(timeout_minutes),
                lane_released=released_now,
                release_reason=release_reason,
                occurred_at=moment,
            )
            captured.append(exit_id)
        alerted.append(exit_id)
    if captured:
        # Throttled with the captures: one line per tick over a stuck exit that
        # has not changed is the same 69000-lines-a-day noise in another file.
        logger.warning(
            "source deletion exits stuck alerted=%s released=%s held=%s captured=%s",
            alerted,
            released,
            held,
            captured,
        )
    return SourceDeletionExitTimeoutResult(
        alerted=tuple(alerted),
        released=tuple(released),
        held=tuple(held),
        captured=tuple(captured),
    )


class ExchangeAbsenceReader:
    """One shared exchange snapshot, read at most once per pass.

    Both exchange lists are read at most once and shared across every exit
    judged in the pass: the snapshot is the same for all of them, and reading
    it per exit would spend the worker's whole read quota on a check that
    usually finds nothing. Callable for the per-exit absence proof; ask
    :meth:`lane_footprint` for what the same snapshot holds in one lane.
    """

    def __init__(
        self,
        *,
        positions_loader: Callable[[], Iterable[Mapping[str, Any]]],
        resting_orders_loader: Callable[[], Iterable[Mapping[str, Any]]],
    ) -> None:
        self._positions_loader = positions_loader
        self._resting_orders_loader = resting_orders_loader
        self._snapshot: dict[str, Any] = {}

    def _load(self) -> dict[str, Any]:
        if "value" not in self._snapshot:
            positions = list(self._positions_loader())
            orders = list(self._resting_orders_loader())
            self._snapshot["value"] = {
                "positions": positions,
                "orders": orders,
                "live_positions": _identity_set(positions, _POSITION_ID_KEYS),
                "live_orders": _identity_set(orders, _ORDER_ID_KEYS),
                "live_order_positions": _identity_set(
                    orders, _ORDER_POSITION_ID_KEYS, allow_missing=True
                ),
            }
        return self._snapshot["value"]

    def __call__(
        self, pos_ids: tuple[str, ...], order_ids: tuple[str, ...]
    ) -> ExchangeAbsenceProof:
        if not pos_ids:
            # No known position identity means there is nothing to prove gone.
            # Refusing here is what keeps the release from being a guess -- the
            # lane-footprint judgement is the separate answer for that shape.
            return ExchangeAbsenceProof(False, "exit_has_no_known_position")
        try:
            snapshot = self._load()
        except ValueError as exc:
            return ExchangeAbsenceProof(False, f"snapshot_unreadable:{exc}"[:96])
        remaining_positions = set(pos_ids) & snapshot["live_positions"]
        remaining_orders = (set(order_ids) & snapshot["live_orders"]) | (
            set(pos_ids) & snapshot["live_order_positions"]
        )
        if remaining_positions:
            return ExchangeAbsenceProof(False, "position_still_open")
        if remaining_orders:
            return ExchangeAbsenceProof(False, "orders_still_resting")
        return ExchangeAbsenceProof(True, POSITION_GONE_REASON, tuple(sorted(pos_ids)))

    def lane_footprint(self, *, symbol: str, side: str) -> ExchangeLaneFootprint:
        """Every live position and resting order that could belong to a lane.

        Fail-closed on every unknown: a row whose instrument or side cannot be
        read counts as being *in* the lane, so it has to be attributed to
        somebody else before anything is released.
        """

        symbol = str(symbol or "").strip().upper()
        side = str(side or "").strip().lower()
        if not symbol or not side:
            return ExchangeLaneFootprint(False, "lane_identity_missing")
        try:
            snapshot = self._load()
        except ValueError as exc:
            return ExchangeLaneFootprint(False, f"snapshot_unreadable:{exc}"[:96])
        pos_ids: set[str] = set()
        order_ids: set[str] = set()
        for row in snapshot["positions"]:
            if not _position_is_live(row):
                continue
            if not _row_in_lane(row, symbol=symbol, side=side):
                continue
            identity = _row_identity(row, _POSITION_ID_KEYS)
            if not identity:
                return ExchangeLaneFootprint(False, "snapshot_unreadable:position")
            pos_ids.add(identity)
        for row in snapshot["orders"]:
            if not _row_in_lane(row, symbol=symbol, side=side):
                continue
            identity = _row_identity(row, _ORDER_ID_KEYS)
            if not identity:
                return ExchangeLaneFootprint(False, "snapshot_unreadable:order")
            order_ids.add(identity)
        return ExchangeLaneFootprint(
            True,
            "lane_snapshot_read",
            tuple(sorted(pos_ids)),
            tuple(sorted(order_ids)),
        )


def build_exchange_absence_reader(
    *,
    positions_loader: Callable[[], Iterable[Mapping[str, Any]]],
    resting_orders_loader: Callable[[], Iterable[Mapping[str, Any]]],
) -> ExchangeAbsenceReader:
    """A reader that refuses to prove absence from an unreadable snapshot."""

    return ExchangeAbsenceReader(
        positions_loader=positions_loader,
        resting_orders_loader=resting_orders_loader,
    )


def _has_no_execution_credentials(candidate: Mapping[str, Any]) -> bool:
    """True when we never wrote anything to the exchange for this message."""

    return (
        candidate["execution_binding_id"] is None
        and not candidate["pos_ids"]
        and not candidate["order_ids"]
    )


def _no_exchange_footprint_verdict(
    session_factory,
    *,
    candidate: Mapping[str, Any],
    exchange_reader: Any,
) -> _NoFootprintVerdict:
    """Decide whether a credential-less exit may stop sealing its lane.

    All three conditions must hold, and every unknown is a refusal:

    1. the exit has no ``execution_binding_id`` and no known position or order
       id -- we never placed anything at the venue for this message;
    2. this pass read the exchange successfully;
    3. every live position and every resting order in the exit's lane
       (chat + symbol + side) already belongs to a *different* execution
       binding.

    Condition 3 is the safety boundary, and it is why this is not "no pos_id,
    so let it go". When 陈哥's exits were released by hand on 2026-09-26 the
    account did hold a BTC long -- it belonged to 米娅's binding 383. Had that
    position been unclaimed, it could have been the orphan this very exit was
    supposed to close, and keeping the lane sealed would have been right.
    """

    if not _has_no_execution_credentials(candidate):
        return _NoFootprintVerdict(False, "exit_has_execution_credentials")
    lane_footprint = getattr(exchange_reader, "lane_footprint", None)
    if lane_footprint is None:
        return _NoFootprintVerdict(False, "lane_footprint_reader_unavailable")
    lane = _lane_identity(session_factory, exit_id=int(candidate["id"]))
    if lane is None:
        return _NoFootprintVerdict(False, "lane_identity_unknown")
    try:
        footprint = lane_footprint(symbol=lane.symbol, side=lane.side)
    except Exception:
        logger.warning(
            "source deletion exit lane read failed exit_id=%s",
            int(candidate["id"]),
            exc_info=True,
        )
        return _NoFootprintVerdict(False, "lane_read_failed")
    if not footprint.read:
        return _NoFootprintVerdict(False, footprint.reason)
    unattributed = _unattributed_lane_rows(
        session_factory,
        pos_ids=footprint.pos_ids,
        order_ids=footprint.order_ids,
        lane=lane,
    )
    if unattributed:
        logger.warning(
            "source deletion exit lane still carries unattributed rows "
            "exit_id=%s symbol=%s side=%s rows=%s",
            int(candidate["id"]),
            lane.symbol,
            lane.side,
            unattributed,
        )
        return _NoFootprintVerdict(False, "lane_footprint_unattributed")
    return _NoFootprintVerdict(True, NO_EXCHANGE_FOOTPRINT_REASON)


def _lane_identity(session_factory, *, exit_id: int) -> _LaneIdentity | None:
    """Name the sealed lane exactly as ``source_execution_barrier`` does.

    The symbol and side come from the deleted message's latest candidate that
    has both, through the same helper the barrier's resume path uses -- there
    must not be a second way of deciding what lane an exit seals.
    """

    from telegram_kol_research.deferred_instruction_recovery import (
        _latest_candidate_symbol_side,
    )

    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, int(exit_id))
        if deletion_exit is None or deletion_exit.raw_message_id is None:
            # An exit with no raw message is not in the barrier's join at all,
            # so it seals nothing and there is no lane to reopen.
            return None
        raw_message = session.get(RawMessage, int(deletion_exit.raw_message_id))
        if raw_message is None:
            return None
        pair = _latest_candidate_symbol_side(
            session, raw_message_id=int(raw_message.id)
        )
        if pair is None:
            return None
        return _LaneIdentity(
            chat_id=int(raw_message.chat_id),
            message_id=int(raw_message.message_id),
            symbol=pair[0],
            side=pair[1],
        )


def _unattributed_lane_rows(
    session_factory,
    *,
    pos_ids: tuple[str, ...],
    order_ids: tuple[str, ...],
    lane: _LaneIdentity,
) -> tuple[str, ...]:
    """Exchange rows in this lane that no *other* binding accounts for.

    Attribution is by exact ``ExecutionOrderLeg`` id match, which is indexed.
    A leg holding a comma-joined id therefore does not match, and the row it
    owns is reported as unattributed -- the direction that keeps the lane
    sealed, which is the safe one.
    """

    if not pos_ids and not order_ids:
        return ()
    with session_factory() as session:
        rows = (
            session.query(
                ExecutionOrderLeg.pos_id,
                ExecutionOrderLeg.order_id,
                ExecutionBinding.chat_id,
                ExecutionBinding.message_id,
            )
            .join(
                ExecutionBinding,
                ExecutionBinding.id == ExecutionOrderLeg.execution_binding_id,
            )
            .filter(
                or_(
                    ExecutionOrderLeg.pos_id.in_(list(pos_ids)),
                    ExecutionOrderLeg.order_id.in_(list(order_ids)),
                )
            )
            .all()
        )
    owners: dict[str, set[tuple[int, int]]] = {}
    for leg_pos_id, leg_order_id, chat_id, message_id in rows:
        owner = (int(chat_id), int(message_id))
        for value in _split_ids(leg_pos_id) + _split_ids(leg_order_id):
            owners.setdefault(value, set()).add(owner)
    unattributed: list[str] = []
    for value in sorted(set(pos_ids) | set(order_ids)):
        claimants = owners.get(value, set())
        others = {owner for owner in claimants if owner != (lane.chat_id, lane.message_id)}
        if not others:
            # Either nobody claims it -- it could be the orphan this exit was
            # meant to close -- or the only claimant is this exit's own
            # message, which is not somebody else either.
            unattributed.append(value)
    return tuple(unattributed)


def _should_capture(*, candidate: Mapping[str, Any], moment: datetime) -> bool:
    """Throttle repeats, never throttle a change. See the module docstring."""

    exit_id = int(candidate["id"])
    state = str(candidate["state"])
    last_reason = str(candidate["last_reason"] or "")
    seen = _LAST_STUCK_CAPTURE.get(exit_id)
    now = _naive_utc(moment)
    if seen is not None:
        seen_state, seen_reason, seen_at = seen
        if (seen_state, seen_reason) == (state, last_reason) and (
            now - seen_at
        ) < STUCK_EXIT_CAPTURE_MIN_INTERVAL:
            return False
    _LAST_STUCK_CAPTURE[exit_id] = (state, last_reason, now)
    return True


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


def _release(
    session_factory,
    *,
    exit_id: int,
    released_at: datetime,
    reason: str = POSITION_GONE_REASON,
) -> bool:
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
                    SourceMessageDeletionExit.last_reason: reason,
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
        value = _row_identity(row, keys)
        if not value:
            if allow_missing:
                continue
            raise ValueError("row_without_identity")
        found.add(value)
    return found


def _row_identity(row: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        candidate = row.get(key)
        if candidate not in (None, ""):
            return str(candidate).strip()
    return ""


def _row_in_lane(row: Mapping[str, Any], *, symbol: str, side: str) -> bool:
    """Whether an exchange row could belong to this symbol+side lane.

    Unknown means yes: an instrument or side we cannot read must not be
    dismissed as somebody else's problem.
    """

    row_symbol = _row_symbol(row)
    if row_symbol is not None and row_symbol != symbol:
        return False
    row_side = _row_side(row)
    if row_side is not None and row_side != side:
        return False
    return True


def _row_symbol(row: Mapping[str, Any]) -> str | None:
    for key in _SYMBOL_KEYS:
        value = row.get(key)
        if value in (None, ""):
            continue
        # ``BTC-USDT-SWAP`` is the venue's instrument id; the lane speaks in
        # the candidate's bare ``BTC``.
        return str(value).strip().upper().split("-")[0] or None
    return None


def _row_side(row: Mapping[str, Any]) -> str | None:
    for key in _SIDE_KEYS:
        value = row.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip().lower()
        # ``buy``/``sell`` does not name a lane: a sell is either opening a
        # short or closing a long, so it stays unknown on purpose.
        return text if text in ("long", "short") else None
    return None


def _position_is_live(row: Mapping[str, Any]) -> bool:
    value = row.get("pos")
    if value in (None, ""):
        value = row.get("size")
    if value in (None, ""):
        return True
    try:
        return Decimal(str(value)) != 0
    except (InvalidOperation, ValueError):
        return True


def _split_ids(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
