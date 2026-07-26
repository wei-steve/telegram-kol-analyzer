"""Persistence helpers for exchange order/position attribution bindings."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_readonly import (
    DeepcoinOrderBinding,
    DeepcoinReadOnlyAccountState,
    DeepcoinReadOnlyClient,
)
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import BoundPositionCloseReservation
from telegram_kol_research.models import ExecutionEvent
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import PositionAttributionAudit
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import StrategyManagementBatch
from telegram_kol_research.models import StrategyManagementLeg
from telegram_kol_research.models import TriggerProtectionIntent
from telegram_kol_research.protection_snapshot import (
    observe_pending_tpsl,
    record_pending_tpsl_observation,
)
from telegram_kol_research.protection_revisions import (
    confirm_visible_protection_revision,
    expire_unconfirmed_protection_revisions,
)
from telegram_kol_research.position_attribution import (
    ATTRIBUTION_POLICY_VERSION,
    FillEvidence,
    LegEvidence,
    PositionEvidence,
    TERMINAL_ENTRY_LEG_STATES,
    classify_leg_exchange_state,
    has_authoritative_persisted_position,
    is_fill_evidence,
    match_entry_legs_to_positions,
)
from telegram_kol_research.position_authority_lock import (
    position_authority_lock,
    serialized_position_authority_mutation,
)

PENDING_ENTRY_RECOVERY_WINDOW_HOURS = 3
_MANAGEMENT_POSITION_RESERVATION_STATUSES = frozenset(
    {
        "executing",
        "reserved",
        "submitted",
        "submit_unknown",
        "reconciling",
        "partial_failed",
        "recovery_required",
    }
)


@dataclass(slots=True)
class ExecutionBindingRecord:
    kol_id: str
    chat_id: int
    message_id: int
    symbol: str
    side: str
    venue: str = "deepcoin"
    order_id: str | None = None
    client_order_id: str | None = None
    pos_id: str | None = None
    margin_mode: str = "cross"
    position_mode: str = "split"
    payload: dict[str, Any] | None = None
    last_exchange_status: str | None = None
    status: str = "open"
    strategy_instance_id: str | None = None


@dataclass(slots=True)
class ExecutionOrderLegRecord:
    execution_binding_id: int
    leg_index: int
    purpose: str = "entry"
    order_kind: str = "unknown"
    strategy_instance_id: str | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    pos_id: str | None = None
    venue: str = "deepcoin"
    attribution_status: str | None = None
    attribution_evidence: dict[str, Any] | None = None
    terminal_reason: str | None = None
    last_verified_at: datetime | None = None
    status: str = "submitted"
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None


@dataclass(slots=True)
class ExecutionOrderLegSnapshot:
    id: int
    execution_binding_id: int
    strategy_instance_id: str | None
    leg_index: int
    purpose: str
    order_kind: str
    order_id: str | None
    client_order_id: str | None
    pos_id: str | None
    status: str
    venue: str = "deepcoin"
    attribution_status: str = "unassigned"
    attribution_evidence: dict[str, Any] | None = None
    terminal_reason: str | None = None
    last_verified_at: datetime | None = None


@dataclass(slots=True)
class ExecutionReconciliationResult:
    active: int = 0
    open: int = 0
    stale: int = 0
    updated: int = 0
    protection_adopted: int = 0
    protection_adoption_deferred: int = 0
    protection_adoption_conflicting: int = 0
    protection_adoption_refused: int = 0
    protection_snapshot_unavailable: int = 0


@dataclass(slots=True)
class ManualCloseSyncResult:
    checked: int = 0
    manually_closed: int = 0
    partial_legs_closed: int = 0
    skipped_without_pos_id: int = 0


@dataclass(slots=True)
class _ReconcileSnapshot:
    positions: list[dict[str, Any]] = field(default_factory=list)
    position_history: list[dict[str, Any]] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    pending_trigger_orders: list[dict[str, Any]] = field(default_factory=list)
    order_history: list[dict[str, Any]] = field(default_factory=list)
    trade_fills: list[dict[str, Any]] = field(default_factory=list)
    trigger_history: list[dict[str, Any]] = field(default_factory=list)
    pending_tpsl_observations: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def build_strategy_instance_id(
    *,
    venue: str,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
) -> str:
    """Build a stable local strategy key used across restart recovery."""

    return (
        f"{venue.lower()}:{int(chat_id)}:{int(message_id)}:"
        f"{symbol.upper()}:{side.lower()}"
    )


def build_client_order_id(
    *,
    strategy_instance_id: str,
    leg_index: int = 1,
    purpose: str = "entry",
    kol_code: str | None = None,
    message_id: int | None = None,
) -> str:
    """Build a deterministic client order id that remains stable after restarts."""

    if kol_code and message_id:
        prefix = f"TK{kol_code.upper()[:8]}{int(message_id)}"
        purpose_code = _client_order_purpose_code(purpose)
        candidate = f"{prefix}{purpose_code}{int(leg_index)}"
        if candidate.isalnum() and len(candidate) <= 20:
            return candidate
        digest_raw = f"{strategy_instance_id}:{purpose}:{int(leg_index)}"
        digest = hashlib.sha1(digest_raw.encode("utf-8")).hexdigest()[:4].upper()
        available = max(2, 20 - len(f"TK{purpose_code}{int(leg_index)}{digest}"))
        candidate = f"TK{kol_code.upper()[:available]}{purpose_code}{int(leg_index)}{digest}"
        return candidate[:20]

    raw = f"{strategy_instance_id}:{purpose}:{int(leg_index)}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14].upper()
    return f"TK{digest}{int(leg_index)}"[:20]


def _client_order_purpose_code(purpose: str) -> str:
    return {
        "entry": "E",
        "exit": "X",
        "take_profit": "T",
        "stop_loss": "S",
    }.get(str(purpose or "").lower(), "O")


def upsert_execution_binding(
    session_factory: sessionmaker,
    record: ExecutionBindingRecord,
) -> int:
    """Create or update the local exchange binding for one source strategy."""

    symbol = record.symbol.upper()
    side = record.side.lower()
    venue = record.venue.lower()
    strategy_instance_id = record.strategy_instance_id or build_strategy_instance_id(
        venue=venue,
        chat_id=record.chat_id,
        message_id=record.message_id,
        symbol=symbol,
        side=side,
    )
    payload_json = (
        json.dumps(record.payload, ensure_ascii=False, sort_keys=True)
        if record.payload is not None
        else None
    )

    with session_factory() as session:
        binding = (
            session.query(ExecutionBinding)
            .filter(
                ExecutionBinding.venue == venue,
                ExecutionBinding.chat_id == record.chat_id,
                ExecutionBinding.message_id == record.message_id,
                ExecutionBinding.symbol == symbol,
                ExecutionBinding.side == side,
            )
            .one_or_none()
        )
        if binding is None:
            binding = ExecutionBinding(
                kol_id=record.kol_id,
                chat_id=record.chat_id,
                message_id=record.message_id,
                symbol=symbol,
                side=side,
                venue=venue,
            )
            session.add(binding)
            session.flush()

        binding.strategy_instance_id = strategy_instance_id
        binding.kol_id = record.kol_id
        binding.order_id = record.order_id
        binding.client_order_id = record.client_order_id
        binding.pos_id = record.pos_id
        binding.margin_mode = _normalize_margin_mode(record.margin_mode)
        binding.position_mode = _normalize_position_mode(record.position_mode)
        binding.payload_json = payload_json
        binding.last_exchange_status = record.last_exchange_status
        binding.status = record.status
        binding.updated_at = datetime.now(UTC)
        binding_id = binding.id
        session.commit()

    return binding_id


def load_deepcoin_order_bindings(
    session_factory: sessionmaker,
) -> list[DeepcoinOrderBinding]:
    """Load active/open Deepcoin bindings for read-only account state mapping."""

    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(["open", "active"]))
            .order_by(ExecutionBinding.id.asc())
            .all()
        )

        return [
            DeepcoinOrderBinding(
                kol_id=row.kol_id,
                chat_id=row.chat_id,
                source_message_id=row.message_id,
                symbol=row.symbol,
                side=row.side,
                pos_id=row.pos_id,
                order_id=row.order_id,
                client_order_id=row.client_order_id,
            )
            for row in rows
        ]


def reconcile_deepcoin_execution_bindings(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
    recovered_at: datetime | None = None,
    snapshot: _ReconcileSnapshot | None = None,
    contract_spec_provider: Any | None = None,
) -> ExecutionReconciliationResult:
    """Reconcile one coherent exchange snapshot through global leg attribution."""

    now = recovered_at or datetime.now(UTC)
    with position_authority_lock():
        if snapshot is None:
            snapshot = load_deepcoin_execution_reconciliation_snapshot(
                session_factory, client=client
            )
        result = _apply_reconcile_snapshot(
            session_factory, snapshot=snapshot, recovered_at=now
        )
        if contract_spec_provider is not None:
            from telegram_kol_research.trigger_backup_stop_executor import (
                submit_verified_trigger_backup_stops,
            )

            submitted_backup_stops = submit_verified_trigger_backup_stops(
                session_factory,
                client=client,
                contract_spec_provider=contract_spec_provider,
                submitted_at=now,
            )
            if submitted_backup_stops:
                snapshot = load_deepcoin_execution_reconciliation_snapshot(
                    session_factory, client=client
                )
                with session_factory() as session:
                    legs = (
                        session.query(ExecutionOrderLeg)
                        .filter(ExecutionOrderLeg.venue == "deepcoin")
                        .filter(ExecutionOrderLeg.purpose == "entry")
                        .filter(ExecutionOrderLeg.attribution_status == "verified")
                        .all()
                    )
                    _ready_verified_trigger_take_profit_convergences(
                        session, legs=legs, snapshot=snapshot, recovered_at=now
                    )
                    session.commit()
        # Reuse the exact snapshot after entry attribution is current. The
        # management reconciler is read-only toward Deepcoin and never retries.
        from telegram_kol_research.strategy_management_reconciliation import (
            reconcile_strategy_management_batches,
        )

        reconcile_strategy_management_batches(
            session_factory, snapshot=snapshot, reconciled_at=now
        )
        return result


def load_deepcoin_execution_reconciliation_snapshot(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
) -> _ReconcileSnapshot:
    """Read one coherent account snapshot reusable by reconciliation and planning."""

    with session_factory() as session:
        instruments = {
            f"{str(symbol or '').upper()}-USDT-SWAP"
            for (symbol,) in session.query(ExecutionBinding.symbol)
            .filter(ExecutionBinding.venue == "deepcoin")
            .all()
            if str(symbol or "").strip()
        }
    snapshot = _load_reconcile_snapshot(client, instruments=instruments)
    for observation in snapshot.pending_tpsl_observations:
        record_pending_tpsl_observation(session_factory, observation=observation)
    return snapshot


def _load_reconcile_snapshot(
    client: DeepcoinReadOnlyClient,
    *,
    instruments: set[str],
) -> _ReconcileSnapshot:
    snapshot = _ReconcileSnapshot()
    snapshot.positions = _read_snapshot_rows(
        client, method_name="list_positions", source="positions", errors=snapshot.errors
    )
    snapshot.open_orders = _read_snapshot_rows(
        client, method_name="list_open_orders", source="open_orders", errors=snapshot.errors
    )
    snapshot.pending_trigger_orders = _read_pending_trigger_snapshot_rows(
        client,
        source="pending_trigger_orders",
        instruments=instruments,
        errors=snapshot.errors,
        observations=snapshot.pending_tpsl_observations,
    )
    snapshot.order_history = _read_instrument_snapshot_rows(
        client,
        method_name="list_order_history",
        source="order_history",
        instruments=instruments,
        errors=snapshot.errors,
    )
    snapshot.trade_fills = _read_instrument_snapshot_rows(
        client,
        method_name="list_trade_fills",
        source="trade_fills",
        instruments=instruments,
        errors=snapshot.errors,
    )
    snapshot.trigger_history = _read_instrument_snapshot_rows(
        client,
        method_name="list_trigger_order_history",
        source="trigger_history",
        instruments=instruments,
        errors=snapshot.errors,
    )
    return snapshot


def _read_snapshot_rows(
    client: DeepcoinReadOnlyClient,
    *,
    method_name: str,
    source: str,
    errors: dict[str, str],
) -> list[dict[str, Any]]:
    method = getattr(client, method_name, None)
    if method is None:
        return []
    try:
        rows = method()
    except Exception as exc:
        errors[source] = str(exc)
        return []
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        errors[source] = "invalid list response schema"
        return []
    return rows


def _read_instrument_snapshot_rows(
    client: DeepcoinReadOnlyClient,
    *,
    method_name: str,
    source: str,
    instruments: set[str],
    errors: dict[str, str],
) -> list[dict[str, Any]]:
    method = getattr(client, method_name, None)
    if method is None:
        return []
    result: list[dict[str, Any]] = []
    called_without_instrument = False
    for instrument_id in sorted(instruments):
        try:
            rows = method(inst_id=instrument_id)
        except TypeError:
            if called_without_instrument:
                continue
            called_without_instrument = True
            try:
                rows = method()
            except Exception as exc:
                errors[source] = str(exc)
                return result
        except Exception as exc:
            errors[f"{source}:{instrument_id}"] = str(exc)
            continue
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            errors[f"{source}:{instrument_id}"] = "invalid list response schema"
        else:
            result.extend(rows)
        if called_without_instrument:
            break
    return _deduplicate_exchange_rows(result)


def _read_pending_trigger_snapshot_rows(
    client: DeepcoinReadOnlyClient,
    *,
    source: str,
    instruments: set[str],
    errors: dict[str, str],
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read pending TPSL rows while retaining response-completeness evidence."""

    raw_reader = getattr(client, "read_trigger_orders_pending", None)
    list_reader = getattr(client, "list_trigger_orders_pending", None)
    result: list[dict[str, Any]] = []
    for instrument_id in sorted(instruments):
        try:
            if raw_reader is not None:
                response = raw_reader(inst_id=instrument_id)
                if not isinstance(response, dict):
                    raise ValueError("invalid raw pending trigger response schema")
            elif list_reader is not None:
                rows = list_reader(inst_id=instrument_id)
                response = {"data": rows}
            else:
                continue
        except Exception as exc:
            errors[f"{source}:{instrument_id}"] = str(exc)
            observations.append(
                {
                    "instrument_id": instrument_id,
                    "complete": False,
                    "reason": "pending_tpsl_read_error",
                    "order_ids": [],
                }
            )
            continue
        observation = observe_pending_tpsl(
            instrument_id=instrument_id,
            response=response,
        )
        observations.append(observation)
        rows = response.get("data")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            errors[f"{source}:{instrument_id}"] = "invalid list response schema"
            continue
        result.extend(rows)
    return _deduplicate_exchange_rows(result)


def _deduplicate_exchange_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        fingerprint = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(row)
    return result


def _confirm_visible_management_protection_revisions(
    session,
    *,
    snapshot: _ReconcileSnapshot,
    bindings: list[ExecutionBinding],
    entry_legs: list[ExecutionOrderLeg],
) -> None:
    """Confirm replacement revisions only from complete per-instrument reads."""

    observations_by_instrument = {
        str(item.get("instrument_id") or "").upper(): item
        for item in snapshot.pending_tpsl_observations
    }
    order_ids_by_instrument_and_pos: dict[tuple[str, str], set[str]] = {}
    for row in snapshot.pending_trigger_orders:
        instrument_id = str(row.get("instId") or row.get("instrument_id") or "").upper()
        pos_id = _first_string(row, "posId", "pos_id")
        order_id = _first_string(row, "ordId", "orderId", "order_id")
        if instrument_id and pos_id and order_id:
            order_ids_by_instrument_and_pos.setdefault((instrument_id, pos_id), set()).add(order_id)
    bindings_by_id = {int(binding.id): binding for binding in bindings}
    for leg in entry_legs:
        if str(leg.attribution_status or "") != "verified" or not leg.pos_id:
            continue
        binding = bindings_by_id.get(int(leg.execution_binding_id))
        if binding is None:
            continue
        instrument_id = f"{str(binding.symbol).upper()}-USDT-SWAP"
        observation = observations_by_instrument.get(instrument_id)
        if observation is None or not bool(observation.get("complete")):
            continue
        confirm_visible_protection_revision(
            session,
            venue=binding.venue,
            pos_id=str(leg.pos_id),
            visible_order_ids=order_ids_by_instrument_and_pos.get(
                (instrument_id, str(leg.pos_id)), set()
            ),
        )


def _apply_reconcile_snapshot(
    session_factory: sessionmaker,
    *,
    snapshot: _ReconcileSnapshot,
    recovered_at: datetime,
) -> ExecutionReconciliationResult:
    result = ExecutionReconciliationResult()
    active_positions = [row for row in snapshot.positions if _has_nonzero_size(row)]
    live_position_ids = {
        pos_id
        for row in active_positions
        if (pos_id := _first_string(row, "posId", "pos_id", "id"))
    }
    with session_factory() as session:
        bindings = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(
                ExecutionBinding.status.in_(
                    ["open", "active", "unknown", "stale", "closed", "cancelled"]
                )
            )
            .all()
        )
        _ensure_legacy_entry_legs(session, bindings=bindings, updated_at=recovered_at)
        _apply_recorded_terminal_entry_events(session, rows=bindings, updated_at=recovered_at)
        legs = (
            session.query(ExecutionOrderLeg)
            .join(ExecutionBinding, ExecutionBinding.id == ExecutionOrderLeg.execution_binding_id)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionOrderLeg.purpose == "entry")
            .all()
        )
        entry_legs_by_binding_id = _entry_legs_by_binding_id(legs)
        bindings_by_id = {int(binding.id): binding for binding in bindings}
        expire_unconfirmed_protection_revisions(session, now=recovered_at)
        _confirm_visible_management_protection_revisions(
            session, snapshot=snapshot, bindings=bindings, entry_legs=legs
        )
        manual_terminal_binding_ids = _manual_terminal_binding_ids(
            session, bindings=bindings
        )
        for leg in legs:
            if int(leg.execution_binding_id) not in manual_terminal_binding_ids:
                continue
            if (
                str(leg.status or "").lower() == "manually_closed"
                and leg.terminal_reason
            ):
                continue
            leg.status = "manually_closed"
            leg.terminal_reason = "manual_lifecycle_terminal"
            leg.updated_at = recovered_at
        reserved_pos_ids = {
            str(pos_id)
            for (pos_id,) in (
                session.query(BoundPositionCloseReservation.pos_id)
                .filter(BoundPositionCloseReservation.status == "reserved")
                .all()
            )
        }
        reserved_pos_ids.update(_active_management_reserved_pos_ids(session))

        if snapshot.errors:
            result.protection_snapshot_unavailable = _trigger_protection_exposure_count(
                session, legs=legs
            )
            _retry_saved_trigger_protection_intents_for_unavailable_snapshot(
                session, legs=legs, snapshot=snapshot, recovered_at=recovered_at, result=result
            )
            for leg in legs:
                if int(leg.execution_binding_id) in manual_terminal_binding_ids:
                    continue
                if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
                    continue
                if leg.pos_id and str(leg.pos_id) in reserved_pos_ids:
                    continue
                evidence = {"errors": dict(sorted(snapshot.errors.items()))}
                _transition_leg_attribution(
                    session,
                    leg=leg,
                    event_type="evidence_unavailable",
                    new_state="evidence_unavailable",
                    evidence=evidence,
                    recovered_at=recovered_at,
                )
            for binding in bindings:
                if int(binding.id) in manual_terminal_binding_ids:
                    _count_reconcile_binding(result, binding)
                    continue
                binding.last_exchange_status = "position_attribution_evidence_unavailable"
                binding.recovered_at = recovered_at
                binding.updated_at = recovered_at
                _count_reconcile_binding(result, binding)
            result.updated = len(bindings)
            session.commit()
            return result

        _refresh_exact_entry_leg_states(
            [
                leg
                for leg in legs
                if int(leg.execution_binding_id) not in manual_terminal_binding_ids
            ],
            snapshot=snapshot,
            recovered_at=recovered_at,
        )
        position_rows = [
            build_position_evidence(row)
            for row in active_positions
            if _first_string(row, "posId", "pos_id", "id") not in reserved_pos_ids
        ]
        position_rows = [row for row in position_rows if row is not None]
        fill_rows = _snapshot_fill_evidence(
            snapshot,
            legs=legs,
            bindings_by_id=bindings_by_id,
        )
        successful_fill_leg_ids = _successful_fill_leg_ids(fill_rows, legs=legs)
        mutated_binding_ids = _post_entry_protection_mutated_binding_ids(
            session, binding_ids=set(bindings_by_id)
        )
        prior_authoritative_leg_ids = {
            int(leg.id)
            for leg in legs
            if _has_prior_authoritative_position_audit(session, leg=leg)
        }
        leg_rows = [
            _leg_evidence(
                leg,
                binding=bindings_by_id[int(leg.execution_binding_id)],
                has_successful_entry_evidence=int(leg.id) in successful_fill_leg_ids,
                protection_mutated=(
                    int(leg.execution_binding_id) in mutated_binding_ids
                ),
                force_direct_pos_id=int(leg.id) in prior_authoritative_leg_ids,
            )
            for leg in legs
            if not (leg.pos_id and str(leg.pos_id) in reserved_pos_ids)
            and int(leg.execution_binding_id) not in manual_terminal_binding_ids
        ]
        attribution = match_entry_legs_to_positions(leg_rows, position_rows, fill_rows)
        legs_by_id = {int(leg.id): leg for leg in legs}
        conflict_leg_ids = {
            int(leg_id)
            for conflict in attribution.conflicts
            for leg_id in conflict.get("leg_ids", [])
        }
        conflict_evidence_by_leg: dict[int, dict[str, Any]] = {}
        for conflict in attribution.conflicts:
            evidence = {
                "candidate_leg_ids": sorted(int(item) for item in conflict.get("leg_ids", [])),
                "candidate_position_ids": sorted(
                    str(item) for item in conflict.get("position_ids", [])
                ),
            }
            for leg_id in evidence["candidate_leg_ids"]:
                conflict_evidence_by_leg[int(leg_id)] = evidence
        assignments = dict(attribution.assignments)
        existing_owner_by_position = {
            str(leg.pos_id): int(leg.id)
            for leg in legs
            if leg.pos_id not in (None, "")
        }
        for leg_id, pos_id in list(assignments.items()):
            leg = legs_by_id[leg_id]
            if int(leg.execution_binding_id) in manual_terminal_binding_ids:
                conflict_leg_ids.add(leg_id)
                assignments.pop(leg_id, None)
                continue
            existing_owner = existing_owner_by_position.get(pos_id)
            contradicted = bool(leg.pos_id and str(leg.pos_id) != str(pos_id))
            owned_elsewhere = existing_owner is not None and existing_owner != leg_id
            if not contradicted and not owned_elsewhere:
                continue
            conflict_leg_ids.add(leg_id)
            if existing_owner is not None:
                conflict_leg_ids.add(existing_owner)
            incident_leg_ids = sorted(
                {leg_id, *([existing_owner] if existing_owner is not None else [])}
            )
            incident_evidence = {
                "candidate_leg_ids": incident_leg_ids,
                "candidate_position_ids": [str(pos_id)],
            }
            for incident_leg_id in incident_leg_ids:
                conflict_evidence_by_leg[incident_leg_id] = incident_evidence
            assignments.pop(leg_id, None)

        for leg_id, pos_id in sorted(assignments.items()):
            leg = legs_by_id[leg_id]
            evidence = attribution.evidence_by_leg[leg_id]
            _transition_leg_attribution(
                session,
                leg=leg,
                event_type="ownership_verified",
                new_state="verified",
                evidence=evidence,
                recovered_at=recovered_at,
                pos_id=pos_id,
            )
            # A trigger child can expose a split position before its requested
            # quantity is fully filled.  Preserve that state so TP convergence
            # cannot allocate only the transient partial size.
            if str(leg.status or "").lower() != "partially_filled":
                leg.status = "active"
            leg.terminal_reason = None

        for leg in legs:
            leg_id = int(leg.id)
            if int(leg.execution_binding_id) in manual_terminal_binding_ids:
                continue
            if leg.pos_id and str(leg.pos_id) in reserved_pos_ids:
                continue
            if leg_id in assignments:
                continue
            if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
                continue
            previously_verified_position_missing = bool(
                leg.pos_id
                and str(leg.attribution_status or "") == "verified"
                and str(leg.pos_id) not in live_position_ids
                and leg_id not in conflict_leg_ids
            )
            if previously_verified_position_missing:
                # A successful empty positions snapshot proves closure, not a new
                # owner conflict. Preserve authority long enough for manual-close
                # sync to terminalize the exact leg and lifecycle.
                continue
            if leg_id in conflict_leg_ids or leg.pos_id:
                _transition_leg_attribution(
                    session,
                    leg=leg,
                    event_type="attribution_conflict",
                    new_state="attribution_conflict",
                    evidence=conflict_evidence_by_leg.get(
                        leg_id,
                        {"candidate_leg_ids": sorted(conflict_leg_ids)},
                    ),
                    recovered_at=recovered_at,
                )
            else:
                leg.attribution_status = "unassigned"
                leg.attribution_evidence_json = None

        _adopt_verified_trigger_entry_protection(
            session,
            legs=legs,
            snapshot=snapshot,
            recovered_at=recovered_at,
            result=result,
        )
        _ready_verified_trigger_take_profit_convergences(
            session, legs=legs, snapshot=snapshot, recovered_at=recovered_at
        )
        from telegram_kol_research.position_take_profit_orders import (
            reconcile_trigger_take_profit_order_history,
        )

        reconcile_trigger_take_profit_order_history(
            session,
            positions=snapshot.positions,
            pending_orders=snapshot.pending_trigger_orders,
            trigger_history=snapshot.trigger_history,
            observed_at=recovered_at,
        )
        from telegram_kol_research.protection_health import (
            reconcile_position_protection_health,
        )

        reconcile_position_protection_health(
            session,
            positions=snapshot.positions,
            pending_orders=snapshot.pending_trigger_orders,
            trigger_history=snapshot.trigger_history,
            snapshot_errors=snapshot.errors,
            observed_at=recovered_at,
        )

        for binding in bindings:
            binding.strategy_instance_id = binding.strategy_instance_id or build_strategy_instance_id(
                venue=binding.venue,
                chat_id=binding.chat_id,
                message_id=binding.message_id,
                symbol=binding.symbol,
                side=binding.side,
            )
            if int(binding.id) in manual_terminal_binding_ids:
                _count_reconcile_binding(result, binding)
                continue
            binding_legs = entry_legs_by_binding_id.get(int(binding.id), [])
            if any(
                leg.pos_id and str(leg.pos_id) in reserved_pos_ids
                for leg in binding_legs
            ):
                _count_reconcile_binding(result, binding)
                continue
            _derive_binding_from_entry_legs(
                session,
                binding=binding,
                legs=binding_legs,
                live_position_ids=live_position_ids,
                recovered_at=recovered_at,
            )
            _count_reconcile_binding(result, binding)
        result.updated = len(bindings)
        session.commit()
    return result


def _ready_verified_trigger_take_profit_convergences(
    session,
    *,
    legs: list[ExecutionOrderLeg],
    snapshot: _ReconcileSnapshot,
    recovered_at: datetime,
) -> None:
    """Release only exact, newly verified trigger legs to TP convergence."""

    from telegram_kol_research.models import TriggerTakeProfitConvergence
    from telegram_kol_research.trigger_take_profit_convergence import (
        mark_trigger_take_profit_convergence_ready,
    )
    from telegram_kol_research.trigger_take_profit_convergence_executor import (
        has_verified_exact_backup_stop,
    )

    leg_ids = [int(leg.id) for leg in legs if leg.id is not None]
    if not leg_ids:
        return
    rows = (
        session.query(TriggerTakeProfitConvergence)
        .filter(TriggerTakeProfitConvergence.execution_order_leg_id.in_(leg_ids))
        .filter(
            TriggerTakeProfitConvergence.status.in_(
                (
                    "waiting_position",
                    "waiting_backup_stop",
                    "ready",
                    "conflicted",
                )
            )
        )
        .order_by(TriggerTakeProfitConvergence.id.asc())
        .all()
    )
    for row in rows:
        # Earlier releases could terminalize a convergence for a full-position
        # (``sz=0``) primary stop or for legacy TPSLs whose exact ledger owner
        # was not consulted. Both are now re-verified below; all other
        # conflicts remain fail-closed.
        if str(row.status) == "conflicted" and str(row.reason_code) not in {
            "convergence_verified_stop_missing",
            "convergence_unowned_take_profit_present",
            "convergence_exchange_preflight_unavailable",
        }:
            continue
        leg = next((item for item in legs if int(item.id) == int(row.execution_order_leg_id)), None)
        if leg is None or _trigger_leg_child_fill_incomplete(leg, snapshot=snapshot):
            continue
        binding = session.get(ExecutionBinding, row.execution_binding_id)
        inst_id = f"{str(binding.symbol).upper()}-USDT-SWAP" if binding is not None else ""
        position_id = str(row.pos_id or leg.pos_id or "")
        positions = [item for item in snapshot.positions if isinstance(item, dict)]
        position_matches = [
            item for item in positions
            if binding is not None
            and str(item.get("instId") or "").upper() == inst_id
            and str(item.get("posId") or item.get("pos_id") or "") == position_id
            and str(item.get("posSide") or item.get("side") or "").lower()
            == str(binding.side).lower()
            and str(item.get("mrgPosition") or item.get("posMode") or "").lower() == "split"
        ]
        if (
            binding is None
            or not position_id
            or len(position_matches) != 1
            or not has_verified_exact_backup_stop(
                session,
                binding_id=int(binding.id),
                leg_id=int(leg.id),
                pos_id=position_id,
                inst_id=inst_id,
                side=str(binding.side).lower(),
                pending=snapshot.pending_trigger_orders,
                position=position_matches[0],
                open_positions=positions,
            )
        ):
            row.status = "waiting_backup_stop"
            row.reason_code = "convergence_waiting_backup_stop"
            row.updated_at = recovered_at
            continue
        if str(row.status) == "conflicted":
            row.status = "waiting_backup_stop"
            row.reason_code = "convergence_waiting_backup_stop"
        mark_trigger_take_profit_convergence_ready(session, row, ready_at=recovered_at)


def _trigger_leg_child_fill_incomplete(leg: ExecutionOrderLeg, *, snapshot: _ReconcileSnapshot) -> bool:
    """Do not allocate TP from a transient partially-filled trigger child."""

    parent_rows = [
        row for row in [*snapshot.pending_trigger_orders, *snapshot.trigger_history]
        if _exchange_row_matches_leg(row, leg)
    ]
    for parent in parent_rows:
        children = [
            child for child in [*snapshot.open_orders, *snapshot.order_history]
            if _trigger_child_order_matches(parent, child)
        ]
        if any(classify_leg_exchange_state(child) != "filled" for child in children):
            return True
    return False


def _adopt_verified_trigger_entry_protection(
    session,
    *,
    legs: list[ExecutionOrderLeg],
    snapshot: _ReconcileSnapshot,
    recovered_at: datetime,
    result: ExecutionReconciliationResult,
) -> None:
    """Adopt strict trigger-entry protection from the bounded read snapshot."""

    from telegram_kol_research.entry_protection_ledger_repair import (
        EntryProtectionLedgerRepairRefusal,
        plan_verified_trigger_entry_protection_adoption,
        upsert_entry_protection_ledger_action,
    )

    intent_leg_ids = _reconcile_saved_trigger_protection_intents(
        session, legs=legs, snapshot=snapshot, recovered_at=recovered_at, result=result
    )

    eligible_legs = [
        leg
        for leg in legs
        if str(leg.venue or "deepcoin").lower() == "deepcoin"
        and str(leg.purpose or "") == "entry"
        and str(leg.order_kind or "") == "trigger_limit"
        and str(leg.attribution_status or "") == "verified"
        and str(leg.status or "").lower() == "active"
        and bool(str(leg.pos_id or "").strip())
        and _request_has_combined_trigger_protection(leg.request_json)
        and int(leg.id) not in intent_leg_ids
    ]
    if not eligible_legs:
        return
    eligible_legs_by_id = {int(leg.id): leg for leg in eligible_legs}
    existing_ledger_rows = session.query(PositionProtectionLedger).all()
    existing_order_associations = {
        (
            str(row.order_id or ""),
            str(row.venue or "").lower(),
            int(row.execution_binding_id),
            int(row.execution_order_leg_id),
            str(row.pos_id or ""),
            str(row.status or "").lower(),
        )
        for row in existing_ledger_rows
    }
    protected_leg_ids = {
        int(row.execution_order_leg_id)
        for row in existing_ledger_rows
        if str(row.venue or "").lower() == "deepcoin"
        and str(row.status or "").lower() == "verified"
        and int(row.execution_order_leg_id) in eligible_legs_by_id
        and int(row.execution_binding_id)
        == int(eligible_legs_by_id[int(row.execution_order_leg_id)].execution_binding_id)
        and str(row.pos_id or "")
        == str(eligible_legs_by_id[int(row.execution_order_leg_id)].pos_id or "")
        and {
            association
            for association in existing_order_associations
            if association[0] == str(row.order_id or "")
        }
        == {
            (
                str(row.order_id or ""),
                "deepcoin",
                int(row.execution_binding_id),
                int(row.execution_order_leg_id),
                str(row.pos_id or ""),
                "verified",
            )
        }
    }
    existing_order_ids = {
        str(row.order_id)
        for row in existing_ledger_rows
        if str(row.order_id or "").strip()
    }
    events_by_binding: dict[int, list[ExecutionEvent]] = {}
    for event in (
        session.query(ExecutionEvent)
        .filter(ExecutionEvent.venue == "deepcoin")
        .filter(ExecutionEvent.action == "create_trigger_entry")
        .filter(
            ExecutionEvent.execution_binding_id.in_(
                sorted({int(leg.execution_binding_id) for leg in eligible_legs})
            )
        )
        .order_by(ExecutionEvent.id.asc())
        .all()
    ):
        events_by_binding.setdefault(int(event.execution_binding_id), []).append(event)

    for leg in sorted(eligible_legs, key=lambda row: int(row.id)):
        if int(leg.id) in protected_leg_ids:
            continue
        matching_events = [
            event
            for event in events_by_binding.get(int(leg.execution_binding_id), [])
            if _same_present_text(event.order_id, leg.order_id)
            and _same_present_text(event.client_order_id, leg.client_order_id)
        ]
        if len(matching_events) != 1:
            refusal = EntryProtectionLedgerRepairRefusal(
                event_id=(
                    int(matching_events[0].id) if len(matching_events) == 1 else None
                ),
                binding_id=int(leg.execution_binding_id),
                pos_id=str(leg.pos_id),
                reason=(
                    "trigger_entry_event_not_unique"
                    if matching_events
                    else "trigger_entry_event_missing"
                ),
                evidence={"candidate_event_count": len(matching_events)},
            )
            result.protection_adoption_refused += 1
            _record_protection_adoption_refusal(
                session, leg=leg, refusal=refusal, created_at=recovered_at
            )
            continue
        adoption = plan_verified_trigger_entry_protection_adoption(
            session,
            entry_leg=leg,
            event=matching_events[0],
            pending_tpsl_rows=snapshot.pending_trigger_orders,
            existing_order_ids=existing_order_ids,
            existing_order_associations=existing_order_associations,
        )
        if adoption.action is not None:
            row = upsert_entry_protection_ledger_action(
                session,
                adoption.action,
                evidence_source="reconciliation_trigger_entry_adoption",
                seen_at=recovered_at,
            )
            if row is not None:
                existing_order_ids.add(adoption.action.order_id)
                existing_order_associations.add(
                    (
                        adoption.action.order_id,
                        "deepcoin",
                        adoption.action.binding_id,
                        adoption.action.leg_id,
                        adoption.action.pos_id,
                        "verified",
                    )
                )
                protected_leg_ids.add(int(leg.id))
                result.protection_adopted += 1
        elif adoption.refusal is not None:
            result.protection_adoption_refused += 1
            _record_protection_adoption_refusal(
                session,
                leg=leg,
                refusal=adoption.refusal,
                created_at=recovered_at,
            )


_TRIGGER_PROTECTION_RETRY_LIMIT = 5


def _retry_saved_trigger_protection_intents_for_unavailable_snapshot(
    session, *, legs, snapshot, recovered_at, result
) -> None:
    """Back off persisted, already-attributed intents when TPSL reads fail."""
    from telegram_kol_research.entry_protection_ledger_repair import EntryProtectionLedgerRepairRefusal
    from telegram_kol_research.trigger_protection_intents import transition_trigger_protection_intent

    sources = sorted(key for key in snapshot.errors if key == "pending_trigger_orders"
                     or key.startswith("pending_trigger_orders:") or key == "trigger_history"
                     or key.startswith("trigger_history:"))
    if not sources:
        return
    legs_by_id = {int(leg.id): leg for leg in legs}
    intents = session.query(TriggerProtectionIntent).filter(
        TriggerProtectionIntent.venue == "deepcoin",
        TriggerProtectionIntent.recovery_state.in_(("pending", "retrying")),
    ).all()
    for intent in intents:
        leg = legs_by_id.get(int(intent.execution_order_leg_id))
        if leg is None or not _trigger_intent_due(intent, recovered_at):
            continue
        if not (str(leg.pos_id or "").strip() and str(leg.attribution_status or "") == "verified"):
            continue
        _record_protection_adoption_refusal(session, leg=leg, refusal=EntryProtectionLedgerRepairRefusal(
            event_id=None, binding_id=int(leg.execution_binding_id), pos_id=str(leg.pos_id),
            reason="trigger_protection_snapshot_unavailable", evidence={"snapshot_sources": sources},
        ), created_at=recovered_at)
        _schedule_trigger_intent_retry(session, intent, recovered_at, transition_trigger_protection_intent)


def _reconcile_saved_trigger_protection_intents(
    session, *, legs, snapshot, recovered_at, result
) -> set[int]:
    """Apply saved trigger intents using only this bounded, read-only snapshot."""
    from telegram_kol_research.entry_protection_ledger_repair import (
        EntryProtectionLedgerRepairRefusal,
        plan_trigger_protection_intent_adoption,
        upsert_entry_protection_ledger_action,
    )
    from telegram_kol_research.trigger_protection_intents import (
        transition_trigger_protection_intent,
    )

    legs_by_id = {int(leg.id): leg for leg in legs}
    saved_intents = (
        session.query(TriggerProtectionIntent)
        .filter(TriggerProtectionIntent.venue == "deepcoin")
        .order_by(TriggerProtectionIntent.id.asc()).all()
    )
    handled = {
        int(intent.execution_order_leg_id)
        for intent in saved_intents
        if int(intent.execution_order_leg_id) in legs_by_id
    }
    intents = [
        intent
        for intent in saved_intents
        if intent.recovery_state in {"pending", "retrying"}
    ]
    eligible = [
        (intent, legs_by_id[int(intent.execution_order_leg_id)]) for intent in intents
        if int(intent.execution_order_leg_id) in legs_by_id
        and int(intent.execution_binding_id) == int(legs_by_id[int(intent.execution_order_leg_id)].execution_binding_id)
        and str(legs_by_id[int(intent.execution_order_leg_id)].purpose or "") == "entry"
        and str(legs_by_id[int(intent.execution_order_leg_id)].order_kind or "") == "trigger_limit"
        and str(legs_by_id[int(intent.execution_order_leg_id)].attribution_status or "") == "verified"
        and str(legs_by_id[int(intent.execution_order_leg_id)].status or "").lower() == "active"
        and bool(str(legs_by_id[int(intent.execution_order_leg_id)].pos_id or "").strip())
    ]
    errors = sorted(
        key for key in snapshot.errors
        if key == "pending_trigger_orders" or key.startswith("pending_trigger_orders:")
        or key == "trigger_history" or key.startswith("trigger_history:")
    )
    if errors:
        for intent, leg in eligible:
            if not _trigger_intent_due(intent, recovered_at):
                continue
            result.protection_snapshot_unavailable += 1
            _record_protection_adoption_refusal(session, leg=leg, refusal=EntryProtectionLedgerRepairRefusal(
                event_id=None, binding_id=int(leg.execution_binding_id), pos_id=str(leg.pos_id),
                reason="trigger_protection_snapshot_unavailable", evidence={"snapshot_sources": errors},
            ), created_at=recovered_at)
            _schedule_trigger_intent_retry(session, intent, recovered_at, transition_trigger_protection_intent)
        return handled

    existing_ledger_rows = session.query(PositionProtectionLedger).all()
    all_intents = saved_intents
    intent_requests = {
        int(intent.id): _safe_json_object(legs_by_id[int(intent.execution_order_leg_id)].request_json)
        for intent in saved_intents
        if intent.id is not None and int(intent.execution_order_leg_id) in legs_by_id
    }
    events = session.query(ExecutionEvent).filter(
        ExecutionEvent.venue == "deepcoin", ExecutionEvent.action == "create_trigger_entry"
    ).order_by(ExecutionEvent.id.asc()).all()
    for intent, leg in eligible:
        if not _trigger_intent_due(intent, recovered_at):
            continue
        parent_events = [event for event in events if int(event.execution_binding_id or 0) == int(leg.execution_binding_id)
                         and _same_present_text(event.order_id, leg.order_id)
                         and _same_present_text(event.client_order_id, leg.client_order_id)]
        if len(parent_events) != 1:
            _refuse_trigger_intent(session, leg, intent, EntryProtectionLedgerRepairRefusal(
                event_id=None, binding_id=int(leg.execution_binding_id), pos_id=str(leg.pos_id),
                reason="trigger_protection_parent_event_not_unique", evidence={"candidate_event_count": len(parent_events)},
            ), recovered_at, result, transition_trigger_protection_intent)
            continue
        parent = parent_events[0]
        adoption = plan_trigger_protection_intent_adoption(
            session, entry_leg=leg, intent=intent, parent_event=parent,
            pending_tpsl_rows=snapshot.pending_trigger_orders,
            history_tpsl_rows=snapshot.trigger_history,
            existing_ledger_rows=existing_ledger_rows, existing_intents=all_intents,
            existing_intent_requests=intent_requests,
            history_time_range_start=parent.created_at, history_time_range_end=recovered_at,
        )
        if adoption.action is not None:
            row = upsert_entry_protection_ledger_action(session, adoption.action,
                evidence_source="reconciliation_trigger_protection_intent", seen_at=recovered_at)
            if row is not None:
                transition_trigger_protection_intent(session, intent, recovery_state="adopted", adopted_order_id=adoption.action.order_id)
                _bind_adopted_primary_protection_leg(
                    session,
                    entry_leg=leg,
                    pos_id=adoption.action.pos_id,
                    exchange_order_id=adoption.action.order_id,
                    evidence=adoption.action.evidence,
                )
                existing_ledger_rows.append(row)
                result.protection_adopted += 1
        elif adoption.deferred is not None:
            result.protection_adoption_deferred += 1
            _record_protection_adoption_refusal(session, leg=leg, refusal=EntryProtectionLedgerRepairRefusal(
                event_id=int(parent.id), binding_id=int(leg.execution_binding_id), pos_id=str(leg.pos_id),
                reason=adoption.deferred.reason, evidence=adoption.deferred.evidence,
            ), created_at=recovered_at)
            _schedule_trigger_intent_retry(session, intent, recovered_at, transition_trigger_protection_intent)
        elif adoption.refusal is not None:
            _refuse_trigger_intent(session, leg, intent, adoption.refusal, recovered_at, result, transition_trigger_protection_intent)
    return handled


def _bind_adopted_primary_protection_leg(
    session,
    *,
    entry_leg: ExecutionOrderLeg,
    pos_id: str,
    exchange_order_id: str,
    evidence: dict[str, Any],
) -> None:
    """Promote only the exact attached primary-stop leg after exchange adoption."""

    from telegram_kol_research.models import PositionProtectionLeg
    from telegram_kol_research.position_protection_legs import (
        bind_filled_position,
        bind_verified_exchange_order,
    )

    protection_legs = (
        session.query(PositionProtectionLeg)
        .filter(PositionProtectionLeg.execution_order_leg_id == int(entry_leg.id))
        .order_by(PositionProtectionLeg.id.asc())
        .all()
    )
    for protection_leg in protection_legs:
        bind_filled_position(session, protection_leg, pos_id=pos_id)
    protection_leg = (
        session.query(PositionProtectionLeg)
        .filter(PositionProtectionLeg.execution_order_leg_id == int(entry_leg.id))
        .filter(PositionProtectionLeg.role == "primary_stop")
        .filter(PositionProtectionLeg.leg_index == 1)
        .one_or_none()
    )
    if protection_leg is None:
        return
    bind_filled_position(session, protection_leg, pos_id=pos_id)
    bind_verified_exchange_order(
        session,
        protection_leg,
        exchange_order_id=exchange_order_id,
        readback_evidence=evidence,
    )


def _trigger_intent_due(intent, now: datetime) -> bool:
    when = intent.next_attempt_at
    if when is None:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return when <= now


def _schedule_trigger_intent_retry(session, intent, now, transition) -> None:
    attempts = min(int(intent.retry_attempts or 0) + 1, _TRIGGER_PROTECTION_RETRY_LIMIT)
    if attempts >= _TRIGGER_PROTECTION_RETRY_LIMIT:
        transition(session, intent, recovery_state="failed", retry_attempts=attempts)
    else:
        transition(session, intent, recovery_state="retrying", retry_attempts=attempts,
                   next_attempt_at=now + timedelta(minutes=min(5 * 2 ** (attempts - 1), 60)))


def _refuse_trigger_intent(session, leg, intent, refusal, now, result, transition) -> None:
    result.protection_adoption_refused += 1
    if "conflict" in str(refusal.reason) or "owned" in str(refusal.reason):
        result.protection_adoption_conflicting += 1
    _record_protection_adoption_refusal(session, leg=leg, refusal=refusal, created_at=now)
    _schedule_trigger_intent_retry(session, intent, now, transition)


def _record_protection_adoption_refusal(
    session,
    *,
    leg: ExecutionOrderLeg,
    refusal: Any,
    created_at: datetime,
) -> None:
    evidence = _bounded_protection_refusal_evidence(refusal)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "binding_id": int(leg.execution_binding_id),
                "leg_id": int(leg.id),
                "pos_id": str(leg.pos_id or ""),
                "event_type": "protection_adoption_refused",
                "evidence": evidence,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    exists = (
        session.query(PositionAttributionAudit.id)
        .filter(PositionAttributionAudit.fingerprint == fingerprint)
        .first()
    )
    if exists is not None:
        return
    session.add(
        PositionAttributionAudit(
            execution_binding_id=int(leg.execution_binding_id),
            execution_order_leg_id=int(leg.id),
            venue=str(leg.venue or "deepcoin"),
            pos_id=str(leg.pos_id or "") or None,
            event_type="protection_adoption_refused",
            prior_state=str(leg.attribution_status or "unassigned"),
            new_state="protection_adoption_refused",
            fingerprint=fingerprint,
            evidence_json=json.dumps(
                evidence,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            notification_status="pending",
            created_at=created_at,
        )
    )


def _bounded_protection_refusal_evidence(refusal: Any) -> dict[str, Any]:
    raw = refusal.evidence if isinstance(getattr(refusal, "evidence", None), dict) else {}
    evidence: dict[str, Any] = {"reason": str(refusal.reason or "unknown")[:128]}
    candidate_order_ids = raw.get("candidate_order_ids")
    if isinstance(candidate_order_ids, list):
        evidence["candidate_order_ids"] = sorted(
            {str(item)[:255] for item in candidate_order_ids if str(item or "").strip()}
        )[:20]
    for key, limit in (
        ("trigger_entry_order_id", 255),
        ("size_text", 64),
    ):
        if raw.get(key) not in (None, ""):
            evidence[key] = str(raw[key])[:limit]
    if raw.get("candidate_event_count") is not None:
        try:
            evidence["candidate_event_count"] = max(
                0, min(int(raw["candidate_event_count"]), 1000)
            )
        except (TypeError, ValueError):
            pass
    snapshot_sources = raw.get("snapshot_sources")
    if isinstance(snapshot_sources, list):
        evidence["snapshot_sources"] = sorted(
            {str(item)[:128] for item in snapshot_sources if str(item or "").strip()}
        )[:20]
    return evidence


def _trigger_protection_exposure_count(
    session, *, legs: list[ExecutionOrderLeg]
) -> int:
    return sum(
        1
        for leg in legs
        if str(leg.venue or "deepcoin").lower() == "deepcoin"
        and str(leg.purpose or "") == "entry"
        and str(leg.order_kind or "") == "trigger_limit"
        and str(leg.status or "").lower() not in TERMINAL_ENTRY_LEG_STATES
        and _request_has_combined_trigger_protection(leg.request_json)
    )


def _request_has_combined_trigger_protection(request_json: str | None) -> bool:
    request = _safe_json_object(request_json)
    return bool(
        _to_float(request.get("tpTriggerPx"))
        and _to_float(request.get("slTriggerPx"))
    )


def _same_present_text(left: Any, right: Any) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    return bool(left_text and right_text and left_text == right_text)


def _manual_terminal_binding_ids(
    session,
    *,
    bindings: list[ExecutionBinding],
) -> set[int]:
    from telegram_kol_research.models import StrategyLifecycle

    binding_keys = {
        (int(binding.chat_id), int(binding.message_id), str(binding.symbol), str(binding.side)): int(
            binding.id
        )
        for binding in bindings
    }
    latest_by_key: dict[tuple[int, int, str, str], Any] = {}
    for lifecycle in session.query(StrategyLifecycle).order_by(StrategyLifecycle.id.asc()).all():
        key = (
            int(lifecycle.chat_id),
            int(lifecycle.message_id),
            str(lifecycle.symbol),
            str(lifecycle.side),
        )
        if key in binding_keys:
            latest_by_key[key] = lifecycle
    return {
        binding_keys[key]
        for key, lifecycle in latest_by_key.items()
        if str(lifecycle.lifecycle_status or "") == "exited"
        and str(lifecycle.exit_reason or "") == "manual"
    }


def _ensure_legacy_entry_legs(
    session,
    *,
    bindings: list[ExecutionBinding],
    updated_at: datetime,
) -> None:
    existing_binding_ids = {
        int(binding_id)
        for (binding_id,) in session.query(ExecutionOrderLeg.execution_binding_id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .all()
    }
    for binding in bindings:
        if int(binding.id) in existing_binding_ids:
            continue
        submitted_orders = _submitted_orders_from_binding_payload(binding)
        if submitted_orders:
            definitions = []
            for index, submitted in enumerate(submitted_orders, start=1):
                definitions.append(
                    {
                        "leg_index": int(submitted.get("leg_index") or index),
                        "order_kind": str(
                            submitted.get("execution_type") or submitted.get("order_kind") or "unknown"
                        ).lower(),
                        "order_id": _first_string(
                            submitted, "order_id", "ordId", "orderId", "id"
                        ),
                        "client_order_id": _first_string(
                            submitted, "client_order_id", "clOrdId", "clientOrderId"
                        ),
                        "request_json": _compact_json(
                            submitted.get("request")
                            if isinstance(submitted.get("request"), dict)
                            else None
                        ),
                        "response_json": _compact_json(
                            submitted.get("response")
                            if isinstance(submitted.get("response"), dict)
                            else None
                        ),
                    }
                )
        else:
            order_ids = _split_ids(binding.order_id)
            client_order_ids = _split_ids(binding.client_order_id)
            count = max(len(order_ids), len(client_order_ids))
            definitions = [
                {
                    "leg_index": index + 1,
                    "order_kind": "unknown",
                    "order_id": order_ids[index] if index < len(order_ids) else None,
                    "client_order_id": (
                        client_order_ids[index] if index < len(client_order_ids) else None
                    ),
                    "request_json": None,
                    "response_json": None,
                }
                for index in range(count)
            ]
        for definition in definitions:
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=int(binding.id),
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=int(definition["leg_index"]),
                    purpose="entry",
                    order_kind=str(definition["order_kind"]),
                    order_id=definition["order_id"],
                    client_order_id=definition["client_order_id"],
                    venue=str(binding.venue or "deepcoin"),
                    status="open" if binding.status in {"open", "unknown"} else binding.status,
                    attribution_status="unassigned",
                    request_json=definition["request_json"],
                    response_json=definition["response_json"],
                    created_at=updated_at,
                    updated_at=updated_at,
                )
            )
        existing_binding_ids.add(int(binding.id))
    session.flush()


def _refresh_exact_entry_leg_states(
    legs: list[ExecutionOrderLeg],
    *,
    snapshot: _ReconcileSnapshot,
    recovered_at: datetime,
) -> None:
    pending_rows = [*snapshot.open_orders, *snapshot.pending_trigger_orders]
    history_rows = [*snapshot.order_history, *snapshot.trigger_history]
    for leg in legs:
        if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
            continue
        pending = next((row for row in pending_rows if _exchange_row_matches_leg(row, leg)), None)
        if pending is not None:
            leg.status = "pending"
            leg.updated_at = recovered_at
            continue
        history = next((row for row in history_rows if _exchange_row_matches_leg(row, leg)), None)
        if history is None:
            if str(leg.status or "").lower() in {"open", "submitted"}:
                leg.status = "unknown"
                leg.updated_at = recovered_at
            continue
        state = classify_leg_exchange_state(history)
        if state != "unknown":
            leg.status = state
            if state in TERMINAL_ENTRY_LEG_STATES:
                leg.terminal_reason = state
            leg.updated_at = recovered_at


def _exchange_row_matches_leg(row: dict[str, Any], leg: ExecutionOrderLeg) -> bool:
    order_id = _first_string(row, "ordId", "orderId", "order_id", "id")
    client_order_id = _first_string(row, "clOrdId", "clientOrderId", "client_order_id")
    return bool(
        (order_id and leg.order_id and order_id == str(leg.order_id))
        or (
            client_order_id
            and leg.client_order_id
            and client_order_id == str(leg.client_order_id)
        )
    )


def build_position_evidence(row: dict[str, Any]) -> PositionEvidence | None:
    pos_id = _first_string(row, "posId", "pos_id", "id")
    if not pos_id:
        return None
    average_price = _to_float(
        row.get("avgPx") or row.get("avgPrice") or row.get("openAvgPx")
    )
    return PositionEvidence(
        pos_id=pos_id,
        symbol=str(row.get("instId") or row.get("symbol") or ""),
        side=_normalize_position_side(str(row.get("posSide") or row.get("side") or "")),
        size=_to_float(row.get("pos") if row.get("pos") not in (None, "") else row.get("size")),
        average_price=average_price,
        created_at_ms=_to_int(row.get("cTime") or row.get("uTime")),
        entry_price=average_price,
        stop_loss=_to_float(row.get("slTriggerPx")),
        take_profits=_direct_price_tuple(row.get("tpTriggerPx")),
        margin_mode=_optional_margin_mode(row.get("mgnMode")),
        position_mode=_optional_position_mode(row.get("mrgPosition")),
    )


def _leg_evidence(
    leg: ExecutionOrderLeg,
    *,
    binding: ExecutionBinding,
    has_successful_entry_evidence: bool = False,
    protection_mutated: bool = False,
    force_direct_pos_id: bool = False,
) -> LegEvidence:
    direct_pos_id = None
    if (
        leg.pos_id
        and (
            force_direct_pos_id
            or (
                str(leg.attribution_status or "") == "verified"
                and has_authoritative_persisted_position(leg)
            )
        )
    ):
        direct_pos_id = leg.pos_id
    response_pos_id = _position_id_from_response_json(leg.response_json)
    if response_pos_id:
        direct_pos_id = response_pos_id
    return build_leg_economic_evidence(
        leg,
        binding=binding,
        pos_id=direct_pos_id,
        has_successful_entry_evidence=has_successful_entry_evidence,
        protection_mutated=protection_mutated,
    )


def _has_prior_authoritative_position_audit(
    session, *, leg: ExecutionOrderLeg
) -> bool:
    if not leg.pos_id:
        return False
    rows = (
        session.query(PositionAttributionAudit.evidence_json)
        .filter(PositionAttributionAudit.execution_order_leg_id == int(leg.id))
        .filter(PositionAttributionAudit.venue == str(leg.venue or "deepcoin"))
        .filter(PositionAttributionAudit.pos_id == str(leg.pos_id))
        .filter(PositionAttributionAudit.event_type == "ownership_verified")
        .filter(PositionAttributionAudit.new_state == "verified")
        .all()
    )
    for (evidence_json,) in rows:
        try:
            evidence = json.loads(evidence_json or "{}")
        except (TypeError, ValueError):
            continue
        if (
            isinstance(evidence, dict)
            and evidence.get("policy_version") == ATTRIBUTION_POLICY_VERSION
        ):
            return True
    return False


def build_leg_economic_evidence(
    leg: ExecutionOrderLeg,
    *,
    binding: ExecutionBinding,
    pos_id: str | None = None,
    has_successful_entry_evidence: bool = False,
    protection_mutated: bool = False,
) -> LegEvidence:
    """Rebuild one leg's normalized current economic evidence."""

    signature = _leg_economic_signature(leg, binding=binding)
    return LegEvidence(
        leg_id=int(leg.id),
        binding_id=int(binding.id),
        venue=str(leg.venue or binding.venue or "deepcoin"),
        symbol=str(binding.symbol or ""),
        side=str(binding.side or ""),
        order_id=leg.order_id,
        client_order_id=leg.client_order_id,
        pos_id=pos_id,
        requested_size=signature["requested_size"],
        terminal=str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES,
        strategy_instance_id=(leg.strategy_instance_id or binding.strategy_instance_id),
        entry_price=signature["entry_price"],
        stop_loss=signature["stop_loss"],
        take_profits=signature["take_profits"],
        margin_mode=_optional_margin_mode(binding.margin_mode),
        position_mode=_optional_position_mode(binding.position_mode),
        order_kind=_optional_string(leg.order_kind),
        has_successful_entry_evidence=has_successful_entry_evidence,
        protection_mutated=protection_mutated,
    )


def _snapshot_fill_evidence(
    snapshot: _ReconcileSnapshot,
    *,
    legs: list[ExecutionOrderLeg],
    bindings_by_id: dict[int, ExecutionBinding],
) -> list[FillEvidence]:
    sourced_rows = [
        *((row, "regular_order") for row in snapshot.order_history),
        *((row, "trade_fill") for row in snapshot.trade_fills),
        *((row, "trigger_fill") for row in snapshot.trigger_history),
    ]
    result: list[FillEvidence] = []
    for row, source in sourced_rows:
        sourced_row = {**row, "_evidence_source": "trade_fill" if source == "trade_fill" else source}
        if not is_fill_evidence(sourced_row):
            continue
        matching_legs = [leg for leg in legs if _exchange_row_matches_leg(row, leg)]
        if not matching_legs:
            continue
        for leg in matching_legs:
            binding = bindings_by_id[int(leg.execution_binding_id)]
            result.append(
                FillEvidence(
                    source=source,
                    order_id=_first_string(row, "ordId", "orderId", "order_id", "id"),
                    client_order_id=_first_string(
                        row, "clOrdId", "clientOrderId", "client_order_id"
                    ),
                    pos_id=_first_string(row, "posId", "pos_id", "positionId"),
                    symbol=str(row.get("instId") or f"{binding.symbol}-USDT-SWAP"),
                    side=_normalize_position_side(
                        str(row.get("posSide") or row.get("side") or binding.side)
                    ),
                    size=_to_float(
                        row.get("fillSz")
                        or row.get("accFillSz")
                        or row.get("sz")
                        or row.get("size")
                    ),
                    price=_to_float(
                        row.get("fillPx")
                        or row.get("avgPx")
                        or row.get("px")
                        or row.get("price")
                    ),
                    created_at_ms=_to_int(
                        row.get("fillTime")
                        or row.get("triggerTime")
                        or row.get("cTime")
                        or row.get("uTime")
                        or row.get("ts")
                    ),
                )
            )
    _append_trigger_child_order_fill_evidence(
        result,
        snapshot=snapshot,
        legs=legs,
        bindings_by_id=bindings_by_id,
    )
    return result


def _append_trigger_child_order_fill_evidence(
    result: list[FillEvidence],
    *,
    snapshot: _ReconcileSnapshot,
    legs: list[ExecutionOrderLeg],
    bindings_by_id: dict[int, ExecutionBinding],
) -> None:
    for trigger_row in snapshot.trigger_history:
        sourced_trigger = {**trigger_row, "_evidence_source": "trigger_fill"}
        if not is_fill_evidence(sourced_trigger):
            continue
        matching_legs = [
            leg for leg in legs if _exchange_row_matches_leg(trigger_row, leg)
        ]
        if len(matching_legs) != 1:
            continue
        child_rows = [
            row
            for row in snapshot.order_history
            if _trigger_child_order_matches(trigger_row, row)
        ]
        if len(child_rows) != 1:
            continue
        leg = matching_legs[0]
        binding = bindings_by_id[int(leg.execution_binding_id)]
        child_row = child_rows[0]
        child_order_id = _first_string(child_row, "ordId", "orderId", "order_id", "id")
        if not child_order_id or child_order_id == str(leg.order_id or ""):
            continue
        result.append(
            FillEvidence(
                source="trigger_child_order",
                order_id=str(leg.order_id) if leg.order_id else None,
                client_order_id=leg.client_order_id,
                pos_id=child_order_id,
                symbol=str(
                    child_row.get("instId")
                    or trigger_row.get("instId")
                    or f"{binding.symbol}-USDT-SWAP"
                ),
                side=_normalize_position_side(
                    str(
                        child_row.get("posSide")
                        or child_row.get("side")
                        or trigger_row.get("posSide")
                        or trigger_row.get("side")
                        or binding.side
                    )
                ),
                size=_order_row_size(child_row),
                price=_order_row_price(child_row),
                created_at_ms=_first_timestamp_ms(
                    child_row, "fillTime", "cTime", "uTime", "ts"
                ),
            )
        )


def _trigger_child_order_matches(
    trigger_row: dict[str, Any], child_row: dict[str, Any]
) -> bool:
    child_state = classify_leg_exchange_state(child_row)
    if child_state not in {"filled", "partially_filled"}:
        return False
    child_order_id = _first_string(child_row, "ordId", "orderId", "order_id", "id")
    trigger_order_id = _first_string(trigger_row, "ordId", "orderId", "order_id", "id")
    if not child_order_id or child_order_id == trigger_order_id:
        return False
    trigger_inst = str(trigger_row.get("instId") or "").upper()
    child_inst = str(child_row.get("instId") or "").upper()
    if trigger_inst and child_inst and trigger_inst != child_inst:
        return False
    trigger_side = _normalize_position_side(
        str(trigger_row.get("posSide") or trigger_row.get("side") or "")
    )
    child_side = _normalize_position_side(
        str(child_row.get("posSide") or child_row.get("side") or "")
    )
    if trigger_side and child_side and trigger_side != child_side:
        return False
    # A child may be only partially filled, so its cumulative fill size is
    # deliberately not comparable to the trigger's requested size here.
    if not _numbers_equal(_order_row_price(trigger_row), _order_row_price(child_row)):
        return False
    trigger_times = _timestamp_ms_values(
        trigger_row, "triggerTime", "fillTime", "cTime", "uTime", "ts"
    )
    child_times = _timestamp_ms_values(child_row, "fillTime", "cTime", "uTime", "ts")
    return bool(trigger_times and child_times and trigger_times & child_times)


def _order_row_size(row: dict[str, Any]) -> float | None:
    return _to_float(
        row.get("fillSz") or row.get("accFillSz") or row.get("sz") or row.get("size")
    )


def _order_row_price(row: dict[str, Any]) -> float | None:
    return _to_float(
        row.get("fillPx")
        or row.get("avgPx")
        or row.get("px")
        or row.get("triggerPx")
        or row.get("price")
    )


def _first_timestamp_ms(row: dict[str, Any], *keys: str) -> int | None:
    values = _timestamp_ms_values(row, *keys)
    return min(values) if values else None


def _timestamp_ms_values(row: dict[str, Any], *keys: str) -> set[int]:
    result: set[int] = set()
    for key in keys:
        value = _to_int(row.get(key))
        if value is not None:
            result.add(value)
    return result


def _numbers_equal(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= 1e-8


def _successful_fill_leg_ids(
    evidence: list[FillEvidence], *, legs: list[ExecutionOrderLeg]
) -> set[int]:
    """Return leg ids with a fill identifier unique among the current legs."""

    successful_leg_ids: set[int] = set()
    for row in evidence:
        matching_legs = [
            candidate
            for candidate in legs
            if _leg_identifiers_match_fill(candidate, row)
        ]
        if len(matching_legs) == 1:
            successful_leg_ids.add(int(matching_legs[0].id))
    return successful_leg_ids


def _entry_legs_by_binding_id(
    legs: list[ExecutionOrderLeg],
) -> dict[int, list[ExecutionOrderLeg]]:
    """Index entry legs once while preserving their database query order."""

    grouped: dict[int, list[ExecutionOrderLeg]] = {}
    for leg in legs:
        grouped.setdefault(int(leg.execution_binding_id), []).append(leg)
    return grouped


def _leg_has_successful_fill_evidence(
    leg: ExecutionOrderLeg,
    evidence: list[FillEvidence],
    *,
    legs: list[ExecutionOrderLeg],
) -> bool:
    return int(leg.id) in _successful_fill_leg_ids(evidence, legs=legs)


def _leg_identifiers_match_fill(
    leg: ExecutionOrderLeg, evidence: FillEvidence
) -> bool:
    shared_identifiers = [
        (str(leg_value), str(evidence_value))
        for leg_value, evidence_value in (
            (leg.order_id, evidence.order_id),
            (leg.client_order_id, evidence.client_order_id),
        )
        if leg_value not in (None, "") and evidence_value not in (None, "")
    ]
    return bool(shared_identifiers) and all(
        leg_value == evidence_value
        for leg_value, evidence_value in shared_identifiers
    )


def _post_entry_protection_mutated_binding_ids(
    session, *, binding_ids: set[int]
) -> set[int]:
    if not binding_ids:
        return set()
    rows = (
        session.query(
            ExecutionEvent.execution_binding_id,
            ExecutionEvent.action,
            ExecutionEvent.reason,
        )
        .filter(ExecutionEvent.execution_binding_id.in_(binding_ids))
        .filter(
            ExecutionEvent.action.in_(
                {
                    "adjust_position_tpsl",
                    "cancel_position_tpsl",
                    "set_position_tpsl",
                }
            )
        )
        .all()
    )
    return {
        int(binding_id)
        for binding_id, action, reason in rows
        if binding_id is not None
        and not (action == "set_position_tpsl" and reason == "entry_protection")
    }


def _leg_economic_signature(
    leg: ExecutionOrderLeg, *, binding: ExecutionBinding
) -> dict[str, Any]:
    request = _safe_json_object(leg.request_json)
    response = _safe_json_object(leg.response_json)
    binding_payload = _safe_json_object(binding.payload_json)
    draft = binding_payload.get("draft")
    if not isinstance(draft, dict):
        draft = binding_payload
    draft_order_legs = draft.get("order_legs")
    if not isinstance(draft_order_legs, list):
        draft_order_legs = []
    draft_leg = next(
        (
            row
            for row in draft_order_legs
            if isinstance(row, dict)
            and _plain_int(row.get("leg_index")) == int(leg.leg_index)
        ),
        {},
    )
    requested_size = _to_float(
        request.get("sz")
        or request.get("size")
        or request.get("quantity")
        or _nested_payload_value(response, "sz", "size", "quantity", "fillSz")
        or draft_leg.get("quantity")
        or draft_leg.get("sz")
    )
    entry_price = _to_float(
        request.get("px")
        or request.get("price")
        or request.get("triggerPx")
        or _nested_payload_value(response, "avgPx", "fillPx", "px", "price")
        or draft_leg.get("price")
    )
    stop_loss = _to_float(
        request.get("slTriggerPx")
        or request.get("stop_loss")
        or _nested_payload_value(response, "slTriggerPx", "stop_loss")
        or draft.get("stop_loss")
    )
    take_profits = _direct_price_tuple(
        request.get("tpTriggerPx")
        or request.get("take_profits")
        or _nested_payload_value(response, "tpTriggerPx", "take_profits")
    )
    if not take_profits:
        draft_take_profit_legs = draft.get("take_profit_legs")
        if not isinstance(draft_take_profit_legs, list):
            draft_take_profit_legs = []
        take_profits = tuple(
            price
            for row in draft_take_profit_legs
            if isinstance(row, dict)
            for price in [_to_float(row.get("price"))]
            if price is not None
        )
    return {
        "requested_size": requested_size,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
    }


def _safe_json_object(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _nested_payload_value(payload: Any, *keys: str) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            if payload.get(key) not in (None, ""):
                return payload[key]
        for value in payload.values():
            found = _nested_payload_value(value, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _nested_payload_value(value, *keys)
            if found not in (None, ""):
                return found
    return None


def _plain_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _direct_price_tuple(value: Any) -> tuple[float, ...]:
    values = value if isinstance(value, (list, tuple)) else [value]
    result = tuple(price for item in values if (price := _to_float(item)) is not None)
    return result


def _optional_string(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _optional_margin_mode(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    if text in {"cross", "crossed", "full", "全仓"}:
        return "cross"
    if text in {"isolated", "fixed", "逐仓"}:
        return "isolated"
    return None


def _optional_position_mode(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    if text in {"split", "hedge", "long_short", "分仓"}:
        return "split"
    if text in {"net", "merged", "one_way", "合仓"}:
        return "net"
    return None


def _position_id_from_response_json(response_json: str | None) -> str | None:
    try:
        payload = json.loads(response_json or "{}")
    except (TypeError, ValueError):
        return None

    def find(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("posId", "pos_id", "positionId"):
                if value.get(key) not in (None, ""):
                    return str(value[key])
            for nested in value.values():
                found = find(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = find(nested)
                if found:
                    return found
        return None

    return find(payload)


def _transition_leg_attribution(
    session,
    *,
    leg: ExecutionOrderLeg,
    event_type: str,
    new_state: str,
    evidence: dict[str, Any],
    recovered_at: datetime,
    pos_id: str | None = None,
) -> None:
    evidence_json = json.dumps(
        evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    audit_pos_id = pos_id if pos_id is not None else leg.pos_id
    if new_state in {"attribution_conflict", "evidence_unavailable"}:
        candidate_position_ids = sorted(
            str(item) for item in evidence.get("candidate_position_ids", [])
        )
        if not candidate_position_ids and audit_pos_id:
            candidate_position_ids = [str(audit_pos_id)]
        fingerprint_payload = {
            "venue": str(leg.venue or "deepcoin"),
            "position_ids": candidate_position_ids,
            "new_state": new_state,
            "candidate_leg_ids": sorted(
                int(item) for item in evidence.get("candidate_leg_ids", [])
            ),
            "errors": evidence.get("errors", {}),
        }
        if len(candidate_position_ids) == 1:
            audit_pos_id = candidate_position_ids[0]
    else:
        fingerprint_payload = {
            "binding_id": int(leg.execution_binding_id),
            "leg_id": int(leg.id),
            "venue": str(leg.venue or "deepcoin"),
            "pos_id": audit_pos_id,
            "event_type": event_type,
            "new_state": new_state,
            "evidence": evidence,
        }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    exists = next(
        (
            row
            for row in session.new
            if isinstance(row, PositionAttributionAudit)
            and row.fingerprint == fingerprint
        ),
        None,
    ) or (
        session.query(PositionAttributionAudit.id)
        .filter(PositionAttributionAudit.fingerprint == fingerprint)
        .first()
    )
    if exists is None:
        session.add(
            PositionAttributionAudit(
                execution_binding_id=int(leg.execution_binding_id),
                execution_order_leg_id=int(leg.id),
                venue=str(leg.venue or "deepcoin"),
                pos_id=audit_pos_id,
                event_type=event_type,
                prior_state=str(leg.attribution_status or "unassigned"),
                new_state=new_state,
                fingerprint=fingerprint,
                evidence_json=evidence_json,
                notification_status=(
                    "pending"
                    if new_state in {"attribution_conflict", "evidence_unavailable"}
                    else None
                ),
                created_at=recovered_at,
            )
        )
    if pos_id is not None:
        leg.pos_id = str(pos_id)
    leg.attribution_status = new_state
    leg.attribution_evidence_json = evidence_json
    if new_state == "verified":
        leg.last_verified_at = recovered_at
    leg.updated_at = recovered_at


def _derive_binding_from_entry_legs(
    session,
    *,
    binding: ExecutionBinding,
    legs: list[ExecutionOrderLeg],
    live_position_ids: set[str],
    recovered_at: datetime,
) -> None:
    verified_live_pos_ids = [
        str(leg.pos_id)
        for leg in sorted(legs, key=lambda item: int(item.leg_index or 0))
        if leg.pos_id
        and str(leg.attribution_status or "") == "verified"
        and str(leg.pos_id) in live_position_ids
    ]
    verified_missing_pos_ids = [
        str(leg.pos_id)
        for leg in sorted(legs, key=lambda item: int(item.leg_index or 0))
        if leg.pos_id
        and str(leg.attribution_status or "") == "verified"
        and str(leg.pos_id) not in live_position_ids
    ]
    has_unavailable = any(
        str(leg.attribution_status or "") == "evidence_unavailable" for leg in legs
    )
    has_conflict = any(
        str(leg.attribution_status or "") == "attribution_conflict" for leg in legs
    )
    has_pending = any(
        str(leg.status or "").lower()
        in {"open", "pending", "submitted", "partially_filled", "partial"}
        for leg in legs
    )
    all_terminal = bool(legs) and all(
        str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES for leg in legs
    )
    if verified_live_pos_ids:
        binding.pos_id = _join_unique_ids(verified_live_pos_ids)
        binding.status = "active"
        binding.last_exchange_status = "position_ownership_verified"
        _attach_binding_to_lifecycle(
            session,
            binding,
            recovered_at,
            clear_expiry_review=not has_pending and not has_unavailable and not has_conflict,
        )
    elif all_terminal:
        binding.pos_id = None
        binding.status = "closed"
        binding.last_exchange_status = "entry_legs_terminal"
        _cancel_missing_entry_lifecycle(session, binding, recovered_at)
    elif verified_missing_pos_ids:
        binding.pos_id = _join_unique_ids(verified_missing_pos_ids)
        binding.status = "stale"
        binding.last_exchange_status = "verified_position_missing_from_exchange"
    elif has_unavailable:
        binding.last_exchange_status = "position_attribution_evidence_unavailable"
    elif has_conflict:
        trusted_pos_ids = [
            str(leg.pos_id)
            for leg in legs
            if leg.pos_id
            and str(leg.attribution_status or "") in {"verified", "evidence_unavailable"}
        ]
        binding.pos_id = _join_unique_ids(trusted_pos_ids) or None
        binding.status = "unknown"
        binding.last_exchange_status = "position_attribution_conflict"
    elif has_pending:
        binding.pos_id = None
        binding.status = "open"
        binding.last_exchange_status = "entry_order_pending"
        _mark_lifecycle_pending(session, binding=binding, updated_at=recovered_at)
    else:
        binding.pos_id = None
        binding.status = "stale"
        binding.last_exchange_status = "position_ownership_unassigned"
    binding.recovered_at = recovered_at
    binding.updated_at = recovered_at


def _mark_lifecycle_pending(
    session,
    *,
    binding: ExecutionBinding,
    updated_at: datetime,
) -> None:
    from telegram_kol_research.models import StrategyLifecycle

    lifecycle = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == binding.chat_id)
        .filter(StrategyLifecycle.message_id == binding.message_id)
        .filter(StrategyLifecycle.symbol == binding.symbol)
        .filter(StrategyLifecycle.side == binding.side)
        .order_by(StrategyLifecycle.id.desc())
        .first()
    )
    if lifecycle is None or _is_terminal_exited_lifecycle(lifecycle):
        return
    lifecycle.execution_binding_id = int(binding.id)
    lifecycle.lifecycle_status = "pending_entry"
    lifecycle.exit_reason = None
    lifecycle.exited_at = None
    lifecycle.updated_at = updated_at


def _count_reconcile_binding(
    result: ExecutionReconciliationResult,
    binding: ExecutionBinding,
) -> None:
    if binding.status == "active":
        result.active += 1
    elif binding.status == "open":
        result.open += 1
    else:
        result.stale += 1


def _load_pending_trigger_orders(
    client: DeepcoinReadOnlyClient,
    *,
    rows: list[ExecutionBinding],
) -> list[dict[str, Any]]:
    method = getattr(client, "list_trigger_orders_pending", None)
    if method is None:
        return []
    instruments = {
        f"{str(row.symbol or '').upper()}-USDT-SWAP"
        for row in rows
        if str(row.symbol or "").strip()
    }
    pending: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for instrument_id in sorted(instruments):
        try:
            rows_for_instrument = method(inst_id=instrument_id)
        except TypeError:
            rows_for_instrument = method()
        except Exception:
            rows_for_instrument = []
        if not isinstance(rows_for_instrument, list):
            continue
        for order in rows_for_instrument:
            if not isinstance(order, dict):
                continue
            order_id = _first_string(order, "ordId", "orderId", "order_id", "id") or ""
            client_order_id = (
                _first_string(order, "clOrdId", "clientOrderId", "client_order_id") or ""
            )
            identity = (order_id, client_order_id)
            if identity in seen:
                continue
            seen.add(identity)
            pending.append(order)
    return pending


def _cancel_missing_entry_lifecycle(session, row: ExecutionBinding, cancelled_at: datetime) -> None:
    """Archive a bound entry order that disappeared before any position was known."""

    from telegram_kol_research.models import StrategyLifecycle, TradeIdea

    lifecycle = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == row.chat_id)
        .filter(StrategyLifecycle.message_id == row.message_id)
        .filter(StrategyLifecycle.symbol == row.symbol)
        .filter(StrategyLifecycle.side == row.side)
        .filter(StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered"]))
        .order_by(StrategyLifecycle.id.desc())
        .first()
    )
    if lifecycle is None:
        return

    lifecycle.lifecycle_status = "exited"
    lifecycle.exit_reason = "cancelled"
    lifecycle.exited_at = cancelled_at
    lifecycle.updated_at = cancelled_at
    if lifecycle.trade_idea_id is not None:
        trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
        if trade_idea is not None and trade_idea.status == "open":
            trade_idea.status = "closed"
            trade_idea.closed_at = cancelled_at


@serialized_position_authority_mutation
def sync_manual_closed_deepcoin_positions(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
    synced_at: datetime | None = None,
) -> ManualCloseSyncResult:
    """Mark bound active positions as manual-closed when they vanish on Deepcoin."""

    from telegram_kol_research.models import StrategyLifecycle, TradeIdea

    now = synced_at or datetime.now(UTC)
    positions = client.list_positions()
    active_pos_ids = {
        _first_string(position, "posId", "pos_id", "id")
        for position in positions
        if _first_string(position, "posId", "pos_id", "id") and _has_nonzero_size(position)
    }
    result = ManualCloseSyncResult()
    with session_factory() as session:
        management_reserved_pos_ids = _active_management_reserved_pos_ids(
            session
        )
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(
                ExecutionBinding.status.in_(["open", "active", "stale", "unknown"])
            )
            .order_by(ExecutionBinding.id.asc())
            .all()
        )
        for row in rows:
            entry_legs = _entry_legs_for_binding(session, row)
            pos_ids = _split_ids(row.pos_id) or _entry_leg_position_ids(entry_legs)
            if not pos_ids:
                result.skipped_without_pos_id += 1
                continue
            if any(pos_id in management_reserved_pos_ids for pos_id in pos_ids):
                continue
            result.checked += 1
            if str(row.status or "") == "unknown" and not entry_legs:
                continue
            if any(pos_id in active_pos_ids for pos_id in pos_ids):
                result.partial_legs_closed += _sync_missing_verified_entry_legs(
                    session,
                    binding=row,
                    client=client,
                    active_pos_ids=active_pos_ids,
                    synced_at=now,
                )
                continue
            if _binding_close_requires_exact_position_history(session, legs=entry_legs):
                if _sync_terminal_exited_history_closed_entry_legs(
                    session,
                    binding=row,
                    legs=entry_legs,
                    client=client,
                    active_pos_ids=active_pos_ids,
                    synced_at=now,
                ):
                    result.manually_closed += 1
                continue
            if _binding_has_unresolved_entry_leg(session, row):
                row.status = "open"
                row.last_exchange_status = "entry_legs_pending_after_position_closed"
                row.updated_at = now
                continue

            row.status = "closed"
            row.last_exchange_status = "manual_closed_or_not_found_on_exchange"
            row.updated_at = now
            result.manually_closed += 1

            for leg in (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.execution_binding_id == int(row.id))
                .filter(ExecutionOrderLeg.purpose == "entry")
                .all()
            ):
                if leg.pos_id and str(leg.pos_id) in pos_ids:
                    leg.status = "manually_closed"
                    leg.terminal_reason = "manual_position_missing"
                    leg.updated_at = now

            lifecycle = (
                session.query(StrategyLifecycle)
                .filter(StrategyLifecycle.chat_id == row.chat_id)
                .filter(StrategyLifecycle.message_id == row.message_id)
                .filter(StrategyLifecycle.symbol == row.symbol)
                .filter(StrategyLifecycle.side == row.side)
                .order_by(StrategyLifecycle.id.desc())
                .first()
            )
            if lifecycle is not None and lifecycle.lifecycle_status == "entered":
                lifecycle.lifecycle_status = "exited"
                lifecycle.exit_reason = (
                    "kol_signal"
                    if lifecycle.management_action == "exit_requested"
                    else "manual"
                )
                lifecycle.exited_at = now
                lifecycle.updated_at = now
                if lifecycle.trade_idea_id is not None:
                    trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
                    if trade_idea is not None and trade_idea.status == "open":
                        trade_idea.status = "closed"
                        trade_idea.closed_at = now
        session.commit()
    return result


def _active_management_reserved_pos_ids(session) -> set[str]:
    return {
        str(pos_id)
        for (pos_id,) in (
            session.query(StrategyManagementLeg.pos_id)
            .join(
                StrategyManagementBatch,
                StrategyManagementBatch.id
                == StrategyManagementLeg.management_batch_id,
            )
            .filter(
                StrategyManagementBatch.status.in_(
                    _MANAGEMENT_POSITION_RESERVATION_STATUSES
                )
            )
            .all()
        )
        if str(pos_id or "").strip()
    }


def _entry_legs_for_binding(
    session, binding: ExecutionBinding
) -> list[ExecutionOrderLeg]:
    return (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
        .filter(ExecutionOrderLeg.purpose == "entry")
        .all()
    )


def _entry_leg_position_ids(legs: list[ExecutionOrderLeg]) -> list[str]:
    joined = _join_unique_ids(
        str(leg.pos_id)
        for leg in legs
        if leg.pos_id
        and str(leg.status or "").lower() not in TERMINAL_ENTRY_LEG_STATES
    )
    return _split_ids(joined)


def _binding_close_requires_exact_position_history(
    session,
    *,
    legs: list[ExecutionOrderLeg],
) -> bool:
    for leg in legs:
        if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
            continue
        if not leg.pos_id:
            continue
        if str(leg.attribution_status or "") != "verified":
            return True
        if not (
            has_authoritative_persisted_position(leg, session=session)
            or _has_prior_authoritative_position_audit(session, leg=leg)
        ):
            return True
    return False


def _sync_terminal_exited_history_closed_entry_legs(
    session,
    *,
    binding: ExecutionBinding,
    legs: list[ExecutionOrderLeg],
    client: DeepcoinReadOnlyClient,
    active_pos_ids: set[str],
    synced_at: datetime,
) -> bool:
    history_reader = getattr(client, "list_position_history", None)
    if history_reader is None:
        return False
    lifecycle = _latest_lifecycle_for_binding(session, binding)
    if lifecycle is None or not _is_terminal_exited_lifecycle(lifecycle):
        return False
    nonterminal_legs = [
        leg
        for leg in legs
        if str(leg.status or "").lower() not in TERMINAL_ENTRY_LEG_STATES
    ]
    if not nonterminal_legs:
        return False
    for leg in nonterminal_legs:
        pos_id = str(leg.pos_id or "")
        if not pos_id or pos_id in active_pos_ids:
            return False
        if not _position_history_proves_full_close(
            history_reader,
            binding=binding,
            pos_id=pos_id,
        ):
            return False
    for leg in nonterminal_legs:
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        leg.updated_at = synced_at
    _derive_binding_from_entry_legs(
        session,
        binding=binding,
        legs=legs,
        live_position_ids=active_pos_ids,
        recovered_at=synced_at,
    )
    return True


def _latest_lifecycle_for_binding(session, binding: ExecutionBinding):
    from telegram_kol_research.models import StrategyLifecycle

    return (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == binding.chat_id)
        .filter(StrategyLifecycle.message_id == binding.message_id)
        .filter(StrategyLifecycle.symbol == binding.symbol)
        .filter(StrategyLifecycle.side == binding.side)
        .order_by(StrategyLifecycle.id.desc())
        .first()
    )


def _sync_missing_verified_entry_legs(
    session,
    *,
    binding: ExecutionBinding,
    client: DeepcoinReadOnlyClient,
    active_pos_ids: set[str],
    synced_at: datetime,
) -> int:
    history_reader = getattr(client, "list_position_history", None)
    if history_reader is None:
        return 0
    legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
        .filter(ExecutionOrderLeg.purpose == "entry")
        .all()
    )
    closed = 0
    for leg in legs:
        pos_id = str(leg.pos_id or "")
        if (
            not pos_id
            or pos_id in active_pos_ids
            or str(leg.attribution_status or "") != "verified"
            or str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
        ):
            continue
        if not (
            has_authoritative_persisted_position(leg, session=session)
            or _has_prior_authoritative_position_audit(session, leg=leg)
        ):
            continue
        if not _position_history_proves_full_close(
            history_reader,
            binding=binding,
            pos_id=pos_id,
        ):
            continue
        leg.status = "manually_closed"
        leg.terminal_reason = "manual_position_missing"
        leg.updated_at = synced_at
        closed += 1
    if closed:
        _derive_binding_from_entry_legs(
            session,
            binding=binding,
            legs=legs,
            live_position_ids=active_pos_ids,
            recovered_at=synced_at,
        )
    return closed


def _position_history_proves_full_close(
    history_reader,
    *,
    binding: ExecutionBinding,
    pos_id: str,
) -> bool:
    instrument_id = _binding_instrument_id(binding)
    try:
        rows = history_reader(inst_id=instrument_id, pos_id=pos_id)
    except Exception:
        return False
    expected_side = _normalize_position_side(str(binding.side or ""))
    for row in (rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        if _first_string(row, "posId", "pos_id", "id") != pos_id:
            continue
        row_instrument_id = _binding_instrument_id_from_value(
            row.get("instId") or row.get("symbol")
        )
        if row_instrument_id != instrument_id:
            continue
        row_side = _normalize_position_side(
            str(row.get("posSide") or row.get("side") or "")
        )
        if row_side != expected_side:
            continue
        opened = _to_float(row.get("pos") or row.get("size"))
        closed = _to_float(
            row.get("closePos") or row.get("close_pos") or row.get("closedSize")
        )
        if (
            opened is not None
            and opened > 0
            and closed is not None
            and abs(opened - closed) <= 1e-9
        ):
            return True
    return False


def _binding_instrument_id(binding: ExecutionBinding) -> str:
    return _binding_instrument_id_from_value(binding.symbol)


def _binding_instrument_id_from_value(value: Any) -> str:
    text = str(value or "").upper().replace("_", "-")
    return text if "-" in text else f"{text}-USDT-SWAP"


def bind_deepcoin_position_to_lifecycle(
    session_factory: sessionmaker,
    *,
    lifecycle_id: int,
    pos_id: str,
    position_payload: dict[str, Any] | None = None,
    bound_at: datetime | None = None,
) -> int:
    """Attach an existing live Deepcoin position to a local KOL lifecycle."""

    from telegram_kol_research.models import StrategyLifecycle

    now = bound_at or datetime.now(UTC)
    with session_factory() as session:
        try:
            lifecycle = session.get(StrategyLifecycle, lifecycle_id)
            if lifecycle is None:
                raise LookupError("strategy lifecycle not found")
            if lifecycle.lifecycle_status not in {"entered", "pending_entry"}:
                raise ValueError("only active or pending strategies can be bound")

            binding = None
            if lifecycle.execution_binding_id is not None:
                candidate = session.get(ExecutionBinding, lifecycle.execution_binding_id)
                if (
                    candidate is not None
                    and candidate.venue == "deepcoin"
                    and candidate.chat_id == lifecycle.chat_id
                    and candidate.message_id == lifecycle.message_id
                    and candidate.symbol == lifecycle.symbol
                    and candidate.side == lifecycle.side
                    and candidate.status in {"open", "active"}
                ):
                    binding = candidate
            if binding is None:
                binding = (
                    session.query(ExecutionBinding)
                    .filter_by(
                        venue="deepcoin",
                        chat_id=lifecycle.chat_id,
                        message_id=lifecycle.message_id,
                        symbol=lifecycle.symbol,
                        side=lifecycle.side,
                    )
                    .one_or_none()
                )
            if binding is None:
                binding = ExecutionBinding(
                    strategy_instance_id=build_strategy_instance_id(
                        venue="deepcoin",
                        chat_id=lifecycle.chat_id,
                        message_id=lifecycle.message_id,
                        symbol=lifecycle.symbol,
                        side=lifecycle.side,
                    ),
                    kol_id=f"group:{lifecycle.chat_id}",
                    chat_id=lifecycle.chat_id,
                    message_id=lifecycle.message_id,
                    symbol=lifecycle.symbol,
                    side=lifecycle.side,
                    venue="deepcoin",
                    payload_json=_compact_json(
                        {"manual_bind_position": position_payload or {}}
                    ),
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
                session.add(binding)
                session.flush()

            binding.pos_id = _join_unique_ids([*_split_ids(binding.pos_id), pos_id])
            binding.status = "active"
            binding.last_exchange_status = "manual_bound_live_position"
            binding.updated_at = now
            lifecycle.execution_binding_id = int(binding.id)
            if lifecycle.lifecycle_status == "pending_entry":
                lifecycle.lifecycle_status = "entered"
                lifecycle.entered_at = now
            lifecycle.updated_at = now

            existing_leg = (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
                .filter(ExecutionOrderLeg.purpose == "entry")
                .filter(ExecutionOrderLeg.pos_id == str(pos_id))
                .one_or_none()
            )
            if existing_leg is None:
                latest = (
                    session.query(ExecutionOrderLeg)
                    .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
                    .filter(ExecutionOrderLeg.purpose == "entry")
                    .order_by(ExecutionOrderLeg.leg_index.desc())
                    .first()
                )
                session.add(
                    ExecutionOrderLeg(
                        execution_binding_id=int(binding.id),
                        strategy_instance_id=binding.strategy_instance_id,
                        leg_index=int(latest.leg_index) + 1 if latest is not None else 1,
                        purpose="entry",
                        order_kind="manual_bind",
                        pos_id=str(pos_id),
                        venue="deepcoin",
                        attribution_status="verified",
                        attribution_evidence_json=_compact_json(
                            {
                                "policy_version": ATTRIBUTION_POLICY_VERSION,
                                "source": "manual_operator_bind",
                                "position": position_payload or {},
                            }
                        ),
                        last_verified_at=now,
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.commit()
            return int(binding.id)
        except IntegrityError:
            session.rollback()
            raise ValueError("position already has a verified owner")


def upsert_execution_order_leg(
    session_factory: sessionmaker,
    record: ExecutionOrderLegRecord,
) -> int:
    """Create or update one per-leg Deepcoin id mapping."""

    request_json = _compact_json(record.request)
    response_json = _compact_json(record.response)
    attribution_evidence_json = _compact_json(record.attribution_evidence)
    purpose = str(record.purpose or "entry").lower()
    order_kind = str(record.order_kind or "unknown").lower()
    now = datetime.now(UTC)

    with session_factory() as session:
        row = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(record.execution_binding_id))
            .filter(ExecutionOrderLeg.purpose == purpose)
            .filter(ExecutionOrderLeg.leg_index == int(record.leg_index))
            .one_or_none()
        )
        if row is None:
            row = ExecutionOrderLeg(
                execution_binding_id=int(record.execution_binding_id),
                purpose=purpose,
                leg_index=int(record.leg_index),
            )
            session.add(row)
            session.flush()

        row.strategy_instance_id = record.strategy_instance_id
        row.venue = str(record.venue or "deepcoin").lower()
        row.order_kind = order_kind
        row.order_id = record.order_id
        row.client_order_id = record.client_order_id
        row.pos_id = record.pos_id
        row.status = str(record.status or "submitted").lower()
        if record.attribution_status is not None:
            row.attribution_status = str(record.attribution_status).lower()
        if attribution_evidence_json is not None:
            row.attribution_evidence_json = attribution_evidence_json
        if record.terminal_reason is not None:
            row.terminal_reason = record.terminal_reason
        if record.last_verified_at is not None:
            row.last_verified_at = record.last_verified_at
        if request_json is not None:
            row.request_json = request_json
        if response_json is not None:
            row.response_json = response_json
        row.updated_at = now
        leg_id = int(row.id)
        session.commit()
    return leg_id


def list_execution_order_legs(
    session_factory: sessionmaker,
    *,
    execution_binding_id: int,
) -> list[ExecutionOrderLegSnapshot]:
    with session_factory() as session:
        rows = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(execution_binding_id))
            .order_by(ExecutionOrderLeg.purpose.asc(), ExecutionOrderLeg.leg_index.asc())
            .all()
        )
        return [
            ExecutionOrderLegSnapshot(
                id=int(row.id),
                execution_binding_id=int(row.execution_binding_id),
                strategy_instance_id=row.strategy_instance_id,
                leg_index=int(row.leg_index),
                purpose=row.purpose,
                order_kind=row.order_kind,
                order_id=row.order_id,
                client_order_id=row.client_order_id,
                pos_id=row.pos_id,
                status=row.status,
                venue=row.venue,
                attribution_status=row.attribution_status,
                attribution_evidence=_parse_json_object(row.attribution_evidence_json),
                terminal_reason=row.terminal_reason,
                last_verified_at=row.last_verified_at,
            )
            for row in rows
        ]


def repair_execution_order_legs_from_binding_payloads(
    session_factory: sessionmaker,
) -> int:
    """Backfill per-leg rows from legacy binding submitted_orders payloads."""

    repaired = 0
    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.payload_json.isnot(None))
            .order_by(ExecutionBinding.id.asc())
            .all()
        )
        snapshots: list[tuple[int, str | None, list[dict[str, Any]]]] = []
        for row in rows:
            submitted_orders = _submitted_orders_from_binding_payload(row)
            if submitted_orders:
                snapshots.append((int(row.id), row.strategy_instance_id, submitted_orders))

    for binding_id, strategy_instance_id, submitted_orders in snapshots:
        for index, order in enumerate(submitted_orders, start=1):
            leg_index = int(order.get("leg_index") or index)
            pos_id = _first_string(order, "pos_id", "posId", "position_id")
            status = "active" if pos_id else "open"
            upsert_execution_order_leg(
                session_factory,
                ExecutionOrderLegRecord(
                    execution_binding_id=binding_id,
                    strategy_instance_id=strategy_instance_id,
                    leg_index=leg_index,
                    purpose="entry",
                    order_kind=str(order.get("execution_type") or order.get("order_kind") or "unknown"),
                    order_id=_first_string(order, "order_id", "ordId", "orderId"),
                    client_order_id=_first_string(order, "client_order_id", "clOrdId", "clientOrderId"),
                    pos_id=pos_id,
                    status=status,
                    request=order.get("request") if isinstance(order.get("request"), dict) else None,
                    response=order.get("response") if isinstance(order.get("response"), dict) else None,
                ),
            )
            repaired += 1
    return repaired


def build_deepcoin_account_state(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
) -> DeepcoinReadOnlyAccountState:
    """Build a read-only Deepcoin account-state provider from persisted bindings."""

    return DeepcoinReadOnlyAccountState(
        client=client,
        bindings=load_deepcoin_order_bindings(session_factory),
    )


def list_active_positions(
    session_factory: sessionmaker,
    *,
    chat_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """List entered-but-not-exited strategies with detailed signal info.

    Combines execution bindings and open trade ideas.  Supports optional
    chat_id filter for per-group display.  Skips strategies that have been
    closed via trade updates or close signals.
    """

    from telegram_kol_research.models import (
        TradeIdea, SignalCandidate, RawMessage, TradeUpdate,
    )
    from sqlalchemy import and_

    results: list[dict[str, object]] = []

    with session_factory() as session:
        # ------------------------------------------------------------------
        # 1. Active execution bindings (exchange-tracked positions)
        # ------------------------------------------------------------------
        bindings_q = session.query(ExecutionBinding).filter(
            ExecutionBinding.status.in_(["open", "active"])
        )
        if chat_id is not None:
            bindings_q = bindings_q.filter(ExecutionBinding.chat_id == chat_id)
        bindings = bindings_q.order_by(ExecutionBinding.created_at.desc()).limit(limit).all()

        already_covered: set[tuple[int, str, str]] = set()
        for row in bindings:
            key = (row.chat_id, row.symbol.upper(), row.side.lower())
            results.append({
                "source": "execution",
                "id": row.id,
                "kol_id": row.kol_id,
                "chat_id": row.chat_id,
                "message_id": row.message_id,
                "symbol": row.symbol,
                "side": row.side,
                "venue": row.venue,
                "order_id": row.order_id,
                "client_order_id": row.client_order_id,
                "pos_id": row.pos_id,
                "strategy_instance_id": row.strategy_instance_id,
                "margin_mode": row.margin_mode,
                "position_mode": row.position_mode,
                "status": row.status,
                "last_exchange_status": row.last_exchange_status,
                "entry_text": None,
                "stop_loss_text": None,
                "take_profit_text": None,
                "confidence": None,
                "posted_at": row.created_at,
                "opened_at": row.created_at,
                "closed": False,
            })
            already_covered.add(key)

        # ------------------------------------------------------------------
        # 2. Open trade ideas with full signal details
        # ------------------------------------------------------------------
        open_trades_q = (
            session.query(TradeIdea, SignalCandidate, RawMessage)
            .join(SignalCandidate, TradeIdea.primary_signal_candidate_id == SignalCandidate.id)
            .join(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
            .filter(TradeIdea.status == "open")
        )
        if chat_id is not None:
            open_trades_q = open_trades_q.filter(RawMessage.chat_id == chat_id)
        open_trades = (
            open_trades_q
            .order_by(TradeIdea.opened_at.desc().nullslast(), TradeIdea.id.desc())
            .limit(limit)
            .all()
        )

        # Collect all trade_idea ids for exit-check
        trade_ids = [
            trade.id for trade, _, _ in open_trades
        ]

        # ------------------------------------------------------------------
        # 3. Pre-load close signals and trade updates for exit detection
        # ------------------------------------------------------------------
        closed_trade_ids: set[int] = set()
        if trade_ids:
            # Close trade updates
            close_updates = (
                session.query(TradeUpdate.trade_idea_id)
                .filter(
                    TradeUpdate.trade_idea_id.in_(trade_ids),
                    TradeUpdate.update_type.in_([
                        "close", "close_signal", "stop_loss_hit",
                        "take_profit_hit", "manual_close", "closed",
                    ]),
                )
                .all()
            )
            closed_trade_ids.update(row[0] for row in close_updates)

            # Close signal candidates for same chat+symbol+side pairs
            for trade, candidate, raw_msg in open_trades:
                if trade.id in closed_trade_ids:
                    continue
                close_exists = session.query(SignalCandidate).filter(
                    SignalCandidate.raw_message_id.in_(
                        session.query(RawMessage.id).filter(
                            RawMessage.chat_id == raw_msg.chat_id,
                            RawMessage.posted_at > raw_msg.posted_at,
                        )
                    ),
                    SignalCandidate.symbol == candidate.symbol,
                    SignalCandidate.side == candidate.side,
                    SignalCandidate.event_type.in_(["close_signal", "stop_loss_update"]),
                ).first()
                if close_exists is not None:
                    closed_trade_ids.add(trade.id)

        # ------------------------------------------------------------------
        # 4. Build result rows for open (non-exited) trade ideas
        # ------------------------------------------------------------------
        for trade, candidate, raw_msg in open_trades:
            if trade.id in closed_trade_ids:
                continue
            key = (raw_msg.chat_id, (trade.symbol or "").upper(), (trade.side or "").lower())
            if key in already_covered:
                continue
            results.append({
                "source": "trade_idea",
                "id": trade.id,
                "kol_id": (candidate.source_id or raw_msg.sender_name or "unknown"),
                "chat_id": raw_msg.chat_id,
                "message_id": raw_msg.message_id,
                "symbol": trade.symbol or "?",
                "side": trade.side or "?",
                "venue": "",
                "order_id": None,
                "pos_id": None,
                "status": trade.status,
                "entry_text": candidate.entry_text,
                "stop_loss_text": candidate.stop_loss_text,
                "take_profit_text": candidate.take_profit_text,
                "confidence": trade.confidence,
                "posted_at": raw_msg.posted_at,
                "opened_at": trade.opened_at,
                "closed": False,
            })
            already_covered.add(key)

    return results[:limit]


def _normalize_margin_mode(value: str | None) -> str:
    text = str(value or "cross").lower()
    if text in {"cross", "crossed", "full", "全仓"}:
        return "cross"
    if text in {"isolated", "fixed", "逐仓"}:
        return "isolated"
    return "cross"


def _normalize_position_mode(value: str | None) -> str:
    text = str(value or "split").lower()
    if text in {"split", "hedge", "long_short", "分仓"}:
        return "split"
    if text in {"net", "merged", "one_way", "合仓"}:
        return "net"
    return "split"


def _compact_json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _parse_json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else None


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _split_ids(value: str | None) -> list[str]:
    return [
        item.strip()
        for item in str(value or "").split(",")
        if item and item.strip()
    ]


def _join_unique_ids(values: list[str]) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return ",".join(result)


def _has_nonzero_size(position: dict[str, Any]) -> bool:
    size = position.get("pos")
    if size in (None, ""):
        size = position.get("size")
    try:
        return abs(float(size or 0)) > 0
    except (TypeError, ValueError):
        return False


def _is_open_order_state(order: dict[str, Any]) -> bool:
    state = str(order.get("state") or order.get("status") or "").lower()
    if not state:
        return True
    return state in {"live", "open", "partially_filled", "partial"}


def _submitted_orders_from_binding_payload(row: ExecutionBinding) -> list[dict[str, Any]]:
    if not row.payload_json:
        return []
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return []
    submitted_orders = payload.get("submitted_orders")
    if not isinstance(submitted_orders, list):
        return []
    return [item for item in submitted_orders if isinstance(item, dict)]


def _attach_binding_to_lifecycle(
    session,
    row: ExecutionBinding,
    updated_at: datetime,
    *,
    clear_expiry_review: bool = False,
) -> bool:
    from telegram_kol_research.models import StrategyLifecycle

    lifecycle = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.execution_binding_id == row.id)
        .order_by(StrategyLifecycle.id.desc())
        .first()
    )
    if lifecycle is None:
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.chat_id == row.chat_id)
            .filter(StrategyLifecycle.message_id == row.message_id)
            .filter(StrategyLifecycle.symbol == row.symbol)
            .filter(StrategyLifecycle.side == row.side)
            .order_by(StrategyLifecycle.id.desc())
            .first()
        )
    if lifecycle is None:
        return True
    if row.status != "active" and _is_stale_unentered_lifecycle(lifecycle, updated_at):
        lifecycle.lifecycle_status = "expired"
        lifecycle.exit_reason = "expired"
        lifecycle.exited_at = _pending_entry_expired_at(lifecycle)
        lifecycle.entered_at = None
        lifecycle.entry_price_actual = None
        lifecycle.execution_binding_id = None
        lifecycle.updated_at = updated_at
        row.status = "stale"
        row.last_exchange_status = "expired_pending_entry_not_attributed"
        return False
    lifecycle.execution_binding_id = row.id
    if (
        row.status == "active"
        and lifecycle.lifecycle_status == "exited"
        and lifecycle.exit_reason == "kol_signal"
    ):
        lifecycle.lifecycle_status = "entered"
        lifecycle.exit_reason = None
        lifecycle.exited_at = None
        lifecycle.management_action = "exit_requested"
        lifecycle.management_signal_message_id = lifecycle.exit_signal_message_id
    if _is_terminal_exited_lifecycle(lifecycle) and not (
        row.status == "active" and lifecycle.exit_reason == "manual"
    ):
        lifecycle.updated_at = updated_at
        return True
    if row.status == "active" and lifecycle.lifecycle_status != "entered":
        lifecycle.lifecycle_status = "entered"
        lifecycle.exit_reason = None
        lifecycle.exited_at = None
        if lifecycle.entered_at is None:
            lifecycle.entered_at = updated_at
    if row.status == "active" and clear_expiry_review:
        _clear_resolved_expiry_review(lifecycle)
    elif row.status == "open" and lifecycle.lifecycle_status in {
        "exited",
        "expired",
        "invalidated",
        "cancelled",
    }:
        lifecycle.lifecycle_status = "pending_entry"
        lifecycle.exit_reason = None
        lifecycle.exited_at = None
    elif lifecycle.lifecycle_status == "pending_entry":
        lifecycle.lifecycle_status = "entered"
        lifecycle.entered_at = updated_at
    _refresh_lifecycle_prices_from_binding_payload(lifecycle, row)
    lifecycle.updated_at = updated_at
    return True


def _clear_resolved_expiry_review(lifecycle) -> None:
    if str(getattr(lifecycle, "management_action", "") or "") not in {
        "expiry_review_requested",
        "expiry_review_continued",
    }:
        return
    lifecycle.management_action = None
    lifecycle.management_note = None


def _binding_has_unresolved_entry_leg(session, row: ExecutionBinding) -> bool:
    legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == row.id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .all()
    )
    if legs:
        eligible_legs = [
            leg
            for leg in legs
            if str(leg.status or "").lower() not in TERMINAL_ENTRY_LEG_STATES
        ]
        if not eligible_legs:
            return False
        leg_pos_ids = {str(leg.pos_id) for leg in eligible_legs if leg.pos_id}
        return not leg_pos_ids or len(leg_pos_ids) < len(eligible_legs)
    return len(_split_ids(row.order_id)) > len(_split_ids(row.pos_id))


def _apply_recorded_terminal_entry_events(
    session,
    *,
    rows: list[ExecutionBinding],
    updated_at: datetime,
) -> None:
    binding_ids = [int(row.id) for row in rows]
    if not binding_ids:
        return
    events = (
        session.query(ExecutionEvent)
        .filter(ExecutionEvent.execution_binding_id.in_(binding_ids))
        .filter(ExecutionEvent.action.in_(["cancel_trigger_entry", "cancel_regular_entry"]))
        .order_by(ExecutionEvent.id.asc())
        .all()
    )
    for event in events:
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == event.execution_binding_id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .all()
        )
        for leg in legs:
            if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
                continue
            order_matches = bool(
                event.order_id and leg.order_id and str(event.order_id) == str(leg.order_id)
            )
            client_order_matches = bool(
                event.client_order_id
                and leg.client_order_id
                and str(event.client_order_id) == str(leg.client_order_id)
            )
            if not order_matches and not client_order_matches:
                continue
            leg.status = "exchange_cancelled"
            leg.terminal_reason = str(event.action)
            leg.updated_at = updated_at


def _is_terminal_exited_lifecycle(lifecycle: Any) -> bool:
    if str(getattr(lifecycle, "lifecycle_status", None) or "") != "exited":
        return False
    exit_reason = str(getattr(lifecycle, "exit_reason", None) or "")
    return exit_reason in {"kol_signal", "manual"}


def _is_stale_unentered_lifecycle(lifecycle: Any, updated_at: datetime) -> bool:
    if lifecycle.signal_at is None:
        return False
    status = str(lifecycle.lifecycle_status or "")
    exit_reason = str(getattr(lifecycle, "exit_reason", None) or "")
    management_action = str(getattr(lifecycle, "management_action", None) or "")
    if status == "entered":
        return False
    if status == "expired" and management_action == "expiry_expired_keep_order":
        return False
    if status == "exited" and exit_reason not in {"expired", "cancelled", "invalidated"}:
        return False
    if status not in {"pending_entry", "expired", "invalidated", "exited"}:
        return False
    return _utc_naive(updated_at) > _pending_entry_expired_at(lifecycle)


def _pending_entry_expired_at(lifecycle: Any) -> datetime:
    signal_at = _utc_naive(lifecycle.signal_at)
    return signal_at + timedelta(hours=PENDING_ENTRY_RECOVERY_WINDOW_HOURS)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _refresh_lifecycle_prices_from_binding_payload(
    lifecycle: Any,
    row: ExecutionBinding,
) -> None:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, ValueError):
        return
    draft = payload.get("draft") if isinstance(payload, dict) else None
    if not isinstance(draft, dict):
        return
    draft_stop_loss = _to_float(draft.get("stop_loss"))
    reference_values = [
        value
        for value in (
            lifecycle.entry_price_actual,
            lifecycle.entry_range_low,
            lifecycle.entry_range_high,
            draft_stop_loss,
        )
        if value is not None and value > 0
    ]
    current_stop_loss = _to_float(lifecycle.stop_loss)
    if draft_stop_loss is not None and (
        current_stop_loss is None
        or not _price_plausible_against_reference(current_stop_loss, reference_values)
    ):
        lifecycle.stop_loss = draft_stop_loss

    draft_take_profit = _take_profit_text_from_draft(draft)
    if draft_take_profit and not lifecycle.take_profit:
        lifecycle.take_profit = draft_take_profit


def _take_profit_text_from_draft(draft: dict[str, Any]) -> str | None:
    legs = draft.get("take_profit_legs")
    if not isinstance(legs, list):
        return None
    prices: list[str] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        price = _to_float(leg.get("price"))
        if price is None or price <= 0:
            continue
        prices.append(f"{price:g}")
    return "/".join(prices) if prices else None


def _price_plausible_against_reference(
    value: float,
    reference_values: list[float],
) -> bool:
    if value <= 0 or not reference_values:
        return value > 0
    reference = max(reference_values)
    return reference * 0.2 <= value <= reference * 5


def _normalize_position_side(value: str) -> str:
    side = value.lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return side


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    if 0 < abs(parsed) < 100_000_000_000:
        return parsed * 1000
    return parsed
