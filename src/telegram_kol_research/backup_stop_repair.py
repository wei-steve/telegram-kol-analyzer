"""Fingerprint-gated, one-position-at-a-time repair of backup stop orders."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from telegram_kol_research.execution_bindings import build_client_order_id
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionLedger,
)
from telegram_kol_research.native_tpsl import NativeTpslExpectation
from telegram_kol_research.native_tpsl import match_native_tpsl_order
from telegram_kol_research.native_tpsl import normalize_native_tpsl
from telegram_kol_research.position_attribution import has_authoritative_persisted_position
from telegram_kol_research.position_mutation_gateway import (
    submit_exact_position_sltp,
)
from telegram_kol_research.repair_confirmation import (
    consume_repair_confirmation_token,
    require_repair_confirmation_token_unused,
)
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.trigger_backup_stop import (
    BackupStopError,
    build_backup_stop_trigger_payload,
    calculate_backup_stop_price,
)


@dataclass(frozen=True, slots=True)
class BackupStopRepairAction:
    binding_id: int
    leg_id: int
    pos_id: str
    instrument_id: str
    side: str
    size: str
    primary_order_id: str
    primary_stop: str
    backup_stop: str
    liquidation_price: str
    client_order_id: str
    request_payload: dict[str, str]
    open_positions: tuple[dict[str, Any], ...]
    action_id: str = ""


@dataclass(frozen=True, slots=True)
class BackupStopRepairPlan:
    created_at: datetime
    actions: tuple[BackupStopRepairAction, ...]
    conflicts: tuple[dict[str, str], ...]
    database_fingerprint: str
    exchange_fingerprint: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class BackupStopRepairResult:
    status: str
    pos_id: str
    order_id: str | None = None
    reason_code: str | None = None


def build_backup_stop_repair_plan(
    session_factory,
    *,
    deepcoin_client,
    contract_spec_provider,
    now: datetime | None = None,
) -> BackupStopRepairPlan:
    """Build a read-only plan; this function never writes the database or exchange."""

    created_at = now or datetime.now(UTC)
    actions: list[BackupStopRepairAction] = []
    conflicts: list[dict[str, str]] = []
    positions_cache: dict[str, list[dict[str, Any]]] = {}
    pending_cache: dict[str, list[dict[str, Any]]] = {}
    database_evidence: list[dict[str, str]] = []
    exchange_evidence: list[dict[str, str]] = []
    backup_stop_buffer_bps = load_trading_settings(session_factory).trigger_backup_stop_buffer_bps
    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding, ExecutionOrderLeg)
            .join(ExecutionOrderLeg, ExecutionOrderLeg.execution_binding_id == ExecutionBinding.id)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(("open", "active")))
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.order_kind != "manual_bind")
            .filter(ExecutionOrderLeg.status == "active")
            .filter(ExecutionOrderLeg.attribution_status == "verified")
            .order_by(ExecutionOrderLeg.id.asc())
            .all()
        )
        for binding, leg in rows:
            pos_id = str(leg.pos_id or "").strip()
            if not pos_id or not has_authoritative_persisted_position(leg, session=session):
                continue
            instrument_id = f"{str(binding.symbol).upper()}-USDT-SWAP"
            primary = _primary(session, binding_id=int(binding.id), leg_id=int(leg.id), pos_id=pos_id)
            existing = _existing_backup(session, pos_id=pos_id)
            database_evidence.append({
                "binding_id": str(binding.id), "leg_id": str(leg.id), "pos_id": pos_id,
                "primary_order_id": str(primary.order_id) if primary is not None else "",
                "existing_backup_order_id": str(existing.order_id or "") if existing else "",
                "existing_backup_status": str(existing.status) if existing else "",
            })
            if primary is None or not str(primary.trigger_price or "").strip():
                conflicts.append(_conflict(pos_id, "primary_stop_not_verified"))
                continue
            spec = contract_spec_provider.get_contract_spec(instrument_id)
            if spec is None:
                conflicts.append(_conflict(pos_id, "contract_spec_unavailable"))
                continue
            try:
                if instrument_id not in positions_cache:
                    positions_cache[instrument_id] = list(
                        deepcoin_client.list_positions(inst_id=instrument_id)
                    )
                if instrument_id not in pending_cache:
                    pending_cache[instrument_id] = list(
                        deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id)
                    )
                positions = positions_cache[instrument_id]
                pending = pending_cache[instrument_id]
            except Exception:
                conflicts.append(_conflict(pos_id, "exchange_snapshot_unavailable"))
                continue
            exact = [
                row for row in positions if isinstance(row, dict)
                and str(row.get("posId") or row.get("pos_id") or "") == pos_id
            ]
            if len(exact) != 1:
                conflicts.append(_conflict(pos_id, "live_position_not_unique"))
                continue
            position = exact[0]
            if (
                str(position.get("instId") or "").upper() != instrument_id
                or str(position.get("posSide") or position.get("side") or "").lower() != str(binding.side).lower()
                or str(position.get("mrgPosition") or position.get("posMode") or "").lower() != "split"
            ):
                conflicts.append(_conflict(pos_id, "live_position_identity_mismatch"))
                continue
            size = str(position.get("pos") or position.get("size") or "")
            liquidation = str(position.get("liqPx") or position.get("liquidationPrice") or "")
            try:
                backup_stop = calculate_backup_stop_price(
                    primary_stop=str(primary.trigger_price), side=str(binding.side),
                    price_tick=spec.price_tick, buffer_bps=backup_stop_buffer_bps,
                )
                payload = build_backup_stop_trigger_payload(
                    instrument_id=instrument_id, side=str(binding.side), margin_mode=str(binding.margin_mode),
                    pos_id=pos_id, primary_stop=str(primary.trigger_price), backup_stop=backup_stop,
                    liquidation_price=liquidation, size=size,
                    client_order_id=build_client_order_id(
                        strategy_instance_id=str(binding.strategy_instance_id),
                        leg_index=int(leg.leg_index), purpose="backup_stop",
                    ),
                )
            except BackupStopError:
                conflicts.append(_conflict(pos_id, "backup_stop_unsafe"))
                continue
            if existing is not None:
                action = BackupStopRepairAction(
                    binding_id=int(binding.id), leg_id=int(leg.id), pos_id=pos_id,
                    instrument_id=instrument_id, side=str(binding.side).lower(), size=size,
                    primary_order_id=str(primary.order_id), primary_stop=str(primary.trigger_price),
                    backup_stop=backup_stop, liquidation_price=liquidation,
                    client_order_id=build_client_order_id(
                        strategy_instance_id=str(binding.strategy_instance_id),
                        leg_index=int(leg.leg_index), purpose="backup_stop",
                    ),
                    request_payload=payload,
                    open_positions=tuple(row for row in positions if isinstance(row, dict)),
                )
                action = replace(
                    action,
                    action_id=_fingerprint(asdict(action)),
                )
                if (
                    str(existing.status) == "active"
                    and str(existing.order_id or "").strip()
                    and _verified_pending_backup(
                        pending,
                        order_id=str(existing.order_id),
                        action=action,
                        position=position,
                        open_positions=action.open_positions,
                    )
                ):
                    exchange_evidence.append({
                        "pos_id": pos_id,
                        "existing_backup_order_id": str(existing.order_id),
                        "pending_order_ids": ",".join(
                            sorted(_order_id(row) for row in pending if _order_id(row))
                        ),
                    })
                    continue
                conflicts.append(_conflict(
                    pos_id,
                    "backup_stop_pending_readback"
                    if str(existing.status) in {"submitting", "pending_readback"}
                    else "backup_exchange_outcome_unknown"
                    if str(existing.status) == "unknown_exchange_outcome"
                    else "backup_stop_missing_on_exchange",
                ))
                continue
            if _unowned_similar_backup(
                pending,
                payload=payload,
                position=position,
                open_positions=positions,
            ):
                conflicts.append(_conflict(pos_id, "backup_similar_unscoped_order"))
                continue
            action = BackupStopRepairAction(
                binding_id=int(binding.id), leg_id=int(leg.id), pos_id=pos_id,
                instrument_id=instrument_id, side=str(binding.side).lower(), size=size,
                primary_order_id=str(primary.order_id), primary_stop=str(primary.trigger_price),
                backup_stop=backup_stop, liquidation_price=liquidation,
                client_order_id=build_client_order_id(
                    strategy_instance_id=str(binding.strategy_instance_id),
                    leg_index=int(leg.leg_index), purpose="backup_stop",
                ),
                request_payload=payload,
                open_positions=tuple(row for row in positions if isinstance(row, dict)),
            )
            actions.append(
                replace(action, action_id=_fingerprint(asdict(action)))
            )
            exchange_evidence.append({
                "pos_id": pos_id, "size": size, "liquidation_price": liquidation,
                "pending_order_ids": ",".join(sorted(_order_id(row) for row in pending if _order_id(row))),
            })
    actions_tuple = tuple(sorted(actions, key=lambda item: item.pos_id))
    conflicts_tuple = tuple(sorted(conflicts, key=lambda item: (item["pos_id"], item["reason"])))
    database_fingerprint = _fingerprint(database_evidence)
    exchange_fingerprint = _fingerprint(exchange_evidence)
    fingerprint = _fingerprint({
        "actions": [asdict(action) for action in actions_tuple], "conflicts": list(conflicts_tuple),
        "database": database_fingerprint, "exchange": exchange_fingerprint,
    })
    return BackupStopRepairPlan(
        created_at=created_at, actions=actions_tuple, conflicts=conflicts_tuple,
        database_fingerprint=database_fingerprint, exchange_fingerprint=exchange_fingerprint,
        fingerprint=fingerprint,
    )


def apply_backup_stop_repair_plan(
    session_factory,
    plan: BackupStopRepairPlan,
    *,
    deepcoin_client,
    contract_spec_provider,
    pos_id: str,
    action_id: str,
    expected_fingerprint: str,
    confirmation_token: str,
    now: datetime | None = None,
) -> BackupStopRepairResult:
    """Apply exactly one reviewed action; unknown outcomes are never retried."""

    clean_pos_id = str(pos_id or "").strip()
    if not clean_pos_id:
        raise ValueError("pos_id is required")
    if not str(action_id or "").strip():
        raise ValueError("action_id is required")
    if not expected_fingerprint:
        raise ValueError("expected fingerprint is required")
    if len(str(confirmation_token or "").strip()) < 8:
        raise ValueError("confirmation_token is required")
    if expected_fingerprint != plan.fingerprint:
        raise ValueError("repair plan fingerprint mismatch")
    reviewed = [
        action
        for action in plan.actions
        if action.pos_id == clean_pos_id and action.action_id == action_id
    ]
    if len(reviewed) != 1:
        raise ValueError("exactly one reviewed repair action is required")
    confirmation_key = _confirmation_idempotency_key(
        "backup-stop-repair",
        action_id=action_id,
        confirmation_token=confirmation_token,
    )
    require_repair_confirmation_token_unused(
        session_factory,
        confirmation_token=confirmation_token,
    )
    fresh = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider, now=now,
    )
    if fresh.fingerprint != expected_fingerprint:
        raise ValueError("repair plan fingerprint changed")
    selected = [
        action
        for action in fresh.actions
        if action.pos_id == clean_pos_id and action.action_id == action_id
    ]
    if len(selected) != 1:
        raise ValueError("exactly one repair action is required for pos_id")
    action = selected[0]
    observed_at = now or datetime.now(UTC)
    consume_repair_confirmation_token(
        session_factory,
        confirmation_token=confirmation_token,
        action_kind="backup_stop_repair",
        action_id=action.action_id,
        pos_id=action.pos_id,
        consumed_at=observed_at,
    )
    with session_factory() as session:
        row = PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=action.binding_id,
            execution_order_leg_id=action.leg_id, pos_id=action.pos_id,
            instrument_id=action.instrument_id, side=action.side, trigger_price=action.backup_stop,
            client_order_id=action.client_order_id, status="submitting",
            request_json=json.dumps(action.request_payload, ensure_ascii=False, sort_keys=True),
            created_at=observed_at, updated_at=observed_at,
        )
        session.add(row)
        session.commit()
        row_id = int(row.id)
    try:
        response = submit_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=deepcoin_client,
            pos_id=action.pos_id,
            payload=action.request_payload,
            idempotency_key=confirmation_key,
            live_execution_gate=lambda: _repair_submission_still_reserved(
                session_factory, row_id
            ),
            now_provider=lambda: observed_at,
        )
        order_id = _response_order_id(response)
    except Exception as exc:
        return _mark_unknown(
            session_factory,
            row_id=row_id,
            pos_id=action.pos_id,
            now=observed_at,
            error=exc,
        )
    try:
        positions = list(deepcoin_client.list_positions())
        exact = [
            row for row in positions if isinstance(row, dict)
            and str(row.get("posId") or row.get("pos_id") or "") == action.pos_id
        ]
        if len(exact) != 1 or not _live_position_matches_action(exact[0], action):
            return _mark_pending_readback(
                session_factory,
                row_id=row_id,
                pos_id=action.pos_id,
                now=observed_at,
                error=RuntimeError("live_position_snapshot_not_unique_or_mismatched"),
            )
        pending = list(deepcoin_client.list_trigger_orders_pending(inst_id=action.instrument_id))
        verified_order_id = _verified_pending_backup(
            pending,
            order_id=order_id,
            action=action,
            position=exact[0],
            open_positions=tuple(row for row in positions if isinstance(row, dict)),
        )
    except Exception as exc:
        return _mark_pending_readback(
            session_factory,
            row_id=row_id,
            pos_id=action.pos_id,
            now=observed_at,
            error=exc,
        )
    if not verified_order_id:
        return _mark_pending_readback(
            session_factory, row_id=row_id, pos_id=action.pos_id, now=observed_at,
            error=RuntimeError("native backup stop pending readback"),
        )
    with session_factory() as session:
        row = session.get(PositionBackupStopOrder, row_id)
        if row is None:
            return BackupStopRepairResult("unknown_exchange_outcome", action.pos_id, reason_code="reservation_missing")
        row.order_id = verified_order_id
        row.status = "active"
        row.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
        row.submitted_at = observed_at
        row.updated_at = observed_at
        session.commit()
    return BackupStopRepairResult("active", action.pos_id, order_id=verified_order_id)


def _primary(session, *, binding_id: int, leg_id: int, pos_id: str):
    return (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.execution_binding_id == binding_id)
        .filter(PositionProtectionLedger.execution_order_leg_id == leg_id)
        .filter(PositionProtectionLedger.pos_id == pos_id)
        .filter(PositionProtectionLedger.status == "verified")
        .filter(PositionProtectionLedger.purpose.in_(("stop_loss", "combined")))
        .order_by(PositionProtectionLedger.id.asc())
        .first()
    )


def _repair_submission_still_reserved(session_factory, row_id: int) -> bool:
    """Recheck that this reviewed repair has not been cancelled or superseded."""

    with session_factory() as session:
        row = session.get(PositionBackupStopOrder, row_id)
        return row is not None and row.status == "submitting"


def _confirmation_idempotency_key(
    prefix: str,
    *,
    action_id: str,
    confirmation_token: str,
) -> str:
    token_hash = hashlib.sha256(
        str(confirmation_token).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:{action_id}:confirmation:{token_hash}"


def _existing_backup(session, *, pos_id: str) -> PositionBackupStopOrder | None:
    return session.query(PositionBackupStopOrder).filter(
        PositionBackupStopOrder.venue == "deepcoin",
        PositionBackupStopOrder.pos_id == pos_id,
        PositionBackupStopOrder.status.in_(
            ("submitting", "pending_readback", "active", "unknown_exchange_outcome")
        ),
    ).order_by(PositionBackupStopOrder.id.asc()).first()


def _unowned_similar_backup(
    pending,
    *,
    payload: dict[str, str],
    position: dict[str, Any],
    open_positions: list[dict[str, Any]],
) -> bool:
    # A full-position native TPSL without posId cannot be safely assigned when
    # the exchange omits the creation-time identity fields. Freeze only when
    # it is an exact duplicate of the backup stop we intend to create; other
    # unowned full-position stops must not make a distinct target ambiguous.
    for raw in pending:
        if not isinstance(raw, dict):
            continue
        order = normalize_native_tpsl(raw)
        if order is None or order.pos_id is not None or order.size != 0:
            continue
        if (
            order.inst_id == payload["instId"]
            and order.pos_side == payload["posSide"]
            and order.stop_loss_trigger_price is not None
            and str(order.stop_loss_trigger_price) == payload["slTriggerPx"]
        ):
            return True
    return False


def _verified_pending_backup(
    pending,
    *,
    order_id: str | None,
    action: BackupStopRepairAction,
    position: dict[str, Any],
    open_positions: tuple[dict[str, Any], ...],
) -> str | None:
    if not order_id or not open_positions:
        return None
    match = match_native_tpsl_order(
        position,
        [row for row in pending if isinstance(row, dict)],
        NativeTpslExpectation(
            purpose="stop_loss",
            trigger_price=action.backup_stop,
            # Position TPSL uses sz=0 to close the exact posId in full.
            size="0",
            ord_id=order_id,
        ),
        open_positions=list(open_positions),
    )
    return match.order.ord_id if match.status == "verified" and match.order is not None else None


def _live_position_matches_action(position: dict[str, Any], action: BackupStopRepairAction) -> bool:
    return (
        str(position.get("instId") or "").upper() == action.instrument_id
        and str(position.get("posSide") or position.get("side") or "").lower() == action.side
        and str(position.get("mrgPosition") or position.get("posMode") or "").lower() == "split"
        and str(position.get("pos") or position.get("size") or "") == action.size
    )


def _mark_pending_readback(
    session_factory,
    *,
    row_id: int,
    pos_id: str,
    now: datetime,
    error: Exception,
) -> BackupStopRepairResult:
    with session_factory() as session:
        row = session.get(PositionBackupStopOrder, row_id)
        if row is not None:
            row.status = "pending_readback"
            row.error_json = json.dumps({"error": str(error)[:512]}, ensure_ascii=False)
            row.updated_at = now
            session.commit()
    return BackupStopRepairResult("pending_readback", pos_id, reason_code="backup_stop_pending_readback")


def _mark_unknown(session_factory, *, row_id: int, pos_id: str, now: datetime, error: Exception) -> BackupStopRepairResult:
    with session_factory() as session:
        row = session.get(PositionBackupStopOrder, row_id)
        if row is not None:
            row.status = "unknown_exchange_outcome"
            row.error_json = json.dumps({"error": str(error)[:512]}, ensure_ascii=False)
            row.updated_at = now
            session.commit()
    return BackupStopRepairResult("unknown_exchange_outcome", pos_id, reason_code="backup_exchange_outcome_unknown")


def _response_order_id(response: object) -> str | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, dict):
        value = data.get("ordId") or data.get("orderId") or data.get("id")
        return str(value) if value not in (None, "") else None
    return None


def _order_id(row: object) -> str:
    return str(row.get("ordId") or row.get("orderId") or "") if isinstance(row, dict) else ""


def _conflict(pos_id: str, reason: str) -> dict[str, str]:
    return {"pos_id": pos_id, "reason": reason}


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
