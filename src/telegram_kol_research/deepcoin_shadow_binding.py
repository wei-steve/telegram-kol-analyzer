"""Build the deterministic entry -> position -> protection chain, in shadow only.

Phase 4 of the REST+WebSocket program. It constructs, from the phase 1-3 event
stream plus targeted REST reads, the chain the handoff document specifies::

    REST main ordId
      -> WS Trade.OS
      -> REST-verified unique split posId
      -> WS TriggerOrder.TU
      -> WS TriggerOrder.OS (the protection order's own ordId)

and records it in ``deepcoin_shadow_bindings``. It writes nothing else. No
existing ledger row is read for a verdict, no ledger row is written, no
decision is driven, and no exchange write of any kind is issued: every REST call
below is one of the existing ``list_*`` GET readers.

**The five criteria are conjunctive.** ``binding_confidence`` is ``exact`` only
when all of :data:`BINDING_CRITERIA` hold simultaneously. Any other outcome is
``unverified`` carrying the specific :data:`REFUSAL_REASONS` entry that says
which one failed, and never a guess. In particular:

* the ordId *allocation pattern* observed in the experiment -- the protection
  order's id being the entry's id minus one, created in the same millisecond --
  is a coincidence of allocation, not a foreign key, and is never used here;
* neither is symbol, direction, size, price, time proximity, clOrdId or tag on
  its own;
* ``Position.PI``, which the exchange pushes but does not document, is recorded
  in ``evidence_json`` as *supporting* evidence -- it can reveal the posId
  before ``TU`` flips away from ``default`` -- and can never satisfy criterion 2
  in place of the REST read, nor stand in for any other criterion.

Contract names are normalised through phase 2's explicit instrument map before
any comparison (the stream says ``ETHUSDT``, REST says ``ETH-USDT-SWAP``). A
name the map does not know fails closed; it is never repaired by string surgery.

An incomplete REST read is unknown, never zero (hard rule 4): it produces
``unverified`` with ``rest_read_incomplete`` and the chain is retried on a later
pass, rather than being concluded against an empty list.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from telegram_kol_research.deepcoin_ws_resync import http_status_from_exception
from telegram_kol_research.models import DeepcoinShadowBinding, DeepcoinWsEvent
from telegram_kol_research.native_tpsl import (
    normalize_native_tpsl,
    protection_order_sides_consistent,
)

logger = logging.getLogger(__name__)

CONFIDENCE_EXACT = "exact"
CONFIDENCE_UNVERIFIED = "unverified"

# The handoff document's stage labels, in the order a chain passes through them.
STAGE_REST_ACCEPTED = "rest_accepted"
STAGE_ORDER_LIVE = "order_live"
STAGE_PARTIALLY_FILLED = "partially_filled"
STAGE_FILLED = "filled"
STAGE_POSITION_BOUND = "position_bound"
STAGE_PROTECTION_BOUND = "protection_bound"
STAGE_ACTIVE = "active"
STAGE_CLOSING = "closing"
STAGE_TERMINAL = "terminal"

SHADOW_STAGES = (
    STAGE_REST_ACCEPTED,
    STAGE_ORDER_LIVE,
    STAGE_PARTIALLY_FILLED,
    STAGE_FILLED,
    STAGE_POSITION_BOUND,
    STAGE_PROTECTION_BOUND,
    STAGE_ACTIVE,
    STAGE_CLOSING,
    STAGE_TERMINAL,
)

# The five conjunctive criteria, verbatim from the handoff document. Names are
# used as evidence keys so the report can show which ones held.
BINDING_CRITERIA = (
    "trade_os_equals_main_ord_id",
    "rest_unique_directional_pos_id",
    "trigger_order_tu_equals_pos_id",
    "trigger_order_os_is_own_ord_id",
    "chain_fields_consistent",
)

REFUSAL_REASONS = frozenset(
    {
        # criterion 1
        "no_trade_frame_for_main_ord_id",
        # criterion 2
        "rest_read_incomplete",
        "no_rest_pos_id_for_main_ord_id",
        "rest_pos_id_not_unique",
        "rest_pos_side_missing",
        "rest_pos_side_mismatch",
        # criterion 3
        "no_trigger_frame_with_tu_equal_pos_id",
        # criterion 4
        "trigger_frame_without_own_ord_id",
        "protection_order_not_observed_in_stream",
        # criterion 5
        "instrument_mismatch",
        "protection_side_mismatch",
        "protection_size_mismatch",
        "protection_tp_sl_mismatch",
        # normalisation, fail-closed
        "instrument_not_in_map",
        "instrument_unknown",
    }
)

# The stream's short keys, exactly as phase 1 decoded them. Long-key spellings
# are still not guessed.
_WS_TRIGGER_TAKE_PROFIT_KEY = "TPT"
_WS_TRIGGER_STOP_LOSS_KEY = "SLT"
_WS_TRIGGER_STATUS_KEY = "TS"
_WS_POSITION_QTY_KEY = "Po"

# Cap on how many observations one timeline keeps. A busy protection order can
# be pushed many times; the report needs the shape of the transition, not every
# repeat, and an unbounded list would grow the evidence column without bound.
_TIMELINE_LIMIT = 20

# Relative tolerance for size and price comparison. Sizes and trigger prices
# cross the boundary as decimal text on one side and JSON floats on the other,
# so an exact string comparison would report a difference that does not exist.
_RELATIVE_TOLERANCE = Decimal("1e-9")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any) -> Decimal | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def _close_enough(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return False
    if left == right:
        return True
    scale = max(abs(left), abs(right), Decimal(1))
    return abs(left - right) <= scale * _RELATIVE_TOLERANCE


def _normalize_position_side(value: Any) -> str | None:
    text = (_text(value) or "").lower()
    if text in {"long", "buy", "1", "开多", "多"}:
        return "long"
    if text in {"short", "sell", "2", "开空", "空"}:
        return "short"
    return None


@dataclass
class ShadowChainInputs:
    """Everything one chain evaluation is allowed to look at.

    Assembled by :func:`collect_chain_inputs` from the inbox and REST; kept as a
    plain value so the whole verdict is reproducible offline from recorded rows.
    """

    main_ord_id: str
    instrument_stream: str | None = None
    instrument_rest: str | None = None
    instrument_resolved: bool = True
    trade_frames: list[dict[str, Any]] = field(default_factory=list)
    order_frames: list[dict[str, Any]] = field(default_factory=list)
    trigger_frames: list[dict[str, Any]] = field(default_factory=list)
    position_frames: list[dict[str, Any]] = field(default_factory=list)
    rest_fills: list[dict[str, Any]] = field(default_factory=list)
    rest_positions: list[dict[str, Any]] = field(default_factory=list)
    rest_trigger_orders: list[dict[str, Any]] = field(default_factory=list)
    rest_complete: bool = True
    rest_read_failures: tuple[str, ...] = ()


@dataclass
class ShadowChainResult:
    """The verdict on one chain. ``exact`` requires all five criteria."""

    main_ord_id: str
    stage: str
    binding_confidence: str
    refusal_reason: str | None
    pos_id: str | None
    protection_ord_ids: tuple[str, ...]
    instrument_rest: str | None
    instrument_stream: str | None
    side: str | None
    trade_os_seen_at: datetime | None
    tu_matched_at: datetime | None
    criteria: dict[str, bool]
    evidence: dict[str, Any]

    @property
    def is_exact(self) -> bool:
        return self.binding_confidence == CONFIDENCE_EXACT


def _frame_payload(frame: dict[str, Any]) -> dict[str, Any]:
    """Return the ``data`` object of one persisted inbox row.

    The inbox stores the frame verbatim, so the short keys phase 1 chose not to
    decode into columns -- ``TPT``, ``SLT`` and the rest -- are still available
    here without a schema change. Anything unparsable yields an empty mapping,
    which fails closed at the comparison rather than raising.
    """

    cached = frame.get("_data")
    if isinstance(cached, dict):
        return cached
    try:
        payload = json.loads(frame.get("raw_payload") or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return {}
    wanted_channel = _text(frame.get("channel"))
    wanted_order = _text(frame.get("order_sys_id"))
    for item in result:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        if wanted_channel is not None and _text(item.get("table")) != wanted_channel:
            continue
        if wanted_order is not None and _text(data.get("OS")) != wanted_order:
            continue
        return data
    return {}


def _earliest_received_at(frames: Iterable[dict[str, Any]]) -> datetime | None:
    stamps = [
        frame.get("received_at")
        for frame in frames
        if isinstance(frame.get("received_at"), datetime)
    ]
    return min(stamps) if stamps else None


def evaluate_shadow_chain(inputs: ShadowChainInputs) -> ShadowChainResult:
    """Apply the five criteria to one candidate chain and return the verdict.

    Pure: it performs no I/O and consults nothing but ``inputs``. The criteria
    are evaluated in chain order and the first failure names the refusal, which
    is what makes each of the five independently observable in a test.
    """

    criteria = {name: False for name in BINDING_CRITERIA}
    evidence: dict[str, Any] = {
        "main_ord_id": inputs.main_ord_id,
        "instrument_stream": inputs.instrument_stream,
        "instrument_rest": inputs.instrument_rest,
        "rest_complete": inputs.rest_complete,
    }
    if inputs.rest_read_failures:
        evidence["rest_read_failures"] = list(inputs.rest_read_failures)

    # Supporting evidence only. ``PI`` can name the posId before ``TU`` stops
    # saying ``default``, which is useful to record and never sufficient to
    # claim anything: criterion 2 still has to come from REST.
    position_pi_values = sorted(
        {
            value
            for frame in inputs.position_frames
            if (value := _text(frame.get("position_id")))
        }
    )
    if position_pi_values:
        # Every posId the stream mentioned in the window, not this chain's --
        # before criterion 2 runs there is nothing to filter by. That is the
        # whole value of ``PI``: it can name a posId before ``TU`` stops saying
        # ``default``. It stays a hint and is never narrowed into a claim.
        evidence["position_pi_seen_in_window"] = position_pi_values

    trade_frames = [
        frame
        for frame in inputs.trade_frames
        if _text(frame.get("order_sys_id")) == inputs.main_ord_id
    ]
    trade_os_seen_at = _earliest_received_at(trade_frames)
    order_frames = [
        frame
        for frame in inputs.order_frames
        if _text(frame.get("order_sys_id")) == inputs.main_ord_id
    ]

    def refuse(reason: str, stage: str) -> ShadowChainResult:
        assert reason in REFUSAL_REASONS, reason
        return ShadowChainResult(
            main_ord_id=inputs.main_ord_id,
            stage=stage,
            binding_confidence=CONFIDENCE_UNVERIFIED,
            refusal_reason=reason,
            pos_id=evidence.get("pos_id"),
            protection_ord_ids=tuple(evidence.get("protection_ord_ids", ())),
            instrument_rest=inputs.instrument_rest,
            instrument_stream=inputs.instrument_stream,
            side=evidence.get("side"),
            trade_os_seen_at=trade_os_seen_at,
            tu_matched_at=evidence.get("tu_matched_at"),
            criteria=criteria,
            evidence=evidence,
        )

    # Normalisation, before any comparison. Fail closed on an unknown contract.
    if inputs.instrument_stream is not None and not inputs.instrument_resolved:
        return refuse("instrument_not_in_map", STAGE_REST_ACCEPTED)
    if inputs.instrument_rest is None:
        return refuse("instrument_unknown", STAGE_REST_ACCEPTED)

    # ---- criterion 1: Trade.OS == REST main ordId -------------------------
    if not trade_frames:
        stage = STAGE_ORDER_LIVE if order_frames else STAGE_REST_ACCEPTED
        return refuse("no_trade_frame_for_main_ord_id", stage)
    criteria["trade_os_equals_main_ord_id"] = True
    evidence["trade_frame_count"] = len(trade_frames)

    # ---- criterion 2: REST gives a unique, correctly directed split posId --
    if not inputs.rest_complete:
        return refuse("rest_read_incomplete", STAGE_FILLED)
    fill_rows = [
        row
        for row in inputs.rest_fills
        if _text(row.get("ordId")) == inputs.main_ord_id
        or _text(row.get("orderId")) == inputs.main_ord_id
    ]
    pos_ids = sorted(
        {
            value
            for row in fill_rows
            if (value := _text(row.get("posId") or row.get("positionId")))
        }
    )
    evidence["rest_fill_count"] = len(fill_rows)
    evidence["rest_pos_id_candidates"] = pos_ids
    filled_size = sum(
        (_decimal(row.get("fillSz") or row.get("sz")) or Decimal(0))
        for row in fill_rows
    )
    evidence["rest_filled_size"] = str(filled_size)
    if not pos_ids:
        return refuse("no_rest_pos_id_for_main_ord_id", STAGE_FILLED)
    if len(pos_ids) > 1:
        return refuse("rest_pos_id_not_unique", STAGE_FILLED)
    pos_id = pos_ids[0]
    evidence["pos_id"] = pos_id

    fill_sides = {
        side
        for row in fill_rows
        if (side := _normalize_position_side(row.get("posSide") or row.get("side")))
    }
    position_rows = [
        row
        for row in inputs.rest_positions
        if _text(row.get("posId") or row.get("positionId")) == pos_id
    ]
    position_sides = {
        side
        for row in position_rows
        if (side := _normalize_position_side(row.get("posSide")))
    }
    evidence["rest_position_row_count"] = len(position_rows)
    observed_sides = fill_sides | position_sides
    if not observed_sides:
        return refuse("rest_pos_side_missing", STAGE_POSITION_BOUND)
    if len(observed_sides) > 1:
        return refuse("rest_pos_side_mismatch", STAGE_POSITION_BOUND)
    side = next(iter(observed_sides))
    evidence["side"] = side
    criteria["rest_unique_directional_pos_id"] = True

    position_size = None
    for row in position_rows:
        position_size = _decimal(
            row.get("pos") or row.get("sz") or row.get("availPos") or row.get("size")
        )
        if position_size is not None:
            break
    if position_size is not None:
        evidence["rest_position_size"] = str(position_size)

    # ---- criterion 3: TriggerOrder.TU == the REST posId --------------------
    tu_frames = [
        frame
        for frame in inputs.trigger_frames
        if _text(frame.get("trade_unit_id")) == pos_id
    ]
    if not tu_frames:
        stage = STAGE_POSITION_BOUND if position_rows else STAGE_CLOSING
        return refuse("no_trigger_frame_with_tu_equal_pos_id", stage)
    tu_matched_at = _earliest_received_at(tu_frames)
    evidence["tu_matched_at"] = tu_matched_at
    evidence["tu_frame_count"] = len(tu_frames)
    criteria["trigger_order_tu_equals_pos_id"] = True

    # ---- criterion 4: TriggerOrder.OS is the protection order's own ordId --
    stream_protection: dict[str, dict[str, Any]] = {}
    for frame in tu_frames:
        own_ord_id = _text(frame.get("order_sys_id"))
        if own_ord_id is None:
            return refuse("trigger_frame_without_own_ord_id", STAGE_PROTECTION_BOUND)
        if own_ord_id == inputs.main_ord_id:
            # The protection order is a distinct exchange object; a frame whose
            # own id is the entry's id is not one, whatever else it says.
            return refuse("trigger_frame_without_own_ord_id", STAGE_PROTECTION_BOUND)
        payload = _frame_payload(frame)
        previous = stream_protection.get(own_ord_id)
        if previous is None or _received_ms(frame) >= _received_ms(previous.get("_frame", {})):
            stream_protection[own_ord_id] = {**payload, "_frame": frame}
    protection_ord_ids = tuple(sorted(stream_protection))
    evidence["protection_ord_ids"] = list(protection_ord_ids)
    criteria["trigger_order_os_is_own_ord_id"] = True

    # ---- criterion 5: contract, direction, size, TP and SL all agree ------
    rest_protection: dict[str, Any] = {}
    for row in inputs.rest_trigger_orders:
        normalized = normalize_native_tpsl(row)
        if normalized is None or normalized.ord_id is None:
            continue
        if normalized.ord_id in stream_protection:
            rest_protection[normalized.ord_id] = (normalized, row)
    evidence["rest_protection_ord_ids"] = sorted(rest_protection)
    missing_in_rest = [
        ord_id for ord_id in protection_ord_ids if ord_id not in rest_protection
    ]
    if missing_in_rest:
        # The stream described a protection order REST does not currently list.
        # That is unknown coverage, not a contradiction -- it is also exactly
        # what a filled or cancelled protection order looks like -- so the
        # chain stays unverified and says which ids were not corroborated.
        evidence["protection_ord_ids_absent_from_rest"] = missing_in_rest
        return refuse(
            "protection_order_not_observed_in_stream", STAGE_PROTECTION_BOUND
        )

    role_sizes: dict[str, Decimal] = {}
    # Per-order trigger prices, for the diff pass to compare against the
    # ledger's own ``trigger_price``. A row carrying both a TP and an SL has no
    # single "the trigger price", so it contributes only to the detail map and
    # is never compared as one value against a ledger column that holds one.
    protection_prices: dict[str, str] = {}
    protection_prices_detail: dict[str, dict[str, str]] = {}
    for ord_id, (normalized, raw_row) in sorted(rest_protection.items()):
        if normalized.inst_id.upper() != str(inputs.instrument_rest).upper():
            evidence["instrument_mismatch_ord_id"] = ord_id
            return refuse("instrument_mismatch", STAGE_PROTECTION_BOUND)
        if normalized.pos_side != side:
            evidence["protection_side_mismatch_ord_id"] = ord_id
            return refuse("protection_side_mismatch", STAGE_PROTECTION_BOUND)
        if not protection_order_sides_consistent(raw_row):
            evidence["protection_side_mismatch_ord_id"] = ord_id
            return refuse("protection_side_mismatch", STAGE_PROTECTION_BOUND)
        stream_row = stream_protection[ord_id]
        stream_tp = _decimal(stream_row.get(_WS_TRIGGER_TAKE_PROFIT_KEY))
        stream_sl = _decimal(stream_row.get(_WS_TRIGGER_STOP_LOSS_KEY))
        for stream_value, rest_value, label in (
            (stream_tp, normalized.take_profit_trigger_price, "take_profit"),
            (stream_sl, normalized.stop_loss_trigger_price, "stop_loss"),
        ):
            if stream_value is None and rest_value is None:
                continue
            if not _close_enough(stream_value, rest_value):
                evidence["protection_tp_sl_mismatch"] = {
                    "ord_id": ord_id,
                    "field": label,
                    "stream": None if stream_value is None else str(stream_value),
                    "rest": None if rest_value is None else str(rest_value),
                }
                return refuse("protection_tp_sl_mismatch", STAGE_PROTECTION_BOUND)
            if rest_value is not None:
                role_sizes[label] = role_sizes.get(label, Decimal(0)) + (
                    normalized.size or Decimal(0)
                )
                protection_prices_detail.setdefault(ord_id, {})[label] = str(rest_value)
        detail = protection_prices_detail.get(ord_id, {})
        if len(detail) == 1:
            protection_prices[ord_id] = next(iter(detail.values()))
    evidence["protection_role_sizes"] = {
        role: str(value) for role, value in sorted(role_sizes.items())
    }
    evidence["protection_prices"] = dict(sorted(protection_prices.items()))
    evidence["protection_prices_detail"] = {
        ord_id: dict(sorted(values.items()))
        for ord_id, values in sorted(protection_prices_detail.items())
    }
    evidence["protection_sizes"] = {
        ord_id: str(normalized.size)
        for ord_id, (normalized, _raw) in sorted(rest_protection.items())
        if normalized.size is not None
    }
    if position_size is not None and position_size > 0:
        for role, covered in sorted(role_sizes.items()):
            if covered <= 0:
                continue
            if covered > position_size and not _close_enough(covered, position_size):
                evidence["protection_size_mismatch"] = {
                    "role": role,
                    "covered": str(covered),
                    "position": str(position_size),
                }
                return refuse("protection_size_mismatch", STAGE_ACTIVE)
    criteria["chain_fields_consistent"] = True

    # Supplementary check 9, pure observation: what each side of the protection
    # does over time, and what the position does after it. Recorded rather than
    # judged -- "TP fired, what happened to the SL" is a question about the
    # exchange's behaviour, and answering it needs the sequence, not a verdict.
    evidence["protection_status_timeline"] = _status_timeline(
        inputs.trigger_frames,
        identity_key="order_sys_id",
        identities=set(protection_ord_ids),
        value_key=_WS_TRIGGER_STATUS_KEY,
    )
    evidence["position_qty_timeline"] = _status_timeline(
        inputs.position_frames,
        identity_key="position_id",
        identities={pos_id},
        value_key=_WS_POSITION_QTY_KEY,
    )
    evidence["rest_protection_status"] = {
        ord_id: {
            "state": _text(raw_row.get("state") or raw_row.get("status")),
            "trigger_order_type": _text(raw_row.get("triggerOrderType")),
        }
        for ord_id, (_normalized, raw_row) in sorted(rest_protection.items())
    }
    evidence["rest_position_present"] = bool(position_rows)

    stage = STAGE_ACTIVE if position_rows else STAGE_CLOSING
    return ShadowChainResult(
        main_ord_id=inputs.main_ord_id,
        stage=stage,
        binding_confidence=CONFIDENCE_EXACT,
        refusal_reason=None,
        pos_id=pos_id,
        protection_ord_ids=protection_ord_ids,
        instrument_rest=inputs.instrument_rest,
        instrument_stream=inputs.instrument_stream,
        side=side,
        trade_os_seen_at=trade_os_seen_at,
        tu_matched_at=tu_matched_at,
        criteria=criteria,
        evidence=evidence,
    )


def _status_timeline(
    frames: Iterable[dict[str, Any]],
    *,
    identity_key: str,
    identities: set[str],
    value_key: str,
) -> dict[str, list[list[str]]]:
    """Return ``identity -> [[received_at, value], ...]``, oldest first.

    Consecutive repeats of the same value are collapsed: the question this
    answers is "what did it become, and when", and a re-push of an unchanged
    value is not an answer to it.
    """

    timelines: dict[str, list[list[str]]] = {}
    for frame in sorted(frames, key=_received_ms):
        identity = _text(frame.get(identity_key))
        if identity is None or identity not in identities:
            continue
        value = _text(_frame_payload(frame).get(value_key))
        if value is None:
            continue
        entries = timelines.setdefault(identity, [])
        if entries and entries[-1][1] == value:
            continue
        received_at = frame.get("received_at")
        entries.append(
            [
                received_at.isoformat() if isinstance(received_at, datetime) else "",
                value,
            ]
        )
        if len(entries) > _TIMELINE_LIMIT:
            del entries[:-_TIMELINE_LIMIT]
    return timelines


def _received_ms(frame: dict[str, Any]) -> int:
    try:
        return int(frame.get("received_ms") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- collection

# How far back the shadow builder looks for candidate entries. Chains older than
# this are left alone: their verdict is already recorded and re-reading REST for
# them would add load without adding information.
SHADOW_LOOKBACK_HOURS = 48

# Upper bound on chains evaluated in one pass. A pass runs inside the reconcile
# loop, so it must stay small and bounded even if the inbox suddenly holds a
# large backlog.
SHADOW_MAX_CANDIDATES_PER_PASS = 25

# How long an ``unverified`` chain rests before it is retried with no new frame
# to justify the retry. A chain that cannot be verified today -- a protection
# order the exchange has since filled, say -- would otherwise re-issue the same
# REST reads every thirty seconds for two days and add nothing.
SHADOW_UNVERIFIED_RETRY_SECONDS = 300.0


@dataclass
class ShadowPassResult:
    """What one shadow-building pass did. Counts and ids only."""

    evaluated: int = 0
    candidates_due: int = 0
    exact: int = 0
    unverified: int = 0
    written: int = 0
    candidates_seen: int = 0
    rest_read_failures: tuple[str, ...] = ()
    shadow_binding_ids: tuple[int, ...] = ()


def _ws_frames_since(
    session_factory: Callable[[], Any],
    *,
    channels: Sequence[str],
    since_ms: int,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Read decoded inbox rows, newest window first. ``duplicate`` rows included.

    A duplicate row is a genuine re-delivery of a fact, so it is perfectly good
    evidence that the fact was pushed; the de-duplication marking exists to stop
    it being counted twice, not to hide it.
    """

    from sqlalchemy import select

    with session_factory() as session:
        rows = session.execute(
            select(
                DeepcoinWsEvent.id,
                DeepcoinWsEvent.channel,
                DeepcoinWsEvent.order_sys_id,
                DeepcoinWsEvent.trade_unit_id,
                DeepcoinWsEvent.position_id,
                DeepcoinWsEvent.instrument_raw,
                DeepcoinWsEvent.exchange_time_ms,
                DeepcoinWsEvent.received_at,
                DeepcoinWsEvent.received_ms,
                DeepcoinWsEvent.raw_payload,
            )
            .where(
                DeepcoinWsEvent.channel.in_(list(channels)),
                DeepcoinWsEvent.received_ms >= since_ms,
            )
            # Newest first with the cap applied, then reversed below: a window
            # holding more rows than the cap must yield the *recent* ones, not
            # the oldest ones the chain has already accounted for.
            .order_by(DeepcoinWsEvent.id.desc())
            .limit(limit)
        ).all()
    rows = list(reversed(rows))
    return [
        {
            "event_id": int(row[0]),
            "channel": row[1],
            "order_sys_id": row[2],
            "trade_unit_id": row[3],
            "position_id": row[4],
            "instrument_raw": row[5],
            "exchange_time_ms": row[6],
            "received_at": row[7],
            "received_ms": row[8],
            "raw_payload": row[9],
        }
        for row in rows
    ]


def _existing_shadow_rows(
    session_factory: Callable[[], Any], main_ord_ids: list[str]
) -> dict[str, tuple[str, datetime]]:
    """Return ``main_ord_id -> (confidence, last_seen_at)`` for known chains."""

    from sqlalchemy import select

    if not main_ord_ids:
        return {}
    with session_factory() as session:
        rows = session.execute(
            select(
                DeepcoinShadowBinding.main_ord_id,
                DeepcoinShadowBinding.binding_confidence,
                DeepcoinShadowBinding.last_seen_at,
            ).where(DeepcoinShadowBinding.main_ord_id.in_(main_ord_ids))
        ).all()
    return {
        str(main_ord_id): (str(confidence), last_seen_at)
        for main_ord_id, confidence, last_seen_at in rows
    }


def _latest_frame_ms(frames_by_channel: dict[str, list[dict[str, Any]]]) -> int:
    latest = 0
    for frames in frames_by_channel.values():
        for frame in frames:
            latest = max(latest, _received_ms(frame))
    return latest


def _needs_reevaluation(
    main_ord_id: str,
    *,
    known: dict[str, tuple[str, datetime]],
    latest_frame_ms: int,
    now: datetime,
) -> bool:
    """Should this candidate cost REST reads on this pass?

    A chain already proved ``exact`` is re-read only when a frame has landed
    since it was last seen; nothing else can change its verdict, and re-reading
    it every thirty seconds would add exchange load for no information. An
    ``unverified`` chain is retried on any new frame and otherwise on a slow
    timer, because its refusal really can resolve later.
    """

    entry = known.get(main_ord_id)
    if entry is None:
        return True
    confidence, last_seen_at = entry
    if last_seen_at is None:
        return True
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=UTC)
    last_seen_ms = int(last_seen_at.timestamp() * 1000)
    if latest_frame_ms > last_seen_ms:
        return True
    if confidence == CONFIDENCE_EXACT:
        return False
    return (now - last_seen_at).total_seconds() >= SHADOW_UNVERIFIED_RETRY_SECONDS


def _ledger_entry_order_ids(
    session_factory: Callable[[], Any], *, since: datetime
) -> dict[str, int]:
    """Entry order ids production recorded, mapped to their binding id.

    This is a *candidate source*, never a verdict: the shadow chain still has to
    prove every one of the five criteria from the stream and REST. Including it
    is what makes ``ledger_only`` observable at all -- an entry the ledger holds
    and the deterministic chain could not confirm would otherwise never be
    looked at.
    """

    from sqlalchemy import select

    from telegram_kol_research.models import ExecutionOrderLeg

    with session_factory() as session:
        rows = session.execute(
            select(
                ExecutionOrderLeg.order_id,
                ExecutionOrderLeg.execution_binding_id,
            ).where(
                ExecutionOrderLeg.purpose == "entry",
                ExecutionOrderLeg.order_id.is_not(None),
                ExecutionOrderLeg.created_at >= since,
            )
        ).all()
    result: dict[str, int] = {}
    for order_id, binding_id in rows:
        text = _text(order_id)
        if text is not None:
            result.setdefault(text, int(binding_id))
    return result


class _RestReader:
    """Per-pass REST cache. GET only, and every failure is recorded, never zero."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._positions: dict[str, list[dict[str, Any]]] = {}
        self._triggers: dict[str, list[dict[str, Any]]] = {}
        self._fills: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.failures: list[str] = []

    def _read(self, label: str, work: Callable[[], Any]) -> list[dict[str, Any]] | None:
        try:
            rows = work()
        except Exception as exc:
            status = http_status_from_exception(exc)
            self.failures.append(
                f"{label}:{type(exc).__name__}"
                + ("" if status is None else f":http{status}")
            )
            # ``None`` means unknown. Callers must not read it as "there are
            # none": hard rule 4.
            return None
        return [row for row in (rows or []) if isinstance(row, dict)]

    def positions(self, inst_id: str) -> list[dict[str, Any]] | None:
        if inst_id not in self._positions:
            rows = self._read(
                f"list_positions[{inst_id}]",
                lambda: self._client.list_positions(inst_id=inst_id),
            )
            if rows is None:
                return None
            self._positions[inst_id] = rows
        return self._positions[inst_id]

    def trigger_orders(self, inst_id: str) -> list[dict[str, Any]] | None:
        if inst_id not in self._triggers:
            rows = self._read(
                f"list_trigger_orders_pending[{inst_id}]",
                lambda: self._client.list_trigger_orders_pending(inst_id=inst_id),
            )
            if rows is None:
                return None
            self._triggers[inst_id] = rows
        return self._triggers[inst_id]

    def fills(self, inst_id: str, order_id: str) -> list[dict[str, Any]] | None:
        key = (inst_id, order_id)
        if key not in self._fills:
            rows = self._read(
                f"list_trade_fills_by_order_id[{inst_id}]",
                lambda: self._client.list_trade_fills_by_order_id(
                    inst_id=inst_id, order_id=order_id
                ),
            )
            if rows is None:
                return None
            self._fills[key] = rows
        return self._fills[key]


def collect_chain_inputs(
    main_ord_id: str,
    *,
    frames_by_channel: dict[str, list[dict[str, Any]]],
    reader: _RestReader,
    instrument_map: Any,
) -> ShadowChainInputs:
    """Assemble one chain's inputs from the inbox and targeted REST reads."""

    trade_frames = [
        frame
        for frame in frames_by_channel.get("Trade", [])
        if _text(frame.get("order_sys_id")) == main_ord_id
    ]
    order_frames = [
        frame
        for frame in frames_by_channel.get("Order", [])
        if _text(frame.get("order_sys_id")) == main_ord_id
    ]
    instrument_stream = None
    for frame in trade_frames + order_frames:
        instrument_stream = _text(frame.get("instrument_raw"))
        if instrument_stream is not None:
            break

    instrument_rest: str | None = None
    instrument_resolved = True
    if instrument_stream is not None:
        instrument_rest = instrument_map.rest_id_for_stream_name(instrument_stream)
        instrument_resolved = instrument_rest is not None

    inputs = ShadowChainInputs(
        main_ord_id=main_ord_id,
        instrument_stream=instrument_stream,
        instrument_rest=instrument_rest,
        instrument_resolved=instrument_resolved,
        trade_frames=trade_frames,
        order_frames=order_frames,
        trigger_frames=list(frames_by_channel.get("TriggerOrder", [])),
        position_frames=list(frames_by_channel.get("Position", [])),
    )
    if instrument_rest is None:
        return inputs

    fills = reader.fills(instrument_rest, main_ord_id)
    positions = reader.positions(instrument_rest)
    triggers = reader.trigger_orders(instrument_rest)
    if fills is None or positions is None or triggers is None:
        inputs.rest_complete = False
        inputs.rest_read_failures = tuple(reader.failures)
        return inputs
    inputs.rest_fills = fills
    inputs.rest_positions = positions
    inputs.rest_trigger_orders = triggers
    return inputs


def upsert_shadow_binding(
    session_factory: Callable[[], Any],
    result: ShadowChainResult,
    *,
    now: datetime,
    observed_execution_binding_id: int | None,
    venue: str = "deepcoin",
) -> int:
    """Write one chain verdict into ``deepcoin_shadow_bindings`` and nowhere else.

    The only table this function may touch is the shadow table. A runtime
    assertion in :func:`run_shadow_binding_pass` holds the existing ledgers to
    their row counts across the whole pass, so a regression here is caught in
    production rather than only in review.
    """

    from sqlalchemy import select

    evidence = dict(result.evidence)
    evidence["criteria"] = dict(result.criteria)
    evidence_json = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, default=str
    )
    protection_text = ",".join(result.protection_ord_ids) or None
    with shadow_only_session(session_factory) as session:
        row = session.execute(
            select(DeepcoinShadowBinding).where(
                DeepcoinShadowBinding.venue == venue,
                DeepcoinShadowBinding.main_ord_id == result.main_ord_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = DeepcoinShadowBinding(
                venue=venue,
                main_ord_id=result.main_ord_id,
                first_seen_at=now,
                created_at=now,
            )
            session.add(row)
        row.instrument_rest = result.instrument_rest
        row.instrument_stream = result.instrument_stream
        row.side = result.side
        row.stage = result.stage
        row.pos_id = result.pos_id
        row.protection_ord_id = protection_text
        row.protection_order_count = len(result.protection_ord_ids)
        row.trade_os_seen_at = result.trade_os_seen_at
        row.tu_matched_at = result.tu_matched_at
        row.binding_confidence = result.binding_confidence
        row.refusal_reason = result.refusal_reason
        row.observed_execution_binding_id = observed_execution_binding_id
        row.evidence_json = evidence_json
        row.last_seen_at = now
        session.commit()
        return int(row.id)


# Tables the shadow path must leave untouched. Counting them around a pass is
# useful *evidence* and a poor *guard*: the worker runs other loops against the
# same database, so a legitimate concurrent trade would trip a count comparison.
# The guard is therefore per-session and exact -- see
# :func:`shadow_only_session` -- and these counts are recorded for the
# observation window instead.
GUARDED_LEDGER_TABLES = (
    "execution_bindings",
    "execution_order_legs",
    "position_protection_ledger",
    "position_protection_legs",
    "trigger_protection_intents",
    "position_take_profit_orders",
    "position_mutation_intents",
    "raw_messages",
)

# The only models a shadow session is allowed to create, modify or delete.
SHADOW_WRITABLE_MODELS = ("DeepcoinShadowBinding", "DeepcoinShadowDiff")


class ShadowLedgerMutationError(RuntimeError):
    """A shadow write reached an object outside the two shadow tables.

    Raised by the session guard *before* the flush, so the offending write never
    reaches the database. This is the runtime half of "the shadow table only
    writes itself"; the static half is a test over this module's imports.
    """


@contextmanager
def shadow_only_session(session_factory: Callable[[], Any]) -> Any:
    """Open a session that refuses to flush anything but the shadow tables.

    A row-count comparison around the pass cannot do this job: the worker has
    other loops writing the same database, so a real trade landing mid-pass
    would look identical to a shadow leak. Inspecting the session's own pending
    objects is exact, race-free, and fails before the write instead of after it.
    """

    from sqlalchemy import event

    session = session_factory()

    def _assert_shadow_only(current_session: Any, flush_context: Any, instances: Any) -> None:
        del flush_context, instances
        offenders = sorted(
            {
                type(obj).__name__
                for obj in (
                    list(current_session.new)
                    + list(current_session.dirty)
                    + list(current_session.deleted)
                )
                if type(obj).__name__ not in SHADOW_WRITABLE_MODELS
            }
        )
        if offenders:
            raise ShadowLedgerMutationError(
                "shadow session attempted to write: " + ", ".join(offenders)
            )

    event.listen(session, "before_flush", _assert_shadow_only)
    try:
        yield session
    finally:
        event.remove(session, "before_flush", _assert_shadow_only)
        session.close()


def guarded_ledger_counts(session_factory: Callable[[], Any]) -> dict[str, int]:
    from sqlalchemy import text as sql_text

    counts: dict[str, int] = {}
    with session_factory() as session:
        available = {
            str(row[0])
            for row in session.execute(
                sql_text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).all()
        }
        for table in GUARDED_LEDGER_TABLES:
            if table not in available:
                continue
            counts[table] = int(
                session.execute(sql_text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
            )
    return counts


def run_shadow_binding_pass(
    session_factory: Callable[[], Any],
    *,
    client: Any,
    instrument_map: Any,
    now: datetime,
    lookback_hours: int = SHADOW_LOOKBACK_HOURS,
    max_candidates: int = SHADOW_MAX_CANDIDATES_PER_PASS,
) -> ShadowPassResult:
    """Build or refresh every open shadow chain once. Writes shadow tables only.

    Candidates come from two places, both of which are only *starting points*:
    order ids the stream reported a fill for, and entry order ids production
    recorded. Everything after that is proved from the five criteria or refused.
    """

    since = now - timedelta(hours=lookback_hours)
    since_ms = int(since.timestamp() * 1000)
    frames = _ws_frames_since(
        session_factory,
        channels=("Trade", "Order", "TriggerOrder", "Position"),
        since_ms=since_ms,
    )
    frames_by_channel: dict[str, list[dict[str, Any]]] = {}
    for frame in frames:
        frames_by_channel.setdefault(str(frame.get("channel")), []).append(frame)

    ledger_entries = _ledger_entry_order_ids(session_factory, since=since)
    stream_candidates = {
        value
        for frame in frames_by_channel.get("Trade", [])
        if (value := _text(frame.get("order_sys_id")))
    }
    candidates = sorted(stream_candidates | set(ledger_entries))
    result = ShadowPassResult(candidates_seen=len(candidates))
    if not candidates:
        return result

    known = _existing_shadow_rows(session_factory, candidates)
    latest_frame_ms = _latest_frame_ms(frames_by_channel)
    due = [
        main_ord_id
        for main_ord_id in candidates
        if _needs_reevaluation(
            main_ord_id, known=known, latest_frame_ms=latest_frame_ms, now=now
        )
    ]
    result.candidates_due = len(due)
    reader = _RestReader(client)
    written_ids: list[int] = []
    for main_ord_id in due[:max_candidates]:
        inputs = collect_chain_inputs(
            main_ord_id,
            frames_by_channel=frames_by_channel,
            reader=reader,
            instrument_map=instrument_map,
        )
        verdict = evaluate_shadow_chain(inputs)
        result.evaluated += 1
        if verdict.is_exact:
            result.exact += 1
        else:
            result.unverified += 1
        written_ids.append(
            upsert_shadow_binding(
                session_factory,
                verdict,
                now=now,
                observed_execution_binding_id=ledger_entries.get(main_ord_id),
            )
        )
    result.written = len(written_ids)
    result.shadow_binding_ids = tuple(written_ids)
    result.rest_read_failures = tuple(reader.failures)
    return result
