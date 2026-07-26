"""One-position-at-a-time migration from legacy generic stops to native TPSL.

This migration is deliberately conservative.  It creates a native backup stop
first, confirms that exact exchange row in the pending TPSL snapshot, and only
then sends the legacy generic-stop cancellation.  Every uncertain state leaves
the legacy stop untouched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.execution_events import ExecutionEventRecord, record_execution_event
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionLedger,
)
from telegram_kol_research.native_tpsl import (
    NativeTpslExpectation,
    match_native_tpsl_order,
    normalize_native_tpsl,
)
from telegram_kol_research.position_mutation_gateway import (
    exact_position_write_gate,
    submit_exact_position_sltp,
)
from telegram_kol_research.repair_confirmation import (
    consume_repair_confirmation_token,
    require_repair_confirmation_token_unused,
)


@dataclass(frozen=True, slots=True)
class NativeTpslMigrationAction:
    legacy_backup_row_id: int
    binding_id: int
    leg_id: int
    pos_id: str
    instrument_id: str
    side: str
    size: str
    primary_stop: str
    legacy_order_id: str
    native_stop: str
    request_payload: dict[str, str]
    action_id: str = ""


@dataclass(frozen=True, slots=True)
class NativeTpslMigrationPlan:
    created_at: datetime
    actions: tuple[NativeTpslMigrationAction, ...]
    conflicts: tuple[dict[str, str], ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class NativeTpslMigrationResult:
    status: str
    pos_id: str
    native_order_id: str | None = None
    reason_code: str | None = None


def build_native_tpsl_migration_plan(
    session_factory,
    *,
    deepcoin_client,
    now: datetime | None = None,
) -> NativeTpslMigrationPlan:
    """Return a read-only migration plan for currently owned legacy backups."""

    created_at = now or datetime.now(UTC)
    actions: list[NativeTpslMigrationAction] = []
    conflicts: list[dict[str, str]] = []
    positions_cache: dict[str, list[dict[str, Any]]] = {}
    pending_cache: dict[str, list[dict[str, Any]]] = {}
    with session_factory() as session:
        rows = (
            session.query(PositionBackupStopOrder, ExecutionBinding, ExecutionOrderLeg)
            .join(ExecutionBinding, ExecutionBinding.id == PositionBackupStopOrder.execution_binding_id)
            .join(ExecutionOrderLeg, ExecutionOrderLeg.id == PositionBackupStopOrder.execution_order_leg_id)
            .filter(PositionBackupStopOrder.venue == "deepcoin")
            .filter(PositionBackupStopOrder.status == "active")
            .filter(ExecutionBinding.status.in_(("open", "active")))
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.status == "active")
            .filter(ExecutionOrderLeg.attribution_status == "verified")
            .order_by(PositionBackupStopOrder.id.asc())
            .all()
        )
        for backup, binding, leg in rows:
            if not _is_legacy_generic(backup.request_json):
                continue
            pos_id = str(backup.pos_id or "").strip()
            legacy_order_id = str(backup.order_id or "").strip()
            if not pos_id or not legacy_order_id:
                conflicts.append(_conflict(pos_id, "legacy_backup_identity_missing"))
                continue
            primary = _primary(session, binding_id=int(binding.id), leg_id=int(leg.id), pos_id=pos_id)
            if primary is None or not str(primary.trigger_price or "").strip():
                conflicts.append(_conflict(pos_id, "primary_stop_not_verified"))
                continue
            instrument_id = str(backup.instrument_id or "").upper()
            side = str(backup.side or binding.side or "").lower()
            if not instrument_id or side not in {"long", "short"}:
                conflicts.append(_conflict(pos_id, "legacy_backup_identity_missing"))
                continue
            try:
                if instrument_id not in positions_cache:
                    positions_cache[instrument_id] = list(deepcoin_client.list_positions(inst_id=instrument_id))
                if instrument_id not in pending_cache:
                    pending_cache[instrument_id] = list(
                        deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id)
                    )
            except Exception:
                conflicts.append(_conflict(pos_id, "exchange_snapshot_unavailable"))
                continue
            positions = [row for row in positions_cache[instrument_id] if isinstance(row, dict)]
            pending = [row for row in pending_cache[instrument_id] if isinstance(row, dict)]
            exact_positions = [row for row in positions if _position_id(row) == pos_id]
            if len(exact_positions) != 1:
                conflicts.append(_conflict(pos_id, "live_position_not_unique"))
                continue
            position = exact_positions[0]
            size = _position_size(position)
            if (
                not size
                or str(position.get("instId") or "").upper() != instrument_id
                or _side(position.get("posSide") or position.get("side")) != side
                or str(position.get("mrgPosition") or position.get("posMode") or "").lower() != "split"
            ):
                conflicts.append(_conflict(pos_id, "live_position_identity_mismatch"))
                continue
            native_stop = str(backup.trigger_price or "").strip()
            if _decimal(native_stop) is None or _decimal(native_stop) <= 0:
                conflicts.append(_conflict(pos_id, "legacy_backup_trigger_invalid"))
                continue
            margin_mode = str(binding.margin_mode or "").lower()
            if margin_mode not in {"cross", "isolated"}:
                conflicts.append(_conflict(pos_id, "margin_mode_invalid"))
                continue
            action = NativeTpslMigrationAction(
                legacy_backup_row_id=int(backup.id), binding_id=int(binding.id), leg_id=int(leg.id),
                pos_id=pos_id, instrument_id=instrument_id, side=side, size=size,
                primary_stop=str(primary.trigger_price), legacy_order_id=legacy_order_id,
                native_stop=native_stop,
                request_payload={
                    "instType": "SWAP", "instId": instrument_id, "posId": pos_id,
                    "posSide": side, "mrgPosition": "split", "tdMode": margin_mode,
                    "slTriggerPx": native_stop, "slTriggerPxType": "last", "slOrdPx": "-1",
                },
            )
            action = replace(
                action,
                action_id=_fingerprint(asdict(action)),
            )
            legacy_rows = [row for row in pending if _order_id(row) == legacy_order_id]
            if len(legacy_rows) != 1 or normalize_native_tpsl(legacy_rows[0]) is not None:
                conflicts.append(_conflict(pos_id, "legacy_backup_not_pending"))
                continue
            if _decimal(_row_size(legacy_rows[0])) != _decimal(size):
                conflicts.append(_conflict(pos_id, "legacy_backup_size_mismatch"))
                continue
            if not _matches_legacy_generic_stop(legacy_rows[0], action=action):
                conflicts.append(_conflict(pos_id, "legacy_backup_payload_mismatch"))
                continue
            if _has_unowned_native_stop(pending, instrument_id=instrument_id, side=side):
                # An existing native row has no provable system ownership at this
                # point.  This includes the user's manual 63,000 BTC stop.
                conflicts.append(_conflict(pos_id, "native_stop_unowned"))
                continue
            actions.append(action)
    action_tuple = tuple(sorted(actions, key=lambda action: action.pos_id))
    conflict_tuple = tuple(sorted(conflicts, key=lambda conflict: (conflict["pos_id"], conflict["reason"])))
    return NativeTpslMigrationPlan(
        created_at=created_at,
        actions=action_tuple,
        conflicts=conflict_tuple,
        fingerprint=_fingerprint({"actions": [asdict(action) for action in action_tuple], "conflicts": conflict_tuple}),
    )


def apply_native_tpsl_migration_plan(
    session_factory,
    plan: NativeTpslMigrationPlan,
    *,
    deepcoin_client,
    pos_id: str,
    action_id: str,
    expected_fingerprint: str,
    confirmation_token: str,
    now: datetime | None = None,
) -> NativeTpslMigrationResult:
    """Migrate exactly one reviewed legacy backup; never retry uncertain writes."""

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
        raise ValueError("migration plan fingerprint mismatch")
    reviewed = [
        action
        for action in plan.actions
        if action.pos_id == clean_pos_id and action.action_id == action_id
    ]
    if len(reviewed) != 1:
        raise ValueError("exactly one reviewed migration action is required")
    confirmation_key = (
        f"native-tpsl-migration:{action_id}:confirmation:"
        f"{hashlib.sha256(str(confirmation_token).encode('utf-8')).hexdigest()}"
    )
    require_repair_confirmation_token_unused(
        session_factory,
        confirmation_token=confirmation_token,
    )
    fresh = build_native_tpsl_migration_plan(session_factory, deepcoin_client=deepcoin_client, now=now)
    if fresh.fingerprint != expected_fingerprint:
        raise ValueError("migration plan fingerprint changed")
    selected = [
        action
        for action in fresh.actions
        if action.pos_id == clean_pos_id and action.action_id == action_id
    ]
    if len(selected) != 1:
        raise ValueError("exactly one migration action is required for pos_id")
    action = selected[0]
    observed_at = now or datetime.now(UTC)
    consume_repair_confirmation_token(
        session_factory,
        confirmation_token=confirmation_token,
        action_kind="native_tpsl_migration",
        action_id=action.action_id,
        pos_id=action.pos_id,
        consumed_at=observed_at,
    )
    try:
        response = submit_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=deepcoin_client,
            pos_id=action.pos_id,
            payload=action.request_payload,
            idempotency_key=confirmation_key,
            live_execution_gate=lambda: exact_position_write_gate(
                session_factory, pos_id=action.pos_id
            ),
            now_provider=lambda: observed_at,
        )
        response_order_id = _response_order_id(response)
    except Exception as exc:
        _record_event(
            session_factory, action, status="unknown", reason="native_submit_unknown",
            response={"error": str(exc)[:512]}, now=observed_at,
        )
        return NativeTpslMigrationResult("native_submit_unknown", action.pos_id, reason_code="native_submit_unknown")
    try:
        positions = [row for row in deepcoin_client.list_positions() if isinstance(row, dict)]
        exact = [row for row in positions if _position_id(row) == action.pos_id]
        pending = [
            row for row in deepcoin_client.list_trigger_orders_pending(inst_id=action.instrument_id)
            if isinstance(row, dict)
        ]
    except Exception as exc:
        _record_event(
            session_factory, action, status="pending_readback", reason="native_pending_readback",
            response={"response": response, "error": str(exc)[:512]}, now=observed_at,
        )
        return NativeTpslMigrationResult("native_pending_readback", action.pos_id, reason_code="native_pending_readback")
    if len(exact) != 1 or not response_order_id:
        _record_event(
            session_factory, action, status="pending_readback", reason="native_pending_readback",
            response={"response": response}, now=observed_at,
        )
        return NativeTpslMigrationResult("native_pending_readback", action.pos_id, reason_code="native_pending_readback")
    match = match_native_tpsl_order(
        exact[0], pending,
        NativeTpslExpectation(
            purpose="stop_loss", trigger_price=action.native_stop, size="0", ord_id=response_order_id,
        ),
        open_positions=positions,
    )
    legacy_rows = [row for row in pending if _order_id(row) == action.legacy_order_id]
    if match.status != "verified" or match.order is None:
        _record_event(
            session_factory, action, status="pending_readback", reason="native_pending_readback",
            response={"response": response, "match_status": match.status}, now=observed_at,
        )
        return NativeTpslMigrationResult(
            "native_pending_readback",
            action.pos_id,
            reason_code="native_pending_readback",
        )
    if (
        len(exact) != 1
        or not _live_position_matches_action(exact[0], action)
        or len(legacy_rows) != 1
        or not _matches_legacy_generic_stop(legacy_rows[0], action=action)
    ):
        _record_event(
            session_factory, action, status="pending_readback", reason="legacy_pending_recheck_failed",
            response={"response": response, "match_status": match.status}, now=observed_at,
        )
        return NativeTpslMigrationResult(
            "legacy_pending_recheck_failed",
            action.pos_id,
            reason_code="legacy_pending_recheck_failed",
        )
    native_order_id = str(match.order.ord_id)
    # Persist the independently verified native row before attempting legacy
    # cancellation.  The old row leaves the active uniqueness scope so both
    # records remain auditable while cancellation is in flight.
    with session_factory() as session:
        legacy = session.get(PositionBackupStopOrder, action.legacy_backup_row_id)
        if legacy is None or legacy.status != "active" or legacy.order_id != action.legacy_order_id:
            return NativeTpslMigrationResult("migration_state_changed", action.pos_id, reason_code="migration_state_changed")
        legacy.status = "migration_cancel_pending"
        legacy.updated_at = observed_at
        session.flush()
        session.add(PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=action.binding_id,
            execution_order_leg_id=action.leg_id, pos_id=action.pos_id,
            instrument_id=action.instrument_id, side=action.side, trigger_price=action.native_stop,
            order_id=native_order_id, client_order_id=f"migration-{native_order_id}",
            status="active", request_json=json.dumps(action.request_payload, sort_keys=True),
            response_json=json.dumps(response, ensure_ascii=False, sort_keys=True),
            submitted_at=observed_at, created_at=observed_at, updated_at=observed_at,
        ))
        _record_event(
            session_factory, action, status="verified", reason="native_tpsl_verified",
            response={"response": response, "native_order_id": native_order_id}, now=observed_at,
            session=session,
        )
        session.commit()
    try:
        cancel_response = deepcoin_client.cancel_trigger_order({
            "instId": action.instrument_id, "ordId": action.legacy_order_id,
        })
    except Exception as exc:
        _mark_legacy_cancel_pending(
            session_factory,
            action,
            observed_at,
            reason="legacy_cancel_unknown",
            status="unknown",
            response={"error": str(exc)[:512]},
        )
        return NativeTpslMigrationResult("legacy_cancel_unknown", action.pos_id, native_order_id, "legacy_cancel_unknown")
    if not _is_confirmed_legacy_cancel_response(cancel_response, order_id=action.legacy_order_id):
        rejected = _is_rejected_cancel_response(cancel_response)
        reason = "legacy_cancel_rejected" if rejected else "legacy_cancel_response_unconfirmed"
        _mark_legacy_cancel_pending(
            session_factory,
            action,
            observed_at,
            reason=reason,
            status="rejected" if rejected else "unconfirmed",
            response=_response_evidence(cancel_response),
        )
        result_status = "legacy_cancel_rejected" if rejected else "legacy_cancel_unconfirmed"
        return NativeTpslMigrationResult(result_status, action.pos_id, native_order_id, reason)
    with session_factory() as session:
        legacy = session.get(PositionBackupStopOrder, action.legacy_backup_row_id)
        if legacy is not None:
            legacy.status = "migrated"
            legacy.completed_at = observed_at
            legacy.response_json = json.dumps(cancel_response, ensure_ascii=False, sort_keys=True)
            legacy.updated_at = observed_at
        _record_event(
            session_factory, action, status="confirmed", reason="legacy_generic_cancelled",
            response=cancel_response, now=observed_at, session=session,
        )
        session.commit()
    return NativeTpslMigrationResult("migrated", action.pos_id, native_order_id)


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


def _has_unowned_native_stop(pending: list[dict[str, Any]], *, instrument_id: str, side: str) -> bool:
    for raw in pending:
        order = normalize_native_tpsl(raw)
        if order is None or order.stop_loss_trigger_price is None:
            continue
        if order.inst_id == instrument_id and order.pos_side == side:
            return True
    return False


def _matches_legacy_generic_stop(
    row: dict[str, Any],
    *,
    action: NativeTpslMigrationAction,
) -> bool:
    """Require the exact legacy market-close semantics before cancellation."""

    expected_close_side = "sell" if action.side == "long" else "buy"
    return (
        normalize_native_tpsl(row) is None
        and _order_id(row) == action.legacy_order_id
        and str(row.get("instId") or "").upper() == action.instrument_id
        and _side(row.get("posSide")) == action.side
        and str(row.get("closePosId") or "") == action.pos_id
        and str(row.get("orderType") or "").lower() == "market"
        and str(row.get("side") or "").lower() == expected_close_side
        and _decimal(row.get("triggerPrice")) == _decimal(action.native_stop)
        and _decimal(_row_size(row)) == _decimal(action.size)
    )


def _live_position_matches_action(position: dict[str, Any], action: NativeTpslMigrationAction) -> bool:
    return (
        _position_id(position) == action.pos_id
        and str(position.get("instId") or "").upper() == action.instrument_id
        and _side(position.get("posSide") or position.get("side")) == action.side
        and str(position.get("mrgPosition") or position.get("posMode") or "").lower() == "split"
        and _decimal(_position_size(position)) == _decimal(action.size)
    )


def _is_legacy_generic(request_json: str | None) -> bool:
    try:
        payload = json.loads(request_json or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and any(
        key in payload for key in ("triggerPrice", "closePosId", "orderType")
    ) and "slTriggerPx" not in payload


def _position_id(row: dict[str, Any]) -> str:
    return str(row.get("posId") or row.get("pos_id") or row.get("id") or "")


def _position_size(row: dict[str, Any]) -> str:
    value = row.get("pos") if row.get("pos") not in (None, "") else row.get("size")
    return str(value or "")


def _row_size(row: dict[str, Any]) -> Any:
    return row.get("sz") if row.get("sz") not in (None, "") else row.get("size")


def _order_id(row: dict[str, Any]) -> str:
    return str(row.get("ordId") or row.get("orderId") or row.get("order_id") or "")


def _side(value: Any) -> str:
    result = str(value or "").lower()
    return {"buy": "long", "sell": "short"}.get(result, result)


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _response_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if isinstance(data, str) and data:
        return data
    if isinstance(data, dict):
        for key in ("ordId", "orderId", "order_id"):
            if data.get(key) not in (None, ""):
                return str(data[key])
    return None


def _is_confirmed_legacy_cancel_response(response: Any, *, order_id: str) -> bool:
    """Accept only an explicit successful cancellation for the requested order."""

    if not isinstance(response, dict) or not _response_codes_are_successful(response):
        return False
    return _response_contains_exact_order_id(response.get("data"), order_id=order_id)


def _is_rejected_cancel_response(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    return any(
        key in response and str(response[key]) not in {"0", "0.0"}
        for key in ("code", "sCode")
    )


def _response_codes_are_successful(response: dict[str, Any]) -> bool:
    codes = [response[key] for key in ("code", "sCode") if key in response]
    return bool(codes) and all(str(code) in {"0", "0.0"} for code in codes)


def _response_contains_exact_order_id(value: Any, *, order_id: str) -> bool:
    if isinstance(value, str):
        return value == order_id
    if isinstance(value, dict):
        return any(
            str(value.get(key) or "") == order_id
            for key in ("ordId", "orderId", "order_id", "id")
        )
    if isinstance(value, list):
        return any(_response_contains_exact_order_id(item, order_id=order_id) for item in value)
    return False


def _response_evidence(response: Any) -> dict[str, Any]:
    return response if isinstance(response, dict) else {"response": repr(response)[:512]}


def _mark_legacy_cancel_pending(
    session_factory,
    action: NativeTpslMigrationAction,
    now: datetime,
    *,
    reason: str,
    status: str,
    response: dict[str, Any],
) -> None:
    with session_factory() as session:
        row = session.get(PositionBackupStopOrder, action.legacy_backup_row_id)
        if row is not None:
            row.status = "migration_cancel_pending"
            row.error_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
            row.updated_at = now
        _record_event(
            session_factory, action, status=status, reason=reason,
            response=response, now=now, session=session,
        )
        session.commit()


def _record_event(
    session_factory,
    action: NativeTpslMigrationAction,
    *,
    status: str,
    reason: str,
    response: dict[str, Any],
    now: datetime,
    session=None,
) -> None:
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="migrate_native_tpsl_backup_stop", status=status,
            execution_binding_id=action.binding_id, venue="deepcoin", symbol=action.instrument_id.split("-")[0],
            side=action.side, order_id=action.legacy_order_id, pos_id=action.pos_id,
            related_order_id=action.legacy_order_id, reason=reason,
            request=action.request_payload, response=response, created_at=now,
        ),
        session=session,
    )


def _conflict(pos_id: str, reason: str) -> dict[str, str]:
    return {"pos_id": pos_id, "reason": reason}


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
