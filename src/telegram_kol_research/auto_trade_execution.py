"""Automatic live execution bridge for freshly recognized strategy signals."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.deepcoin_execution_actions import (
    cancel_revision_entry_leg,
)
from telegram_kol_research.deepcoin_order_builder import build_deepcoin_order_draft
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.execution_bindings import build_strategy_instance_id
from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MediaAsset,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    Source,
    StrategyLifecycle,
)
from telegram_kol_research.message_instruction_items import (
    claim_next_message_instruction_item,
    defer_message_instruction_item_for_visibility,
    finish_message_instruction_item,
    has_message_instruction_items,
    list_message_instruction_item_results,
    should_defer_instruction_result,
)
from telegram_kol_research.price_normalization import extract_normalized_prices
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_live_submit import process_trade_signal_live
from telegram_kol_research.recovery_live_submit import (
    submit_strategy_revision_replacement_live,
)
from telegram_kol_research.recovery_live_submit import submit_recovery_order_live
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.recovery_scan import _parse_entry_range
from telegram_kol_research.recovery_scan import _resolve_runtime_config
from telegram_kol_research.recovery_scan import _resolve_signal_max_loss_usdt
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trading_settings import apply_trading_settings_to_group_config
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.trading_settings import SymbolEntryThresholds
from telegram_kol_research.strategy_management_planner import (
    plan_strategy_management_batch,
)
from telegram_kol_research.strategy_management_executor import (
    execute_management_batch,
)
from telegram_kol_research.strategy_management_composite_executor import (
    execute_composite_management_batch,
)
from telegram_kol_research.strategy_revision_planner import (
    advance_strategy_revision,
    plan_strategy_revision,
)


def execute_strategy_revision(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    strategy_thread_id: int | None,
    replacement: dict[str, Any],
    deepcoin_client: DeepcoinTradingClientProtocol,
    replacement_writer: Callable[..., dict[str, Any]] | None,
    read_leg_state: Callable[..., dict[str, Any]] | None = None,
    processed_at: datetime | None = None,
) -> dict[str, Any]:
    """Run one revision only when both exact cancellation and replacement exist."""

    if replacement_writer is None:
        return {
            "status": "blocked",
            "reason": "revision_replacement_writer_unavailable",
        }
    now = processed_at or datetime.now(UTC)
    planned = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_message_id,
        strategy_thread_id=strategy_thread_id,
        replacement=replacement,
        planned_at=now,
    )
    if planned.status != "planned" or planned.batch_id is None:
        return {
            "status": planned.status,
            "reason": planned.reason_code,
            "batch_id": planned.batch_id,
        }

    def cancel_writer(**kwargs):
        return cancel_revision_entry_leg(
            session_factory,
            strategy_thread_id=kwargs["strategy_thread_id"],
            execution_binding_id=kwargs["execution_binding_id"],
            execution_order_leg_id=kwargs["execution_order_leg_id"],
            deepcoin_client=deepcoin_client,
            executed_at=now,
        )

    advanced = advance_strategy_revision(
        session_factory,
        batch_id=planned.batch_id,
        cancel_leg_writer=cancel_writer,
        replacement_writer=replacement_writer,
        read_leg_state=read_leg_state,
        advanced_at=now,
    )
    return {
        "status": advanced.status,
        "reason": advanced.reason_code,
        "batch_id": advanced.batch_id,
        "remaining_fraction": advanced.remaining_fraction,
    }


def auto_process_message_trade_signal(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    group_config: GroupConfig,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    processed_at: datetime | None = None,
    revision_replacement_writer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute projected items, or use the legacy single-candidate path."""

    from telegram_kol_research.source_message_deletion import (
        source_execution_barrier,
    )

    barrier = source_execution_barrier(
        session_factory,
        raw_message_id=raw_message_id,
    )
    if barrier.status == "block":
        return {"status": "blocked", "reason": barrier.reason}
    if barrier.status == "hold":
        return {"status": "deferred", "reason": barrier.reason}

    if has_message_instruction_items(
        session_factory,
        raw_message_id=raw_message_id,
    ):
        return execute_message_instruction_items(
            session_factory,
            raw_message_id=raw_message_id,
            group_config=group_config,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            processed_at=processed_at,
            revision_replacement_writer=revision_replacement_writer,
        )
    return _auto_process_single_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=group_config,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        processed_at=processed_at,
        revision_replacement_writer=revision_replacement_writer,
    )


def execute_message_instruction_items(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    group_config: GroupConfig,
    deepcoin_client: DeepcoinTradingClientProtocol | None,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    processed_at: datetime | None = None,
    revision_replacement_writer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Claim and execute each durable instruction independently in sequence."""

    now = processed_at or datetime.now(UTC)
    while True:
        item = claim_next_message_instruction_item(
            session_factory,
            raw_message_id=raw_message_id,
            now=now,
        )
        if item is None:
            break
        try:
            result = _auto_process_single_message_trade_signal(
                session_factory,
                raw_message_id=raw_message_id,
                group_config=group_config,
                deepcoin_client=deepcoin_client,
                contract_spec_provider=contract_spec_provider,
                processed_at=now,
                instruction_kind=item.instruction_kind,
                candidate_id=item.signal_candidate_id,
                revision_replacement_writer=revision_replacement_writer,
            )
        except DeepcoinRequestOutcomeUnknown as exc:
            finish_status = "unknown"
            result = {"type": type(exc).__name__, "message": str(exc)}
        except Exception as exc:
            finish_status = "failed"
            result = {"type": type(exc).__name__, "message": str(exc)}
        else:
            finish_status = _instruction_finish_status(result)
        if should_defer_instruction_result(result):
            defer_message_instruction_item_for_visibility(
                session_factory,
                item_id=item.id,
                result=result,
                now=now,
            )
            continue
        finish_message_instruction_item(
            session_factory,
            item_id=item.id,
            status=finish_status,
            result=result,
            now=now,
        )

    items = list_message_instruction_item_results(
        session_factory,
        raw_message_id=raw_message_id,
    )
    return {"status": _message_instruction_status(items), "items": items}


def _instruction_finish_status(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip().lower()
    reason = str(result.get("reason") or "").strip().lower()
    leg_statuses = {
        str(leg.get("status") or "").strip().lower()
        for leg in result.get("legs", [])
        if isinstance(leg, dict)
    }
    if (
        status in {"unknown", "submit_unknown", "recovery_required"}
        or "unknown" in reason
        or "submit_unknown" in leg_statuses
    ):
        return "unknown"
    if status in {"failed", "partial_failed", "blocked"}:
        return "failed"
    if status == "submitted" or result.get("submitted") is True:
        return "submitted"
    return "succeeded"


def _message_instruction_status(items: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in items}
    if statuses & {"pending", "executing"}:
        return "in_progress"
    if "unknown" in statuses:
        return "unknown"
    if "failed" in statuses:
        return "partial_failed"
    return "completed"


def _auto_process_single_message_trade_signal(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    group_config: GroupConfig,
    deepcoin_client: DeepcoinTradingClientProtocol | None,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    processed_at: datetime | None = None,
    instruction_kind: str | None = None,
    candidate_id: int | None = None,
    revision_replacement_writer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute exactly one selected candidate through the existing venue path."""

    now = processed_at or datetime.now(UTC)
    settings = load_trading_settings(session_factory)
    management_loaded = None
    if instruction_kind != "entry":
        management_loaded = _load_best_management_candidate(
            session_factory,
            raw_message_id=raw_message_id,
            candidate_id=candidate_id if instruction_kind == "management" else None,
        )
    if management_loaded is not None:
        if _is_informational_management_candidate(management_loaded[1]):
            raw_message, candidate, _source, _has_media = management_loaded
            reason = "management_intent_informational"
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    action="management_auto_trade_skipped",
                    status="skipped",
                    kol_id=f"group:{raw_message.chat_id}",
                    chat_id=raw_message.chat_id,
                    message_id=raw_message.message_id,
                    symbol=candidate.symbol,
                    side=candidate.side,
                    reason=reason,
                    request={
                        "raw_message_id": raw_message.id,
                        "candidate_id": candidate.id,
                        "management_action": candidate.management_action,
                    },
                    created_at=now,
                ),
            )
            return {"status": "skipped", "reason": reason}
        composite_candidate = bool(
            management_loaded[1].management_contract_json
            and management_loaded[1].management_contract_fingerprint
        )
        planning_enabled = settings.management_planning_enabled or (
            composite_candidate
            and settings.effective_composite_management_v2_mode
            in {"shadow", "live"}
        )
        if not planning_enabled:
            raw_message, candidate, _source, _has_media = management_loaded
            reason = "management_execution_disabled"
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    action="management_auto_trade_skipped",
                    status="skipped",
                    kol_id=f"group:{raw_message.chat_id}",
                    chat_id=raw_message.chat_id,
                    message_id=raw_message.message_id,
                    symbol=candidate.symbol,
                    side=candidate.side,
                    reason=reason,
                    request={
                        "raw_message_id": raw_message.id,
                        "candidate_id": candidate.id,
                        "management_action": candidate.management_action,
                    },
                    created_at=now,
                ),
            )
            return {"status": "skipped", "reason": reason}
        composite_gate_reason = _composite_management_gate_reason(
            management_loaded[1], settings
        )
        if composite_gate_reason is not None:
            raw_message, candidate, _source, _has_media = management_loaded
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    action="management_auto_trade_skipped",
                    status="skipped",
                    kol_id=f"group:{raw_message.chat_id}",
                    chat_id=raw_message.chat_id,
                    message_id=raw_message.message_id,
                    symbol=candidate.symbol,
                    side=candidate.side,
                    reason=composite_gate_reason,
                    request={
                        "raw_message_id": raw_message.id,
                        "candidate_id": candidate.id,
                        "management_action": candidate.management_action,
                        "management_contract_fingerprint": (
                            candidate.management_contract_fingerprint
                        ),
                    },
                    created_at=now,
                ),
            )
            return {"status": "skipped", "reason": composite_gate_reason}
        if deepcoin_client is None:
            return {"status": "blocked", "reason": "deepcoin_client_unavailable"}
        return _auto_process_management_signal(
            session_factory,
            raw_message_id=raw_message_id,
            candidate_id=management_loaded[1].id,
            loaded=management_loaded,
            group_config=group_config,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            settings=settings,
            processed_at=now,
            revision_replacement_writer=revision_replacement_writer,
        )
    if instruction_kind == "management":
        return {"status": "blocked", "reason": "management_candidate_unavailable"}
    loaded = _load_best_entry_candidate(
        session_factory,
        raw_message_id=raw_message_id,
        candidate_id=candidate_id if instruction_kind == "entry" else None,
    )
    if loaded is None:
        return {"status": "skipped", "reason": "no_entry_signal_candidate"}
    raw_message, candidate, source, has_media = loaded
    if candidate.parse_source == "mimo_symbol_review":
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="symbol_price_scale_conflict_review_required",
            processed_at=now,
        )
    if not settings.auto_trade_enabled:
        return {"status": "skipped", "reason": "auto_trade_disabled"}
    if deepcoin_client is None:
        return {"status": "blocked", "reason": "deepcoin_client_unavailable"}
    if candidate.parse_source in {"entry_confirm_heuristic", "lifecycle_ai"}:
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="lifecycle_event_not_new_entry",
            processed_at=now,
        )

    runtime_group_config = apply_trading_settings_to_group_config(group_config, settings)
    runtime_config = _resolve_runtime_config(
        runtime_group_config,
        raw_message=raw_message,
        source=source,
    )
    if runtime_config is None:
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="group_not_configured_for_auto_trade",
            processed_at=now,
        )
    if runtime_config["trading_mode"] != "auto_trade":
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="kol_or_group_auto_trade_disabled",
            runtime_kol_id=str(runtime_config.get("kol_id") or ""),
            processed_at=now,
        )

    symbol = (candidate.symbol or "").upper()
    side = (candidate.side or "").lower()
    if not symbol or symbol not in {item.upper() for item in settings.allowed_symbols}:
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="symbol_not_allowed",
            runtime_kol_id=str(runtime_config.get("kol_id") or ""),
            processed_at=now,
            extra={"symbol": symbol},
        )
    entry_thresholds = settings.entry_thresholds_for_symbol(symbol)
    if candidate.confidence < settings.min_ai_confidence:
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="confidence_below_minimum",
            runtime_kol_id=str(runtime_config.get("kol_id") or ""),
            processed_at=now,
        )
    if has_media and not settings.allow_vision_auto_trade:
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="vision_auto_trade_disabled",
            runtime_kol_id=str(runtime_config.get("kol_id") or ""),
            processed_at=now,
        )

    from telegram_kol_research.runtime_incident_scanner import (
        list_critical_unprotected_positions,
    )

    unprotected = list_critical_unprotected_positions(
        session_factory, chat_id=int(raw_message.chat_id), limit=20
    )
    if unprotected:
        return {
            "status": "blocked",
            "reason": "critical_unprotected_position_in_chat",
            "pos_ids": [str(row["pos_id"]) for row in unprotected],
        }

    current_position_count = _count_group_effective_positions(
        session_factory,
        chat_id=raw_message.chat_id,
    )
    if current_position_count >= settings.max_concurrent_positions:
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="group_position_limit_reached",
            runtime_kol_id=str(runtime_config.get("kol_id") or ""),
            processed_at=now,
            extra={
                "current_position_count": current_position_count,
                "max_concurrent_positions": settings.max_concurrent_positions,
            },
        )

    instrument_id = _to_deepcoin_swap_instrument(symbol)
    reference_price = _safe_ticker_price(deepcoin_client, inst_id=instrument_id)
    entry_execution_type = _infer_entry_execution_type(
        candidate.entry_text,
        raw_message.text,
        candidate.parse_source,
    )
    entry_range = _parse_entry_range(
        candidate.entry_text,
        symbol=symbol,
        reference_price=reference_price,
    )
    if entry_execution_type == "market":
        market_price = reference_price
        if market_price is None:
            return _record_entry_auto_trade_skip(
                session_factory,
                raw_message=raw_message,
                candidate=candidate,
                reason="market_price_unavailable",
                runtime_kol_id=str(runtime_config.get("kol_id") or ""),
                processed_at=now,
            )
        entry_range = (market_price, market_price)
    if entry_range is None:
        return _record_entry_auto_trade_skip(
            session_factory,
            raw_message=raw_message,
            candidate=candidate,
            reason="missing_entry_range",
            runtime_kol_id=str(runtime_config.get("kol_id") or ""),
            processed_at=now,
        )
    if (
        entry_execution_type == "limit"
        and _is_nearby_single_entry_text(candidate.entry_text, raw_message.text)
        and _is_single_price_entry_range(entry_range)
        and _single_entry_price_is_near_market(
            current_price=reference_price,
            entry_range=entry_range,
            max_deviation_pct=settings.nearby_entry_market_deviation_pct,
        )
    ):
        entry_execution_type = "market"
        if reference_price is not None:
            entry_range = (reference_price, reference_price)

    strategy_instance_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=int(raw_message.chat_id),
        message_id=int(raw_message.message_id),
        symbol=symbol,
        side=side,
    )
    assembly = assemble_entry_strategy(
        session_factory,
        strategy_raw_message_id=int(raw_message.id),
        signal_candidate_id=int(candidate.id),
        strategy_instance_id=strategy_instance_id,
        mode=settings.entry_preamble_mode,
        assembled_at=now,
    )
    if assembly.status == "unresolved":
        return {"status": "deferred", "reason": assembly.reason_code}
    if assembly.status == "blocked":
        return {"status": "blocked", "reason": assembly.reason_code}
    configured_risk = Decimal(
        str(_resolve_signal_max_loss_usdt(runtime_config, symbol=symbol))
    )
    multiplier = assembly.effective_risk_multiplier
    if (
        not multiplier.is_finite()
        or multiplier <= Decimal("0")
        or multiplier > Decimal("1")
    ):
        return {
            "status": "blocked",
            "reason": "entry_preamble_multiplier_invalid",
        }
    effective_risk = configured_risk * multiplier
    assembly_evidence = {
        "mode": assembly.mode,
        "status": assembly.status,
        "configured_risk_budget_usdt": float(configured_risk),
        "risk_multiplier": str(assembly.proposed_risk_multiplier),
        "applied_risk_multiplier": str(multiplier),
        "effective_risk_budget_usdt": float(effective_risk),
        "preamble_message_id": assembly.preamble_message_id,
        "strategy_message_id": int(raw_message.message_id),
        "assembly_fingerprint": assembly.assembly_fingerprint,
    }

    signal = RecoverySignal(
        kol_id=str(runtime_config["kol_id"]),
        chat_id=raw_message.chat_id,
        message_id=raw_message.message_id,
        posted_at=raw_message.posted_at or now,
        symbol=symbol,
        side=side,
        entry_range=entry_range,
        stop_loss_text=candidate.stop_loss_text,
        take_profit_text=candidate.take_profit_text,
        parse_source=candidate.parse_source,
        confidence=candidate.confidence,
        trading_mode="auto_trade",
        max_loss_usdt=float(effective_risk),
        entry_preamble_assembly=assembly_evidence,
        symbol_whitelist=[str(item).upper() for item in runtime_config["symbol_whitelist"]],
    )
    decision = RecoveryDecision(
        action="eligible_for_recovery_limit_order",
        reason_codes=[f"live_signal_auto_trade_{entry_execution_type}"],
        entry_range=signal.entry_range,
        max_loss_usdt=signal.max_loss_usdt,
    )
    persist_recovery_evaluations(
        session_factory,
        [RecoveryEvaluation(signal=signal, decision=decision)],
        run_at=now,
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=signal.chat_id,
        message_id=signal.message_id,
        symbol=symbol,
        side=side,
        review_status="approved_for_order",
        note="auto_trade_live_signal",
        reviewed_at=now,
    )
    confirm_recovery_order_dry_run(
        session_factory,
        chat_id=signal.chat_id,
        message_id=signal.message_id,
        symbol=symbol,
        side=side,
        contract_spec_provider=contract_spec_provider,
        persist_ready_confirmation=True,
        confirmed_at=now,
    )
    auto_draft = None
    execution_plan = None
    if entry_execution_type == "market":
        auto_draft = _build_auto_market_deepcoin_draft(
            signal=signal,
            runtime_kol_id=str(runtime_config["kol_id"]),
            reference_price=reference_price,
            stop_loss_text=candidate.stop_loss_text,
            take_profit_text=candidate.take_profit_text,
            take_profit_allocations=settings.take_profit_allocations,
            contract_spec_provider=contract_spec_provider,
        )
        execution_plan = "single_entry_market"
    elif entry_execution_type == "limit" and market_price_is_near_entry_edge(
        current_price=reference_price,
        entry_range=entry_range,
        side=side,
        max_distance=entry_thresholds.market_leg_threshold,
    ):
        auto_draft = _build_auto_hybrid_deepcoin_draft(
            signal=signal,
            runtime_kol_id=str(runtime_config["kol_id"]),
            entry_execution_type=entry_execution_type,
            reference_price=reference_price,
            stop_loss_text=candidate.stop_loss_text,
            take_profit_text=candidate.take_profit_text,
            take_profit_allocations=settings.take_profit_allocations,
            entry_thresholds=entry_thresholds,
            contract_spec_provider=contract_spec_provider,
        )
        execution_plan = "range_hybrid_market_half_limit_half"
    if auto_draft is not None:
        auto_draft["entry_preamble_assembly"] = assembly_evidence
        trade_signal = enqueue_trade_signal(
            session_factory,
            venue="deepcoin",
            source_type="recovery",
            kol_id=str(runtime_config["kol_id"]),
            chat_id=signal.chat_id,
            message_id=signal.message_id,
            symbol=symbol,
            side=side,
            action="open_position",
            payload={
                "source": {
                    "chat_id": signal.chat_id,
                    "message_id": signal.message_id,
                    "symbol": symbol,
                    "side": side,
                },
                "deepcoin_order_draft": auto_draft,
                "execution_plan": execution_plan,
                "entry_preamble_assembly": assembly_evidence,
            },
            strategy_instance_id=str(auto_draft.get("strategy_instance_id") or ""),
            enqueued_at=now,
        )
        submit_result = process_trade_signal_live(
            session_factory,
            signal_id=trade_signal.id,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            processed_at=now,
        )
    else:
        submit_result = submit_recovery_order_live(
            session_factory,
            chat_id=signal.chat_id,
            message_id=signal.message_id,
            symbol=symbol,
            side=side,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            submitted_at=now,
            max_order_legs=1 if entry_execution_type == "market" else None,
        )
    return {
        "status": "submitted",
        "entry_execution_type": entry_execution_type,
        "entry_preamble_assembly": assembly_evidence,
        "result": submit_result,
    }


def _record_entry_auto_trade_skip(
    session_factory: sessionmaker,
    *,
    raw_message: RawMessage,
    candidate: SignalCandidate,
    reason: str,
    runtime_kol_id: str | None = None,
    processed_at: datetime,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = (candidate.symbol or "").upper() or None
    side = (candidate.side or "").lower() or None
    payload = {
        "candidate_id": candidate.id,
        "parse_source": candidate.parse_source,
        "confidence": candidate.confidence,
        "event_type": candidate.event_type,
        "entry_text": candidate.entry_text,
        "stop_loss_text": candidate.stop_loss_text,
        "take_profit_text": candidate.take_profit_text,
    }
    if extra:
        payload.update(extra)
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="auto_trade_skipped",
            status="skipped",
            kol_id=runtime_kol_id,
            chat_id=raw_message.chat_id,
            message_id=raw_message.message_id,
            symbol=symbol,
            side=side,
            reason=reason,
            request=payload,
            created_at=processed_at,
        ),
    )
    result: dict[str, Any] = {"status": "skipped", "reason": reason}
    if extra:
        result.update(extra)
    return result


def disabled_management_message_needs_no_client(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> bool:
    """Return true only for a management candidate gated off before planning."""

    item_results = list_message_instruction_item_results(
        session_factory,
        raw_message_id=raw_message_id,
    )
    if any(
        item["instruction_kind"] == "entry" and item["status"] == "pending"
        for item in item_results
    ):
        return False
    settings = load_trading_settings(session_factory)
    loaded = _load_best_management_candidate(
        session_factory, raw_message_id=raw_message_id
    )
    return loaded is not None and (
        _is_informational_management_candidate(loaded[1])
        or not settings.management_planning_enabled
        or _composite_management_gate_reason(loaded[1], settings) is not None
    )


def _composite_management_gate_reason(
    candidate: SignalCandidate,
    settings,
) -> str | None:
    composite_action = (
        str(candidate.management_action or "").strip().lower()
        == "partial_then_break_even"
    )
    has_contract_json = bool(candidate.management_contract_json)
    has_contract_fingerprint = bool(
        candidate.management_contract_fingerprint
    )
    if not (
        composite_action or has_contract_json or has_contract_fingerprint
    ):
        return None
    if not (has_contract_json and has_contract_fingerprint):
        return "management_instruction_component_dropped"
    mode = settings.effective_composite_management_v2_mode
    if mode == "disabled":
        return "composite_management_v2_disabled"
    return None


def _is_informational_management_candidate(candidate: SignalCandidate) -> bool:
    return str(candidate.management_action or "").strip().lower() == "hold_update"


def _auto_process_management_signal(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    candidate_id: int,
    loaded: tuple[RawMessage, SignalCandidate, Source | None, bool],
    group_config: GroupConfig,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
    settings,
    processed_at: datetime,
    revision_replacement_writer: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw_message, candidate, source, has_media = loaded
    if candidate.id != candidate_id:
        return {"status": "blocked", "reason": "management_candidate_changed"}
    runtime_group_config = apply_trading_settings_to_group_config(
        group_config, settings
    )
    runtime_config = _resolve_runtime_config(
        runtime_group_config,
        raw_message=raw_message,
        source=source,
    )
    if runtime_config is None:
        return {"status": "skipped", "reason": "group_not_configured_for_auto_trade"}
    if runtime_config["trading_mode"] != "auto_trade":
        return {"status": "skipped", "reason": "kol_or_group_auto_trade_disabled"}
    symbol = str(candidate.symbol or "").upper()
    allowed_symbols = {item.upper() for item in settings.allowed_symbols}
    if (
        candidate.event_type != "strategy_revision"
        and (not symbol or symbol not in allowed_symbols)
    ):
        return {"status": "skipped", "reason": "symbol_not_allowed", "symbol": symbol}
    if candidate.confidence < settings.min_ai_confidence:
        return {"status": "skipped", "reason": "confidence_below_minimum"}
    if has_media and not settings.allow_vision_auto_trade:
        return {"status": "skipped", "reason": "vision_auto_trade_disabled"}
    if candidate.event_type == "strategy_revision":
        with session_factory() as session:
            lifecycle = session.get(
                StrategyLifecycle,
                int(candidate.target_lifecycle_id or 0),
            )
            strategy_thread_id = (
                int(lifecycle.strategy_thread_id)
                if lifecycle is not None
                and lifecycle.strategy_thread_id is not None
                else None
            )
            binding = (
                session.get(
                    ExecutionBinding,
                    int(lifecycle.execution_binding_id),
                )
                if lifecycle is not None
                and lifecycle.execution_binding_id is not None
                else None
            )
            if binding is not None:
                session.expunge(binding)
        if binding is None:
            return {
                "status": "blocked",
                "reason": "revision_binding_missing",
            }
        binding_symbol = str(binding.symbol or "").upper()
        if not binding_symbol or binding_symbol not in allowed_symbols:
            return {
                "status": "skipped",
                "reason": "symbol_not_allowed",
                "symbol": binding_symbol,
            }
        replacement_writer = revision_replacement_writer
        if replacement_writer is None:
            revision_entry_thresholds = settings.entry_thresholds_for_symbol(
                binding_symbol
            )
            reference_price = _safe_ticker_price(
                deepcoin_client,
                inst_id=_to_deepcoin_swap_instrument(binding_symbol),
            )
            entry_range = _parse_entry_range(
                candidate.entry_text,
                symbol=binding_symbol,
                reference_price=reference_price,
            )
            if entry_range is None:
                return {
                    "status": "blocked",
                    "reason": "revision_entry_range_invalid",
                }
            max_loss_usdt = _resolve_signal_max_loss_usdt(
                runtime_config,
                symbol=binding_symbol,
            )

            def replacement_writer(**kwargs):
                draft = _build_revision_deepcoin_draft(
                    binding=binding,
                    entry_range=entry_range,
                    stop_loss_text=candidate.stop_loss_text,
                    take_profit_text=candidate.take_profit_text,
                    risk_budget_usdt=(
                        max_loss_usdt * float(kwargs["remaining_fraction"])
                    ),
                    entry_thresholds=revision_entry_thresholds,
                    contract_spec_provider=contract_spec_provider,
                )
                return submit_strategy_revision_replacement_live(
                    session_factory,
                    batch_id=int(kwargs["batch_id"]),
                    draft=draft,
                    deepcoin_client=deepcoin_client,
                    contract_spec_provider=contract_spec_provider,
                    submitted_at=processed_at,
                )

            try:
                _build_revision_deepcoin_draft(
                    binding=binding,
                    entry_range=entry_range,
                    stop_loss_text=candidate.stop_loss_text,
                    take_profit_text=candidate.take_profit_text,
                    risk_budget_usdt=max_loss_usdt,
                    entry_thresholds=revision_entry_thresholds,
                    contract_spec_provider=contract_spec_provider,
                )
            except Exception:
                return {
                    "status": "blocked",
                    "reason": "revision_replacement_preflight_failed",
                }
        return execute_strategy_revision(
            session_factory,
            raw_message_id=raw_message.id,
            strategy_thread_id=strategy_thread_id,
            replacement={
                "entry": candidate.entry_text,
                "stop_loss": candidate.stop_loss_text,
                "take_profit": candidate.take_profit_text,
                "leverage": candidate.leverage_text,
            },
            deepcoin_client=deepcoin_client,
            replacement_writer=replacement_writer,
            processed_at=processed_at,
        )

    management_execution_mode = (
        settings.effective_composite_management_v2_mode
        if candidate.management_contract_json
        and candidate.management_contract_fingerprint
        else settings.management_execution_mode
    )
    result = plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_message_id,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        planned_at=processed_at,
        candidate_id=candidate_id,
        shadow_only=management_execution_mode == "shadow",
        execution_mode=management_execution_mode,
    )
    if result.status != "ready" or result.batch is None:
        return {
            "status": "blocked" if result.status == "blocked" else result.status,
            "reason": result.reason_code,
            "batch_id": result.batch_id,
            "execution_mode": management_execution_mode,
        }
    if management_execution_mode == "shadow":
        return {
            "status": "shadow_planned",
            "management_action": result.batch.effective_action,
            "batch_id": result.batch.id,
        }

    if result.batch.management_contract_json:
        composite = execute_composite_management_batch(
            session_factory,
            batch_id=result.batch.id,
            deepcoin_client=deepcoin_client,
            contract_spec_provider=contract_spec_provider,
            live_execution_gate=lambda: (
                load_trading_settings(
                    session_factory
                ).effective_composite_management_v2_mode
                == "live"
            ),
            now_provider=lambda: processed_at,
            backup_buffer_bps=str(settings.trigger_backup_stop_buffer_bps),
        )
        component_states = {row.status for row in composite.components}
        if composite.status == "succeeded":
            status = "succeeded"
        elif component_states.intersection(
            {"submitting", "awaiting_exchange", "recovery_required", "operator_required"}
        ):
            status = "recovery_required"
        else:
            status = "submitted"
        return {
            "status": status,
            "reason": composite.reason_code,
            "batch_id": composite.id,
            "submitted": status == "submitted",
        }
    return execute_management_batch(
        session_factory,
        batch_id=result.batch.id,
        deepcoin_client=deepcoin_client,
        executed_at=processed_at,
    )


def market_price_is_near_entry_edge(
    *,
    current_price: float | None,
    entry_range: tuple[float, float],
    side: str,
    max_distance: Decimal,
) -> bool:
    if current_price is None or max_distance <= 0:
        return False
    low, high = sorted((float(entry_range[0]), float(entry_range[1])))
    anchor = high if side.lower() == "long" else low
    return (
        abs(Decimal(str(current_price)) - Decimal(str(anchor)))
        <= max_distance
    )


def _build_auto_market_deepcoin_draft(
    *,
    signal: RecoverySignal,
    runtime_kol_id: str,
    reference_price: float | None,
    stop_loss_text: str | None,
    take_profit_text: str | None,
    take_profit_allocations: list[float],
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> dict[str, Any] | None:
    if reference_price is None:
        return None
    contract = f"{signal.symbol}-USDT"
    instrument_id = _to_deepcoin_swap_instrument(signal.symbol)
    contract_spec = (
        contract_spec_provider.get_contract_spec(instrument_id)
        if contract_spec_provider is not None
        else None
    )
    strategy_instance_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=signal.chat_id,
        message_id=signal.message_id,
        symbol=signal.symbol,
        side=signal.side,
    )
    return build_deepcoin_order_draft(
        {
            "venue": "deepcoin",
            "contract": contract,
            "order_type": "market",
            "open_side": "buy" if signal.side == "long" else "sell",
            "position_side": signal.side,
            "margin_mode": "cross",
            "position_mode": "split",
            "entry_range": f"{reference_price}-{reference_price}",
            "stop_loss": stop_loss_text,
            "take_profit": take_profit_text,
            "take_profit_allocations": take_profit_allocations,
            "risk_budget_usdt": signal.max_loss_usdt,
            "strategy_instance_id": strategy_instance_id,
            "source": {
                "kol_id": runtime_kol_id,
                "chat_id": signal.chat_id,
                "message_id": signal.message_id,
            },
        },
        contract_spec=contract_spec,
    )


def _build_auto_hybrid_deepcoin_draft(
    *,
    signal: RecoverySignal,
    runtime_kol_id: str,
    entry_execution_type: str,
    reference_price: float | None,
    stop_loss_text: str | None,
    take_profit_text: str | None,
    take_profit_allocations: list[float],
    entry_thresholds: SymbolEntryThresholds,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> dict[str, Any] | None:
    if reference_price is None:
        return None
    contract = f"{signal.symbol}-USDT"
    instrument_id = _to_deepcoin_swap_instrument(signal.symbol)
    contract_spec = (
        contract_spec_provider.get_contract_spec(instrument_id)
        if contract_spec_provider is not None
        else None
    )
    strategy_instance_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=signal.chat_id,
        message_id=signal.message_id,
        symbol=signal.symbol,
        side=signal.side,
    )
    return build_deepcoin_order_draft(
        {
            "venue": "deepcoin",
            "contract": contract,
            "order_type": entry_execution_type,
            "open_side": "buy" if signal.side == "long" else "sell",
            "position_side": signal.side,
            "margin_mode": "cross",
            "position_mode": "split",
            "entry_range": f"{signal.entry_range[0]}-{signal.entry_range[1]}",
            "stop_loss": stop_loss_text,
            "take_profit": take_profit_text,
            "take_profit_allocations": take_profit_allocations,
            "current_price": reference_price,
            **entry_thresholds.to_dict(),
            "risk_budget_usdt": signal.max_loss_usdt,
            "strategy_instance_id": strategy_instance_id,
            "source": {
                "kol_id": runtime_kol_id,
                "chat_id": signal.chat_id,
                "message_id": signal.message_id,
            },
        },
        contract_spec=contract_spec,
    )


def _build_revision_deepcoin_draft(
    *,
    binding: ExecutionBinding,
    entry_range: tuple[float, float],
    stop_loss_text: str | None,
    take_profit_text: str | None,
    risk_budget_usdt: float,
    entry_thresholds: SymbolEntryThresholds,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> dict[str, Any]:
    instrument_id = _to_deepcoin_swap_instrument(str(binding.symbol))
    contract_spec = (
        contract_spec_provider.get_contract_spec(instrument_id)
        if contract_spec_provider is not None
        else None
    )
    if contract_spec is None:
        raise ValueError("revision_contract_spec_unavailable")
    draft = build_deepcoin_order_draft(
        {
            "venue": "deepcoin",
            "contract": f"{str(binding.symbol).upper()}-USDT",
            "order_type": "limit",
            "open_side": "buy" if str(binding.side).lower() == "long" else "sell",
            "position_side": str(binding.side).lower(),
            "margin_mode": str(binding.margin_mode or "cross"),
            "position_mode": str(binding.position_mode or "split"),
            "entry_range": f"{entry_range[0]}-{entry_range[1]}",
            "stop_loss": stop_loss_text,
            "take_profit": take_profit_text,
            **entry_thresholds.to_dict(),
            "risk_budget_usdt": float(risk_budget_usdt),
            "strategy_instance_id": str(binding.strategy_instance_id),
            "source": {
                "kol_id": str(binding.kol_id),
                "chat_id": int(binding.chat_id),
                "message_id": int(binding.message_id),
            },
        },
        contract_spec=contract_spec,
    )
    if not draft.get("order_legs"):
        raise ValueError("revision_order_legs_missing")
    return draft


def _load_best_entry_candidate(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    candidate_id: int | None = None,
) -> tuple[RawMessage, SignalCandidate, Source | None, bool] | None:
    with session_factory() as session:
        query = (
            session.query(RawMessage, SignalCandidate, Source)
            .join(SignalCandidate, SignalCandidate.raw_message_id == RawMessage.id)
            .outerjoin(Source, SignalCandidate.source_id == Source.id)
            .filter(RawMessage.id == raw_message_id)
            .filter(SignalCandidate.event_type == "entry_signal")
        )
        if candidate_id is not None:
            query = query.filter(SignalCandidate.id == int(candidate_id))
        if session.query(RecognitionDecision.id).filter(
            RecognitionDecision.raw_message_id == raw_message_id
        ).first() is not None:
            query = query.filter(SignalCandidate.parse_source == "mimo_authoritative")
        row = query.order_by(
            SignalCandidate.confidence.desc(), SignalCandidate.id.asc()
        ).first()
        if row is None:
            return None
        raw_message, candidate, source = row
        has_media = (
            session.query(MediaAsset.id)
            .filter(MediaAsset.raw_message_id == raw_message.id)
            .first()
            is not None
        )
        session.expunge(raw_message)
        session.expunge(candidate)
        if source is not None:
            session.expunge(source)
        return raw_message, candidate, source, has_media


def _load_best_management_candidate(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    candidate_id: int | None = None,
) -> tuple[RawMessage, SignalCandidate, Source | None, bool] | None:
    with session_factory() as session:
        query = (
            session.query(RawMessage, SignalCandidate, Source)
            .join(SignalCandidate, SignalCandidate.raw_message_id == RawMessage.id)
            .outerjoin(Source, SignalCandidate.source_id == Source.id)
            .filter(RawMessage.id == raw_message_id)
            .filter(
                SignalCandidate.event_type.in_(
                    ["close_signal", "position_update", "strategy_revision"]
                )
            )
        )
        if candidate_id is not None:
            query = query.filter(SignalCandidate.id == int(candidate_id))
        if session.query(RecognitionDecision.id).filter(
            RecognitionDecision.raw_message_id == raw_message_id
        ).first() is not None:
            query = query.filter(SignalCandidate.parse_source == "mimo_authoritative")
        row = query.order_by(
            SignalCandidate.confidence.desc(), SignalCandidate.id.asc()
        ).first()
        if row is None:
            return None
        raw_message, candidate, source = row
        has_media = (
            session.query(MediaAsset.id)
            .filter(MediaAsset.raw_message_id == raw_message.id)
            .first()
            is not None
        )
        session.expunge(raw_message)
        session.expunge(candidate)
        if source is not None:
            session.expunge(source)
        return raw_message, candidate, source, has_media


def _load_active_execution_binding(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    kol_id: str | None = None,
    symbol: str,
    side: str,
) -> ExecutionBinding | None:
    with session_factory() as session:
        base_query = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.chat_id == chat_id)
            .filter(ExecutionBinding.symbol == symbol.upper())
            .filter(ExecutionBinding.side == side.lower())
            .filter(ExecutionBinding.status.in_(["open", "active"]))
        )
        if kol_id:
            rows = (
                base_query.filter(ExecutionBinding.kol_id == kol_id)
                .order_by(ExecutionBinding.id.desc())
                .limit(2)
                .all()
            )
            if len(rows) == 1:
                session.expunge(rows[0])
                return rows[0]
            if len(rows) > 1:
                return None
        rows = base_query.order_by(ExecutionBinding.id.desc()).limit(2).all()
        if len(rows) != 1:
            return None
        session.expunge(rows[0])
        return rows[0]


def _count_group_effective_positions(
    session_factory: sessionmaker,
    *,
    chat_id: int,
) -> int:
    with session_factory() as session:
        count = (
            session.query(func.count(func.distinct(ExecutionOrderLeg.pos_id)))
            .join(
                ExecutionBinding,
                ExecutionBinding.id == ExecutionOrderLeg.execution_binding_id,
            )
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.chat_id == chat_id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.status == "active")
            .filter(ExecutionOrderLeg.attribution_status == "verified")
            .filter(ExecutionOrderLeg.pos_id.is_not(None))
            .filter(ExecutionOrderLeg.pos_id != "")
            .scalar()
        )
    return int(count or 0)


def _load_active_execution_bindings(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    kol_id: str,
    symbol: str,
    side: str,
) -> list[ExecutionBinding]:
    """Return every exact active binding for a multi-position KOL management action."""

    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.chat_id == chat_id)
            .filter(ExecutionBinding.kol_id == kol_id)
            .filter(ExecutionBinding.symbol == symbol.upper())
            .filter(ExecutionBinding.side == side.lower())
            .filter(ExecutionBinding.status.in_(["open", "active"]))
            .order_by(ExecutionBinding.id.asc())
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _infer_entry_execution_type(
    entry_text: str | None,
    message_text: str | None,
    parse_source: str | None,
) -> str:
    if parse_source in {"entry_confirm_heuristic", "lifecycle_ai"}:
        return "market"
    text = " ".join(str(part or "") for part in (entry_text, message_text)).lower()
    if any(token in text for token in ["market", "市价", "现价", "直接", "马上", "立即"]):
        return "market"
    return "limit"


def _is_nearby_single_entry_text(entry_text: str | None, message_text: str | None) -> bool:
    text = " ".join(str(part or "") for part in (entry_text, message_text)).lower()
    return any(token in text for token in ["附近", "左右", "一线", "nearby", "around"])


def _is_single_price_entry_range(entry_range: tuple[float, float]) -> bool:
    return abs(float(entry_range[0]) - float(entry_range[1])) < 1e-9


def _single_entry_price_is_near_market(
    *,
    current_price: float | None,
    entry_range: tuple[float, float],
    max_deviation_pct: float,
) -> bool:
    if current_price is None:
        return False
    entry_price = float(entry_range[0])
    if entry_price <= 0:
        return False
    return abs(float(current_price) - entry_price) / entry_price * 100 <= float(
        max_deviation_pct
    )


def _to_deepcoin_swap_instrument(symbol: str) -> str:
    normalized = symbol.upper().replace("_", "-")
    if normalized.endswith("-SWAP"):
        return normalized
    if normalized.endswith("-USDT"):
        return f"{normalized}-SWAP"
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT-SWAP"
    return f"{normalized}-USDT-SWAP"


def _safe_ticker_price(
    deepcoin_client: DeepcoinTradingClientProtocol,
    *,
    inst_id: str,
) -> float | None:
    try:
        return deepcoin_client.get_ticker_price(inst_id=inst_id)
    except Exception:
        return None


def _first_price(
    text: str | None,
    *,
    symbol: str | None = None,
    reference_price: float | None = None,
) -> float | None:
    prices = extract_normalized_prices(
        text,
        symbol=symbol,
        reference_price=reference_price,
    )
    return prices[0] if prices else None


def _extract_partial_close_fraction(text: str | None) -> float | None:
    normalized = str(text or "")
    if not normalized:
        return None
    if not any(
        token in normalized
        for token in ("止盈", "减仓", "平仓", "平加仓", "利润", "仓位", "走", "出")
    ):
        return None
    percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", normalized)
    if percent_match:
        value = float(percent_match.group(1)) / 100
        return value if 0 < value < 1 else None
    if "一半" in normalized or "半仓" in normalized or "平加仓" in normalized:
        return 0.5
    chinese_digits = {
        "三成": 0.3,
        "四成": 0.4,
        "五成": 0.5,
        "六成": 0.6,
        "七成": 0.7,
        "八成": 0.8,
    }
    for token, value in chinese_digits.items():
        if token in normalized:
            return value
    return None


def _requests_breakeven_protection(text: str | None) -> bool:
    normalized = str(text or "")
    return any(token in normalized for token in ("回成本", "保护成本", "成本保护", "止损至成本", "止损到成本"))
