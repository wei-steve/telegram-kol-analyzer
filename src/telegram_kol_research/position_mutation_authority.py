"""Exact, immutable authority checks for live position mutations."""

from __future__ import annotations

from dataclasses import dataclass


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
