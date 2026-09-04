"""Pure account-wide assignment of anonymous trigger-protection orders."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
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
    client_order_id: str | None = None
    intent_id: int | None = None
    intent_created_at: datetime | None = None
    snapshot_observed_at: datetime | None = None
    child_regular_order_id: str | None = None
    child_exchange_created_at: datetime | None = None
    child_exchange_created_at_raw: str | None = None
    parent_trigger_order_id: str | None = None
    request_fingerprint: str | None = None
    owner_baseline_order_ids: tuple[str, ...] = ()
    owner_baseline_fingerprint: str | None = None
    lineage_attestation_fingerprint: str | None = None
    lineage_evidence_required: bool = False
    direct_identity_permitted: bool = True


@dataclass(frozen=True, slots=True)
class ProtectionOrderCandidate:
    order_id: str
    instrument_id: str
    side: str
    size_text: str
    stop_price: str
    created_at: datetime
    order_id_aliases: tuple[str, ...] = ()
    instrument_id_aliases: tuple[str, ...] = ()
    side_aliases: tuple[str, ...] = ()
    size_aliases: tuple[str, ...] = ()
    stop_price_aliases: tuple[str, ...] = ()
    order_type_aliases: tuple[str, ...] = ()
    evidence_complete: bool = True
    explicit_pos_ids: tuple[str, ...] = ()
    explicit_parent_order_ids: tuple[str, ...] = ()
    explicit_client_order_ids: tuple[str, ...] = ()
    created_at_raw: str | None = None


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
            conflicts=(
                ProtectionAssignmentConflict(
                    reason_code="snapshot_incomplete",
                    owner_leg_ids=tuple(owner.leg_id for owner in sorted_owners),
                ),
            ),
            snapshot_fingerprint=fingerprint,
        )

    conflicts: list[ProtectionAssignmentConflict] = []
    exclusions: dict[str, str] = {}
    new_authority_present = any(
        owner.lineage_evidence_required
        or bool(owner.lineage_attestation_fingerprint)
        for owner in sorted_owners
    )
    owner_pos_counts: dict[str, int] = {}
    for owner in sorted_owners:
        owner_pos_id = str(owner.pos_id).strip()
        if owner_pos_id:
            owner_pos_counts[owner_pos_id] = owner_pos_counts.get(owner_pos_id, 0) + 1
    duplicate_owner_pos_ids = {
        pos_id for pos_id, count in owner_pos_counts.items() if count != 1
    }
    owners_by_pos = {
        str(owner.pos_id).strip(): owner
        for owner in sorted_owners
        if str(owner.pos_id).strip() not in duplicate_owner_pos_ids
    }
    for pos_id in sorted(duplicate_owner_pos_ids):
        conflicts.append(
            ProtectionAssignmentConflict(
                reason_code="owner_pos_id_not_unique",
                owner_leg_ids=tuple(
                    sorted(
                        owner.leg_id
                        for owner in sorted_owners
                        if str(owner.pos_id).strip() == pos_id
                    )
                ),
                evidence={"pos_id": pos_id},
            )
        )
    if duplicate_owner_pos_ids and new_authority_present:
        return ProtectionAssignmentResult(
            assignments={},
            evidence_by_leg={},
            exclusions={},
            conflicts=_deduplicate_conflicts(conflicts),
            snapshot_fingerprint=fingerprint,
        )
    satisfied_owner_leg_ids: set[int] = set()
    candidate_by_order: dict[str, ProtectionOrderCandidate] = {}
    eligible_candidates: list[ProtectionOrderCandidate] = []
    blocking_candidate_ids: set[str] = set()
    unbounded_candidate_ids: set[str] = set()
    candidate_order_counts: dict[str, int] = {}
    for candidate in sorted_candidates:
        for order_id in _candidate_order_ids(candidate):
            candidate_order_counts[order_id] = (
                candidate_order_counts.get(order_id, 0) + 1
            )

    for candidate in sorted_candidates:
        if _candidate_is_explicit_non_tpsl(candidate):
            continue
        order_id = str(candidate.order_id).strip()
        if not order_id or candidate_order_counts.get(order_id, 0) != 1:
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="candidate_order_id_not_unique",
                    candidate_order_ids=(order_id,) if order_id else (),
                )
            )
            if new_authority_present:
                unbounded_candidate_ids.add(order_id or "<missing-order-id>")
            continue
        order_aliases = _candidate_order_ids(candidate)
        if order_aliases != (order_id,):
            exclusions[order_id] = "candidate_order_id_alias_conflict"
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="candidate_order_id_alias_conflict",
                    candidate_order_ids=order_aliases or (order_id,),
                    evidence={"order_id_aliases": list(order_aliases)},
                )
            )
            if new_authority_present:
                unbounded_candidate_ids.add(order_id)
            continue
        shape_conflict = _candidate_shape_conflict(candidate)
        if shape_conflict is not None:
            exclusions[order_id] = shape_conflict
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code=shape_conflict,
                    candidate_order_ids=(order_id,),
                )
            )
            if new_authority_present:
                unbounded_candidate_ids.add(order_id)
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
            if new_authority_present:
                unbounded_candidate_ids.add(order_id)
            continue

        if not candidate.evidence_complete:
            exclusions[order_id] = "candidate_evidence_incomplete"
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="candidate_evidence_incomplete",
                    candidate_order_ids=(order_id,),
                )
            )
            blocking_candidate_ids.add(order_id)
        parent_aliases = _explicit_parent_order_ids(candidate)
        if len(parent_aliases) > 1:
            exclusions[order_id] = "candidate_parent_id_conflict"
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="candidate_parent_id_conflict",
                    candidate_order_ids=(order_id,),
                    evidence={"explicit_parent_order_ids": list(parent_aliases)},
                )
            )
            if new_authority_present:
                unbounded_candidate_ids.add(order_id)
            continue
        client_aliases = _explicit_client_order_ids(candidate)
        if len(client_aliases) > 1:
            exclusions[order_id] = "candidate_client_id_conflict"
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="candidate_client_id_conflict",
                    candidate_order_ids=(order_id,),
                    evidence={"explicit_client_order_ids": list(client_aliases)},
                )
            )
            if new_authority_present:
                unbounded_candidate_ids.add(order_id)
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

        if candidate.created_at is None:
            exclusions[order_id] = "candidate_time_unavailable"
            continue
        eligible_candidates.append(candidate)

    if unbounded_candidate_ids:
        conflicts.append(
            ProtectionAssignmentConflict(
                reason_code="candidate_evidence_unbounded",
                owner_leg_ids=tuple(owner.leg_id for owner in sorted_owners),
                candidate_order_ids=tuple(sorted(unbounded_candidate_ids)),
            )
        )
        return ProtectionAssignmentResult(
            assignments={},
            evidence_by_leg={},
            exclusions=dict(sorted(exclusions.items())),
            conflicts=_deduplicate_conflicts(conflicts),
            snapshot_fingerprint=fingerprint,
        )

    edges_by_owner: dict[int, list[str]] = {
        owner.leg_id: [] for owner in sorted_owners
    }
    edges_by_candidate: dict[str, list[int]] = {
        candidate.order_id: [] for candidate in eligible_candidates
    }
    prefill_candidates: set[str] = set()
    parent_mismatch_owner_ids: dict[str, set[int]] = {}

    for owner in sorted_owners:
        if str(owner.pos_id).strip() in duplicate_owner_pos_ids:
            continue
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
            parent_aliases = _explicit_parent_order_ids(candidate)
            client_aliases = _explicit_client_order_ids(candidate)
            blocking_only_owner = bool(
                owner.lineage_evidence_required
                and not owner.lineage_attestation_fingerprint
                and not owner.direct_identity_permitted
            )
            if blocking_only_owner:
                if not aliases and not parent_aliases and not client_aliases:
                    edges_by_owner[owner.leg_id].append(candidate.order_id)
                    edges_by_candidate[candidate.order_id].append(owner.leg_id)
                continue
            if candidate.order_id in {
                str(value).strip() for value in owner.owner_baseline_order_ids
            }:
                exclusions.setdefault(
                    candidate.order_id,
                    (
                        "candidate_in_owner_baseline"
                        if owner.lineage_attestation_fingerprint
                        else "candidate_in_global_baseline"
                    ),
                )
                continue
            sequence_reason = _lineage_sequence_refusal(owner, candidate)
            if sequence_reason is not None:
                exclusions.setdefault(candidate.order_id, sequence_reason)
                continue
            if client_aliases and client_aliases != (
                str(owner.client_order_id or "").strip(),
            ):
                continue
            if parent_aliases and parent_aliases != (
                str(owner.parent_trigger_order_id or "").strip(),
            ):
                parent_mismatch_owner_ids.setdefault(candidate.order_id, set()).add(
                    owner.leg_id
                )
                continue
            if aliases:
                if aliases[0] != str(owner.pos_id).strip():
                    continue
            else:
                if (
                    owner.lineage_evidence_required
                    and not owner.lineage_attestation_fingerprint
                ):
                    edges_by_owner[owner.leg_id].append(candidate.order_id)
                    edges_by_candidate[candidate.order_id].append(owner.leg_id)
                    continue
                lineage_reason = _lineage_edge_refusal(owner, candidate)
                if owner.lineage_attestation_fingerprint:
                    if lineage_reason is not None:
                        exclusions.setdefault(candidate.order_id, lineage_reason)
                        conflicts.append(
                            ProtectionAssignmentConflict(
                                reason_code=lineage_reason,
                                owner_leg_ids=(owner.leg_id,),
                                candidate_order_ids=(candidate.order_id,),
                            )
                        )
                        continue
                elif candidate.created_at < owner.position_created_at:
                    prefill_candidates.add(candidate.order_id)
                    continue
            edges_by_owner[owner.leg_id].append(candidate.order_id)
            edges_by_candidate[candidate.order_id].append(owner.leg_id)

    for order_id in sorted(prefill_candidates):
        if not edges_by_candidate.get(order_id):
            exclusions[order_id] = "candidate_predates_fill"
    for order_id, owner_leg_ids in sorted(parent_mismatch_owner_ids.items()):
        if edges_by_candidate.get(order_id):
            continue
        exclusions[order_id] = "candidate_parent_id_conflict"
        conflicts.append(
            ProtectionAssignmentConflict(
                reason_code="candidate_parent_id_conflict",
                owner_leg_ids=tuple(sorted(owner_leg_ids)),
                candidate_order_ids=(order_id,),
            )
        )

    assignments: dict[int, str] = {}
    evidence_by_leg: dict[int, dict[str, object]] = {}
    for owner in sorted_owners:
        if owner.leg_id in satisfied_owner_leg_ids:
            continue
        owner_edges = tuple(sorted(edges_by_owner.get(owner.leg_id, ())))
        if (
            owner.lineage_evidence_required
            and not owner.lineage_attestation_fingerprint
            and not (
                owner.direct_identity_permitted
                and len(owner_edges) == 1
                and _explicit_pos_ids(candidate_by_order[owner_edges[0]])
                == (str(owner.pos_id).strip(),)
            )
        ):
            conflicts.append(
                ProtectionAssignmentConflict(
                    reason_code="owner_lineage_evidence_incomplete",
                    owner_leg_ids=(owner.leg_id,),
                    candidate_order_ids=owner_edges,
                )
            )
            continue
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
        if order_id in blocking_candidate_ids:
            continue
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
        match_kind = (
            "explicit_pos_id"
            if _explicit_pos_ids(candidate)
            else (
                "lineage_attested_attached_stop"
                if owner.lineage_attestation_fingerprint
                else "mutual_unique"
            )
        )
        assignments[owner.leg_id] = order_id
        evidence_by_leg[owner.leg_id] = {
            "binding_id": owner.binding_id,
            "candidate_order_id": order_id,
            "match_kind": match_kind,
            "pos_id": owner.pos_id,
            "snapshot_fingerprint": fingerprint,
        }
        if match_kind == "lineage_attested_attached_stop":
            evidence_by_leg[owner.leg_id].update(
                {
                    "intent_id": owner.intent_id,
                    "parent_trigger_order_id": owner.parent_trigger_order_id,
                    "child_regular_order_id": owner.child_regular_order_id,
                    "child_exchange_created_at": _datetime_text(
                        owner.child_exchange_created_at
                    ),
                    "candidate_created_at": _datetime_text(candidate.created_at),
                    "request_fingerprint": owner.request_fingerprint,
                    "owner_baseline_fingerprint": owner.owner_baseline_fingerprint,
                    "lineage_attestation_fingerprint": (
                        owner.lineage_attestation_fingerprint
                    ),
                }
            )

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


def _lineage_edge_refusal(
    owner: ProtectionOwner,
    candidate: ProtectionOrderCandidate,
) -> str | None:
    """Return a closed reason for an attested anonymous candidate edge."""

    if not owner.lineage_attestation_fingerprint:
        return "lineage_attestation_missing"
    if candidate.order_id in {
        str(value).strip() for value in owner.owner_baseline_order_ids
    }:
        return "candidate_in_owner_baseline"
    if not isinstance(candidate.created_at, datetime):
        return "candidate_time_unavailable"
    if not isinstance(owner.intent_created_at, datetime):
        return "intent_time_unavailable"
    if not isinstance(owner.child_exchange_created_at, datetime):
        return "child_time_unavailable"
    if not isinstance(owner.snapshot_observed_at, datetime):
        return "snapshot_time_unavailable"
    if (
        not owner.child_exchange_created_at_raw
        or not candidate.created_at_raw
        or str(candidate.created_at_raw).strip()
        != str(owner.child_exchange_created_at_raw).strip()
    ):
        return "candidate_child_time_mismatch"
    candidate_time = _as_utc_naive(candidate.created_at)
    intent_time = _as_utc_naive(owner.intent_created_at)
    child_time = _as_utc_naive(owner.child_exchange_created_at)
    snapshot_time = _as_utc_naive(owner.snapshot_observed_at)
    if candidate_time != child_time:
        return "candidate_child_time_mismatch"
    if candidate_time < intent_time:
        return "candidate_predates_submission_intent"
    if candidate_time > snapshot_time:
        return "candidate_after_snapshot"
    return None


def _lineage_sequence_refusal(
    owner: ProtectionOwner,
    candidate: ProtectionOrderCandidate,
) -> str | None:
    """Apply common new-authority time bounds to direct and anonymous edges."""

    if not owner.lineage_evidence_required:
        return None
    if not isinstance(candidate.created_at, datetime):
        return "candidate_time_unavailable"
    if not isinstance(owner.intent_created_at, datetime):
        return "intent_time_unavailable"
    if not isinstance(owner.snapshot_observed_at, datetime):
        return "snapshot_time_unavailable"
    candidate_time = _as_utc_naive(candidate.created_at)
    if candidate_time < _as_utc_naive(owner.intent_created_at):
        return "candidate_predates_submission_intent"
    if candidate_time > _as_utc_naive(owner.snapshot_observed_at):
        return "candidate_after_snapshot"
    return None


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


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


def _candidate_order_ids(candidate: ProtectionOrderCandidate) -> tuple[str, ...]:
    values = candidate.order_id_aliases or (candidate.order_id,)
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


def _candidate_shape_conflict(candidate: ProtectionOrderCandidate) -> str | None:
    text_fields = (
        (candidate.instrument_id_aliases or (candidate.instrument_id,), candidate.instrument_id, True),
        (candidate.side_aliases or (candidate.side,), candidate.side, False),
    )
    for aliases, canonical, uppercase in text_fields:
        normalized = {
            (str(value).strip().upper() if uppercase else _normalize_side_alias(value))
            for value in aliases
            if str(value).strip()
        }
        expected = str(canonical).strip().upper() if uppercase else _normalize_side_alias(canonical)
        if normalized != {expected}:
            return "candidate_shape_alias_conflict"
    for aliases, canonical in (
        (candidate.size_aliases or (candidate.size_text,), candidate.size_text),
        (candidate.stop_price_aliases or (candidate.stop_price,), candidate.stop_price),
    ):
        normalized = {_canonical_number(value) for value in aliases if str(value).strip()}
        if None in normalized or normalized != {_canonical_number(canonical)}:
            return "candidate_shape_alias_conflict"
    return None


def _candidate_is_explicit_non_tpsl(
    candidate: ProtectionOrderCandidate,
) -> bool:
    order_types = {
        str(value).strip().upper()
        for value in candidate.order_type_aliases
        if str(value).strip()
    }
    return len(order_types) == 1 and order_types != {"TPSL"}


def _canonical_number(value: Any) -> str | None:
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize_side_alias(value: Any) -> str:
    normalized = str(value).strip().lower()
    return {"buy": "long", "sell": "short"}.get(normalized, normalized)


def _explicit_parent_order_ids(
    candidate: ProtectionOrderCandidate,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(value).strip()
                for value in candidate.explicit_parent_order_ids
                if str(value).strip()
            }
        )
    )


def _explicit_client_order_ids(
    candidate: ProtectionOrderCandidate,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(value).strip()
                for value in candidate.explicit_client_order_ids
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
                "explicit_client_order_ids": list(
                    _explicit_client_order_ids(candidate)
                ),
                "explicit_parent_order_ids": list(
                    _explicit_parent_order_ids(candidate)
                ),
            }
            for candidate in candidates
        ],
        "existing_order_owners": sorted(
            (str(order_id), str(pos_id))
            for order_id, pos_id in existing_order_owners.items()
        ),
        "owners": [_owner_fingerprint_payload(owner) for owner in owners],
        "snapshot_complete": bool(snapshot_complete),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _owner_fingerprint_payload(owner: ProtectionOwner) -> dict[str, object]:
    payload = asdict(owner)
    # Observation time is polling metadata, not ownership evidence. Keeping it
    # here would defeat incident deduplication on every unchanged snapshot.
    payload.pop("snapshot_observed_at", None)
    payload.update(
        {
            "position_created_at": _datetime_text(owner.position_created_at),
            "intent_created_at": _datetime_text(owner.intent_created_at),
            "child_exchange_created_at": _datetime_text(
                owner.child_exchange_created_at
            ),
            "owner_baseline_order_ids": sorted(
                str(value) for value in owner.owner_baseline_order_ids
            ),
        }
    )
    return payload


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
