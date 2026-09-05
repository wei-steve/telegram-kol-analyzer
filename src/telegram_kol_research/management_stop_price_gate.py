"""Fail-closed management stop checks; provenance is necessary, never sufficient."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.runtime_incidents import record_runtime_incident

IMPLICIT_STOP_ACTIONS = frozenset(
    {"partial_then_break_even", "move_stop_to_break_even", "break_even_by_market"}
)


@dataclass(frozen=True)
class StopGateResult:
    reason_code: str | None
    evidence: dict[str, Any]


def _stop_check_now() -> datetime:
    return datetime.now(UTC)


def stop_gate_clock(_event_time=None):
    """Use current check time, never the dispatcher's reused processed_at.

    Separate the injectable freshness clock from business event timestamps:
    planning and execution can share one old processed_at across multiple I/O.
    """
    return lambda: _stop_check_now()


def _positive(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def stop_action_conflicts(action, stop_mode, stop_price):
    return action in IMPLICIT_STOP_ACTIONS and (
        stop_mode == "explicit_price" or stop_price not in (None, "")
    )


def validate_management_stop(
    *,
    action,
    stop_mode,
    stop_price,
    stop_price_source,
    current_message_text,
    side,
    entry_prices,
    instrument_id,
    quote,
    settings,
    now,
):
    evidence = {
        "action": action,
        "stop_mode": stop_mode,
        "stop_price": str(stop_price),
        "side": side,
        "entry_prices": [str(p) for p in entry_prices],
        "reference_price_source": "current_market_last",
        "reference_price": None,
        "max_deviation_pct": str(settings.max_management_stop_deviation_pct),
        "checked_at": now.isoformat(),
    }

    def refuse(reason):
        return StopGateResult(reason, evidence)

    if stop_action_conflicts(action, stop_mode, stop_price):
        return refuse("management_stop_action_conflict")
    if stop_mode != "explicit_price":
        evidence["reference_price_source"] = (
            "actual_entry_price" if action in IMPLICIT_STOP_ACTIONS else None
        )
        return StopGateResult(None, evidence)
    # Text provenance proves origin, not financial meaning: signatures, QQ/phone
    # numbers, timestamps, points and percentages can all occur in message text.
    if (
        stop_price_source != "current_message_text"
        or not str(current_message_text or "").strip()
    ):
        return refuse("management_stop_provenance_invalid")
    stop = _positive(stop_price)
    if stop is None:
        return refuse("management_stop_price_invalid")
    limit = _positive(settings.max_management_stop_deviation_pct)
    age_limit = _positive(settings.management_stop_quote_max_age_seconds)
    if limit is None or age_limit is None:
        return refuse("management_stop_configuration_invalid")
    quote = quote if isinstance(quote, dict) else {}
    price = _positive(quote.get("price"))
    try:
        observed = datetime.fromisoformat(str(quote.get("observed_at")))
        age = (now - observed).total_seconds() if observed.tzinfo is not None else -1
    except (TypeError, ValueError):
        age = -1
    if (
        price is None
        or quote.get("instrument_id") != instrument_id
        or quote.get("price_field") not in {"last", "lastPx"}
        or age < 0
        or Decimal(str(age)) > age_limit
    ):
        return refuse("management_stop_reference_unavailable")
    deviation = abs(stop - price) / price * 100
    evidence.update(
        reference_price=str(price),
        quote_observed_at=quote["observed_at"],
        price_field=quote["price_field"],
        deviation_pct=str(deviation),
    )
    # Check magnitude first so QQ-shaped prices have a stable distinct reason.
    if deviation > limit:
        return refuse("management_stop_deviation_exceeded")
    if side not in {"long", "short"} or not (
        stop < price if side == "long" else stop > price
    ):
        return refuse("management_stop_direction_invalid")
    return StopGateResult(None, evidence)


def record_stop_gate_rejection(
    session_factory, *, batch_id, raw_message_id, result, now
):
    """Mandatory deterministic ledger capture, independent of optional AI capture.

    Failure propagates and stops execution; this never enables an AI playbook.
    """
    reason = result.reason_code
    return record_runtime_incident(
        session_factory,
        source_kind="strategy_management_batch",
        source_record_id=str(batch_id),
        incident_type="management_stop_rejected",
        severity="high",
        fingerprint=hashlib.sha256(
            f"management_stop_rejected:{batch_id}:{reason}".encode()
        ).hexdigest(),
        redacted_summary=json.dumps(
            {"component": "strategy_management", "reason_code": reason}
        ),
        occurred_at=now,
        feature_policy_version="management-stop-gate-v1",
        prompt_version="none",
        tool_policy_version="no-exchange-write",
        diagnosis_json=json.dumps(
            {"observed_state": result.evidence}, ensure_ascii=False
        ),
        evidence_refs_json=json.dumps(
            [f"raw_message:{raw_message_id}", f"strategy_management_batch:{batch_id}"]
        ),
    )


def read_stop_quote(client, instrument_id):
    try:
        return client.get_ticker_quote(inst_id=instrument_id)
    except Exception:  # noqa: BLE001 - any incomplete external read is unavailable
        return None


def validate_batch_stops(
    session_factory, *, batch, client, now, now_provider=None, quote=None
):
    """Recheck old/frozen batches before any composite component or write."""
    from telegram_kol_research.models import RawMessage
    from telegram_kol_research.trading_settings import load_trading_settings

    checked_at = now_provider or stop_gate_clock(now)
    contract = json.loads(batch.management_contract_json or "{}")
    targets = (
        [
            (
                contract.get("stop_mode"),
                contract.get("stop_price"),
                contract.get("stop_price_source"),
            )
        ]
        if contract
        else []
    )
    for leg in batch.legs:
        planned = leg.planned_tpsl or {}
        stop = planned.get("stop_loss_text")
        if stop not in (None, ""):
            targets.append(("explicit_price", stop, planned.get("stop_price_source")))
    if not targets:
        return None
    # Semantic rejection does not need market reads or valid provenance.
    for mode, stop, _ in targets:
        if stop_action_conflicts(batch.intent, mode, stop):
            return StopGateResult(
                "management_stop_action_conflict",
                {
                    "action": batch.intent,
                    "stop_mode": mode,
                    "stop_price": stop,
                    "reference_price_source": "not_used_semantic_conflict",
                },
            )
    if not any(mode == "explicit_price" for mode, *_ in targets):
        return None
    from telegram_kol_research.models import ExecutionBinding

    with session_factory() as session:
        raw = session.get(RawMessage, batch.raw_message_id)
        text = raw.text if raw else None
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        side = str(binding.side).lower() if binding else None
        symbol = str(binding.symbol).upper() if binding else None
    instrument_id = f"{symbol}-USDT-SWAP"
    if quote is None:
        quote = read_stop_quote(client, instrument_id)
    settings = load_trading_settings(session_factory)
    for mode, stop, source in targets:
        result = validate_management_stop(
            action=batch.intent,
            stop_mode=mode,
            stop_price=stop,
            stop_price_source=source,
            current_message_text=text,
            side=side,
            entry_prices=[leg.avg_entry_price for leg in batch.legs],
            instrument_id=instrument_id,
            quote=quote,
            settings=settings,
            now=checked_at(),
        )
        if result.reason_code:
            return result
    return None


def reject_execution_stop(session_factory, *, batch, result, now):
    from telegram_kol_research.strategy_management_batches import transition_batch

    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={batch.status},
        new_status="recovery_required" if batch.status == "executing" else "blocked",
        transitioned_at=now,
        reason_code=result.reason_code,
    )
    record_stop_gate_rejection(
        session_factory,
        batch_id=batch.id,
        raw_message_id=batch.raw_message_id,
        result=result,
        now=now,
    )
