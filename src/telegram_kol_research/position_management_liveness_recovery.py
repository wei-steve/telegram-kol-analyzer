"""Reviewed, fingerprinted recovery for one exact position-management path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.execution_bindings import (
    load_deepcoin_execution_reconciliation_snapshot_read_only,
)
from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_swap_instrument,
)
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionMutationIntent,
    PositionProtectionLedger,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.position_authority_lock import position_authority_lock
from telegram_kol_research.position_attribution import (
    PositionAttributionError,
    require_verified_position_ownership,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.strategy_management_executor import (
    execute_trigger_protection_stop_rescue,
)
from telegram_kol_research.strategy_management_planner import (
    _prepare_trigger_protection_stop_rescue,
    plan_trigger_protection_stop_rescue,
)
from telegram_kol_research.trigger_take_profit_convergence_executor import (
    _prepare_plan as _prepare_take_profit_convergence,
    execute_trigger_take_profit_convergence,
)
from telegram_kol_research.trading_settings import load_trading_settings


RecoveryActionKind = Literal[
    "adopt_unique_native_stop",
    "create_exact_backup_stop",
    "converge_staged_take_profit",
    "noop",
]


@dataclass(frozen=True, slots=True)
class PositionManagementLivenessRecoveryPlan:
    action_kind: RecoveryActionKind
    pos_id: str
    binding_id: int | None
    leg_id: int | None
    action_record_id: int | None
    exact_position: dict[str, str]
    excluded_candidates: tuple[dict[str, str], ...]
    target_take_profit_sizes: tuple[dict[str, str], ...]
    fingerprint: str
    reason_code: str | None = None
    selected_order_id: str | None = None
    selected_trigger_price: str | None = None


@dataclass(frozen=True, slots=True)
class PositionManagementLivenessRecoveryResult:
    status: str
    action_kind: RecoveryActionKind
    pos_id: str
    fingerprint: str
    reason_code: str | None = None


class _CoherentSnapshotClient:
    """Read-only client facade that never escapes the reviewed snapshot."""

    def __init__(self, *, positions, pending_trigger_orders):
        self._positions = tuple(positions)
        self._pending = tuple(pending_trigger_orders)

    def list_positions(self, *, inst_id=None):
        if inst_id is None:
            return list(self._positions)
        return [
            row for row in self._positions
            if str(row.get("instId") or "").upper() == str(inst_id).upper()
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        return [
            row for row in self._pending
            if str(row.get("instId") or "").upper() == str(inst_id).upper()
        ]


def build_position_management_liveness_recovery_plan(
    session_factory: sessionmaker,
    *,
    pos_id: str,
    deepcoin_client: Any,
    snapshot_loader: Callable[..., Any] = (
        load_deepcoin_execution_reconciliation_snapshot_read_only
    ),
    contract_spec_provider: Any | None = None,
    planned_at: datetime | None = None,
) -> PositionManagementLivenessRecoveryPlan:
    """Build one bounded dry-run from one coherent exchange snapshot."""

    del planned_at  # A review fingerprint must be stable across wall-clock time.
    normalized_pos_id = str(pos_id or "").strip()
    if not normalized_pos_id:
        return _noop_plan("", "exact_pos_id_required", snapshot=None)
    snapshot = snapshot_loader(session_factory, client=deepcoin_client)
    if getattr(snapshot, "errors", None):
        return _noop_plan(
            normalized_pos_id, "snapshot_incomplete", snapshot=snapshot
        )
    positions = [
        row for row in getattr(snapshot, "positions", ()) if isinstance(row, dict)
    ]
    matches = [
        row for row in positions
        if str(row.get("posId") or row.get("pos_id") or "") == normalized_pos_id
        and str(row.get("mrgPosition") or row.get("posMode") or "").lower()
        == "split"
        and _positive_decimal(row.get("pos") or row.get("size")) is not None
    ]
    if len(matches) != 1:
        return _noop_plan(
            normalized_pos_id,
            "exact_live_position_not_verified",
            snapshot=snapshot,
        )
    position = matches[0]
    inst_id = str(position.get("instId") or "").upper()
    side = str(position.get("posSide") or position.get("side") or "").lower()
    if not _target_pending_snapshot_complete(snapshot, inst_id=inst_id):
        return _noop_plan(
            normalized_pos_id, "snapshot_incomplete", snapshot=snapshot
        )

    with session_factory() as session:
        try:
            leg = require_verified_position_ownership(
                session, venue="deepcoin", pos_id=normalized_pos_id
            )
        except PositionAttributionError:
            return _noop_plan(
                normalized_pos_id,
                "exact_entry_leg_not_verified",
                snapshot=snapshot,
            )
        if (
            str(leg.purpose) != "entry"
            or str(leg.status).lower() not in {"active", "partially_filled"}
        ):
            return _noop_plan(
                normalized_pos_id,
                "exact_entry_leg_not_verified",
                snapshot=snapshot,
            )
        binding = session.get(ExecutionBinding, int(leg.execution_binding_id))
        if (
            binding is None
            or normalized_pos_id not in _split_ids(binding.pos_id)
            or normalize_deepcoin_swap_instrument(binding.symbol) != inst_id
            or str(binding.side).lower() != side
        ):
            return _noop_plan(
                normalized_pos_id,
                "exact_binding_not_verified",
                snapshot=snapshot,
            )
        if _terminal_or_inflight_state(session, pos_id=normalized_pos_id):
            return _noop_plan(
                normalized_pos_id,
                "position_mutation_in_progress",
                snapshot=snapshot,
                binding_id=int(binding.id),
                leg_id=int(leg.id),
            )
        ledger_rows = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.execution_order_leg_id == leg.id)
            .filter(PositionProtectionLedger.pos_id == normalized_pos_id)
            .order_by(PositionProtectionLedger.id.asc())
            .all()
        )
        backups = (
            session.query(PositionBackupStopOrder)
            .filter(PositionBackupStopOrder.execution_order_leg_id == leg.id)
            .filter(PositionBackupStopOrder.pos_id == normalized_pos_id)
            .order_by(PositionBackupStopOrder.id.asc())
            .all()
        )
        intent = (
            session.query(TriggerProtectionIntent)
            .filter(TriggerProtectionIntent.execution_order_leg_id == leg.id)
            .order_by(TriggerProtectionIntent.id.desc())
            .first()
        )
        convergence = (
            session.query(TriggerTakeProfitConvergence)
            .filter(TriggerTakeProfitConvergence.execution_order_leg_id == leg.id)
            .order_by(TriggerTakeProfitConvergence.id.desc())
            .first()
        )
        rescue = (
            session.query(TriggerProtectionStopRescue)
            .filter(TriggerProtectionStopRescue.execution_order_leg_id == leg.id)
            .order_by(TriggerProtectionStopRescue.id.desc())
            .first()
        )
        parent_events = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.venue == "deepcoin")
            .filter(ExecutionEvent.order_id == str(intent.parent_trigger_order_id or ""))
            .order_by(ExecutionEvent.id.asc())
            .all()
            if intent is not None and intent.parent_trigger_order_id
            else []
        )
        proposed_stop = _proposed_stop_from_parent_events(parent_events)
        snapshot_client = _CoherentSnapshotClient(
            positions=positions,
            pending_trigger_orders=[
                row for row in getattr(snapshot, "pending_trigger_orders", ())
                if isinstance(row, dict)
            ],
        )
        rescue_preflight = (
            _prepare_trigger_protection_stop_rescue(
                session, intent=intent, deepcoin_client=snapshot_client
            )
            if intent is not None else "rescue_intent_not_found"
        )
        convergence_preflight = (
            _prepare_take_profit_convergence(
                session,
                convergence=convergence,
                deepcoin_client=snapshot_client,
                contract_spec_provider=contract_spec_provider,
            )
            if convergence is not None and str(convergence.status) == "ready"
            else "convergence_not_ready"
        )
        # The normal TP preflight may materialize uncommitted protection legs.
        # A reviewed dry-run is read-only, so discard those planning writes.
        session.rollback()
        pending = [
            row for row in getattr(snapshot, "pending_trigger_orders", ())
            if isinstance(row, dict)
        ]
        exact_stop_candidates = _exact_native_stop_candidates(
            pending,
            pos_id=normalized_pos_id,
            inst_id=inst_id,
            side=side,
        )
        candidate_order_ids = {
            str(row.get("ordId") or row.get("orderId") or "")
            for row in exact_stop_candidates
        }
        foreign_owners = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.venue == "deepcoin")
            .filter(PositionProtectionLedger.order_id.in_(candidate_order_ids))
            .filter(PositionProtectionLedger.pos_id != normalized_pos_id)
            .order_by(PositionProtectionLedger.id.asc())
            .all()
            if candidate_order_ids else []
        )
        owned_ids = {
            str(row.order_id) for row in ledger_rows if row.order_id
        } | {
            str(row.order_id) for row in backups if row.order_id
        }
        unowned_exact = [
            row for row in exact_stop_candidates
            if str(row.get("ordId") or row.get("orderId") or "") not in owned_ids
        ]
        action_kind: RecoveryActionKind = "noop"
        action_record_id = None
        selected_order_id = None
        selected_trigger_price = None
        reason_code = "already_converged_or_no_safe_action"
        owned_stop_rows = [
            row for row in ledger_rows
            if str(row.purpose) in {"stop_loss", "combined"}
            and str(row.status) in {"verified", "active"}
        ]
        terminal_or_ambiguous = _terminal_recovery_reason(
            intent=intent,
            rescue=rescue,
            convergence=convergence,
            backups=backups,
            foreign_owners=foreign_owners,
        )
        if terminal_or_ambiguous is not None:
            reason_code = terminal_or_ambiguous
        elif len(unowned_exact) == 1 and not owned_stop_rows:
            action_kind = "adopt_unique_native_stop"
            selected = unowned_exact[0]
            selected_order_id = str(
                selected.get("ordId") or selected.get("orderId")
            )
            selected_trigger_price = str(
                selected.get("slTriggerPx") or selected.get("slTriggerPrice")
            )
            reason_code = None
        elif (
            intent is not None
            and str(intent.recovery_disposition or "") == "exact_backup"
            and str(intent.recovery_state) in {"pending", "retrying", "failed"}
            and not any(
                str(row.status) in {
                    "active", "submitting", "pending_readback",
                    "unknown_exchange_outcome",
                }
                for row in backups
            )
            and len(parent_events) == 1
            and str(leg.order_id or "") == str(intent.parent_trigger_order_id or "")
            and proposed_stop is not None
            and _stop_is_liquidation_safe(
                side=side,
                stop_loss=proposed_stop,
                liquidation_price=position.get("liqPx")
                or position.get("liquidationPrice"),
            )
            and not isinstance(rescue_preflight, str)
        ):
            action_kind = "create_exact_backup_stop"
            action_record_id = int(intent.id)
            reason_code = None
        elif (
            convergence is not None
            and str(convergence.status) == "ready"
            and str(convergence.pos_id or normalized_pos_id) == normalized_pos_id
            and not isinstance(convergence_preflight, str)
        ):
            action_kind = "converge_staged_take_profit"
            action_record_id = int(convergence.id)
            reason_code = None

        target_sizes = _target_take_profit_sizes(
            convergence,
            position_size=_positive_decimal(position.get("pos")) or Decimal("0"),
            contract_spec_provider=contract_spec_provider,
            inst_id=inst_id,
        )
        if not isinstance(convergence_preflight, str):
            target_sizes = tuple(
                {
                    "price": str(payload["tpTriggerPx"]),
                    "size": str(payload["sz"]),
                }
                for payload in convergence_preflight.payloads
            )
        contract_spec = _bounded_contract_spec(
            contract_spec_provider, inst_id=inst_id
        )
        excluded = _excluded_pending_candidates(
            pending,
            pos_id=normalized_pos_id,
            selected_order_id=selected_order_id,
        )
        evidence = {
            "schema_version": 2,
            "action_kind": action_kind,
            "reason_code": reason_code,
            "binding": {"id": int(binding.id), "status": str(binding.status)},
            "leg": {
                "id": int(leg.id), "status": str(leg.status),
                "attribution_status": str(leg.attribution_status),
                "order_id": leg.order_id,
                "request_fingerprint": _text_fingerprint(leg.request_json),
            },
            "position": _bounded_position(position),
            "pending": sorted(
                (_bounded_pending(row) for row in pending),
                key=lambda row: (
                    row["order_id"], row["pos_id"], row["instrument_id"],
                    row["side"], row["size"], row["stop_price"],
                    row["take_profit_price"],
                ),
            ),
            "ledger": [
                {
                    "id": int(row.id), "order_id": row.order_id,
                    "purpose": str(row.purpose), "status": str(row.status),
                    "trigger_price": str(row.trigger_price) if row.trigger_price else None,
                    "size_text": row.size_text,
                }
                for row in ledger_rows
            ],
            "backups": [
                {"id": int(row.id), "order_id": row.order_id, "status": str(row.status)}
                for row in backups
            ],
            "intent": (
                {
                    "id": int(intent.id), "state": str(intent.recovery_state),
                    "disposition": intent.recovery_disposition,
                    "request_fingerprint": str(intent.request_fingerprint),
                    "parent_trigger_order_id": intent.parent_trigger_order_id,
                    "last_reason_code": intent.last_reason_code,
                }
                if intent is not None else None
            ),
            "convergence": (
                {
                    "id": int(convergence.id), "status": str(convergence.status),
                    "reason_code": convergence.reason_code,
                    "targets": convergence.desired_take_profits_json,
                    "request_fingerprint": _text_fingerprint(convergence.request_json),
                }
                if convergence is not None else None
            ),
            "rescue": (
                {
                    "id": int(rescue.id), "status": str(rescue.status),
                    "reason_code": rescue.reason_code,
                    "request_fingerprint": _text_fingerprint(rescue.request_json),
                }
                if rescue is not None else None
            ),
            "parent_events": [
                {
                    "id": int(row.id),
                    "order_id": row.order_id,
                    "request_fingerprint": _text_fingerprint(row.request_json),
                }
                for row in parent_events
            ],
            "foreign_owners": [
                {
                    "id": int(row.id), "order_id": row.order_id,
                    "pos_id": row.pos_id, "leg_id": int(row.execution_order_leg_id),
                    "status": row.status,
                }
                for row in foreign_owners
            ],
            "action_payload": {
                "selected_order_id": selected_order_id,
                "selected_trigger_price": selected_trigger_price,
                "proposed_stop": proposed_stop,
                "target_take_profit_sizes": target_sizes,
                "contract_spec": contract_spec,
                "rescue_preflight": (
                    rescue_preflight
                    if isinstance(rescue_preflight, str)
                    else dict(rescue_preflight[1])
                ),
                "take_profit_preflight": (
                    convergence_preflight
                    if isinstance(convergence_preflight, str)
                    else [dict(payload) for payload in convergence_preflight.payloads]
                ),
            },
        }
        fingerprint = _fingerprint(evidence)
        return PositionManagementLivenessRecoveryPlan(
            action_kind=action_kind,
            pos_id=normalized_pos_id,
            binding_id=int(binding.id),
            leg_id=int(leg.id),
            action_record_id=action_record_id,
            exact_position=_bounded_position(position),
            excluded_candidates=excluded,
            target_take_profit_sizes=target_sizes,
            fingerprint=fingerprint,
            reason_code=reason_code,
            selected_order_id=selected_order_id,
            selected_trigger_price=selected_trigger_price,
        )


def apply_position_management_liveness_recovery(
    session_factory: sessionmaker,
    *,
    pos_id: str,
    expected_fingerprint: str,
    deepcoin_client: Any,
    snapshot_loader: Callable[..., Any] = (
        load_deepcoin_execution_reconciliation_snapshot_read_only
    ),
    contract_spec_provider: Any | None = None,
    applied_at: datetime | None = None,
) -> PositionManagementLivenessRecoveryResult:
    """Rebuild and apply at most one normal mutation component."""

    if not str(expected_fingerprint or "").strip():
        raise ValueError("expected fingerprint is required")
    if (
        load_trading_settings(
            session_factory
        ).effective_position_management_liveness_v2_mode
        != "live"
    ):
        raise ValueError("position management liveness recovery is not live")
    now = applied_at or datetime.now(UTC)
    with position_authority_lock():
        plan = build_position_management_liveness_recovery_plan(
            session_factory,
            pos_id=pos_id,
            deepcoin_client=deepcoin_client,
            snapshot_loader=snapshot_loader,
            contract_spec_provider=contract_spec_provider,
            planned_at=now,
        )
        if plan.fingerprint != str(expected_fingerprint):
            raise ValueError("recovery plan fingerprint changed")
        if plan.action_kind == "noop":
            return _result(plan, "noop", plan.reason_code)
        if plan.action_kind == "adopt_unique_native_stop":
            with session_factory() as session:
                leg = session.get(ExecutionOrderLeg, int(plan.leg_id or 0))
                try:
                    authoritative_leg = require_verified_position_ownership(
                        session, venue="deepcoin", pos_id=plan.pos_id
                    )
                except PositionAttributionError as exc:
                    raise ValueError(
                        "immutable ownership revalidation failed"
                    ) from exc
                if (
                    leg is None
                    or int(authoritative_leg.id) != int(leg.id)
                    or not plan.selected_order_id
                    or not plan.selected_trigger_price
                ):
                    raise ValueError("immutable ownership revalidation failed")
                upsert_protection_ledger_row(
                    session,
                    venue="deepcoin",
                    execution_binding_id=int(plan.binding_id or 0),
                    execution_order_leg_id=int(leg.id),
                    strategy_instance_id=leg.strategy_instance_id,
                    pos_id=plan.pos_id,
                    instrument_id=str(plan.exact_position["instrument_id"]),
                    side=str(plan.exact_position["side"]),
                    order_id=plan.selected_order_id,
                    purpose="stop_loss",
                    trigger_price=plan.selected_trigger_price,
                    size_text=None,
                    status="verified",
                    evidence_source="reviewed_liveness_recovery",
                    evidence={"fingerprint": plan.fingerprint},
                    seen_at=now,
                )
                intent = (
                    session.query(TriggerProtectionIntent)
                    .filter(TriggerProtectionIntent.execution_order_leg_id == leg.id)
                    .order_by(TriggerProtectionIntent.id.desc())
                    .first()
                )
                if intent is not None:
                    intent.recovery_state = "adopted"
                    intent.adopted_order_id = plan.selected_order_id
                    intent.recovery_disposition = None
                    intent.updated_at = now
                session.commit()
            return _result(plan, "applied", None)
        if plan.action_kind == "create_exact_backup_stop":
            rescue_plan = plan_trigger_protection_stop_rescue(
                session_factory,
                intent_id=int(plan.action_record_id or 0),
                deepcoin_client=deepcoin_client,
                planned_at=now,
            )
            if rescue_plan.status != "ready" or rescue_plan.rescue_id is None:
                return _result(plan, "recovery_required", rescue_plan.reason_code)
            outcome = execute_trigger_protection_stop_rescue(
                session_factory,
                rescue_id=int(rescue_plan.rescue_id),
                deepcoin_client=deepcoin_client,
                executed_at=now,
            )
            status = str(outcome.get("status") or "recovery_required")
            return _result(
                plan,
                "recovery_required" if status in {"submit_unknown", "conflicted"} else status,
                str(outcome.get("reason") or "") or None,
            )
        outcome = execute_trigger_take_profit_convergence(
            session_factory,
            convergence_id=int(plan.action_record_id or 0),
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            executed_at=now,
        )
        status = str(outcome.get("status") or "recovery_required")
        return _result(
            plan,
            "recovery_required" if status in {"submit_unknown", "conflicted"} else status,
            str(outcome.get("reason") or "") or None,
        )


def _result(plan, status: str, reason_code: str | None):
    return PositionManagementLivenessRecoveryResult(
        status=status,
        action_kind=plan.action_kind,
        pos_id=plan.pos_id,
        fingerprint=plan.fingerprint,
        reason_code=reason_code,
    )


def _noop_plan(
    pos_id: str,
    reason_code: str,
    *,
    snapshot: Any,
    binding_id: int | None = None,
    leg_id: int | None = None,
) -> PositionManagementLivenessRecoveryPlan:
    evidence = {
        "schema_version": 2,
        "action_kind": "noop",
        "pos_id": pos_id,
        "reason_code": reason_code,
        "positions": sorted(
            (
                _bounded_position(row)
                for row in getattr(snapshot, "positions", ())
                if isinstance(row, dict)
            ),
            key=lambda row: (
                row["pos_id"], row["instrument_id"], row["side"], row["size"]
            ),
        ),
        "pending": sorted(
            (
                _bounded_pending(row)
                for row in getattr(snapshot, "pending_trigger_orders", ())
                if isinstance(row, dict)
            ),
            key=lambda row: (
                row["order_id"], row["pos_id"], row["instrument_id"],
                row["side"], row["size"], row["stop_price"],
                row["take_profit_price"],
            ),
        ),
        "errors": sorted(str(key) for key in getattr(snapshot, "errors", {})),
    }
    return PositionManagementLivenessRecoveryPlan(
        action_kind="noop",
        pos_id=pos_id,
        binding_id=binding_id,
        leg_id=leg_id,
        action_record_id=None,
        exact_position={},
        excluded_candidates=(),
        target_take_profit_sizes=(),
        fingerprint=_fingerprint(evidence),
        reason_code=reason_code,
    )


def _target_pending_snapshot_complete(snapshot: Any, *, inst_id: str) -> bool:
    observations = getattr(snapshot, "pending_tpsl_observations", ())
    matching = [
        row for row in observations
        if isinstance(row, dict)
        and str(row.get("instrument_id") or "").upper() == inst_id
    ]
    return len(matching) == 1 and matching[0].get("complete") is True


def _terminal_or_inflight_state(session, *, pos_id: str) -> bool:
    return bool(
        session.query(PositionMutationIntent.id)
        .filter(PositionMutationIntent.pos_id == pos_id)
        .filter(PositionMutationIntent.status.in_((
            "reserved", "submitted", "submit_unknown", "recovery_required",
        )))
        .first()
        or session.query(BoundPositionCloseReservation.id)
        .filter(BoundPositionCloseReservation.pos_id == pos_id)
        .filter(BoundPositionCloseReservation.status.in_((
            "reserved", "submitted", "submit_unknown", "recovery_required",
        )))
        .first()
    )


def _terminal_recovery_reason(
    *, intent, rescue, convergence, backups, foreign_owners
):
    if foreign_owners:
        return "native_stop_owned_by_another_position"
    if intent is not None:
        disposition = str(intent.recovery_disposition or "")
        state = str(intent.recovery_state or "")
        if disposition in {"manual_review", "terminal"}:
            return "protection_recovery_requires_manual_review"
        if state in {"submit_unknown", "recovery_required"}:
            return "protection_recovery_exchange_outcome_unknown"
    if any(
        str(row.status) in {
            "unknown_exchange_outcome", "pending_readback", "submitting"
        }
        for row in backups
    ):
        return "backup_stop_exchange_state_unknown"
    if rescue is not None and str(rescue.status) in {
        "submit_unknown", "recovery_required"
    }:
        return "stop_rescue_exchange_outcome_unknown"
    if convergence is not None:
        status = str(convergence.status or "")
        reason = str(convergence.reason_code or "")
        if status == "submit_unknown":
            return "take_profit_exchange_outcome_unknown"
        if status == "conflicted" or "immutable" in reason or "unowned" in reason:
            return "take_profit_immutable_ownership_conflict"
    return None


def _text_fingerprint(value: object) -> str | None:
    if value in (None, ""):
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _proposed_stop_from_parent_events(events) -> str | None:
    if len(events) != 1:
        return None
    try:
        request = json.loads(events[0].request_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict):
        return None
    value = (
        request.get("slTriggerPx")
        or request.get("slTriggerPrice")
        or request.get("closeSLTriggerPrice")
    )
    return str(value) if value not in (None, "") else None


def _bounded_contract_spec(provider: Any | None, *, inst_id: str):
    if provider is None:
        return None
    try:
        spec = provider.get_contract_spec(inst_id)
    except Exception:
        return {"status": "unavailable"}
    if spec is None:
        return None
    return {
        "quantity_step": str(getattr(spec, "quantity_step", "")),
        "minimum_order_size": str(
            getattr(
                spec,
                "minimum_order_size",
                getattr(spec, "min_order_size", getattr(spec, "min_quantity", "")),
            )
        ),
    }


def _stop_is_liquidation_safe(*, side, stop_loss, liquidation_price) -> bool:
    stop = _positive_decimal(stop_loss)
    liquidation = _positive_decimal(liquidation_price)
    if stop is None or liquidation is None:
        return False
    if str(side).lower() == "short":
        return stop < liquidation
    if str(side).lower() == "long":
        return stop > liquidation
    return False


def _exact_native_stop_candidates(pending, *, pos_id: str, inst_id: str, side: str):
    return [
        row for row in pending
        if str(row.get("posId") or "") == pos_id
        and str(row.get("instId") or "").upper() == inst_id
        and str(row.get("posSide") or "").lower() == side
        and _positive_decimal(row.get("slTriggerPx") or row.get("slTriggerPrice"))
        is not None
        and str(row.get("slOrdPx") or "") in {"-1", "0"}
        and str(row.get("ordId") or row.get("orderId") or "").strip()
    ]


def _excluded_pending_candidates(pending, *, pos_id: str, selected_order_id: str | None):
    result = []
    for row in pending:
        order_id = str(row.get("ordId") or row.get("orderId") or "").strip()
        if order_id == selected_order_id:
            continue
        result.append({
            "order_id": order_id or "unknown",
            "reason_code": (
                "different_pos_id"
                if str(row.get("posId") or "") not in {"", pos_id}
                else "not_selected"
            ),
        })
    return tuple(result[:50])


def _target_take_profit_sizes(
    convergence,
    *,
    position_size: Decimal,
    contract_spec_provider: Any | None,
    inst_id: str,
):
    if convergence is None:
        return ()
    try:
        targets = json.loads(convergence.desired_take_profits_json)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(targets, list):
        return ()
    step = Decimal("1")
    if contract_spec_provider is not None:
        try:
            spec = contract_spec_provider.get_contract_spec(inst_id)
            step = Decimal(str(spec.quantity_step)) if spec is not None else step
        except Exception:
            return ()
    remaining = position_size
    result = []
    for index, row in enumerate(targets):
        if not isinstance(row, dict):
            return ()
        allocation = _positive_decimal(row.get("allocation_pct"))
        price = _positive_decimal(row.get("price"))
        if allocation is None or price is None:
            return ()
        if index == len(targets) - 1:
            quantity = remaining
        else:
            quantity = (position_size * allocation / Decimal("100") // step) * step
            remaining -= quantity
        result.append({"price": _decimal_text(price), "size": _decimal_text(quantity)})
    return tuple(result)


def _bounded_position(row: dict[str, Any]) -> dict[str, str]:
    return {
        "pos_id": str(row.get("posId") or row.get("pos_id") or ""),
        "instrument_id": str(row.get("instId") or "").upper(),
        "side": str(row.get("posSide") or row.get("side") or "").lower(),
        "size": str(row.get("pos") or row.get("size") or ""),
        "average_price": str(row.get("avgPx") or ""),
        "liquidation_price": str(row.get("liqPx") or ""),
    }


def _bounded_pending(row: dict[str, Any]) -> dict[str, str]:
    return {
        "order_id": str(row.get("ordId") or row.get("orderId") or ""),
        "pos_id": str(row.get("posId") or ""),
        "instrument_id": str(row.get("instId") or "").upper(),
        "side": str(row.get("posSide") or "").lower(),
        "stop_price": str(row.get("slTriggerPx") or ""),
        "take_profit_price": str(row.get("tpTriggerPx") or ""),
        "size": str(row.get("sz") or ""),
    }


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _split_ids(value: object) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
