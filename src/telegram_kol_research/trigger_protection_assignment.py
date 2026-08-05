"""Pure account-wide assignment of anonymous trigger-protection orders."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any


@dataclass(frozen=True, slots=True)
class ProtectionOwner:
    leg_id: int
    binding_id: int
    pos_id: str
    instrument_id: str
    side: str
    size_text: str
    stop_price: str
    position_created_at: datetime


@dataclass(frozen=True, slots=True)
class ProtectionOrderCandidate:
    order_id: str
    instrument_id: str
    side: str
    size_text: str
    stop_price: str
    created_at: datetime
    explicit_pos_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProtectionAssignmentConflict:
    reason_code: str
    owner_leg_ids: tuple[int, ...] = ()
    candidate_order_ids: tuple[str, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProtectionAssignmentResult:
    assignments: dict[int, str]
    evidence_by_leg: dict[int, dict[str, object]]
    exclusions: dict[str, str]
    conflicts: tuple[ProtectionAssignmentConflict, ...]
    snapshot_fingerprint: str


def assign_trigger_protection_orders(
    *,
    owners: tuple[ProtectionOwner, ...],
    candidates: tuple[ProtectionOrderCandidate, ...],
    existing_order_owners: dict[str, str],
    snapshot_complete: bool,
) -> ProtectionAssignmentResult:
    """Accept only account-wide owner/order edges that are mutually unique."""

    sorted_owners = tuple(sorted(owners, key=_owner_sort_key))
    sorted_candidates = tuple(sorted(candidates, key=_candidate_sort_key))
    fingerprint = _snapshot_fingerprint(
        owners=sorted_owners,
        candidates=sorted_candidates,
        existing_order_owners=existing_order_owners,
        snapshot_complete=snapshot_complete,
    )
    if not snapshot_complete:
        return ProtectionAssignmentResult(
            assignments={},
            evidence_by_leg={},
            exclusions={},
            conflicts=(ProtectionAssignmentConflict(reason_code="snapshot_incomplete"),),
            snapshot_fingerprint=fingerprint,
        )

    conflicts: list[ProtectionAssignmentConflict] = []
    exclusions: dict[str, str] = {}
    owners_by_pos = {str(owner.pos_id).strip(): owner for owner in sorted_owners}
    satisfied_owner_leg_ids: set[int] = set()
    candidate_by_order: dict[str, ProtectionOrderCandidate] = {}
    eligible_candidates: list[ProtectionOrderCandidate] = []

    for candidate in sorted_candidates:
        order_id = str(candidate.order_id).strip()
        if not order_id or order_id in candidate_by_order:
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="candidate_order_id_not_unique",
                    candidate_order_ids=(order_id,) if order_id else (),
                )
            )
            continue
        candidate_by_order[order_id] = candidate
        aliases = _explicit_pos_ids(candidate)
        if len(aliases) > 1:
            exclusions[order_id] = "candidate_pos_id_conflict"
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="candidate_pos_id_conflict",
                    candidate_order_ids=(order_id,),
                    evidence={"explicit_pos_ids": list(aliases)},
                )
            )
            continue

        immutable_owner = str(existing_order_owners.get(order_id) or "").strip()
        if immutable_owner:
            if aliases and aliases[0] != immutable_owner:
                exclusions[order_id] = "immutable_owner_conflict"
                conflicts.append(
                    ProtectionAssignmentConflict(
                        reason_code="immutable_owner_conflict",
                        candidate_order_ids=(order_id,),
                        evidence={
                            "immutable_pos_id": immutable_owner,
                            "explicit_pos_id": aliases[0],
                        },
                    )
                )
            elif immutable_owner in owners_by_pos:
                exclusions[order_id] = "candidate_already_owned"
                satisfied_owner_leg_ids.add(owners_by_pos[immutable_owner].leg_id)
            else:
                exclusions[order_id] = "candidate_owned_by_other_position"
            continue

        if candidate.created_at is None and not aliases:
            exclusions[order_id] = "candidate_time_unavailable"
            continue
        eligible_candidates.append(candidate)

    edges_by_owner: dict[int, list[str]] = {
        owner.leg_id: [] for owner in sorted_owners
    }
    edges_by_candidate: dict[str, list[int]] = {
        candidate.order_id: [] for candidate in eligible_candidates
    }
    prefill_candidates: set[str] = set()

    for owner in sorted_owners:
        if owner.leg_id in satisfied_owner_leg_ids:
            continue
        if owner.position_created_at is None:
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="owner_time_unavailable",
                    owner_leg_ids=(owner.leg_id,),
                )
            )
            continue
        for candidate in eligible_candidates:
            if not _same_protection_signature(owner, candidate):
                continue
            aliases = _explicit_pos_ids(candidate)
            if aliases:
                if aliases[0] != str(owner.pos_id).strip():
                    continue
            elif candidate.created_at < owner.position_created_at:
                prefill_candidates.add(candidate.order_id)
                continue
            edges_by_owner[owner.leg_id].append(candidate.order_id)
            edges_by_candidate[candidate.order_id].append(owner.leg_id)

    for order_id in sorted(prefill_candidates):
        if not edges_by_candidate.get(order_id):
            exclusions[order_id] = "candidate_predates_fill"

    assignments: dict[int, str] = {}
    evidence_by_leg: dict[int, dict[str, object]] = {}
    for owner in sorted_owners:
        if owner.leg_id in satisfied_owner_leg_ids:
            continue
        owner_edges = tuple(sorted(edges_by_owner.get(owner.leg_id, ())))
        if len(owner_edges) != 1:
            if len(owner_edges) > 1:
                conflicts.append(
                    ProtectionAssignmentConflict(
                        reason_code="protection_assignment_not_mutual_unique",
                        owner_leg_ids=(owner.leg_id,),
                        candidate_order_ids=owner_edges,
                    )
                )
            continue
        order_id = owner_edges[0]
        candidate_edges = tuple(sorted(edges_by_candidate.get(order_id, ())))
        if candidate_edges != (owner.leg_id,):
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="protection_assignment_not_mutual_unique",
                    owner_leg_ids=candidate_edges,
                    candidate_order_ids=(order_id,),
                )
            )
            continue
        candidate = candidate_by_order[order_id]
        match_kind = "explicit_pos_id" if _explicit_pos_ids(candidate) else "mutual_unique"
        assignments[owner.leg_id] = order_id
        evidence_by_leg[owner.leg_id] = {
            "binding_id": owner.binding_id,
            "candidate_order_id": order_id,
            "match_kind": match_kind,
            "pos_id": owner.pos_id,
            "snapshot_fingerprint": fingerprint,
        }

    return ProtectionAssignmentResult(
        assignments=dict(sorted(assignments.items())),
        evidence_by_leg=dict(sorted(evidence_by_leg.items())),
        exclusions=dict(sorted(exclusions.items())),
        conflicts=_deduplicate_conflicts(conflicts),
        snapshot_fingerprint=fingerprint,
    )


def _same_protection_signature(
    owner: ProtectionOwner,
    candidate: ProtectionOrderCandidate,
) -> bool:
    return (
        str(owner.instrument_id).strip().upper()
        == str(candidate.instrument_id).strip().upper()
        and str(owner.side).strip().lower() == str(candidate.side).strip().lower()
        and _same_number(owner.size_text, candidate.size_text)
        and _same_number(owner.stop_price, candidate.stop_price)
    )


def _same_number(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _explicit_pos_ids(candidate: ProtectionOrderCandidate) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(value).strip()
                for value in candidate.explicit_pos_ids
                if str(value).strip()
            }
        )
    )


def _owner_sort_key(owner: ProtectionOwner) -> tuple[int, str]:
    return int(owner.leg_id), str(owner.pos_id)


def _candidate_sort_key(candidate: ProtectionOrderCandidate) -> tuple[str, str]:
    return str(candidate.order_id), _datetime_text(candidate.created_at)


def _datetime_text(value: datetime | None) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def _snapshot_fingerprint(
    *,
    owners: tuple[ProtectionOwner, ...],
    candidates: tuple[ProtectionOrderCandidate, ...],
    existing_order_owners: dict[str, str],
    snapshot_complete: bool,
) -> str:
    payload = {
        "candidates": [
            {
                **asdict(candidate),
                "created_at": _datetime_text(candidate.created_at),
                "explicit_pos_ids": list(_explicit_pos_ids(candidate)),
            }
            for candidate in candidates
        ],
        "existing_order_owners": sorted(
            (str(order_id), str(pos_id))
            for order_id, pos_id in existing_order_owners.items()
        ),
        "owners": [
            {
                **asdict(owner),
                "position_created_at": _datetime_text(owner.position_created_at),
            }
            for owner in owners
        ],
        "snapshot_complete": bool(snapshot_complete),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _deduplicate_conflicts(
    conflicts: list[ProtectionAssignmentConflict],
) -> tuple[ProtectionAssignmentConflict, ...]:
    unique: dict[str, ProtectionAssignmentConflict] = {}
    for conflict in conflicts:
        key = json.dumps(
            {
                "candidate_order_ids": conflict.candidate_order_ids,
                "evidence": conflict.evidence,
                "owner_leg_ids": conflict.owner_leg_ids,
                "reason_code": conflict.reason_code,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        unique[key] = conflict
    return tuple(unique[key] for key in sorted(unique))
