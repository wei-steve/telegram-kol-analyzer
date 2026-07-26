"""Exact, immutable authority checks for live position mutations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


class PositionMutationAuthorityError(RuntimeError):
    """Raised before a position write when exact ownership is unavailable."""


@dataclass(frozen=True, slots=True)
class PositionMutationAuthority:
    venue: str
    strategy_instance_id: str
    execution_binding_id: int
    execution_order_leg_id: int
    pos_id: str
    instrument_id: str
    side: str
    position_fingerprint: str
    protection_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ProtectionOrderOwner:
    venue: str
    order_id: str
    strategy_instance_id: str
    execution_binding_id: int
    execution_order_leg_id: int
    pos_id: str
    instrument_id: str
    side: str


def require_order_owned_by_authority(
    *,
    authority: PositionMutationAuthority,
    owner: ProtectionOrderOwner | None,
) -> None:
    """Reject a mutation unless every persisted owner field matches."""

    if owner is None:
        raise PositionMutationAuthorityError("order_owner_missing")
    expected = (
        authority.venue.lower(),
        authority.strategy_instance_id,
        authority.execution_binding_id,
        authority.execution_order_leg_id,
        authority.pos_id,
        authority.instrument_id.upper(),
        authority.side.lower(),
    )
    actual = (
        owner.venue.lower(),
        owner.strategy_instance_id,
        owner.execution_binding_id,
        owner.execution_order_leg_id,
        owner.pos_id,
        owner.instrument_id.upper(),
        owner.side.lower(),
    )
    if actual != expected:
        raise PositionMutationAuthorityError("order_owner_mismatch")


def position_authority_fingerprint(position: Mapping[str, Any]) -> str:
    """Fingerprint exact live position facts without aggregate SL/TP fields."""

    facts = {
        "avg_price": _canonical_decimal(position.get("avgPx")),
        "instrument_id": str(position.get("instId") or "").upper(),
        "margin_mode": str(
            position.get("mgnMode") or position.get("marginMode") or ""
        ).lower(),
        "pos_id": str(position.get("posId") or ""),
        "position_mode": str(
            position.get("mrgPosition") or position.get("positionMode") or ""
        ).lower(),
        "side": str(position.get("posSide") or position.get("side") or "").lower(),
        "size": _canonical_decimal(position.get("pos")),
    }
    encoded = json.dumps(
        facts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_position_mutation_authority(
    session,
    *,
    venue: str,
    pos_id: str,
    live_position: Mapping[str, Any],
) -> PositionMutationAuthority:
    """Build authority only from the sole authoritative entry leg and live row."""

    from telegram_kol_research.models import ExecutionBinding
    from telegram_kol_research.position_attribution import (
        PositionAttributionError,
        canonical_live_position_economics,
        require_verified_position_ownership,
    )

    try:
        leg = require_verified_position_ownership(
            session,
            venue=venue,
            pos_id=pos_id,
        )
    except PositionAttributionError as exc:
        raise PositionMutationAuthorityError(str(exc)) from None
    if str(leg.purpose or "") != "entry":
        raise PositionMutationAuthorityError("position_owner_not_entry_leg")
    binding = session.get(ExecutionBinding, leg.execution_binding_id)
    if binding is None:
        raise PositionMutationAuthorityError("execution_binding_missing")
    instrument_id = str(live_position.get("instId") or "").upper()
    side = str(
        live_position.get("posSide") or live_position.get("side") or ""
    ).lower()
    live_pos_id = str(live_position.get("posId") or "")
    try:
        economics = canonical_live_position_economics(
            [live_position],
            target_pos_ids={str(pos_id)},
            instrument_id=instrument_id,
            side=side,
        )[0]
    except PositionAttributionError as exc:
        raise PositionMutationAuthorityError(str(exc)) from None
    if (
        live_pos_id != str(pos_id)
        or not instrument_id.startswith(f"{str(binding.symbol).upper()}-")
        or side != str(binding.side or "").lower()
        or str(binding.venue or "").lower() != str(venue or "").lower()
        or str(binding.strategy_instance_id or "")
        != str(leg.strategy_instance_id or "")
        or str(binding.status or "").lower() not in {"active", "open", "partial"}
        or economics["margin_mode"] != str(binding.margin_mode or "").lower()
        or economics["position_mode"] != str(binding.position_mode or "").lower()
    ):
        raise PositionMutationAuthorityError("live_position_binding_mismatch")
    return PositionMutationAuthority(
        venue=str(venue or "").lower(),
        strategy_instance_id=str(binding.strategy_instance_id or ""),
        execution_binding_id=int(binding.id),
        execution_order_leg_id=int(leg.id),
        pos_id=live_pos_id,
        instrument_id=instrument_id,
        side=side,
        position_fingerprint=position_authority_fingerprint(live_position),
    )


def _canonical_decimal(value: object) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if not number.is_finite():
        return ""
    normalized = format(number.normalize(), "f")
    return "0" if normalized == "-0" else normalized
