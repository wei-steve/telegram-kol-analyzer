"""Strict repair of historical entry-protection TPSL ledger rows."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

from telegram_kol_research.models import (
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    TriggerProtectionIntent,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.trigger_protection_assignment import (
    ProtectionAssignmentResult,
    ProtectionOrderCandidate,
    ProtectionOwner,
    assign_trigger_protection_orders,
)


@dataclass(frozen=True, slots=True)
class EntryProtectionLedgerRepairAction:
    event_id: int
    binding_id: int
    leg_id: int
    strategy_instance_id: str | None
    pos_id: str
    instrument_id: str
    side: str
    order_id: str
    purpose: str
    trigger_price: str | None
    size_text: str | None
    evidence: dict[str, Any] = field(default_factory=dict)
    action_id: str = ""

    def __post_init__(self) -> None:
        if self.action_id:
            return
        payload = {
            "event_id": self.event_id,
            "binding_id": self.binding_id,
            "leg_id": self.leg_id,
            "pos_id": self.pos_id,
            "order_id": self.order_id,
            "purpose": self.purpose,
            "trigger_price": self.trigger_price,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        object.__setattr__(
            self,
            "action_id",
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class EntryProtectionLedgerRepairRefusal:
    event_id: int | None
    binding_id: int | None
    pos_id: str | None
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EntryProtectionLedgerRepairAdoptionResult:
    """The single, pure outcome of evaluating a trigger-entry TPSL snapshot."""

    action: EntryProtectionLedgerRepairAction | None = None
    refusal: EntryProtectionLedgerRepairRefusal | None = None

    def __post_init__(self) -> None:
        if self.action is not None and self.refusal is not None:
            raise ValueError("adoption result cannot contain both action and refusal")


@dataclass(frozen=True, slots=True)
class TriggerProtectionIntentAdoptionDeferred:
    """A strict recovery candidate is not yet safely observable."""

    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TriggerProtectionIntentAdoptionResult:
    """Exactly one pure post-baseline trigger-protection outcome."""

    action: EntryProtectionLedgerRepairAction | None = None
    deferred: TriggerProtectionIntentAdoptionDeferred | None = None
    refusal: EntryProtectionLedgerRepairRefusal | None = None

    def __post_init__(self) -> None:
        if sum(value is not None for value in (self.action, self.deferred, self.refusal)) != 1:
            raise ValueError("intent adoption result must have exactly one outcome")


@dataclass(frozen=True, slots=True)
class TriggerProtectionOwnerState:
    """Authoritative fill state for one protection intent's entry leg."""

    execution_order_leg_id: int
    status: str
    attribution_status: str
    pos_id: str | None
    parent_order_id: str | None

    @property
    def is_verified_filled_owner(self) -> bool:
        return (
            str(self.status or "").lower() == "active"
            and str(self.attribution_status or "").lower() == "verified"
            and bool(str(self.pos_id or "").strip())
            and bool(str(self.parent_order_id or "").strip())
        )


@dataclass(frozen=True, slots=True)
class TriggerProtectionLivePosition:
    """Bounded current exchange evidence for one exact trigger-entry position."""

    pos_id: str
    instrument_id: str
    side: str
    size_text: str
    created_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TriggerProtectionLivePositionResult:
    position: TriggerProtectionLivePosition | None = None
    refusal: EntryProtectionLedgerRepairRefusal | None = None

    def __post_init__(self) -> None:
        if (self.position is None) == (self.refusal is None):
            raise ValueError("live position result must have exactly one outcome")


@dataclass(frozen=True, slots=True)
class TriggerProtectionAssignmentContext:
    entry_leg: ExecutionOrderLeg
    intent: TriggerProtectionIntent
    parent_event: ExecutionEvent
    live_position: TriggerProtectionLivePosition


@dataclass(frozen=True, slots=True)
class TriggerProtectionAssignmentPlan:
    actions: dict[int, EntryProtectionLedgerRepairAction]
    refusals: dict[int, EntryProtectionLedgerRepairRefusal]
    assignment: ProtectionAssignmentResult


@dataclass(frozen=True, slots=True)
class EntryProtectionLedgerRepairPlan:
    created_at: datetime
    actions: tuple[EntryProtectionLedgerRepairAction, ...]
    refusals: tuple[EntryProtectionLedgerRepairRefusal, ...]
    fingerprint: str

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)


@dataclass(frozen=True, slots=True)
class EntryProtectionLedgerRepairResult:
    applied: int


def build_trigger_protection_live_position(
    *,
    entry_leg: ExecutionOrderLeg,
    position_rows: list[dict[str, Any]],
    observed_at: datetime,
) -> TriggerProtectionLivePositionResult:
    """Prove one current nonzero position exactly matches a trigger entry leg."""

    binding_id = int(entry_leg.execution_binding_id or 0)
    pos_id = str(entry_leg.pos_id or "").strip()
    request = _loads_json(entry_leg.request_json)
    instrument_id = str(_request_instrument_id(request) or "").upper()
    side = str(_request_side(request) or "").lower()
    expected_size = _request_size_text(request)
    matching = [
        row
        for row in position_rows
        if isinstance(row, dict)
        and str(row.get("posId") or row.get("pos_id") or row.get("id") or "").strip()
        == pos_id
    ]

    def refuse(reason: str, evidence: dict[str, Any] | None = None):
        return TriggerProtectionLivePositionResult(
            refusal=EntryProtectionLedgerRepairRefusal(
                event_id=None,
                binding_id=binding_id or None,
                pos_id=pos_id or None,
                reason=reason,
                evidence=evidence or {},
            )
        )

    if len(matching) == 0:
        return refuse("trigger_protection_position_not_live")
    if len(matching) != 1:
        return refuse(
            "trigger_protection_position_not_unique",
            {"candidate_count": len(matching)},
        )
    row = matching[0]
    current_size = _first_nonzero_text(row, "pos", "size", "availPos", "avail_pos")
    if current_size is None:
        return refuse("trigger_protection_position_not_live")
    current_instrument = str(row.get("instId") or row.get("inst_id") or "").upper()
    if not instrument_id or current_instrument != instrument_id:
        return refuse(
            "trigger_protection_position_instrument_conflict",
            {"instrument_id": current_instrument or None},
        )
    current_side = str(row.get("posSide") or row.get("pos_side") or row.get("side") or "").lower()
    if not side or current_side != side:
        return refuse(
            "trigger_protection_position_side_conflict",
            {"side": current_side or None},
        )
    if expected_size is None or not _same_numeric_text(current_size, expected_size):
        return refuse(
            "trigger_protection_position_size_changed",
            {"current_size": current_size, "expected_size": expected_size},
        )
    created_at = _parse_datetime(row.get("cTime"))
    if created_at is None:
        return refuse("trigger_protection_position_time_unavailable")
    return TriggerProtectionLivePositionResult(
        position=TriggerProtectionLivePosition(
            pos_id=pos_id,
            instrument_id=instrument_id,
            side=side,
            size_text=current_size,
            created_at=created_at,
            observed_at=observed_at,
        )
    )


def upsert_entry_protection_ledger_action(
    session,
    action: EntryProtectionLedgerRepairAction,
    *,
    evidence_source: str,
    seen_at: datetime | None = None,
) -> PositionProtectionLedger | None:
    """Persist one already-planned action without performing exchange I/O."""

    return upsert_protection_ledger_row(
        session,
        venue="deepcoin",
        execution_binding_id=action.binding_id,
        execution_order_leg_id=action.leg_id,
        strategy_instance_id=action.strategy_instance_id,
        pos_id=action.pos_id,
        instrument_id=action.instrument_id,
        side=action.side,
        order_id=action.order_id,
        purpose=action.purpose,
        trigger_price=action.trigger_price,
        size_text=action.size_text,
        status="verified",
        evidence_source=evidence_source,
        evidence=action.evidence,
        seen_at=seen_at,
    )


def finalize_trigger_protection_adoption(
    session,
    *,
    action: EntryProtectionLedgerRepairAction,
    intent: TriggerProtectionIntent,
    seen_at: datetime,
    evidence_source: str = "reconciliation_trigger_protection_intent",
) -> PositionProtectionLedger:
    """Atomically bind one verified child order and its exact ledger identity."""

    entry_leg = session.get(ExecutionOrderLeg, int(action.leg_id))
    if (
        entry_leg is None
        or int(entry_leg.execution_binding_id or 0) != int(action.binding_id)
        or str(entry_leg.pos_id or "").strip() != str(action.pos_id)
        or str(entry_leg.status or "").lower() != "active"
        or str(entry_leg.attribution_status or "").lower() != "verified"
        or int(intent.execution_binding_id or 0) != int(action.binding_id)
        or int(intent.execution_order_leg_id or 0) != int(action.leg_id)
    ):
        raise ValueError("trigger_protection_adoption_identity_invalid")

    from telegram_kol_research.models import PositionProtectionLeg
    from telegram_kol_research.position_protection_legs import (
        bind_verified_exchange_order,
        bind_verified_filled_position_protection,
    )
    from telegram_kol_research.protection_revisions import (
        activate_protection_revision,
    )
    from telegram_kol_research.trigger_protection_intents import (
        transition_trigger_protection_intent,
    )

    bind_verified_filled_position_protection(
        session,
        execution_order_leg_id=int(action.leg_id),
        pos_id=action.pos_id,
    )
    primary = (
        session.query(PositionProtectionLeg)
        .filter(PositionProtectionLeg.execution_order_leg_id == int(action.leg_id))
        .filter(PositionProtectionLeg.role == "primary_stop")
        .filter(PositionProtectionLeg.leg_index == 1)
        .one_or_none()
    )
    if primary is None:
        raise ValueError("trigger_protection_primary_leg_missing")
    logical_order_owners = (
        session.query(PositionProtectionLeg)
        .filter(PositionProtectionLeg.venue == "deepcoin")
        .filter(PositionProtectionLeg.exchange_order_id == action.order_id)
        .all()
    )
    if any(int(row.id) != int(primary.id) for row in logical_order_owners):
        raise ValueError("trigger_protection_logical_order_owned")
    existing_ledger = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == "deepcoin")
        .filter(PositionProtectionLedger.order_id == action.order_id)
        .one_or_none()
    )
    if existing_ledger is not None and (
        int(existing_ledger.execution_binding_id) != int(action.binding_id)
        or int(existing_ledger.execution_order_leg_id) != int(action.leg_id)
        or existing_ledger.strategy_instance_id != action.strategy_instance_id
        or str(existing_ledger.pos_id or "") != str(action.pos_id)
        or str(existing_ledger.instrument_id or "").upper()
        != str(action.instrument_id).upper()
        or str(existing_ledger.side or "").lower() != str(action.side).lower()
    ):
        raise ValueError("protection_ledger_owner_conflict")
    bind_verified_exchange_order(
        session,
        primary,
        exchange_order_id=action.order_id,
        readback_evidence=action.evidence,
    )
    row = upsert_entry_protection_ledger_action(
        session,
        action,
        evidence_source=evidence_source,
        seen_at=seen_at,
    )
    if row is None:
        raise ValueError("trigger_protection_ledger_write_failed")
    transition_trigger_protection_intent(
        session,
        intent,
        recovery_state="adopted",
        adopted_order_id=action.order_id,
    )
    activate_protection_revision(
        session,
        venue="deepcoin",
        execution_binding_id=action.binding_id,
        execution_order_leg_id=action.leg_id,
        strategy_instance_id=action.strategy_instance_id,
        pos_id=action.pos_id,
        source=evidence_source,
        protection_json={
            "order_ids": [action.order_id],
            "rows": [
                {
                    "order_id": action.order_id,
                    "purpose": action.purpose,
                    "trigger_price": action.trigger_price,
                    "size_text": action.size_text,
                }
            ],
        },
    )
    return row


def build_entry_protection_ledger_repair_plan(
    session_factory,
    *,
    deepcoin_client,
    now: datetime | None = None,
    binding_id: int | None = None,
    event_id: int | None = None,
    pos_id: str | None = None,
    include_trigger_entries: bool = False,
    max_event_to_order_seconds: int = 120,
    max_sibling_time_seconds: int = 3,
) -> EntryProtectionLedgerRepairPlan:
    """Build a dry-run repair plan from exchange-returned TPSL order ids."""

    created_at = now or datetime.now(UTC)
    pending_cache: dict[str, list[dict[str, Any]]] = {}
    live_position_rows: list[dict[str, Any]] = []
    live_position_snapshot_error: str | None = None
    if include_trigger_entries:
        try:
            raw_positions = deepcoin_client.list_positions()
        except Exception as exc:
            live_position_snapshot_error = type(exc).__name__
        else:
            if isinstance(raw_positions, list) and all(
                isinstance(row, dict) for row in raw_positions
            ):
                live_position_rows = list(raw_positions)
            else:
                live_position_snapshot_error = "invalid_list_response"
    actions: list[EntryProtectionLedgerRepairAction] = []
    refusals: list[EntryProtectionLedgerRepairRefusal] = []
    with session_factory() as session:
        events_query = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.venue == "deepcoin")
            .filter(ExecutionEvent.action == "set_position_tpsl")
            .filter(ExecutionEvent.reason == "entry_protection")
            .filter(ExecutionEvent.execution_binding_id.isnot(None))
        )
        if binding_id is not None:
            events_query = events_query.filter(
                ExecutionEvent.execution_binding_id == int(binding_id)
            )
        if event_id is not None:
            events_query = events_query.filter(ExecutionEvent.id == int(event_id))
        clean_pos_id = str(pos_id or "").strip()
        if clean_pos_id:
            events_query = events_query.filter(ExecutionEvent.pos_id == clean_pos_id)
        events = events_query.order_by(ExecutionEvent.id.asc()).all()
        existing_ledger_rows = session.query(PositionProtectionLedger).all()
        existing_order_ids = {
            str(row.order_id)
            for row in existing_ledger_rows
            if str(row.order_id or "")
        }
        existing_order_associations = {
            _protection_association(row) for row in existing_ledger_rows
        }
        for event in events:
            planned, refusal = _plan_event_repair(
                session,
                event,
                deepcoin_client=deepcoin_client,
                pending_cache=pending_cache,
                existing_order_ids=existing_order_ids,
                max_event_to_order_seconds=max_event_to_order_seconds,
                max_sibling_time_seconds=max_sibling_time_seconds,
            )
            if refusal is not None:
                refusals.append(refusal)
                continue
            actions.extend(planned)
            existing_order_ids.update(action.order_id for action in planned)
            existing_order_associations.update(
                _action_protection_association(action) for action in planned
            )
        if include_trigger_entries:
            trigger_events_query = (
                session.query(ExecutionEvent)
                .filter(ExecutionEvent.venue == "deepcoin")
                .filter(ExecutionEvent.action == "create_trigger_entry")
                .filter(ExecutionEvent.execution_binding_id.isnot(None))
            )
            if binding_id is not None:
                trigger_events_query = trigger_events_query.filter(
                    ExecutionEvent.execution_binding_id == int(binding_id)
                )
            if event_id is not None:
                trigger_events_query = trigger_events_query.filter(
                    ExecutionEvent.id == int(event_id)
                )
            trigger_events = trigger_events_query.order_by(ExecutionEvent.id.asc()).all()
            for event in trigger_events:
                planned, refusal = _plan_trigger_entry_repair(
                    session,
                    event,
                    deepcoin_client=deepcoin_client,
                    pending_cache=pending_cache,
                    existing_order_ids=existing_order_ids,
                    existing_order_associations=existing_order_associations,
                    live_position_rows=live_position_rows,
                    live_position_snapshot_error=live_position_snapshot_error,
                    observed_at=created_at,
                )
                if clean_pos_id:
                    planned = [row for row in planned if row.pos_id == clean_pos_id]
                    if refusal is not None and refusal.pos_id != clean_pos_id:
                        refusal = None
                if refusal is not None:
                    refusals.append(refusal)
                    continue
                actions.extend(planned)
                existing_order_ids.update(action.order_id for action in planned)
                existing_order_associations.update(
                    _action_protection_association(action) for action in planned
                )

    actions_tuple = tuple(sorted(actions, key=lambda row: (row.binding_id, row.order_id)))
    refusals_tuple = tuple(
        sorted(
            refusals,
            key=lambda row: (
                -1 if row.binding_id is None else row.binding_id,
                -1 if row.event_id is None else row.event_id,
                row.reason,
            ),
        )
    )
    return EntryProtectionLedgerRepairPlan(
        created_at=created_at,
        actions=actions_tuple,
        refusals=refusals_tuple,
        fingerprint=_plan_fingerprint(actions_tuple, refusals_tuple),
    )


def apply_entry_protection_ledger_repair_plan(
    session_factory,
    plan: EntryProtectionLedgerRepairPlan,
    *,
    action_id: str,
    pos_id: str,
    expected_fingerprint: str,
    confirmation_token: str,
    seen_at: datetime | None = None,
) -> EntryProtectionLedgerRepairResult:
    """Apply one exact reviewed action guarded by a single-use token."""

    if not expected_fingerprint:
        raise ValueError("expected_fingerprint is required")
    if expected_fingerprint != plan.fingerprint:
        raise ValueError("repair plan fingerprint mismatch")
    selected = [
        action
        for action in plan.actions
        if action.action_id == str(action_id)
        and action.pos_id == str(pos_id)
        and action.evidence.get("match") in {
            "response_anchored_order",
            "verified_filled_leg_unique_child",
        }
    ]
    if len(selected) != 1:
        raise ValueError("exactly one supported repair action is required")
    action = selected[0]
    consumed_at = seen_at or datetime.now(UTC)
    from telegram_kol_research.repair_confirmation import (
        consume_repair_confirmation_token,
    )

    consume_repair_confirmation_token(
        session_factory,
        confirmation_token=confirmation_token,
        action_kind="entry_protection_ledger_repair",
        action_id=action.action_id,
        pos_id=action.pos_id,
        consumed_at=consumed_at,
    )
    with session_factory() as session:
        if action.evidence.get("match") == "verified_filled_leg_unique_child":
            intent = (
                session.query(TriggerProtectionIntent)
                .filter(TriggerProtectionIntent.venue == "deepcoin")
                .filter(
                    TriggerProtectionIntent.execution_binding_id
                    == int(action.binding_id)
                )
                .filter(
                    TriggerProtectionIntent.execution_order_leg_id
                    == int(action.leg_id)
                )
                .one_or_none()
            )
            if intent is None or str(intent.recovery_state or "") != "failed":
                raise ValueError("failed trigger protection intent is required")
            row = finalize_trigger_protection_adoption(
                session,
                action=action,
                intent=intent,
                seen_at=consumed_at,
                evidence_source="supervised_failed_trigger_intent_repair",
            )
        else:
            row = upsert_entry_protection_ledger_action(
                session,
                action,
                evidence_source="entry_protection_event_repair",
                seen_at=consumed_at,
            )
        session.commit()
    return EntryProtectionLedgerRepairResult(applied=1 if row is not None else 0)


def _plan_event_repair(
    session,
    event: ExecutionEvent,
    *,
    deepcoin_client,
    pending_cache: dict[str, list[dict[str, Any]]],
    existing_order_ids: set[str],
    max_event_to_order_seconds: int,
    max_sibling_time_seconds: int,
) -> tuple[list[EntryProtectionLedgerRepairAction], EntryProtectionLedgerRepairRefusal | None]:
    binding_id = int(event.execution_binding_id or 0)
    pos_id = str(event.pos_id or "").strip()
    request = _loads_json(event.request_json)
    response = _loads_json(event.response_json)
    expected_rows = _expected_protection_rows(request)
    instrument_id = _request_instrument_id(request) or _event_instrument_id(event)
    side = _request_side(request) or str(event.side or "").lower()
    if not binding_id or not pos_id or not expected_rows or not instrument_id or not side:
        return [], _refusal(event, "missing_event_identity")
    request_pos_id = _request_pos_id(request)
    if request_pos_id and request_pos_id != pos_id:
        return [], _refusal(
            event,
            "request_position_mismatch",
            {"request_pos_id": request_pos_id},
        )
    leg = _verified_entry_leg(session, binding_id=binding_id, pos_id=pos_id)
    if leg is None:
        return [], _refusal(event, "verified_entry_leg_missing")
    response_order_ids = _response_order_ids(response)
    if not response_order_ids:
        return [], _refusal(event, "response_order_id_missing")
    pending_rows = _cached_pending_tpsl_rows(
        pending_cache, deepcoin_client=deepcoin_client, inst_id=instrument_id
    )
    pending_by_order_id = {
        order_id: row
        for row in pending_rows
        if (order_id := _row_order_id(row))
        and _row_matches_exchange_identity(
            row, instrument_id=instrument_id, side=side, pos_id=pos_id
        )
    }
    returned_rows = [
        (order_id, pending_by_order_id.get(order_id)) for order_id in response_order_ids
    ]
    if any(row is None for _, row in returned_rows):
        return [], _refusal(
            event,
            "returned_order_not_pending",
            {"response_order_ids": response_order_ids},
        )
    event_time = _coerce_utc_naive(event.created_at)
    returned_times = [_row_time(row) for _, row in returned_rows if row is not None]
    if not returned_times or any(time is None for time in returned_times):
        return [], _refusal(event, "returned_order_time_missing")
    if any(
        abs((time - event_time).total_seconds()) > max_event_to_order_seconds
        for time in returned_times
        if time is not None
    ):
        return [], _refusal(event, "returned_order_time_mismatch")

    expected_by_purpose = {row["purpose"]: row for row in expected_rows}
    planned: list[EntryProtectionLedgerRepairAction] = []
    matched_purposes: set[str] = set()
    returned_order_id_set = {order_id for order_id, _ in returned_rows}
    for order_id, row in returned_rows:
        if row is None:
            continue
        purpose = _row_matching_expected_purpose(row, expected_rows)
        if purpose is None:
            return [], _refusal(
                event,
                "returned_order_price_mismatch",
                {"order_id": order_id},
            )
        matched_purposes.add(purpose)
        if order_id not in existing_order_ids:
            planned.append(
                _action_from_expected(
                    event,
                    leg,
                    expected_by_purpose[purpose],
                    row,
                    order_id=order_id,
                    match="response_anchored_order",
                    anchor_order_ids=response_order_ids,
                )
            )

    missing_purposes = [
        purpose for purpose in expected_by_purpose if purpose not in matched_purposes
    ]
    if missing_purposes:
        return [], _refusal(
            event,
            "response_order_id_missing_for_purpose",
            {
                "missing_purposes": missing_purposes,
                "anchor_order_ids": response_order_ids,
            },
        )
    return planned, None


def _plan_trigger_entry_repair(
    session,
    event: ExecutionEvent,
    *,
    deepcoin_client,
    pending_cache: dict[str, list[dict[str, Any]]],
    existing_order_ids: set[str],
    existing_order_associations: set[tuple[str, str, int, int, str, str]],
    live_position_rows: list[dict[str, Any]],
    live_position_snapshot_error: str | None,
    observed_at: datetime,
) -> tuple[list[EntryProtectionLedgerRepairAction], EntryProtectionLedgerRepairRefusal | None]:
    binding_id = int(event.execution_binding_id or 0)
    request = _loads_json(event.request_json)
    instrument_id = _request_instrument_id(request) or _event_instrument_id(event)
    if not binding_id or not instrument_id:
        return [], _refusal(event, "missing_trigger_entry_identity")
    leg = _verified_trigger_entry_leg(
        session,
        binding_id=binding_id,
        order_id=str(event.order_id or ""),
        client_order_id=str(event.client_order_id or ""),
    )
    if leg is None:
        return [], _refusal(event, "verified_trigger_entry_leg_missing")
    exact_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.venue == "deepcoin")
        .filter(PositionProtectionLedger.execution_binding_id == binding_id)
        .filter(PositionProtectionLedger.execution_order_leg_id == int(leg.id))
        .filter(PositionProtectionLedger.pos_id == str(leg.pos_id or ""))
        .filter(PositionProtectionLedger.status == "verified")
        .all()
    )
    if exact_rows:
        exact_order_ids = sorted({str(row.order_id) for row in exact_rows})
        if all(
            {
                association
                for association in existing_order_associations
                if association[0] == order_id
            }
            == {
                _protection_association(row)
                for row in exact_rows
                if str(row.order_id) == order_id
            }
            for order_id in exact_order_ids
        ):
            return [], None
        return [], EntryProtectionLedgerRepairRefusal(
            event_id=int(event.id) if event.id is not None else None,
            binding_id=binding_id,
            pos_id=str(leg.pos_id or "") or None,
            reason="trigger_entry_tpsl_identity_conflict",
            evidence={"candidate_order_ids": exact_order_ids},
        )
    if live_position_snapshot_error is not None:
        return [], EntryProtectionLedgerRepairRefusal(
            event_id=int(event.id) if event.id is not None else None,
            binding_id=binding_id,
            pos_id=str(leg.pos_id or "") or None,
            reason="trigger_protection_position_snapshot_unavailable",
            evidence={"snapshot_error": live_position_snapshot_error},
        )
    live_position_result = build_trigger_protection_live_position(
        entry_leg=leg,
        position_rows=live_position_rows,
        observed_at=observed_at,
    )
    if live_position_result.refusal is not None:
        refusal = live_position_result.refusal
        return [], replace(
            refusal,
            event_id=int(event.id) if event.id is not None else None,
        )
    live_position = live_position_result.position
    assert live_position is not None
    pending_rows = _cached_pending_tpsl_rows(
        pending_cache, deepcoin_client=deepcoin_client, inst_id=instrument_id
    )
    intent = (
        session.query(TriggerProtectionIntent)
        .filter(TriggerProtectionIntent.venue == "deepcoin")
        .filter(TriggerProtectionIntent.execution_binding_id == binding_id)
        .filter(TriggerProtectionIntent.execution_order_leg_id == int(leg.id))
        .one_or_none()
    )
    if intent is not None and str(intent.recovery_state or "") == "failed":
        existing_intents = (
            session.query(TriggerProtectionIntent)
            .filter(TriggerProtectionIntent.venue == "deepcoin")
            .order_by(TriggerProtectionIntent.id.asc())
            .all()
        )
        intent_leg_ids = {
            int(row.execution_order_leg_id)
            for row in existing_intents
            if row.execution_order_leg_id is not None
        }
        intent_legs = {
            int(row.id): row
            for row in session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.id.in_(intent_leg_ids))
            .all()
        }
        intent_requests = {
            int(row.id): _loads_json(
                intent_legs[int(row.execution_order_leg_id)].request_json
            )
            for row in existing_intents
            if row.id is not None
            and int(row.execution_order_leg_id) in intent_legs
        }
        owner_states = {
            int(row.id): TriggerProtectionOwnerState(
                execution_order_leg_id=int(owner_leg.id),
                status=str(owner_leg.status or ""),
                attribution_status=str(owner_leg.attribution_status or ""),
                pos_id=str(owner_leg.pos_id) if owner_leg.pos_id else None,
                parent_order_id=(
                    str(owner_leg.order_id) if owner_leg.order_id else None
                ),
            )
            for row in existing_intents
            if row.id is not None
            and (
                owner_leg := intent_legs.get(int(row.execution_order_leg_id))
            )
            is not None
        }
        result = plan_trigger_protection_intent_adoption(
            session,
            entry_leg=leg,
            intent=intent,
            parent_event=event,
            pending_tpsl_rows=pending_rows,
            history_tpsl_rows=[],
            existing_ledger_rows=session.query(PositionProtectionLedger).all(),
            existing_intents=existing_intents,
            existing_intent_requests=intent_requests,
            existing_intent_owner_states=owner_states,
            live_position=live_position,
            history_time_range_start=event.created_at,
            history_time_range_end=datetime.now(UTC),
        )
        if result.action is not None:
            evidence = dict(result.action.evidence)
            evidence["sibling_states"] = _bounded_sibling_owner_states(
                intent=intent,
                existing_intents=existing_intents,
                owner_states=owner_states,
            )
            evidence["live_position"] = _bounded_live_position_evidence(
                live_position
            )
            return [replace(result.action, evidence=evidence)], None
        if result.refusal is not None:
            return [], result.refusal
        return [], EntryProtectionLedgerRepairRefusal(
            event_id=int(event.id) if event.id is not None else None,
            binding_id=binding_id,
            pos_id=str(leg.pos_id or "") or None,
            reason=(
                result.deferred.reason
                if result.deferred is not None
                else "trigger_protection_repair_deferred"
            ),
            evidence=(
                result.deferred.evidence if result.deferred is not None else {}
            ),
        )
    result = plan_verified_trigger_entry_protection_adoption(
        session,
        entry_leg=leg,
        event=event,
        pending_tpsl_rows=pending_rows,
        existing_order_ids=existing_order_ids,
        existing_order_associations=existing_order_associations,
        live_position=live_position,
    )
    return ([result.action] if result.action is not None else []), result.refusal


def _bounded_live_position_evidence(
    position: TriggerProtectionLivePosition,
) -> dict[str, Any]:
    return {
        "pos_id": position.pos_id,
        "instrument_id": position.instrument_id,
        "side": position.side,
        "size_text": position.size_text,
        "created_at": position.created_at.isoformat(),
        "observed_at": position.observed_at.isoformat(),
    }


def _bounded_sibling_owner_states(
    *,
    intent: TriggerProtectionIntent,
    existing_intents: list[TriggerProtectionIntent],
    owner_states: dict[int, TriggerProtectionOwnerState],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for other in existing_intents:
        if other.id is None or other.id == intent.id:
            continue
        if int(other.execution_binding_id or 0) != int(intent.execution_binding_id or 0):
            continue
        state = owner_states.get(int(other.id))
        if state is None:
            continue
        rows.append(
            {
                "execution_order_leg_id": int(state.execution_order_leg_id),
                "status": str(state.status or "")[:32],
                "attribution_status": str(state.attribution_status or "")[:32],
                "has_pos_id": bool(str(state.pos_id or "").strip()),
            }
        )
    return sorted(rows, key=lambda row: row["execution_order_leg_id"])[:20]


def plan_verified_trigger_entry_protection_adoption(
    session,
    *,
    entry_leg: ExecutionOrderLeg,
    event: ExecutionEvent,
    pending_tpsl_rows: list[dict[str, Any]],
    existing_order_ids: set[str],
    existing_order_associations: set[tuple[str, str, int, int, str, str]],
    live_position: TriggerProtectionLivePosition,
) -> EntryProtectionLedgerRepairAdoptionResult:
    """Purely match one verified trigger entry to one pending TPSL order.

    ``session`` is deliberately accepted for call-site consistency but is not
    read or written; callers must supply the verified leg and pending snapshot.
    """

    del session
    binding_id = int(event.execution_binding_id or 0)
    pos_id = str(entry_leg.pos_id or "").strip()
    request = _loads_json(event.request_json)
    expected_rows = _expected_protection_rows(request)
    instrument_id = _request_instrument_id(request) or _event_instrument_id(event)
    side = _request_side(request) or str(event.side or "").lower()
    if (
        not binding_id
        or binding_id != int(entry_leg.execution_binding_id or 0)
        or event.action != "create_trigger_entry"
        or event.venue != "deepcoin"
        or entry_leg.venue != "deepcoin"
        or entry_leg.purpose != "entry"
        or entry_leg.order_kind != "trigger_limit"
        or entry_leg.attribution_status != "verified"
        or entry_leg.status != "active"
        or not pos_id
        or not expected_rows
        or not instrument_id
        or not side
        or not _same_nonempty_text(event.order_id, entry_leg.order_id)
        or not _same_nonempty_text(event.client_order_id, entry_leg.client_order_id)
        or live_position.pos_id != pos_id
        or live_position.instrument_id.upper() != instrument_id.upper()
        or live_position.side.lower() != side.lower()
        or not _same_numeric_text(
            live_position.size_text, _request_size_text(request) or ""
        )
    ):
        return EntryProtectionLedgerRepairAdoptionResult(
            refusal=_refusal(event, "missing_trigger_entry_identity")
        )

    candidates = [
        row
        for row in pending_tpsl_rows
        if isinstance(row, dict)
        and _row_order_id(row)
        and _row_matches_exchange_identity(
            row, instrument_id=instrument_id, side=side, pos_id=pos_id
        )
        and _row_matches_trigger_entry_expected_protection(row, expected_rows)
        and _same_size_text(_row_size_text(row), _request_size_text(request))
    ]
    for candidate in candidates:
        candidate_created_at = _row_creation_time(candidate)
        if candidate_created_at is None:
            return EntryProtectionLedgerRepairAdoptionResult(
                refusal=EntryProtectionLedgerRepairRefusal(
                    event_id=int(event.id) if event.id is not None else None,
                    binding_id=binding_id,
                    pos_id=pos_id,
                    reason="trigger_protection_candidate_time_unavailable",
                    evidence={"candidate_order_ids": [_row_order_id(candidate)]},
                )
            )
        if candidate_created_at < _coerce_utc_naive(live_position.created_at):
            return EntryProtectionLedgerRepairAdoptionResult(
                refusal=EntryProtectionLedgerRepairRefusal(
                    event_id=int(event.id) if event.id is not None else None,
                    binding_id=binding_id,
                    pos_id=pos_id,
                    reason="trigger_protection_candidate_predates_fill",
                    evidence={"candidate_order_ids": [_row_order_id(candidate)]},
                )
            )
    unique_order_ids = sorted({_row_order_id(row) for row in candidates if _row_order_id(row)})
    if len(unique_order_ids) != 1:
        return EntryProtectionLedgerRepairAdoptionResult(
            refusal=EntryProtectionLedgerRepairRefusal(
                event_id=int(event.id) if event.id is not None else None,
                binding_id=binding_id,
                pos_id=pos_id,
                reason=(
                    "trigger_entry_tpsl_not_unique"
                    if unique_order_ids
                    else "trigger_entry_tpsl_missing"
                ),
                evidence={
                    "candidate_order_ids": unique_order_ids,
                    "trigger_entry_order_id": event.order_id,
                    "size_text": _request_size_text(request),
                },
            )
        )
    order_id = unique_order_ids[0]
    if order_id in existing_order_ids:
        expected_association = (
            order_id,
            "deepcoin",
            binding_id,
            int(entry_leg.id),
            pos_id,
            "verified",
        )
        candidate_associations = {
            association
            for association in existing_order_associations
            if association[0] == order_id
        }
        if candidate_associations == {expected_association}:
            return EntryProtectionLedgerRepairAdoptionResult()
        return EntryProtectionLedgerRepairAdoptionResult(
            refusal=EntryProtectionLedgerRepairRefusal(
                event_id=int(event.id) if event.id is not None else None,
                binding_id=binding_id,
                pos_id=pos_id,
                reason="trigger_entry_tpsl_identity_conflict",
                evidence={
                    "candidate_order_ids": [order_id],
                    "trigger_entry_order_id": event.order_id,
                    "size_text": _request_size_text(request),
                },
            )
        )
    row = next(row for row in candidates if _row_order_id(row) == order_id)
    return EntryProtectionLedgerRepairAdoptionResult(
        action=EntryProtectionLedgerRepairAction(
            event_id=int(event.id),
            binding_id=binding_id,
            leg_id=int(entry_leg.id),
            strategy_instance_id=entry_leg.strategy_instance_id,
            pos_id=pos_id,
            instrument_id=instrument_id,
            side=side,
            order_id=order_id,
            purpose="combined",
            trigger_price=None,
            size_text=_row_size_text(row) or _request_size_text(request),
            evidence={
                "match": "trigger_entry_unique_expected_protection_shape",
                "execution_event_id": int(event.id),
                "trigger_entry_order_id": event.order_id,
                "take_profit": _expected_price(expected_rows, "take_profit"),
                "stop_loss": _expected_price(expected_rows, "stop_loss"),
                "live_position": _bounded_live_position_evidence(live_position),
            },
        )
    )


def plan_trigger_protection_intent_assignments(
    *,
    contexts: tuple[TriggerProtectionAssignmentContext, ...],
    pending_tpsl_rows: list[dict[str, Any]],
    existing_ledger_rows: list[PositionProtectionLedger],
    snapshot_complete: bool,
) -> TriggerProtectionAssignmentPlan:
    """Plan all anonymous stop-only adoptions from one coherent snapshot."""

    owners: list[ProtectionOwner] = []
    contexts_by_leg: dict[int, TriggerProtectionAssignmentContext] = {}
    refusals: dict[int, EntryProtectionLedgerRepairRefusal] = {}
    baseline_order_ids: set[str] = set()
    for context in sorted(contexts, key=lambda item: int(item.entry_leg.id or 0)):
        leg = context.entry_leg
        intent = context.intent
        event = context.parent_event
        live_position = context.live_position
        leg_id = int(leg.id or 0)
        binding_id = int(leg.execution_binding_id or 0)
        pos_id = str(leg.pos_id or "").strip()
        request = _loads_json(leg.request_json)
        event_request = _loads_json(event.request_json)
        expected_rows = _expected_protection_rows(request)
        expected_purposes = {str(row.get("purpose") or "") for row in expected_rows}
        instrument_id = _request_instrument_id(request)
        side = _request_side(request)
        size_text = _request_size_text(request)
        expected_parent = str(intent.parent_trigger_order_id or "").strip()
        baseline_ids = _baseline_order_ids(intent.pre_submit_tpsl_baseline_json)
        if baseline_ids is None:
            refusals[leg_id] = _assignment_refusal(
                event,
                binding_id,
                pos_id,
                "trigger_protection_baseline_invalid",
            )
            continue
        baseline_order_ids.update(baseline_ids)
        if expected_purposes != {"stop_loss"}:
            continue
        if (
            not leg_id
            or not binding_id
            or int(intent.execution_binding_id or 0) != binding_id
            or int(intent.execution_order_leg_id or 0) != leg_id
            or str(intent.venue or "").lower() != "deepcoin"
            or str(leg.venue or "").lower() != "deepcoin"
            or str(leg.purpose or "") != "entry"
            or str(leg.order_kind or "") != "trigger_limit"
            or str(leg.attribution_status or "") != "verified"
            or str(leg.status or "").lower() != "active"
            or not pos_id
            or not instrument_id
            or not side
            or not size_text
            or not expected_parent
            or not _same_nonempty_text(expected_parent, leg.order_id)
            or not _same_nonempty_text(expected_parent, event.order_id)
            or event.action != "create_trigger_entry"
            or str(event.venue or "").lower() != "deepcoin"
            or int(event.execution_binding_id or 0) != binding_id
            or _trigger_protection_fingerprint(request) != intent.request_fingerprint
            or _trigger_protection_fingerprint(event_request)
            != intent.request_fingerprint
            or live_position.pos_id != pos_id
            or live_position.instrument_id.upper() != instrument_id.upper()
            or live_position.side.lower() != side.lower()
            or not _same_numeric_text(live_position.size_text, size_text)
        ):
            refusals[leg_id] = _assignment_refusal(
                event,
                binding_id,
                pos_id,
                "trigger_protection_intent_identity_invalid",
            )
            continue
        owners.append(
            ProtectionOwner(
                leg_id=leg_id,
                binding_id=binding_id,
                pos_id=pos_id,
                instrument_id=instrument_id,
                side=side,
                size_text=size_text,
                stop_price=str(expected_rows[0]["trigger_price"]),
                position_created_at=live_position.created_at,
            )
        )
        contexts_by_leg[leg_id] = context

    candidates: list[ProtectionOrderCandidate] = []
    candidate_rows: dict[str, dict[str, Any]] = {}
    for row in pending_tpsl_rows:
        if not isinstance(row, dict):
            continue
        order_id = _row_order_id(row)
        instrument_id = _request_instrument_id(row)
        side = _request_side(row)
        size_text = _row_size_text(row)
        stop_price = _first_nonzero_text(
            row,
            "slTriggerPx",
            "slTriggerPrice",
            "closeSLTriggerPrice",
        )
        take_profit = _first_nonzero_text(
            row,
            "tpTriggerPx",
            "tpTriggerPrice",
            "closeTPTriggerPrice",
        )
        if (
            not order_id
            or order_id in baseline_order_ids
            or str(row.get("triggerOrderType") or "TPSL").upper() != "TPSL"
            or not instrument_id
            or not side
            or not size_text
            or not stop_price
            or take_profit is not None
        ):
            continue
        candidates.append(
            ProtectionOrderCandidate(
                order_id=order_id,
                instrument_id=instrument_id,
                side=side,
                size_text=size_text,
                stop_price=stop_price,
                created_at=_row_creation_time(row),
                explicit_pos_ids=tuple(sorted(_row_position_ids(row))),
            )
        )
        candidate_rows[order_id] = row

    existing_order_owners: dict[str, str] = {}
    conflicted_order_ids: set[str] = set()
    for ledger in existing_ledger_rows:
        if (
            str(ledger.venue or "").lower() != "deepcoin"
            or str(ledger.status or "").lower() != "verified"
            or not str(ledger.order_id or "").strip()
            or not str(ledger.pos_id or "").strip()
        ):
            continue
        order_id = str(ledger.order_id).strip()
        pos_id = str(ledger.pos_id).strip()
        previous = existing_order_owners.get(order_id)
        if previous is not None and previous != pos_id:
            conflicted_order_ids.add(order_id)
        existing_order_owners[order_id] = pos_id
    for order_id in conflicted_order_ids:
        existing_order_owners[order_id] = "__immutable_owner_conflict__"

    assignment = assign_trigger_protection_orders(
        owners=tuple(owners),
        candidates=tuple(candidates),
        existing_order_owners=existing_order_owners,
        snapshot_complete=snapshot_complete,
    )
    actions: dict[int, EntryProtectionLedgerRepairAction] = {}
    for leg_id, order_id in assignment.assignments.items():
        context = contexts_by_leg[leg_id]
        owner = next(owner for owner in owners if owner.leg_id == leg_id)
        row = candidate_rows[order_id]
        actions[leg_id] = EntryProtectionLedgerRepairAction(
            event_id=int(context.parent_event.id),
            binding_id=owner.binding_id,
            leg_id=leg_id,
            strategy_instance_id=context.entry_leg.strategy_instance_id,
            pos_id=owner.pos_id,
            instrument_id=owner.instrument_id,
            side=owner.side,
            order_id=order_id,
            purpose="stop_loss",
            trigger_price=owner.stop_price,
            size_text=_row_size_text(row) or owner.size_text,
            evidence={
                "assignment_method": "account_wide_mutual_unique",
                "intent_id": (
                    int(context.intent.id)
                    if context.intent.id is not None
                    else None
                ),
                "match": "verified_filled_leg_unique_child",
                "parent_trigger_order_id": context.intent.parent_trigger_order_id,
                "snapshot_fingerprint": assignment.snapshot_fingerprint,
                "source": "pending",
            },
        )
    for conflict in assignment.conflicts:
        for leg_id in conflict.owner_leg_ids:
            context = contexts_by_leg.get(leg_id)
            if context is None or leg_id in actions or leg_id in refusals:
                continue
            refusals[leg_id] = _assignment_refusal(
                context.parent_event,
                int(context.entry_leg.execution_binding_id),
                str(context.entry_leg.pos_id),
                f"trigger_{conflict.reason_code}",
                list(conflict.candidate_order_ids),
                extra_evidence={
                    "snapshot_fingerprint": assignment.snapshot_fingerprint
                },
            )
    return TriggerProtectionAssignmentPlan(
        actions=dict(sorted(actions.items())),
        refusals=dict(sorted(refusals.items())),
        assignment=assignment,
    )


def _assignment_refusal(
    event: ExecutionEvent,
    binding_id: int,
    pos_id: str,
    reason: str,
    candidate_ids: list[str] | None = None,
    *,
    extra_evidence: dict[str, Any] | None = None,
) -> EntryProtectionLedgerRepairRefusal:
    evidence: dict[str, Any] = {
        "candidate_order_ids": sorted(set(candidate_ids or []))
    }
    evidence.update(extra_evidence or {})
    return EntryProtectionLedgerRepairRefusal(
        event_id=int(event.id) if event.id is not None else None,
        binding_id=binding_id or None,
        pos_id=pos_id or None,
        reason=reason,
        evidence=evidence,
    )


def plan_trigger_protection_intent_adoption(
    session,
    *,
    entry_leg: ExecutionOrderLeg,
    intent: TriggerProtectionIntent,
    parent_event: ExecutionEvent,
    pending_tpsl_rows: list[dict[str, Any]],
    history_tpsl_rows: list[dict[str, Any]],
    existing_ledger_rows: list[PositionProtectionLedger],
    existing_intents: list[TriggerProtectionIntent],
    live_position: TriggerProtectionLivePosition,
    existing_intent_requests: dict[int, dict[str, Any]] | None = None,
    existing_intent_owner_states: dict[int, TriggerProtectionOwnerState] | None = None,
    history_time_range_start: datetime | None = None,
    history_time_range_end: datetime | None = None,
) -> TriggerProtectionIntentAdoptionResult:
    """Plan one post-baseline attached-protection adoption without I/O or writes.

    History is deliberately weaker than pending visibility: a history-only row
    needs an explicit parent reference and a caller-calibrated time range.
    """

    del session
    binding_id = int(entry_leg.execution_binding_id or 0)
    pos_id = str(entry_leg.pos_id or "").strip()
    request = _loads_json(entry_leg.request_json)
    event_request = _loads_json(parent_event.request_json)
    expected_rows = _expected_protection_rows(request)
    instrument_id = _request_instrument_id(request)
    side = _request_side(request)
    expected_parent = str(intent.parent_trigger_order_id or "").strip()
    expected_purposes = {str(row.get("purpose") or "") for row in expected_rows}
    if expected_purposes not in ({"take_profit", "stop_loss"}, {"stop_loss"}):
        return _intent_refusal(
            parent_event,
            binding_id,
            pos_id,
            "trigger_protection_candidate_protection_conflict",
        )
    if (
        not binding_id
        or int(intent.execution_binding_id or 0) != binding_id
        or int(intent.execution_order_leg_id or 0) != int(entry_leg.id or 0)
        or str(intent.venue or "").lower() != "deepcoin"
        or str(entry_leg.venue or "").lower() != "deepcoin"
        or str(entry_leg.purpose or "") != "entry"
        or str(entry_leg.order_kind or "") != "trigger_limit"
        or str(entry_leg.attribution_status or "") != "verified"
        or str(entry_leg.status or "").lower() != "active"
        or not pos_id
        or not expected_rows
        or not instrument_id
        or not side
        or not expected_parent
        or not _same_nonempty_text(expected_parent, entry_leg.order_id)
        or not _same_nonempty_text(expected_parent, parent_event.order_id)
        or parent_event.action != "create_trigger_entry"
        or str(parent_event.venue or "").lower() != "deepcoin"
        or int(parent_event.execution_binding_id or 0) != binding_id
        or _trigger_protection_fingerprint(request) != intent.request_fingerprint
        or _trigger_protection_fingerprint(event_request) != intent.request_fingerprint
        or live_position.pos_id != pos_id
        or live_position.instrument_id.upper() != instrument_id.upper()
        or live_position.side.lower() != side.lower()
        or not _same_numeric_text(
            live_position.size_text, _request_size_text(request) or ""
        )
    ):
        return _intent_refusal(parent_event, binding_id, pos_id, "trigger_protection_intent_identity_invalid")

    baseline_ids = _baseline_order_ids(intent.pre_submit_tpsl_baseline_json)
    if baseline_ids is None:
        return _intent_refusal(parent_event, binding_id, pos_id, "trigger_protection_baseline_invalid")
    anonymous_stop_key = _anonymous_stop_request_key(request)
    sibling_can_own_anonymous_stop = anonymous_stop_key is not None and any(
        _other_intent_can_own_anonymous_stop(
            intent=intent,
            other_intent=other_intent,
            anonymous_stop_key=anonymous_stop_key,
            existing_intent_requests=existing_intent_requests,
            existing_intent_owner_states=existing_intent_owner_states,
        )
        for other_intent in existing_intents
    )
    if expected_purposes == {"stop_loss"} and not sibling_can_own_anonymous_stop:
        global_plan = plan_trigger_protection_intent_assignments(
            contexts=(
                TriggerProtectionAssignmentContext(
                    entry_leg=entry_leg,
                    intent=intent,
                    parent_event=parent_event,
                    live_position=live_position,
                ),
            ),
            pending_tpsl_rows=pending_tpsl_rows,
            existing_ledger_rows=existing_ledger_rows,
            snapshot_complete=True,
        )
        action = global_plan.actions.get(int(entry_leg.id or 0))
        if action is not None:
            return TriggerProtectionIntentAdoptionResult(action=action)
        refusal = global_plan.refusals.get(int(entry_leg.id or 0))
        if (
            refusal is not None
            and refusal.reason
            == "trigger_protection_assignment_not_mutual_unique"
        ):
            return _intent_refusal(
                parent_event,
                binding_id,
                pos_id,
                "trigger_protection_candidate_not_unique",
                list(refusal.evidence.get("candidate_order_ids") or []),
            )
    candidates: list[tuple[dict[str, Any], str]] = []
    prefill_candidate_ids: list[str] = []
    pending_order_ids = {
        _row_order_id(row)
        for row in pending_tpsl_rows
        if isinstance(row, dict) and _row_order_id(row)
    }
    for source, rows in (("pending", pending_tpsl_rows), ("history", history_tpsl_rows)):
        for row in rows:
            if not isinstance(row, dict) or not _row_order_id(row):
                continue
            if source == "history" and _row_order_id(row) in pending_order_ids:
                continue
            if (
                _row_matches_instrument_side(row, instrument_id=instrument_id, side=side)
                and _same_size_text(_row_size_text(row), _request_size_text(request))
            ):
                candidate_pos_id = _canonical_row_pos_id(row)
                if candidate_pos_id == pos_id and not _row_matches_expected_protection_set(
                    row, expected_rows
                ):
                    return _intent_refusal(
                        parent_event, binding_id, pos_id,
                        "trigger_protection_candidate_protection_conflict", [_row_order_id(row)],
                    )
                if not _row_matches_expected_protection_set(row, expected_rows):
                    continue
                if candidate_pos_id is None:
                    if not (
                        source == "pending"
                        and expected_purposes == {"stop_loss"}
                        and not _row_position_ids(row)
                    ):
                        return _intent_refusal(
                            parent_event, binding_id, pos_id,
                            "trigger_protection_candidate_position_invalid", [_row_order_id(row)],
                        )
                if candidate_pos_id != pos_id:
                    if candidate_pos_id is not None:
                        return _intent_refusal(
                            parent_event, binding_id, pos_id,
                            "trigger_protection_candidate_position_conflict", [_row_order_id(row)],
                        )
            if (
                _row_matches_instrument_side(row, instrument_id=instrument_id, side=side)
                and (
                    _canonical_row_pos_id(row) == pos_id
                    or (
                        source == "pending"
                        and expected_purposes == {"stop_loss"}
                        and _canonical_row_pos_id(row) is None
                        and not _row_position_ids(row)
                    )
                )
                and _row_matches_expected_protection_set(row, expected_rows)
                and _same_size_text(_row_size_text(row), _request_size_text(request))
            ):
                candidate_created_at = _row_creation_time(row)
                if candidate_created_at is None:
                    return _intent_refusal(
                        parent_event,
                        binding_id,
                        pos_id,
                        "trigger_protection_candidate_time_unavailable",
                        [_row_order_id(row)],
                    )
                if candidate_created_at < _coerce_utc_naive(
                    live_position.created_at
                ):
                    order_id = _row_order_id(row)
                    if order_id:
                        prefill_candidate_ids.append(order_id)
                    continue
                candidates.append((row, source))
    if not candidates:
        if prefill_candidate_ids:
            return _intent_refusal(
                parent_event,
                binding_id,
                pos_id,
                "trigger_protection_candidate_predates_fill",
                prefill_candidate_ids,
            )
        return TriggerProtectionIntentAdoptionResult(
            deferred=TriggerProtectionIntentAdoptionDeferred(
                reason="trigger_protection_not_yet_observable",
                evidence={"parent_trigger_order_id": expected_parent},
            )
        )
    candidate_ids = [_row_order_id(row) for row, _ in candidates]
    if any(order_id in baseline_ids for order_id in candidate_ids):
        return _intent_refusal(parent_event, binding_id, pos_id, "trigger_protection_candidate_in_baseline", candidate_ids)
    if len(candidates) != 1:
        return _intent_refusal(parent_event, binding_id, pos_id, "trigger_protection_candidate_not_unique", candidate_ids)
    row, source = candidates[0]
    order_id = _row_order_id(row)
    assert order_id is not None
    if not _row_position_ids(row) and any(
        _other_intent_can_own_anonymous_stop(
            intent=intent,
            other_intent=other_intent,
            anonymous_stop_key=anonymous_stop_key,
            existing_intent_requests=existing_intent_requests,
            existing_intent_owner_states=existing_intent_owner_states,
        )
        for other_intent in existing_intents
    ):
        return _intent_refusal(
            parent_event,
            binding_id,
            pos_id,
            "trigger_protection_candidate_not_unique",
            [order_id],
        )
    if source == "history" and not _history_row_is_proven_in_range(
        row,
        parent_order_id=expected_parent,
        start=history_time_range_start,
        end=history_time_range_end,
    ):
        return _intent_refusal(parent_event, binding_id, pos_id, "trigger_protection_history_unproven", [order_id])
    for other_intent in existing_intents:
        same_intent = other_intent is intent or (
            intent.id is not None and other_intent.id == intent.id
        )
        if (
            not same_intent
            and str(other_intent.adopted_order_id or "").strip() == order_id
        ):
            return _intent_refusal(parent_event, binding_id, pos_id, "trigger_protection_order_owned", [order_id])
    adopted_order_id = str(intent.adopted_order_id or "").strip()
    if adopted_order_id:
        if adopted_order_id != order_id:
            return _intent_refusal(
                parent_event, binding_id, pos_id,
                "trigger_protection_adopted_order_conflict", [order_id],
            )
    matching_ledgers = [
        ledger
        for ledger in existing_ledger_rows
        if str(ledger.order_id or "").strip() == order_id
    ]
    if matching_ledgers:
        exact_ledger = len(matching_ledgers) == 1 and (
            str(matching_ledgers[0].venue or "").lower() == "deepcoin"
            and int(matching_ledgers[0].execution_binding_id or 0) == binding_id
            and int(matching_ledgers[0].execution_order_leg_id or 0) == int(entry_leg.id or 0)
            and str(matching_ledgers[0].pos_id or "") == pos_id
            and str(matching_ledgers[0].status or "").lower() == "verified"
        )
        if adopted_order_id and exact_ledger:
            return TriggerProtectionIntentAdoptionResult(
                deferred=TriggerProtectionIntentAdoptionDeferred(reason="trigger_protection_already_adopted")
            )
        return _intent_refusal(parent_event, binding_id, pos_id, "trigger_protection_order_owned", [order_id])
    if adopted_order_id:
        return TriggerProtectionIntentAdoptionResult(
            deferred=TriggerProtectionIntentAdoptionDeferred(reason="trigger_protection_already_adopted")
        )
    return TriggerProtectionIntentAdoptionResult(
        action=EntryProtectionLedgerRepairAction(
            event_id=int(parent_event.id), binding_id=binding_id, leg_id=int(entry_leg.id),
            strategy_instance_id=entry_leg.strategy_instance_id, pos_id=pos_id,
            instrument_id=instrument_id, side=side, order_id=order_id,
            purpose="stop_loss" if expected_purposes == {"stop_loss"} else "combined",
            trigger_price=(expected_rows[0].get("trigger_price") if expected_purposes == {"stop_loss"} else None),
            size_text=_row_size_text(row) or _request_size_text(request),
            evidence={
                "match": (
                    "verified_filled_leg_unique_child"
                    if not _row_position_ids(row)
                    else "trigger_protection_intent_post_baseline"
                ),
                "intent_id": int(intent.id) if intent.id is not None else None,
                "parent_trigger_order_id": expected_parent,
                "source": source,
            },
        )
    )


def _other_intent_can_own_anonymous_stop(
    *,
    intent: TriggerProtectionIntent,
    other_intent: TriggerProtectionIntent,
    anonymous_stop_key: tuple[str, str, str, str] | None,
    existing_intent_requests: dict[int, dict[str, Any]] | None,
    existing_intent_owner_states: dict[int, TriggerProtectionOwnerState] | None,
) -> bool:
    if other_intent is intent or (
        intent.id is not None and other_intent.id == intent.id
    ):
        return False
    if anonymous_stop_key is None or existing_intent_requests is None:
        return False
    other_id = int(other_intent.id or 0)
    if (
        _anonymous_stop_request_key(existing_intent_requests.get(other_id, {}))
        != anonymous_stop_key
    ):
        return False
    if existing_intent_owner_states is None:
        return True
    owner_state = existing_intent_owner_states.get(other_id)
    if owner_state is None:
        return True
    return bool(owner_state.is_verified_filled_owner)


def _intent_refusal(
    event: ExecutionEvent, binding_id: int, pos_id: str, reason: str, candidate_ids: list[str | None] | None = None
) -> TriggerProtectionIntentAdoptionResult:
    return TriggerProtectionIntentAdoptionResult(
        refusal=EntryProtectionLedgerRepairRefusal(
            event_id=int(event.id) if event.id is not None else None,
            binding_id=binding_id or None, pos_id=pos_id or None, reason=reason,
            evidence={"candidate_order_ids": sorted({value for value in candidate_ids or [] if value})},
        )
    )


def _trigger_protection_fingerprint(request: Any) -> str:
    if not isinstance(request, dict):
        return ""
    payload = {
        key: value
        for key, value in request.items()
        if key not in {"merged_from_leg_indices"}
    }
    payload["tpTriggerPx"] = request.get("tpTriggerPx")
    payload["slTriggerPx"] = request.get("slTriggerPx")
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _baseline_order_ids(value: str | None) -> set[str] | None:
    parsed = _loads_json(value)
    if not isinstance(parsed, (list, dict)):
        return None
    if isinstance(parsed, dict) and set(parsed) != {"orders"}:
        return None
    rows = parsed if isinstance(parsed, list) else parsed.get("orders")
    if not isinstance(rows, list):
        return None
    order_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "ord_id", "instrument", "side", "trigger_order_type", "size",
            "take_profit_trigger_price", "stop_loss_trigger_price", "exchange_created_at",
            "exchange_updated_at",
        }:
            return None
        order_id = str(row.get("ord_id") or "").strip()
        if not order_id:
            return None
        order_ids.add(order_id)
    return order_ids


def _row_matches_expected_protection_set(row: dict[str, Any], expected_rows: list[dict[str, str | None]]) -> bool:
    expected_by_purpose = {str(item["purpose"]): item for item in expected_rows}
    if not expected_by_purpose:
        return False
    candidate_prices = {
        "take_profit": _first_nonzero_text(
            row, "tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice"
        ),
        "stop_loss": _first_nonzero_text(
            row, "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice"
        ),
    }
    if set(expected_by_purpose) != {
        purpose for purpose, price in candidate_prices.items() if price is not None
    }:
        return False
    return all(
        _same_numeric_text(candidate_prices[purpose], str(expected["trigger_price"]))
        for purpose, expected in expected_by_purpose.items()
        if candidate_prices[purpose] is not None and expected.get("trigger_price") is not None
    )


def _row_matches_instrument_side(row: dict[str, Any], *, instrument_id: str, side: str) -> bool:
    return (
        str(row.get("triggerOrderType") or "TPSL").upper() == "TPSL"
        and str(row.get("instId") or "").upper() == instrument_id.upper()
        and str(row.get("posSide") or row.get("side") or "").lower() == side.lower()
    )


def _canonical_row_pos_id(row: dict[str, Any]) -> str | None:
    position_ids = _row_position_ids(row)
    return next(iter(position_ids)) if len(position_ids) == 1 else None


def _row_position_ids(row: dict[str, Any]) -> set[str]:
    return {
        str(row.get(key) or "").strip()
        for key in ("closePosId", "close_pos_id", "closePositionId", "posId", "pos_id", "positionId")
        if str(row.get(key) or "").strip()
    }


def _anonymous_stop_request_key(request: dict[str, Any]) -> tuple[str, str, str, str] | None:
    expected = _expected_protection_rows(request)
    if {str(row.get("purpose") or "") for row in expected} != {"stop_loss"}:
        return None
    stop = str(expected[0].get("trigger_price") or "")
    instrument = _request_instrument_id(request)
    side = _request_side(request)
    size = _request_size_text(request)
    if not instrument or not side or not size or not stop:
        return None
    return instrument.upper(), side.lower(), size, stop


def _history_row_is_proven_in_range(
    row: dict[str, Any], *, parent_order_id: str, start: datetime | None, end: datetime | None
) -> bool:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return False
    start = _coerce_utc_naive(start)
    end = _coerce_utc_naive(end)
    if end < start:
        return False
    row_time = _row_time(row)
    if row_time is None or not (start <= row_time <= end):
        return False
    parent_ids = {
        str(row.get(key) or "").strip()
        for key in ("parentOrdId", "parentOrderId", "parent_order_id", "triggerOrderId")
    }
    return parent_order_id in parent_ids


def _protection_association(
    row: PositionProtectionLedger,
) -> tuple[str, str, int, int, str, str]:
    return (
        str(row.order_id or ""),
        str(row.venue or "").lower(),
        int(row.execution_binding_id),
        int(row.execution_order_leg_id),
        str(row.pos_id or ""),
        str(row.status or "").lower(),
    )


def _action_protection_association(
    action: EntryProtectionLedgerRepairAction,
) -> tuple[str, str, int, int, str, str]:
    return (
        action.order_id,
        "deepcoin",
        action.binding_id,
        action.leg_id,
        action.pos_id,
        "verified",
    )


def _verified_entry_leg(session, *, binding_id: int, pos_id: str) -> ExecutionOrderLeg | None:
    legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.venue == "deepcoin")
        .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .filter(ExecutionOrderLeg.pos_id == pos_id)
        .filter(ExecutionOrderLeg.attribution_status == "verified")
        .all()
    )
    return legs[0] if len(legs) == 1 else None


def _verified_trigger_entry_leg(
    session, *, binding_id: int, order_id: str, client_order_id: str
) -> ExecutionOrderLeg | None:
    query = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.venue == "deepcoin")
        .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .filter(ExecutionOrderLeg.order_kind == "trigger_limit")
        .filter(ExecutionOrderLeg.attribution_status == "verified")
    )
    legs = query.all()
    matches = [
        leg
        for leg in legs
        if (order_id and str(leg.order_id or "") == order_id)
        or (client_order_id and str(leg.client_order_id or "") == client_order_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _action_from_expected(
    event: ExecutionEvent,
    leg: ExecutionOrderLeg,
    expected: dict[str, str | None],
    pending_row: dict[str, Any],
    *,
    order_id: str,
    match: str,
    anchor_order_ids: list[str],
) -> EntryProtectionLedgerRepairAction:
    row_time = _row_time(pending_row)
    return EntryProtectionLedgerRepairAction(
        event_id=int(event.id),
        binding_id=int(event.execution_binding_id or 0),
        leg_id=int(leg.id),
        strategy_instance_id=leg.strategy_instance_id,
        pos_id=str(event.pos_id or ""),
        instrument_id=str(
            _request_instrument_id(_loads_json(event.request_json))
            or _event_instrument_id(event)
        ),
        side=str(_request_side(_loads_json(event.request_json)) or event.side or "").lower(),
        order_id=order_id,
        purpose=str(expected["purpose"]),
        trigger_price=expected.get("trigger_price"),
        size_text=_row_size_text(pending_row) or expected.get("size_text"),
        evidence={
            "match": match,
            "execution_event_id": int(event.id),
            "anchor_order_ids": anchor_order_ids,
            "exchange_order_created_at": row_time.isoformat() if row_time else None,
        },
    )


def _refusal(
    event: ExecutionEvent,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> EntryProtectionLedgerRepairRefusal:
    return EntryProtectionLedgerRepairRefusal(
        event_id=int(event.id) if event.id is not None else None,
        binding_id=int(event.execution_binding_id) if event.execution_binding_id else None,
        pos_id=str(event.pos_id) if event.pos_id else None,
        reason=reason,
        evidence=evidence or {},
    )


def _safe_pending_tpsl_rows(deepcoin_client, *, inst_id: str) -> list[dict[str, Any]]:
    method = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if method is None:
        return []
    rows = method(inst_id=inst_id)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _cached_pending_tpsl_rows(
    pending_cache: dict[str, list[dict[str, Any]]],
    *,
    deepcoin_client,
    inst_id: str,
) -> list[dict[str, Any]]:
    """Read a pending snapshot once per instrument for one dry-run plan."""

    if inst_id not in pending_cache:
        pending_cache[inst_id] = _safe_pending_tpsl_rows(
            deepcoin_client, inst_id=inst_id
        )
    return pending_cache[inst_id]


def _expected_protection_rows(protection_request: Any) -> list[dict[str, str | None]]:
    requests = protection_request if isinstance(protection_request, list) else [protection_request]
    rows: list[dict[str, str | None]] = []
    for request in requests:
        if not isinstance(request, dict):
            continue
        size_text = str(request.get("sz")) if request.get("sz") is not None else None
        for purpose, keys in (
            ("take_profit", ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")),
            ("stop_loss", ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")),
        ):
            trigger_price = _first_nonzero_text(request, *keys)
            if trigger_price is not None:
                rows.append(
                    {
                        "purpose": purpose,
                        "trigger_price": trigger_price,
                        "size_text": size_text,
                    }
                )
    return rows


def _request_instrument_id(request: Any) -> str | None:
    for row in request if isinstance(request, list) else [request]:
        if isinstance(row, dict):
            value = _first_nonzero_text(row, "instId", "instrument_id", "instrumentId")
            if value:
                return value.upper()
    return None


def _request_side(request: Any) -> str | None:
    for row in request if isinstance(request, list) else [request]:
        if isinstance(row, dict):
            value = _first_nonzero_text(row, "posSide", "side")
            if value:
                return value.lower()
    return None


def _request_pos_id(request: Any) -> str | None:
    for row in request if isinstance(request, list) else [request]:
        if isinstance(row, dict):
            value = _first_nonzero_text(row, "posId", "pos_id", "closePosId")
            if value:
                return value
    return None


def _event_instrument_id(event: ExecutionEvent) -> str | None:
    symbol = str(event.symbol or "").strip().upper()
    return f"{symbol}-USDT-SWAP" if symbol else None


def _response_order_ids(response: Any) -> list[str]:
    ids: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"ordId", "orderId", "order_id", "algoId", "triggerOrderId"}:
                    text = str(child or "").strip()
                    if text and text not in ids:
                        ids.append(text)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(response)
    return ids


def _row_matches_exchange_identity(
    row: dict[str, Any], *, instrument_id: str, side: str, pos_id: str
) -> bool:
    if str(row.get("triggerOrderType") or "TPSL").upper() != "TPSL":
        return False
    if str(row.get("instId") or "").upper() != instrument_id.upper():
        return False
    if str(row.get("posSide") or row.get("side") or "").lower() != side.lower():
        return False
    position_ids = [
        str(row[key]).strip()
        for key in (
            "closePosId",
            "close_pos_id",
            "closePositionId",
            "posId",
            "pos_id",
            "positionId",
        )
        if str(row.get(key) or "").strip()
    ]
    return all(row_pos_id == pos_id for row_pos_id in position_ids)


def _row_matching_expected_purpose(
    row: dict[str, Any], expected_rows: list[dict[str, str | None]]
) -> str | None:
    matches = [
        str(expected["purpose"])
        for expected in expected_rows
        if _row_matches_expected(row, expected)
    ]
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def _row_matches_expected(
    row: dict[str, Any],
    expected: dict[str, str | None],
    *,
    allow_generic_trigger_price: bool = True,
) -> bool:
    expected_price = expected.get("trigger_price")
    if expected_price is None:
        return False
    purpose = str(expected.get("purpose") or "")
    keys = (
        ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
        if purpose == "take_profit"
        else ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
    )
    price = _first_nonzero_text(row, *keys)
    if price is None and allow_generic_trigger_price:
        price = _first_nonzero_text(row, "triggerPx", "triggerPrice")
    return bool(price and _same_numeric_text(price, expected_price))


def _row_matches_trigger_entry_expected_protection(
    row: dict[str, Any], expected_rows: list[dict[str, str | None]]
) -> bool:
    expected_by_purpose = {str(item["purpose"]): item for item in expected_rows}
    required = {"take_profit", "stop_loss"}
    if not required.issubset(expected_by_purpose):
        return False
    return all(
        _row_matches_expected(
            row,
            expected_by_purpose[purpose],
            allow_generic_trigger_price=False,
        )
        for purpose in sorted(required)
    )


def _expected_price(expected_rows: list[dict[str, str | None]], purpose: str) -> str | None:
    for row in expected_rows:
        if row.get("purpose") == purpose:
            return row.get("trigger_price")
    return None


def _request_size_text(request: Any) -> str | None:
    for row in request if isinstance(request, list) else [request]:
        if isinstance(row, dict):
            value = _first_nonzero_text(row, "sz", "size", "orderSize")
            if value:
                return value
    return None


def _same_size_text(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return _same_numeric_text(left, right)


def _same_nonempty_text(left: Any, right: Any) -> bool:
    clean_left = str(left or "").strip()
    clean_right = str(right or "").strip()
    return bool(clean_left and clean_left == clean_right)


def _row_time_within(
    row: dict[str, Any], anchors: list[datetime | None], seconds: int
) -> bool:
    row_time = _row_time(row)
    if row_time is None:
        return False
    return any(
        anchor is not None and abs((row_time - anchor).total_seconds()) <= seconds
        for anchor in anchors
    )


def _row_time(row: dict[str, Any]) -> datetime | None:
    for key in ("cTime", "uTime", "createdAt", "created_at", "createdTime"):
        value = row.get(key)
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _row_creation_time(row: dict[str, Any]) -> datetime | None:
    for key in ("cTime", "createdAt", "created_at", "createdTime"):
        parsed = _parse_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            numeric = int(text)
            if numeric > 10_000_000_000:
                return datetime.fromtimestamp(numeric / 1000, UTC).replace(tzinfo=None)
            return datetime.fromtimestamp(numeric, UTC).replace(tzinfo=None)
        return _coerce_utc_naive(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except (ValueError, OSError, OverflowError):
        return None


def _coerce_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _row_order_id(row: dict[str, Any]) -> str | None:
    return _first_nonzero_text(row, "ordId", "orderId", "order_id", "algoId", "triggerOrderId")


def _row_size_text(row: dict[str, Any]) -> str | None:
    return _first_nonzero_text(row, "sz", "size", "orderSize")


def _first_nonzero_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value in (None, "", "0", 0):
            continue
        return str(value)
    return None


def _same_numeric_text(left: str, right: str) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        return str(left) == str(right)


def _loads_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _plan_fingerprint(
    actions: tuple[EntryProtectionLedgerRepairAction, ...],
    refusals: tuple[EntryProtectionLedgerRepairRefusal, ...],
) -> str:
    payload = {
        "actions": [
            {
                "event_id": row.event_id,
                "binding_id": row.binding_id,
                "leg_id": row.leg_id,
                "pos_id": row.pos_id,
                "order_id": row.order_id,
                "purpose": row.purpose,
                "trigger_price": row.trigger_price,
                "size_text": row.size_text,
                "evidence": _fingerprint_evidence(row.evidence),
            }
            for row in actions
        ],
        "refusals": [
            {
                "event_id": row.event_id,
                "binding_id": row.binding_id,
                "pos_id": row.pos_id,
                "reason": row.reason,
                "evidence": row.evidence,
            }
            for row in refusals
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fingerprint_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Exclude only the local observation clock from otherwise stable evidence."""

    stable = dict(evidence)
    live_position = stable.get("live_position")
    if isinstance(live_position, dict):
        stable["live_position"] = {
            key: value
            for key, value in live_position.items()
            if key != "observed_at"
        }
    return stable
