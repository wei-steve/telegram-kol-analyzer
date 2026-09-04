"""Opt-in submission of one exact-position trigger backup stop per entry leg."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.execution_bindings import build_client_order_id
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionEvent
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import PositionBackupStopOrder
from telegram_kol_research.models import PositionProtectionIncident
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import PositionProtectionLeg
from telegram_kol_research.native_tpsl import NativeTpslExpectation
from telegram_kol_research.native_tpsl import match_native_tpsl_order
from telegram_kol_research.native_tpsl import normalize_native_tpsl
from telegram_kol_research.position_attribution import has_authoritative_persisted_position
from telegram_kol_research.position_mutation_gateway import submit_exact_position_sltp
from telegram_kol_research.position_protection_legs import (
    bind_verified_exchange_order,
    protection_write_block_reason,
)
from telegram_kol_research.protection_ledger import (
    load_account_protection_ownership,
    upsert_protection_ledger_row,
)
from telegram_kol_research.protection_snapshot import (
    read_complete_pending_tpsl_snapshot,
)
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.trigger_backup_stop import BackupStopError
from telegram_kol_research.trigger_backup_stop import build_backup_stop_trigger_payload
from telegram_kol_research.trigger_backup_stop import calculate_backup_stop_price


@dataclass(frozen=True, slots=True)
class BackupStopPlan:
    """One read-only result for a single exact-position backup-stop candidate."""

    status: str
    reason_code: str | None = None
    binding_id: int | None = None
    leg_id: int | None = None
    pos_id: str | None = None
    primary_order_id: str | None = None
    primary_stop: str | None = None
    payload: dict[str, str] | None = None
    position: dict[str, Any] | None = None
    open_positions: tuple[dict[str, Any], ...] = ()


def submit_verified_trigger_backup_stops(
    session_factory: sessionmaker,
    *,
    client: Any,
    contract_spec_provider: DeepcoinContractSpecProvider,
    submitted_at: datetime,
) -> int:
    """Submit opt-in stops only after a fresh exact live-position read.

    The caller must hold the account-level authority lock.  A durable
    ``submitting`` row is committed before the exchange request, so an
    ambiguous network outcome cannot be blindly retried.
    """

    list_positions = getattr(client, "list_positions", None)
    read_pending = getattr(client, "read_trigger_orders_pending", None)
    if not callable(list_positions) or not callable(read_pending):
        return 0
    settings = load_trading_settings(session_factory)
    backup_stop_buffer_bps = settings.trigger_backup_stop_buffer_bps
    rollout_mode = settings.effective_position_management_liveness_v2_mode
    if rollout_mode == "disabled":
        return 0
    with session_factory() as session:
        candidates = _eligible_candidates(session)
    submitted = 0
    for binding_id, leg_id, pos_id in candidates:
        with session_factory() as session:
            plan = _plan_submission(
                session,
                binding_id=binding_id,
                leg_id=leg_id,
                pos_id=pos_id,
                client=client,
                contract_spec_provider=contract_spec_provider,
                backup_stop_buffer_bps=backup_stop_buffer_bps,
                submitted_at=submitted_at,
            )
            if plan.status == "already_protected":
                continue
            if plan.status != "ready" or plan.payload is None:
                _record_incident(
                    session,
                    plan=plan,
                    incident_type="backup_stop_blocked",
                    observed_at=submitted_at,
                )
                session.commit()
                continue
            if rollout_mode == "shadow":
                _record_incident(
                    session,
                    plan=plan,
                    incident_type="backup_stop_shadow_ready",
                    observed_at=submitted_at,
                )
                session.commit()
                continue
            row = _reserve_submission(session, plan=plan, submitted_at=submitted_at)
            row_id = int(row.id)
            session.commit()
        try:
            response = submit_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=client,
                pos_id=str(plan.pos_id),
                payload=plan.payload,
                idempotency_key=(
                    f"trigger-backup-stop:{plan.binding_id}:"
                    f"{plan.leg_id}:{plan.pos_id}:set"
                ),
                live_execution_gate=lambda: _submission_still_reserved(
                    session_factory, row_id
                ),
                now_provider=lambda: submitted_at,
            )
            order_id = _response_order_id(response)
        except Exception as exc:
            with session_factory() as session:
                row = session.get(PositionBackupStopOrder, row_id)
                if row is not None:
                    row.status = "unknown_exchange_outcome"
                    row.error_json = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    row.updated_at = submitted_at
                    _record_incident(
                        session,
                        plan=BackupStopPlan(
                            status="exchange_unavailable",
                            reason_code="backup_exchange_outcome_unknown",
                            binding_id=int(row.execution_binding_id),
                            leg_id=int(row.execution_order_leg_id),
                            pos_id=str(row.pos_id),
                        ),
                        incident_type="backup_exchange_outcome_unknown",
                        observed_at=submitted_at,
                    )
                    session.commit()
            return submitted
        with session_factory() as session:
            row = session.get(PositionBackupStopOrder, row_id)
            if row is None:
                continue
            row.order_id = order_id
            row.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
            row.submitted_at = submitted_at
            row.updated_at = submitted_at
            session.commit()
            instrument_id = row.instrument_id
        try:
            post_submit_positions = list(client.list_positions())
            if any(
                not _live_position_aliases_consistent(row)
                for row in post_submit_positions
                if isinstance(row, dict)
            ):
                raise RuntimeError("live_position_snapshot_alias_conflict")
            exact_post_submit = [
                row
                for row in post_submit_positions
                if isinstance(row, dict)
                and _live_position_matches_plan(row, plan)
            ]
            if len(exact_post_submit) != 1:
                raise RuntimeError("live_position_snapshot_not_unique_or_mismatched")
            pending = _read_pending_trigger_orders(client, instrument_id=instrument_id)
            primary_is_still_verified = _pending_matches_primary(
                pending,
                order_id=plan.primary_order_id,
                trigger_price=plan.primary_stop,
                position=exact_post_submit[0],
                open_positions=tuple(
                    row for row in post_submit_positions if isinstance(row, dict)
                ),
            )
            verified_order_id = _pending_matches_backup(
                pending,
                order_id=order_id,
                payload=plan.payload,
                position=exact_post_submit[0],
                open_positions=tuple(
                    row for row in post_submit_positions if isinstance(row, dict)
                ),
            )
        except Exception:
            primary_is_still_verified = False
            verified_order_id = None
        if not primary_is_still_verified or not verified_order_id:
            with session_factory() as session:
                row = session.get(PositionBackupStopOrder, row_id)
                if row is not None:
                    row.status = "pending_readback"
                    row.error_json = json.dumps(
                        {"error": "native backup stop pending readback"},
                        ensure_ascii=False,
                    )
                    row.updated_at = submitted_at
                    _record_incident(
                        session,
                        plan=BackupStopPlan(
                            status="exchange_unavailable",
                            reason_code="backup_stop_pending_readback",
                            binding_id=int(row.execution_binding_id),
                            leg_id=int(row.execution_order_leg_id),
                            pos_id=str(row.pos_id),
                        ),
                        incident_type="backup_stop_pending_readback",
                        observed_at=submitted_at,
                    )
                    session.commit()
            return submitted
        with session_factory() as session:
            row = session.get(PositionBackupStopOrder, row_id)
            if row is None:
                continue
            row.order_id = verified_order_id
            row.status = "active"
            protection_leg = (
                session.query(PositionProtectionLeg)
                .filter(PositionProtectionLeg.execution_order_leg_id == row.execution_order_leg_id)
                .filter(PositionProtectionLeg.role == "backup_stop")
                .filter(PositionProtectionLeg.leg_index == 1)
                .one_or_none()
            )
            if protection_leg is not None:
                protection_leg.planned_trigger_price = row.trigger_price
                protection_leg.planned_size = "0"
                bind_verified_exchange_order(
                    session,
                    protection_leg,
                    exchange_order_id=verified_order_id,
                    readback_evidence={"response": row.response_json, "order_id": verified_order_id},
                )
            upsert_protection_ledger_row(
                session,
                venue=row.venue, execution_binding_id=int(row.execution_binding_id),
                execution_order_leg_id=int(row.execution_order_leg_id),
                strategy_instance_id=_strategy_instance_id(session, row.execution_binding_id),
                pos_id=row.pos_id, instrument_id=row.instrument_id, side=row.side,
                order_id=verified_order_id, purpose="stop_loss", trigger_price=row.trigger_price,
                size_text="0", status="verified", evidence_source="trigger_backup_stop_pending_readback",
                evidence={"backup_stop": True}, seen_at=submitted_at,
            )
            session.add(ExecutionEvent(
                execution_binding_id=row.execution_binding_id,
                strategy_instance_id=_strategy_instance_id(session, row.execution_binding_id),
                venue=row.venue,
                action="create_backup_stop",
                status="submitted",
                symbol=row.instrument_id.split("-", 1)[0],
                side=row.side,
                order_id=verified_order_id,
                client_order_id=row.client_order_id,
                pos_id=row.pos_id,
                request_json=row.request_json,
                response_json=row.response_json,
                created_at=submitted_at,
            ))
            session.commit()
            submitted += 1
    return submitted


def _eligible_candidates(session) -> list[tuple[int, int, str]]:
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
    result = []
    for binding, leg in rows:
        pos_id = str(leg.pos_id or "").strip()
        if (
            not pos_id
            or _is_manual_operator_binding(leg)
            or not has_authoritative_persisted_position(leg, session=session)
        ):
            continue
        result.append((int(binding.id), int(leg.id), pos_id))
    return result


def _plan_submission(
    session,
    *,
    binding_id,
    leg_id,
    pos_id,
    client,
    contract_spec_provider,
    backup_stop_buffer_bps,
    submitted_at,
):
    binding = session.get(ExecutionBinding, binding_id)
    leg = session.get(ExecutionOrderLeg, leg_id)
    if binding is None or leg is None or str(leg.pos_id or "") != pos_id:
        return BackupStopPlan(
            status="blocked", reason_code="binding_or_leg_unavailable",
            binding_id=binding_id, leg_id=leg_id, pos_id=pos_id,
        )
    if str(binding.status or "").lower() not in {"open", "active"}:
        return _blocked_plan(binding_id, leg_id, pos_id, "binding_not_active")
    if block_reason := protection_write_block_reason(session, pos_id=pos_id):
        return _blocked_plan(
            binding_id,
            leg_id,
            pos_id,
            f"backup_{block_reason}",
        )
    if _is_manual_operator_binding(leg):
        return _blocked_plan(binding_id, leg_id, pos_id, "manual_binding")
    if not has_authoritative_persisted_position(leg, session=session):
        return _blocked_plan(binding_id, leg_id, pos_id, "position_not_authoritative")
    existing = (
        session.query(PositionBackupStopOrder)
        .filter(PositionBackupStopOrder.venue == "deepcoin")
        .filter(PositionBackupStopOrder.pos_id == pos_id)
        .filter(
            PositionBackupStopOrder.status.in_(
                ("submitting", "pending_readback", "active", "unknown_exchange_outcome")
            )
        )
        .first()
    )
    primary = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.execution_binding_id == binding_id)
        .filter(PositionProtectionLedger.execution_order_leg_id == leg_id)
        .filter(PositionProtectionLedger.pos_id == pos_id)
        .filter(PositionProtectionLedger.status == "verified")
        .filter(PositionProtectionLedger.purpose.in_(("stop_loss", "combined")))
        .order_by(PositionProtectionLedger.id.asc())
        .first()
    )
    primary_stop = _primary_stop_price(primary)
    if primary_stop is None:
        return _blocked_plan(binding_id, leg_id, pos_id, "primary_stop_not_verified")
    primary_order_id = str(primary.order_id or "").strip() if primary is not None else ""
    if not primary_order_id:
        return _blocked_plan(binding_id, leg_id, pos_id, "primary_stop_identifier_unavailable")
    instrument_id = f"{str(binding.symbol).upper()}-USDT-SWAP"
    spec = contract_spec_provider.get_contract_spec(instrument_id)
    if spec is None:
        return _blocked_plan(binding_id, leg_id, pos_id, "contract_spec_unavailable")
    try:
        positions = client.list_positions(inst_id=instrument_id)
    except TypeError:
        try:
            positions = client.list_positions()
        except Exception:
            return BackupStopPlan(
                status="exchange_unavailable", reason_code="live_position_snapshot_unavailable",
                binding_id=binding_id, leg_id=leg_id, pos_id=pos_id,
            )
    except Exception:
        return BackupStopPlan(
            status="exchange_unavailable", reason_code="live_position_snapshot_unavailable",
            binding_id=binding_id, leg_id=leg_id, pos_id=pos_id,
        )
    if any(
        not _live_position_aliases_consistent(row)
        for row in positions
        if isinstance(row, dict)
    ):
        return _blocked_plan(
            binding_id, leg_id, pos_id, "live_position_alias_conflict"
        )
    exact = [
        row
        for row in positions
        if isinstance(row, dict)
        and _live_position_aliases_match(
            row,
            pos_id=pos_id,
            instrument_id=instrument_id,
            side=str(binding.side).lower(),
        )
    ]
    if len(exact) != 1:
        return _blocked_plan(binding_id, leg_id, pos_id, "live_position_not_unique")
    position = exact[0]
    try:
        pending_before_submit = _read_pending_trigger_orders(client, instrument_id=instrument_id)
    except Exception:
        return BackupStopPlan(
            status="exchange_unavailable",
            reason_code="primary_stop_readback_unavailable",
            binding_id=binding_id,
            leg_id=leg_id,
            pos_id=pos_id,
        )
    if not _pending_matches_primary(
        pending_before_submit,
        order_id=primary_order_id,
        trigger_price=primary_stop,
        position=position,
        open_positions=tuple(row for row in positions if isinstance(row, dict)),
    ):
        return _blocked_plan(binding_id, leg_id, pos_id, "primary_stop_missing_on_exchange")
    ownership = load_account_protection_ownership(
        session,
        live_pos_ids={
            next(iter(_text_alias_values(row, "posId", "pos_id")))
            for row in positions
            if isinstance(row, dict)
            and _live_position_aliases_consistent(row)
        },
    )
    if _unowned_pending_stop_can_affect_position(
        pending=pending_before_submit,
        instrument_id=instrument_id,
        side=str(binding.side).lower(),
        pos_id=pos_id,
        primary_order_id=primary_order_id,
        ownership=ownership,
    ):
        return _blocked_plan(
            binding_id, leg_id, pos_id, "unowned_pending_stop_present"
        )
    liquidation = position.get("liqPx") or position.get("liquidationPrice")
    if liquidation in (None, "", "0"):
        return _blocked_plan(binding_id, leg_id, pos_id, "liquidation_price_unavailable")
    try:
        backup_price = calculate_backup_stop_price(
            primary_stop=primary_stop, side=binding.side,
            price_tick=spec.price_tick,
            buffer_bps=backup_stop_buffer_bps,
        )
        payload = build_backup_stop_trigger_payload(
            instrument_id=instrument_id, side=binding.side, margin_mode=binding.margin_mode,
            pos_id=pos_id, primary_stop=primary_stop, backup_stop=backup_price,
            liquidation_price=liquidation, size=position.get("pos") or position.get("size"),
            client_order_id=build_client_order_id(
                strategy_instance_id=str(binding.strategy_instance_id), leg_index=int(leg.leg_index),
                purpose="backup_stop",
            ),
        )
    except BackupStopError:
        return _blocked_plan(binding_id, leg_id, pos_id, "backup_stop_unsafe")
    try:
        from telegram_kol_research.position_protection_legs import (
            materialize_verified_position_protection,
        )

        materialize_verified_position_protection(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(leg.id),
            pos_id=pos_id,
            primary_order_id=primary_order_id,
            primary_stop=primary_stop,
            backup_stop=str(backup_price),
        )
    except ValueError:
        return _blocked_plan(binding_id, leg_id, pos_id, "protection_leg_conflict")
    if existing is not None:
        try:
            pending = _read_pending_trigger_orders(client, instrument_id=instrument_id)
        except Exception:
            return BackupStopPlan(
                status="exchange_unavailable",
                reason_code="backup_stop_readback_unavailable",
                binding_id=binding_id,
                leg_id=leg_id,
                pos_id=pos_id,
            )
        if (
            str(existing.status) == "active"
            and str(existing.order_id or "").strip()
            and _pending_matches_backup(
                pending,
                order_id=str(existing.order_id),
                payload=payload,
                position=position,
                open_positions=tuple(row for row in positions if isinstance(row, dict)),
            )
        ):
            return BackupStopPlan(
                status="already_protected", binding_id=binding_id, leg_id=leg_id, pos_id=pos_id
            )
        return _blocked_plan(
            binding_id,
            leg_id,
            pos_id,
            "backup_stop_pending_readback"
            if str(existing.status) in {"submitting", "pending_readback"}
            else "backup_exchange_outcome_unknown"
            if str(existing.status) == "unknown_exchange_outcome"
            else "backup_stop_missing_on_exchange",
        )
    return BackupStopPlan(
        status="ready", binding_id=binding_id, leg_id=leg_id, pos_id=pos_id,
        primary_order_id=primary_order_id,
        primary_stop=primary_stop,
        payload=payload, position=position,
        open_positions=tuple(row for row in positions if isinstance(row, dict)),
    )


def _reserve_submission(session, *, plan: BackupStopPlan, submitted_at: datetime) -> PositionBackupStopOrder:
    if (
        plan.binding_id is None
        or plan.leg_id is None
        or plan.pos_id is None
        or plan.payload is None
    ):
        raise ValueError("backup stop reservation requires a ready exact-position plan")
    binding = session.get(ExecutionBinding, plan.binding_id)
    if binding is None:
        raise ValueError("backup stop binding disappeared before reservation")
    row = PositionBackupStopOrder(
        venue="deepcoin", execution_binding_id=plan.binding_id, execution_order_leg_id=plan.leg_id,
        pos_id=plan.pos_id, instrument_id=plan.payload["instId"], side=str(binding.side).lower(),
        trigger_price=plan.payload["slTriggerPx"],
        client_order_id=build_client_order_id(
            strategy_instance_id=str(binding.strategy_instance_id),
            leg_index=int(session.get(ExecutionOrderLeg, plan.leg_id).leg_index),
            purpose="backup_stop",
        ),
        status="submitting",
        request_json=json.dumps(plan.payload, ensure_ascii=False, sort_keys=True),
        created_at=submitted_at, updated_at=submitted_at,
    )
    session.add(row)
    session.flush()
    return row


def _submission_still_reserved(
    session_factory: sessionmaker,
    row_id: int,
) -> bool:
    """Recheck the durable operation gate immediately before the REST write."""

    with session_factory() as session:
        row = session.get(PositionBackupStopOrder, row_id)
        return row is not None and row.status == "submitting"


def _blocked_plan(binding_id: int, leg_id: int, pos_id: str, reason_code: str) -> BackupStopPlan:
    return BackupStopPlan(
        status="blocked", reason_code=reason_code, binding_id=binding_id, leg_id=leg_id, pos_id=pos_id
    )


def _is_manual_operator_binding(leg: ExecutionOrderLeg) -> bool:
    if str(leg.order_kind or "").lower() == "manual_bind":
        return True
    try:
        evidence = json.loads(str(leg.attribution_evidence_json or "{}"))
    except json.JSONDecodeError:
        return False
    return isinstance(evidence, dict) and evidence.get("source") == "manual_operator_bind"


def _record_incident(
    session,
    *,
    plan: BackupStopPlan,
    incident_type: str,
    observed_at: datetime,
) -> None:
    if plan.binding_id is None or plan.leg_id is None or not plan.pos_id:
        return
    evidence = {"reason_code": str(plan.reason_code or incident_type)}
    fingerprint = hashlib.sha256(json.dumps({
        "venue": "deepcoin", "binding_id": plan.binding_id, "leg_id": plan.leg_id,
        "pos_id": plan.pos_id, "incident_type": incident_type, "evidence": evidence,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if session.query(PositionProtectionIncident.id).filter(
        PositionProtectionIncident.fingerprint == fingerprint
    ).first() is not None:
        return
    session.add(PositionProtectionIncident(
        venue="deepcoin", execution_binding_id=plan.binding_id,
        execution_order_leg_id=plan.leg_id, pos_id=plan.pos_id,
        incident_type=incident_type, fingerprint=fingerprint,
        evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        delivery_status="pending", created_at=observed_at, updated_at=observed_at,
    ))


def _response_order_id(response: Any) -> str | None:
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, str) and data.strip():
        return data.strip()

    rows = data if isinstance(data, list) else [data, response]
    for row in rows:
        if isinstance(row, dict):
            aliases = _text_alias_values(
                row, "ordId", "orderId", "order_id", "id"
            )
            if len(aliases) == 1:
                return next(iter(aliases))
    return None


def _read_pending_trigger_orders(client: Any, *, instrument_id: str) -> list[dict[str, Any]]:
    return read_complete_pending_tpsl_snapshot(
        client,
        instrument_id=instrument_id,
    )


def _unowned_pending_stop_can_affect_position(
    *, pending, instrument_id, side, pos_id, primary_order_id, ownership
) -> bool:
    for raw in pending:
        if not _row_has_stop_fields(raw):
            continue
        order_types = _text_alias_values(
            raw, "triggerOrderType", "trigger_order_type", transform=str.upper
        )
        if order_types and order_types != {"TPSL"}:
            if len(order_types) == 1:
                continue
            return True
        if order_types != {"TPSL"} or not _native_tpsl_aliases_consistent(raw):
            return True
        instrument_ids = _text_alias_values(
            raw,
            "instId",
            "instrument_id",
            "instrumentId",
            transform=str.upper,
        )
        sides = _text_alias_values(
            raw,
            "posSide",
            "pos_side",
            "side",
            transform=_normalize_position_side_alias,
        )
        if len(instrument_ids) != 1 or len(sides) != 1:
            return True
        if (
            instrument_ids != {instrument_id.upper()}
            or sides != {_normalize_position_side_alias(side)}
        ):
            continue
        order = normalize_native_tpsl(raw)
        if order is None:
            return True
        if (
            order.stop_loss_trigger_price is None
            or order.inst_id != instrument_id
            or order.pos_side != side
            or order.ord_id == primary_order_id
        ):
            continue
        if order.pos_id is not None and order.pos_id != pos_id:
            continue
        known_owner = ownership.owner_for_order(order.ord_id)
        if known_owner is not None and known_owner.pos_id != pos_id:
            continue
        if known_owner is None:
            return True
    return False


def _pending_matches_backup(
    pending: list[dict[str, Any]],
    *,
    order_id: str | None,
    payload: dict[str, str],
    position: dict[str, Any] | None,
    open_positions: tuple[dict[str, Any], ...],
) -> str | None:
    if not order_id or position is None or not open_positions:
        return None
    match = match_native_tpsl_order(
        position,
        [
            row
            for row in pending
            if _native_tpsl_aliases_consistent(row)
        ],
        NativeTpslExpectation(
            purpose="stop_loss",
            trigger_price=payload["slTriggerPx"],
            # DeepCoin's position-level TPSL represents "close this exact
            # split position in full" as sz=0; posId supplies the exact scope.
            size="0",
            ord_id=order_id,
        ),
        open_positions=list(open_positions),
    )
    return match.order.ord_id if match.status == "verified" and match.order is not None else None


def _pending_matches_primary(
    pending: list[dict[str, Any]],
    *,
    order_id: str | None,
    trigger_price: str | None,
    position: dict[str, Any] | None,
    open_positions: tuple[dict[str, Any], ...],
) -> bool:
    """Verify the pre-existing native stop survived a second-stop submission."""

    if not order_id or not trigger_price or position is None or not open_positions:
        return False
    exact = [
        order
        for raw in pending
        if _native_tpsl_aliases_consistent(raw)
        and (order := normalize_native_tpsl(raw)) is not None
        and order.ord_id == order_id
    ]
    if len(exact) != 1 or exact[0].size is None:
        return False
    match = match_native_tpsl_order(
        position,
        [
            row
            for row in pending
            if _native_tpsl_aliases_consistent(row)
        ],
        NativeTpslExpectation(
            purpose="stop_loss",
            trigger_price=trigger_price,
            size=exact[0].size,
            ord_id=order_id,
        ),
        open_positions=list(open_positions),
    )
    return match.status == "verified" and match.order is not None


def _live_position_matches_plan(position: dict[str, Any], plan: BackupStopPlan) -> bool:
    if plan.pos_id is None or plan.payload is None:
        return False
    return _live_position_aliases_match(
        position,
        pos_id=plan.pos_id,
        instrument_id=plan.payload["instId"],
        side=plan.payload["posSide"],
    )


def _live_position_aliases_match(
    row: dict[str, Any], *, pos_id: str, instrument_id: str, side: str
) -> bool:
    return (
        _live_position_aliases_consistent(row)
        and _text_alias_values(row, "posId", "pos_id") == {str(pos_id)}
        and _text_alias_values(
            row,
            "instId",
            "instrument_id",
            "instrumentId",
            transform=str.upper,
        )
        == {str(instrument_id).upper()}
        and _text_alias_values(
            row,
            "posSide",
            "pos_side",
            "side",
            transform=_normalize_position_side_alias,
        )
        == {_normalize_position_side_alias(side)}
    )


def _live_position_aliases_consistent(row: dict[str, Any]) -> bool:
    size_values = _numeric_alias_values(row, "pos", "size", "sz")
    return (
        len(_text_alias_values(row, "posId", "pos_id")) == 1
        and len(
            _text_alias_values(
                row,
                "instId",
                "instrument_id",
                "instrumentId",
                transform=str.upper,
            )
        )
        == 1
        and len(
            _text_alias_values(
                row,
                "posSide",
                "pos_side",
                "side",
                transform=_normalize_position_side_alias,
            )
        )
        == 1
        and _text_alias_values(
            row,
            "mrgPosition",
            "posMode",
            "positionMode",
            transform=str.lower,
        )
        == {"split"}
        and len(size_values) == 1
        and next(iter(size_values)) > 0
        and len(_numeric_alias_values(row, "liqPx", "liquidationPrice")) <= 1
    )


def _native_tpsl_aliases_consistent(row: dict[str, Any]) -> bool:
    text_groups = (
        (("ordId", "orderId", "order_id", "id"), str),
        (("instId", "instrument_id", "instrumentId"), str.upper),
        (("posId", "pos_id", "closePosId"), str),
        (("posSide", "pos_side", "side"), _normalize_position_side_alias),
        (("triggerOrderType", "trigger_order_type"), str.upper),
    )
    numeric_groups = (
        ("sz", "size", "orderSize"),
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice"),
        ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice"),
        ("slOrdPx", "slOrderPrice"),
        ("tpOrdPx", "tpOrderPrice"),
    )
    return all(
        len(_text_alias_values(row, *keys, transform=transform)) <= 1
        for keys, transform in text_groups
    ) and all(len(_numeric_alias_values(row, *keys)) <= 1 for keys in numeric_groups)


def _normalize_position_side_alias(value: str) -> str:
    normalized = str(value).strip().lower()
    return {"buy": "long", "sell": "short"}.get(normalized, normalized)


def _row_has_stop_fields(row: dict[str, Any]) -> bool:
    return any(
        row.get(key) not in (None, "")
        for key in (
            "slTriggerPx",
            "slTriggerPrice",
            "closeSLTriggerPrice",
        )
    )


def _text_alias_values(
    row: dict[str, Any], *keys: str, transform=lambda value: value
) -> set[str]:
    return {
        transform(str(row[key]).strip())
        for key in keys
        if row.get(key) is not None and str(row[key]).strip()
    }


def _numeric_alias_values(row: dict[str, Any], *keys: str) -> set[float]:
    values: set[float] = set()
    for key in keys:
        if row.get(key) in (None, ""):
            continue
        try:
            parsed = float(row[key])
        except (TypeError, ValueError):
            return {0.0, 1.0}
        if parsed != parsed or parsed in {float("inf"), float("-inf")}:
            return {0.0, 1.0}
        values.add(parsed)
    return values


def _primary_stop_price(row: PositionProtectionLedger | None) -> str | None:
    if row is None:
        return None
    if str(row.trigger_price or "").strip():
        return str(row.trigger_price)
    try:
        evidence = json.loads(str(row.evidence_json or "{}"))
    except json.JSONDecodeError:
        return None
    value = evidence.get("stop_loss") if isinstance(evidence, dict) else None
    return str(value) if value not in (None, "") else None


def _strategy_instance_id(session, binding_id: int) -> str | None:
    binding = session.get(ExecutionBinding, binding_id)
    return str(binding.strategy_instance_id) if binding and binding.strategy_instance_id else None
