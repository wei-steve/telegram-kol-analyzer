"""Bounded orchestration for exact trigger-position stop rescue."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    PositionProtectionIncident,
    TriggerProtectionIntent,
)
from telegram_kol_research.position_authority_lock import position_authority_lock
from telegram_kol_research.strategy_management_executor import (
    execute_trigger_protection_stop_rescue,
)
from telegram_kol_research.strategy_management_planner import (
    _prepare_trigger_protection_stop_rescue,
    plan_trigger_protection_stop_rescue,
)
from telegram_kol_research.trading_settings import load_trading_settings


@dataclass(frozen=True, slots=True)
class TriggerProtectionRescueTickResult:
    mode: str
    discovered: int = 0
    evaluated: int = 0
    shadow_ready: int = 0
    planned: int = 0
    executed: int = 0
    blocked: int = 0
    recovery_required: int = 0


def run_trigger_protection_rescue_tick(
    session_factory: sessionmaker,
    *,
    deepcoin_client,
    processed_at: datetime | None = None,
    limit: int = 20,
) -> TriggerProtectionRescueTickResult:
    """Evaluate due trigger intents and optionally execute one exact rescue each."""

    now = processed_at or datetime.now(UTC)
    settings = load_trading_settings(session_factory)
    liveness_mode = settings.effective_position_management_liveness_v2_mode
    if liveness_mode == "disabled":
        return TriggerProtectionRescueTickResult(mode="disabled")
    # The liveness-v2 gate is authoritative for this exact-position lane: it
    # is both the enable switch and the kill switch.  Keeping the legacy gate
    # in this decision would make liveness=live unable to enable the fallback.
    mode = liveness_mode
    if mode == "disabled":
        return TriggerProtectionRescueTickResult(mode=mode)
    bounded_limit = max(1, min(int(limit), 100))
    with position_authority_lock():
        with session_factory() as session:
            intent_ids = [
                int(row[0])
                for row in (
                    session.query(TriggerProtectionIntent.id)
                    .filter(TriggerProtectionIntent.venue == "deepcoin")
                    .filter(
                        TriggerProtectionIntent.recovery_state.in_(
                            ("pending", "retrying", "failed")
                        )
                    )
                    .filter(
                        or_(
                            TriggerProtectionIntent.recovery_disposition.is_(None),
                            TriggerProtectionIntent.recovery_disposition.in_(
                                ("retry", "exact_backup")
                            ),
                        )
                    )
                    .filter(
                        or_(
                            TriggerProtectionIntent.next_attempt_at.is_(None),
                            TriggerProtectionIntent.next_attempt_at <= now,
                        )
                    )
                    .order_by(TriggerProtectionIntent.id.asc())
                    .limit(bounded_limit)
                    .all()
                )
            ]
        counts = {
            "evaluated": 0,
            "shadow_ready": 0,
            "planned": 0,
            "executed": 0,
            "blocked": 0,
            "recovery_required": 0,
        }
        for intent_id in intent_ids:
            if mode == "shadow":
                with session_factory() as session:
                    intent = session.get(TriggerProtectionIntent, intent_id)
                    prepared = (
                        _prepare_trigger_protection_stop_rescue(
                            session,
                            intent=intent,
                            deepcoin_client=deepcoin_client,
                        )
                        if intent is not None
                        else "rescue_intent_not_found"
                    )
                    counts["evaluated"] += 1
                    if isinstance(prepared, str):
                        counts["blocked"] += 1
                        continue
                    leg, payload = prepared
                    _record_shadow_ready(
                        session,
                        intent=intent,
                        leg=leg,
                        payload=payload,
                        observed_at=now,
                    )
                    session.commit()
                    counts["shadow_ready"] += 1
                continue

            counts["evaluated"] += 1
            planned = plan_trigger_protection_stop_rescue(
                session_factory,
                intent_id=intent_id,
                deepcoin_client=deepcoin_client,
                planned_at=now,
            )
            if planned.status != "ready" or planned.rescue_id is None:
                counts["blocked"] += 1
                continue
            counts["planned"] += 1
            result = execute_trigger_protection_stop_rescue(
                session_factory,
                rescue_id=planned.rescue_id,
                deepcoin_client=deepcoin_client,
                executed_at=now,
            )
            if result["status"] in {"submitted", "verified"}:
                counts["executed"] += 1
            elif result["status"] in {"submit_unknown", "recovery_required"}:
                counts["recovery_required"] += 1
                break
            else:
                counts["blocked"] += 1
        return TriggerProtectionRescueTickResult(
            mode=mode,
            discovered=len(intent_ids),
            **counts,
        )


def _record_shadow_ready(session, *, intent, leg, payload, observed_at: datetime) -> None:
    evidence = {
        "mode": "shadow",
        "intent_id": int(intent.id),
        "execution_binding_id": int(intent.execution_binding_id),
        "execution_order_leg_id": int(leg.id),
        "pos_id": str(leg.pos_id),
        "planned_stop": str(payload["slTriggerPx"]),
    }
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = (
        session.query(PositionProtectionIncident)
        .filter(PositionProtectionIncident.fingerprint == fingerprint)
        .one_or_none()
    )
    if existing is not None:
        existing.updated_at = observed_at
        return
    session.add(
        PositionProtectionIncident(
            venue="deepcoin",
            execution_binding_id=int(intent.execution_binding_id),
            execution_order_leg_id=int(leg.id),
            pos_id=str(leg.pos_id),
            incident_type="stop_rescue_shadow_ready",
            fingerprint=fingerprint,
            evidence_json=json.dumps(evidence, sort_keys=True),
            created_at=observed_at,
            updated_at=observed_at,
        )
    )
