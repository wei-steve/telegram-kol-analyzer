"""The stop a market fill never got, when exactly one position can be it.

Phase 6-pre-2 (B-5d). A market entry leg carries no stop in its own payload:
the stop is written afterwards against an exact ``posId``. When the identity
equation cannot say which position the order opened, that write is refused --
and it is refused three times over, by ``build_position_mutation_authority``,
by ``exact_position_write_gate`` and by the binding check inside
``set_exact_position_sltp``. Those refusals are correct. ``verified`` is the
precondition for every automatic modify, cancel and claim in this repository,
and phase 5 did not weaken it.

The consequence is a position that filled and has nothing bounding its loss
but the liquidation price. It has never happened -- 153 of 153 production
market entry legs satisfy the equation -- and this module is the net under
that fact rather than a routine path.

**What this is allowed to do, and nothing else.** Attach one stop-loss to one
position, once, when four preconditions all hold. It never attaches a take
profit, never claims ownership, never closes, never cancels. It is authorized
by a uniqueness argument, not by the identity equation, so it is deliberately
kept in its own module with its own authority type: ``NakedFillStopAuthority``
exists so that the bypass cannot be picked up by any other caller, and
:func:`submit_naked_fill_stop` refuses any purpose but ``stop_loss``.
``tests/test_naked_fill_stop_net_boundary.py`` fails if any module other than
this one imports the constructor.

**Why "unclaimed" excludes this leg itself.** The subject leg usually already
holds a fallback ``pos_id`` taken from the pre/post submission snapshot
difference -- ``recovery_live_submit`` records it precisely so the stop has
somewhere to go, and marks the leg ``unverified`` so nothing treats it as
ownership. Reading "unclaimed" as "referenced by no leg at all" would
therefore exclude the very position the net exists for. It means: referenced
by no *other* leg, and by no protection ledger row.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_ordinary_entry_binding import (
    ATTRIBUTION_UNVERIFIED,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
)
from telegram_kol_research.native_tpsl import normalize_native_tpsl
from telegram_kol_research.position_mutation_authority import (
    PositionMutationAuthority,
    PositionMutationAuthorityError,
    position_authority_fingerprint,
)

logger = logging.getLogger(__name__)

# The marker the rescued leg carries afterwards. It is deliberately not
# ``verified``: every ownership check in this repository compares against that
# exact string, so this value keeps failing all of them -- which is the point.
# The stop is attached; the position is still not ours to modify or claim.
NAKED_FILL_STOP_ATTRIBUTION = "unverified_sl_by_unique_candidate"

NAKED_FILL_INCIDENT_TYPE = "naked_market_fill_safety_net"
NAKED_FILL_POLICY_VERSION = "phase-6-pre-2-naked-fill-stop-v1"
NAKED_FILL_AUDIT_ACTION = "naked_fill_stop_attached"

# How long a fill is given to attribute itself normally before the net looks at
# it. The equation resolves in milliseconds when it resolves at all; a minute
# is long enough that a slow resync is never mistaken for a naked position.
NAKED_FILL_GRACE = timedelta(seconds=60)


class NakedFillStopNetError(RuntimeError):
    """The net refused to act. Never raised to break the caller's loop."""


@dataclass(frozen=True, slots=True)
class NakedFillStopAuthority(PositionMutationAuthority):
    """Authority for one stop-loss on an unattributed fill, and nothing else.

    A distinct type rather than a flag: it makes the bypass impossible to pass
    to a general mutation by accident, and it makes the static boundary test
    able to name exactly what it is guarding.
    """

    candidate_reason: str = ""


@dataclass(frozen=True, slots=True)
class NakedFillDecision:
    status: str  # "attach" | "alert_only" | "skip"
    reason: str
    leg_id: int | None = None
    order_id: str | None = None
    pos_id: str | None = None
    inst_id: str = ""
    side: str = ""
    fill_size: str = ""
    stop_loss: str | None = None
    preconditions: tuple[str, ...] = ()


def naked_fill_idempotency_key(*, order_id: str, pos_id: str) -> str:
    """One stop per (order, position). One order triggers the net once."""

    return f"naked-fill-sl:{order_id}:{pos_id}"


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def claimed_position_ids(session, *, venue: str, exclude_leg_id: int) -> set[str]:
    """Every position some other leg or protection row already speaks for."""

    claimed = {
        str(pos_id)
        for (pos_id,) in session.query(ExecutionOrderLeg.pos_id)
        .filter(
            ExecutionOrderLeg.venue == str(venue).lower(),
            ExecutionOrderLeg.pos_id.is_not(None),
            ExecutionOrderLeg.pos_id != "",
            ExecutionOrderLeg.id != int(exclude_leg_id),
        )
        .all()
        if pos_id
    }
    claimed.update(
        str(pos_id)
        for (pos_id,) in session.query(PositionProtectionLedger.pos_id)
        .filter(
            PositionProtectionLedger.venue == str(venue).lower(),
            PositionProtectionLedger.pos_id.is_not(None),
            PositionProtectionLedger.pos_id != "",
        )
        .all()
        if pos_id
    )
    return claimed


def evaluate_naked_fill(
    session_factory: sessionmaker,
    *,
    leg_id: int,
    live_positions: Any,
    now: datetime,
    venue: str = "deepcoin",
    pending_orders: Any = None,
) -> NakedFillDecision:
    """Decide, from the five preconditions, whether one stop may be attached.

    Every precondition is named in the returned decision whether it passed or
    failed, because the audit row has to carry them verbatim: an operator
    reading it later must be able to see what the net believed, not just what
    it did.
    """

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, int(leg_id))
        if leg is None:
            return NakedFillDecision("skip", "leg_missing")
        binding = session.get(ExecutionBinding, int(leg.execution_binding_id))
        order_id = str(leg.order_id or "")
        pos_id_hint = str(leg.pos_id or "")
        inst_id = _leg_instrument_id(leg, binding)
        side = _leg_position_side(leg, binding)
        fill_size = _leg_fill_size(leg)
        stop_loss = _draft_stop_loss(binding)

        # (a) an ordinary market entry leg whose attribution never resolved.
        if (
            str(leg.purpose or "") != "entry"
            or str(leg.order_kind or "") != "market"
            or str(leg.attribution_status or "") != ATTRIBUTION_UNVERIFIED
            or not order_id
        ):
            return NakedFillDecision(
                "skip",
                "not_an_unverified_market_entry",
                leg_id=int(leg.id),
                order_id=order_id or None,
            )
        # One order triggers the net once, ever. The marker is the record of
        # that, so re-running the tick cannot write a second stop.
        if str(leg.attribution_status or "") == NAKED_FILL_STOP_ATTRIBUTION:
            return NakedFillDecision(
                "skip", "already_handled", leg_id=int(leg.id), order_id=order_id
            )

        # (b) the grace period the equation gets to resolve on its own.
        filled_at = _as_utc(leg.updated_at) or _as_utc(leg.created_at)
        if filled_at is None or now - filled_at < NAKED_FILL_GRACE:
            return NakedFillDecision(
                "skip", "within_grace_period", leg_id=int(leg.id), order_id=order_id
            )

        if not inst_id or not side or fill_size is None or stop_loss is None:
            return NakedFillDecision(
                "alert_only",
                "leg_facts_incomplete",
                leg_id=int(leg.id),
                order_id=order_id,
                inst_id=inst_id,
                side=side,
                fill_size=str(fill_size) if fill_size is not None else "",
                preconditions=("a:pass", "b:pass", "c:unknown", "d:unknown"),
            )

        # (d) an unreadable snapshot is never a verdict. "No candidate" and
        # "could not look" must not produce the same action.
        if not isinstance(live_positions, list):
            return NakedFillDecision(
                "alert_only",
                "position_snapshot_incomplete",
                leg_id=int(leg.id),
                order_id=order_id,
                inst_id=inst_id,
                side=side,
                fill_size=str(fill_size),
                stop_loss=stop_loss,
                preconditions=("a:pass", "b:pass", "c:unknown", "d:fail"),
            )

        claimed = claimed_position_ids(
            session, venue=venue, exclude_leg_id=int(leg.id)
        )

    # (c) exactly one unclaimed active position on this instrument and side
    # whose size is exactly the size this order filled.
    candidates = []
    for row in live_positions:
        if not isinstance(row, Mapping):
            continue
        row_pos_id = str(row.get("posId") or "")
        row_inst = str(row.get("instId") or "").upper()
        row_side = str(row.get("posSide") or row.get("side") or "").lower()
        row_size = _decimal(row.get("pos"))
        if (
            not row_pos_id
            or row_inst != inst_id.upper()
            or row_side != side
            or row_size is None
            or row_size == 0
            or row_pos_id in claimed
            or row_size != fill_size
        ):
            continue
        candidates.append(row_pos_id)

    base = NakedFillDecision(
        "alert_only",
        "",
        leg_id=int(leg_id),
        order_id=order_id,
        inst_id=inst_id,
        side=side,
        fill_size=str(fill_size),
        stop_loss=stop_loss,
    )
    if not candidates:
        return _with(base, reason="no_unclaimed_candidate", preconditions=("a:pass", "b:pass", "c:fail_none", "d:pass"))
    if len(candidates) > 1:
        return _with(base, reason="candidate_not_unique", preconditions=("a:pass", "b:pass", "c:fail_many", "d:pass"))

    # (e) Phase 6 task 4. "Unclaimed" up to here is a purely *local* notion:
    # no other leg and no ledger row names this position. After a restart --
    # or after any ledger write that did not land -- that says nothing about
    # what the exchange is holding. Asking it is the difference between
    # attaching the stop a position lacks and attaching a second one beside
    # the stop it already has.
    #
    # Unreadable is unknown, never "no protection" (hard rule 4): the net
    # alerts instead of acting, exactly as it does for an unreadable position
    # snapshot.
    if not isinstance(pending_orders, list):
        return _with(
            base,
            reason="protection_snapshot_incomplete",
            pos_id=candidates[0],
            preconditions=("a:pass", "b:pass", "c:pass", "d:pass", "e:unknown"),
        )
    existing = _exchange_stop_order_ids(pending_orders, pos_id=candidates[0])
    if existing:
        return _with(
            base,
            reason="position_already_protected_on_exchange",
            pos_id=candidates[0],
            preconditions=("a:pass", "b:pass", "c:pass", "d:pass", "e:fail"),
        )
    return _with(
        base,
        status="attach",
        reason="unique_unclaimed_candidate",
        pos_id=candidates[0],
        preconditions=("a:pass", "b:pass", "c:pass", "d:pass", "e:pass"),
    )


def _exchange_stop_order_ids(
    pending_orders: list, *, pos_id: str
) -> tuple[str, ...]:
    """Stops the exchange already holds for this exact position.

    Matched by the position id the row itself carries -- the only thing on a
    pending row that names a position. A row that names no position is not
    counted: it may be a resting entry's own attached stop, which belongs to an
    order that has not filled and protects nothing here.

    The looseness is deliberately in the safe direction. This function can only
    ever *prevent* a write, so a false match costs one alert and a person's
    glance, while a false miss costs a duplicate stop on a live position.
    """

    found: list[str] = []
    for row in pending_orders:
        if not isinstance(row, Mapping):
            continue
        normalized = normalize_native_tpsl(dict(row))
        if normalized is None or normalized.stop_loss_trigger_price is None:
            continue
        if str(normalized.pos_id or "") != str(pos_id):
            continue
        if normalized.ord_id:
            found.append(str(normalized.ord_id))
    return tuple(found)


def _with(decision: NakedFillDecision, **changes: Any) -> NakedFillDecision:
    from dataclasses import replace

    return replace(decision, **changes)


def _leg_instrument_id(leg, binding) -> str:
    request = _json(leg.request_json)
    inst_id = str(request.get("instId") or "") if isinstance(request, dict) else ""
    if inst_id:
        return inst_id.upper()
    draft = _draft(binding)
    return str(draft.get("instrument_id") or "").upper() if draft else ""


def _leg_position_side(leg, binding) -> str:
    """The side the order itself declared, falling back to its binding.

    ``execution_order_legs`` has no side column: the submitted payload in
    ``request_json`` is the leg's own record of which side it opened, and the
    binding is the durable second opinion.
    """

    request = _json(leg.request_json)
    if isinstance(request, dict):
        side = str(request.get("posSide") or "").lower()
        if side:
            return side
    return str(getattr(binding, "side", "") or "").lower()


def _leg_fill_size(leg) -> Decimal | None:
    request = _json(leg.request_json)
    if isinstance(request, dict):
        size = _decimal(request.get("sz"))
        if size is not None and size > 0:
            return size
    return None


def _draft_stop_loss(binding) -> str | None:
    draft = _draft(binding)
    if not draft:
        return None
    stop_loss = draft.get("stop_loss")
    if stop_loss in (None, ""):
        return None
    parsed = _decimal(stop_loss)
    return None if parsed is None or parsed <= 0 else str(stop_loss)


def _draft(binding) -> dict[str, Any] | None:
    payload = _json(getattr(binding, "payload_json", None))
    if not isinstance(payload, dict):
        return None
    draft = payload.get("draft")
    return draft if isinstance(draft, dict) else None


def _json(value: Any) -> Any:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def build_naked_fill_stop_authority(
    session_factory: sessionmaker,
    *,
    decision: NakedFillDecision,
    live_position: Mapping[str, Any],
    venue: str = "deepcoin",
) -> NakedFillStopAuthority:
    """Authorize one stop on a position the uniqueness argument identified.

    This is the bypass, and it is the whole of it. It does not consult
    ``require_verified_position_ownership`` -- that is the point -- so every
    other precondition has to be re-proved here rather than assumed from the
    decision that was made a moment ago.
    """

    if decision.status != "attach" or not decision.pos_id or not decision.leg_id:
        raise PositionMutationAuthorityError("naked_fill_decision_not_actionable")
    if str(live_position.get("posId") or "") != decision.pos_id:
        raise PositionMutationAuthorityError("naked_fill_live_position_mismatch")
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, int(decision.leg_id))
        if leg is None:
            raise PositionMutationAuthorityError("naked_fill_leg_missing")
        if str(leg.attribution_status or "") != ATTRIBUTION_UNVERIFIED:
            raise PositionMutationAuthorityError("naked_fill_leg_not_unverified")
        binding = session.get(ExecutionBinding, int(leg.execution_binding_id))
        if binding is None:
            raise PositionMutationAuthorityError("naked_fill_binding_missing")
        if str(binding.status or "").lower() not in {"active", "open", "partial"}:
            raise PositionMutationAuthorityError("naked_fill_binding_not_open")
        return NakedFillStopAuthority(
            venue=str(venue).lower(),
            strategy_instance_id=str(leg.strategy_instance_id or ""),
            execution_binding_id=int(leg.execution_binding_id),
            execution_order_leg_id=int(leg.id),
            pos_id=str(decision.pos_id),
            instrument_id=str(decision.inst_id).upper(),
            side=str(decision.side).lower(),
            position_fingerprint=position_authority_fingerprint(live_position),
            candidate_reason=decision.reason,
        )


def naked_fill_incident_fingerprint(*, order_id: str, reason: str) -> str:
    return hashlib.sha256(
        f"{NAKED_FILL_INCIDENT_TYPE}:{order_id}:{reason}".encode()
    ).hexdigest()


def build_naked_fill_stop_payload(
    session_factory: sessionmaker,
    *,
    authority: NakedFillStopAuthority,
    stop_loss: str,
) -> dict[str, Any]:
    """The one payload shape this module is allowed to send.

    Built here rather than by a caller so the take-profit prohibition is
    structural: there is no parameter that could add one.
    """

    with session_factory() as session:
        binding = session.get(ExecutionBinding, int(authority.execution_binding_id))
        if binding is None:
            raise PositionMutationAuthorityError("naked_fill_binding_missing")
        margin_mode = str(binding.margin_mode or "").lower()
        position_mode = str(binding.position_mode or "").lower()
    payload = {
        "instType": "SWAP",
        "instId": authority.instrument_id,
        "posSide": authority.side,
        "mrgPosition": position_mode,
        "posId": authority.pos_id,
        "tdMode": margin_mode,
        "slTriggerPx": str(stop_loss),
    }
    if any(key.lower().startswith("tp") for key in payload):
        raise PositionMutationAuthorityError("naked_fill_take_profit_forbidden")
    return payload


def submit_naked_fill_stop(
    session_factory: sessionmaker,
    *,
    deepcoin_client: Any,
    authority: NakedFillStopAuthority,
    decision: NakedFillDecision,
    now: datetime,
    revalidate: Any,
) -> Mapping[str, Any] | None:
    """Write one stop-loss, and nothing else, under its own durable intent.

    This is a separate minimal writer rather than a call into
    ``PositionMutationGateway`` because that gateway proves ownership three
    times -- in ``build_position_mutation_authority``, in the caller's
    ``exact_position_write_gate``, and again inside ``set_exact_position_sltp``
    where ``_load_verified_binding`` requires ``attribution_status`` to be
    literally ``verified`` and then re-runs the ownership check. Reusing it
    would have meant weakening one of those, which is the opposite of the point.
    So the discipline is reproduced here instead of the guarantee being cut:
    durable intent before the request, one last revalidation immediately
    before it, no resend on an unknown outcome, and an exact readback after.

    Returns ``None`` when the last-moment revalidation refused; the caller
    treats that as "did not act".
    """

    if not isinstance(authority, NakedFillStopAuthority):
        raise PositionMutationAuthorityError("naked_fill_authority_required")
    if decision.status != "attach" or not decision.stop_loss or not decision.order_id:
        raise PositionMutationAuthorityError("naked_fill_decision_not_actionable")

    from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
    from telegram_kol_research.position_mutation_gateway import (
        _response_order_id,
        _set_position_sltp_readback_matches,
    )

    payload = build_naked_fill_stop_payload(
        session_factory, authority=authority, stop_loss=str(decision.stop_loss)
    )
    key = naked_fill_idempotency_key(
        order_id=str(decision.order_id), pos_id=authority.pos_id
    )
    intent_id = _reserve_naked_fill_intent(
        session_factory,
        authority=authority,
        payload=payload,
        idempotency_key=key,
        now=now,
    )
    if intent_id is None:
        # An intent under this key already exists. Whatever its state, this is
        # not a fresh decision to write: hard rule 2 forbids the resend.
        return None

    # Last moment. Between deciding and writing, another leg could have claimed
    # the candidate, the position could have closed, or the snapshot could have
    # gone unreadable.
    try:
        still_valid = bool(revalidate())
    except Exception:
        still_valid = False
    if not still_valid:
        _close_naked_fill_intent(
            session_factory, intent_id=intent_id, status="blocked", now=now,
            error={"reason": "naked_fill_revalidation_failed"},
        )
        return None

    try:
        response = deepcoin_client.set_position_sltp(payload)
    except DeepcoinRequestOutcomeUnknown:
        # The venue may or may not have acted. It is never replayed.
        _close_naked_fill_intent(
            session_factory, intent_id=intent_id, status="unknown", now=now,
            error={"reason": "unknown_exchange_outcome"},
        )
        raise
    except Exception as exc:
        _close_naked_fill_intent(
            session_factory, intent_id=intent_id, status="blocked", now=now,
            error={"reason": "naked_fill_write_failed", "error": str(exc)[:256]},
        )
        raise

    order_id = _response_order_id(response or {})
    if not order_id:
        _close_naked_fill_intent(
            session_factory, intent_id=intent_id, status="unknown", now=now,
            error={"reason": "naked_fill_response_missing_order_id"},
        )
        raise DeepcoinRequestOutcomeUnknown("naked_fill_response_missing_order_id")

    try:
        pending = deepcoin_client.list_trigger_orders_pending(
            inst_id=authority.instrument_id
        )
    except Exception as exc:
        _close_naked_fill_intent(
            session_factory, intent_id=intent_id, status="unknown", now=now,
            order_id=order_id, response=response,
            error={"reason": "naked_fill_readback_unavailable"},
        )
        raise DeepcoinRequestOutcomeUnknown("naked_fill_readback_unavailable") from exc

    # The pending endpoint names this field ``slTriggerPrice``, not the
    # position row's ``slTriggerPx``; the shared matcher already accepts both,
    # and reading the wrong key would look exactly like "no such order".
    if not _set_position_sltp_readback_matches(
        pending,
        order_id=order_id,
        authority=authority,
        purpose="stop_loss",
        trigger_price=str(decision.stop_loss),
    ):
        _close_naked_fill_intent(
            session_factory, intent_id=intent_id, status="unknown", now=now,
            order_id=order_id, response=response,
            error={"reason": "naked_fill_pending_readback"},
        )
        raise DeepcoinRequestOutcomeUnknown("naked_fill_pending_readback")

    _close_naked_fill_intent(
        session_factory, intent_id=intent_id, status="confirmed", now=now,
        order_id=order_id, response=response,
    )
    return response


def _reserve_naked_fill_intent(
    session_factory: sessionmaker,
    *,
    authority: NakedFillStopAuthority,
    payload: Mapping[str, Any],
    idempotency_key: str,
    now: datetime,
) -> int | None:
    """Claim the key, or report that somebody already did."""

    from sqlalchemy.exc import IntegrityError

    from telegram_kol_research.models import PositionMutationIntent

    request_json = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with session_factory() as session:
        existing = (
            session.query(PositionMutationIntent)
            .filter(PositionMutationIntent.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            return None
        intent = PositionMutationIntent(
            idempotency_key=idempotency_key,
            venue=authority.venue,
            operation="naked_fill_set_position_sltp",
            strategy_instance_id=authority.strategy_instance_id,
            execution_binding_id=int(authority.execution_binding_id),
            execution_order_leg_id=int(authority.execution_order_leg_id),
            pos_id=authority.pos_id,
            authority_fingerprint=authority.position_fingerprint,
            request_fingerprint=hashlib.sha256(
                request_json.encode("utf-8")
            ).hexdigest(),
            status="reserved",
            request_json=request_json,
            reserved_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(intent)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None
        return int(intent.id)


def _close_naked_fill_intent(
    session_factory: sessionmaker,
    *,
    intent_id: int,
    status: str,
    now: datetime,
    order_id: str | None = None,
    response: Any = None,
    error: Mapping[str, Any] | None = None,
) -> None:
    from telegram_kol_research.models import PositionMutationIntent

    with session_factory() as session:
        intent = session.get(PositionMutationIntent, int(intent_id))
        if intent is None:  # pragma: no cover - reserved a moment ago
            return
        intent.status = status
        intent.updated_at = now
        if order_id:
            intent.order_id = str(order_id)
        if response is not None:
            intent.response_json = json.dumps(response, ensure_ascii=False, default=str)
            intent.submitted_at = intent.submitted_at or now
        if error is not None:
            intent.error_json = json.dumps(dict(error), ensure_ascii=False)
        if status == "confirmed":
            intent.confirmed_at = now
        session.commit()


def mark_leg_rescued(
    session_factory: sessionmaker,
    *,
    leg_id: int,
    pos_id: str,
    now: datetime,
) -> bool:
    """Stamp the marker that is both the audit trail and the once-only latch.

    The compare-and-set on ``unverified`` is what makes a second tick a no-op:
    the marker is not ``verified``, so ownership is still refused everywhere,
    but it is no longer ``unverified``, so the net will not select this leg
    again.
    """

    from sqlalchemy import update

    with session_factory() as session:
        result = session.execute(
            update(ExecutionOrderLeg)
            .where(
                ExecutionOrderLeg.id == int(leg_id),
                ExecutionOrderLeg.attribution_status == ATTRIBUTION_UNVERIFIED,
            )
            .values(
                attribution_status=NAKED_FILL_STOP_ATTRIBUTION,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return False
        session.commit()
        return True


def record_naked_fill_incident(
    session_factory: sessionmaker,
    *,
    decision: NakedFillDecision,
    now: datetime,
    attached_order_id: str | None = None,
) -> None:
    """Say out loud that the equation failed, whatever the net then did.

    Both outcomes are critical. "A stop was attached by a uniqueness argument"
    and "a filled position may be naked and the net could not act" are each a
    fact a person has to see within seconds, so the type is delivered whatever
    the environment whitelist lists (``config.ALWAYS_NOTIFIED_INCIDENT_TYPES``).
    Recording evidence never raises into the caller: the exchange write, if any,
    already happened.
    """

    from telegram_kol_research.runtime_incidents import record_runtime_incident

    detail = (
        f"inst={decision.inst_id or 'none'} side={decision.side or 'none'} "
        f"sz={decision.fill_size or 'none'} pos={decision.pos_id or 'none'}"
    )
    containment = (
        "stop_attached_ownership_not_claimed"
        if decision.status == "attach"
        else "no_action_position_may_be_unprotected"
    )
    full = {
        "component": "naked_fill_stop_net",
        "reason_code": decision.reason or "naked_fill_unresolved",
        "impact": detail,
        "containment": containment,
    }
    minimal = {
        "component": "naked_fill_stop_net",
        "reason_code": decision.reason or "naked_fill_unresolved",
        "impact": "market_fill_attribution_unresolved",
        "containment": containment,
    }
    for summary in (full, minimal):
        try:
            record_runtime_incident(
                session_factory,
                source_kind="deepcoin_entry_order",
                source_record_id=str(decision.order_id or ""),
                incident_type=NAKED_FILL_INCIDENT_TYPE,
                severity="critical",
                fingerprint=naked_fill_incident_fingerprint(
                    order_id=str(decision.order_id or ""), reason=decision.reason
                ),
                redacted_summary=json.dumps(
                    summary, ensure_ascii=False, sort_keys=True
                ),
                occurred_at=now,
                feature_policy_version=NAKED_FILL_POLICY_VERSION,
                prompt_version="none",
                tool_policy_version=(
                    "stop-loss-only" if decision.status == "attach" else "no-exchange-write"
                ),
                evidence_refs_json=json.dumps(
                    [
                        f"deepcoin_entry_order:{decision.order_id or ''}",
                        *( [f"deepcoin_position:{decision.pos_id}"] if decision.pos_id else [] ),
                        *( [f"deepcoin_order:{attached_order_id}"] if attached_order_id else [] ),
                    ]
                ),
            )
            return
        except Exception:  # pragma: no cover - evidence must never break the net
            logger.exception(
                "naked_fill_incident_record_failed ord_id=%s", decision.order_id
            )


def record_naked_fill_audit(
    session_factory: sessionmaker,
    *,
    decision: NakedFillDecision,
    authority: NakedFillStopAuthority,
    attached_order_id: str,
    response: Mapping[str, Any] | None,
    now: datetime,
) -> None:
    """The durable audit row for a write no ownership proof authorized.

    It carries the four preconditions verbatim. An operator asking months later
    "why did something attach a stop to a position we never claimed" needs to
    read the argument the net actually made, not reconstruct it.
    """

    from telegram_kol_research.execution_events import (
        ExecutionEventRecord,
        record_execution_event,
    )

    try:
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action=NAKED_FILL_AUDIT_ACTION,
                status="submitted",
                execution_binding_id=int(authority.execution_binding_id),
                strategy_instance_id=authority.strategy_instance_id or None,
                symbol=(authority.instrument_id.split("-")[0] or None),
                side=authority.side,
                order_id=str(attached_order_id),
                related_order_id=str(decision.order_id or "") or None,
                pos_id=authority.pos_id,
                reason=decision.reason,
                before={
                    "attribution_status": ATTRIBUTION_UNVERIFIED,
                    "entry_order_id": str(decision.order_id or ""),
                    "candidate_pos_id": authority.pos_id,
                    "fill_size": decision.fill_size,
                    "preconditions": list(decision.preconditions),
                },
                after={
                    "attribution_status": NAKED_FILL_STOP_ATTRIBUTION,
                    "stop_loss": str(decision.stop_loss or ""),
                    "ownership_claimed": False,
                    "take_profit_attached": False,
                },
                response=dict(response) if isinstance(response, Mapping) else None,
                created_at=now,
            ),
        )
    except Exception:  # pragma: no cover - the write already happened
        logger.exception(
            "naked_fill_audit_record_failed ord_id=%s pos_id=%s",
            decision.order_id,
            authority.pos_id,
        )


@dataclass(frozen=True, slots=True)
class NakedFillNetResult:
    examined: int = 0
    attached: int = 0
    alerted: int = 0
    skipped: int = 0


def reconcile_naked_market_fills(
    session_factory: sessionmaker,
    *,
    deepcoin_client: Any,
    now: datetime,
    limit: int = 5,
    venue: str = "deepcoin",
) -> NakedFillNetResult:
    """One bounded pass over fills the identity equation could not attribute.

    Expected to do nothing forever: 153 of 153 production market entry legs
    satisfied the equation. It selects only legs still marked ``unverified``,
    so a leg it has already rescued -- or one the equation later resolved --
    drops out of the query on its own.
    """

    bounded = max(0, min(int(limit), 20))
    if bounded == 0:
        return NakedFillNetResult()
    cutoff = now - NAKED_FILL_GRACE
    with session_factory() as session:
        leg_ids = [
            int(row_id)
            for (row_id,) in session.query(ExecutionOrderLeg.id)
            .filter(
                ExecutionOrderLeg.venue == str(venue).lower(),
                ExecutionOrderLeg.purpose == "entry",
                ExecutionOrderLeg.order_kind == "market",
                ExecutionOrderLeg.attribution_status == ATTRIBUTION_UNVERIFIED,
                ExecutionOrderLeg.order_id.is_not(None),
                ExecutionOrderLeg.updated_at <= cutoff,
            )
            .order_by(ExecutionOrderLeg.updated_at, ExecutionOrderLeg.id)
            .limit(bounded)
            .all()
        ]
    if not leg_ids:
        return NakedFillNetResult()

    counts = {"examined": 0, "attached": 0, "alerted": 0, "skipped": 0}
    for leg_id in leg_ids:
        counts["examined"] += 1
        try:
            _handle_one_naked_fill(
                session_factory,
                deepcoin_client=deepcoin_client,
                leg_id=leg_id,
                now=now,
                venue=venue,
                counts=counts,
            )
        except Exception:
            # This runs inside the operator tick, whose loop swallows
            # exceptions wholesale. Logging here is the only way a failure of
            # the safety net itself is visible at all.
            counts["skipped"] += 1
            logger.warning(
                "naked_fill_stop_net_failed leg_id=%s", leg_id, exc_info=True
            )
    return NakedFillNetResult(**counts)


def _handle_one_naked_fill(
    session_factory: sessionmaker,
    *,
    deepcoin_client: Any,
    leg_id: int,
    now: datetime,
    venue: str,
    counts: dict[str, int],
) -> None:
    live_positions = _read_positions(deepcoin_client, session_factory, leg_id=leg_id)
    pending_orders = _read_pending_orders(
        deepcoin_client, session_factory, leg_id=leg_id
    )
    decision = evaluate_naked_fill(
        session_factory,
        leg_id=leg_id,
        live_positions=live_positions,
        now=now,
        venue=venue,
        pending_orders=pending_orders,
    )
    if decision.status == "skip":
        counts["skipped"] += 1
        return
    if decision.status == "alert_only":
        record_naked_fill_incident(session_factory, decision=decision, now=now)
        counts["alerted"] += 1
        return

    live_position = next(
        (
            row
            for row in live_positions
            if isinstance(row, Mapping)
            and str(row.get("posId") or "") == decision.pos_id
        ),
        None,
    )
    if live_position is None:  # pragma: no cover - decision proved it present
        counts["skipped"] += 1
        return
    authority = build_naked_fill_stop_authority(
        session_factory,
        decision=decision,
        live_position=live_position,
        venue=venue,
    )

    def revalidate() -> bool:
        # Both snapshots are re-read here, not just the positions one. The
        # last-moment check exists because the exchange can change between the
        # decision and the write, and "somebody else attached a stop to this
        # position in the meantime" is exactly one of those changes -- leaving
        # it out would re-ask four of the five preconditions and take the fifth
        # on trust from a read that is now old.
        fresh = _read_positions(deepcoin_client, session_factory, leg_id=leg_id)
        fresh_pending = _read_pending_orders(
            deepcoin_client, session_factory, leg_id=leg_id
        )
        recheck = evaluate_naked_fill(
            session_factory,
            leg_id=leg_id,
            live_positions=fresh,
            now=now,
            venue=venue,
            pending_orders=fresh_pending,
        )
        return recheck.status == "attach" and recheck.pos_id == decision.pos_id

    response = submit_naked_fill_stop(
        session_factory,
        deepcoin_client=deepcoin_client,
        authority=authority,
        decision=decision,
        now=now,
        revalidate=revalidate,
    )
    if response is None:
        # The last-moment revalidation refused, or the key was already claimed.
        # Nothing reached the exchange, so nothing is marked, audited or counted
        # as attached.
        counts["skipped"] += 1
        return
    # The venue's raw response carries the id under ``data``. Reuse the
    # gateway's own extractor rather than guessing the shape a second time.
    from telegram_kol_research.position_mutation_gateway import _response_order_id

    attached_order_id = str(_response_order_id(response) or "")
    mark_leg_rescued(
        session_factory, leg_id=leg_id, pos_id=decision.pos_id, now=now
    )
    record_naked_fill_audit(
        session_factory,
        decision=decision,
        authority=authority,
        attached_order_id=attached_order_id,
        response=response,
        now=now,
    )
    record_naked_fill_incident(
        session_factory,
        decision=decision,
        now=now,
        attached_order_id=attached_order_id or None,
    )
    counts["attached"] += 1


def _read_pending_orders(
    deepcoin_client: Any, session_factory: sessionmaker, *, leg_id: int
):
    """Return the live pending trigger rows, or ``None`` -- never an empty list.

    Same rule as :func:`_read_positions`: an unreadable exchange produces
    "unknown", never "there is no protection". Here the difference decides
    whether the net attaches a second stop beside one that already exists.
    """

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, int(leg_id))
        binding = (
            session.get(ExecutionBinding, int(leg.execution_binding_id))
            if leg is not None
            else None
        )
        inst_id = _leg_instrument_id(leg, binding) if leg is not None else ""
    if not inst_id:
        return None
    lister = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if not callable(lister):
        return None
    try:
        rows = lister(inst_id=inst_id)
    except Exception:
        logger.warning(
            "naked_fill_protection_snapshot_unavailable inst_id=%s", inst_id,
            exc_info=True,
        )
        return None
    return rows if isinstance(rows, list) else None


def _read_positions(deepcoin_client: Any, session_factory: sessionmaker, *, leg_id: int):
    """Return the live rows, or ``None`` -- never an empty list on failure.

    Hard rule 4: an unreadable exchange produces "unknown", never "zero". The
    difference decides whether the net alerts or acts.
    """

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, int(leg_id))
        binding = (
            session.get(ExecutionBinding, int(leg.execution_binding_id))
            if leg is not None
            else None
        )
        inst_id = _leg_instrument_id(leg, binding) if leg is not None else ""
    if not inst_id:
        return None
    try:
        positions = deepcoin_client.list_positions(inst_id=inst_id)
    except Exception:
        logger.warning(
            "naked_fill_position_snapshot_unavailable inst_id=%s", inst_id,
            exc_info=True,
        )
        return None
    return positions if isinstance(positions, list) else None
