"""Automatic live execution bridge for freshly recognized strategy signals."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.deepcoin_order_builder import build_deepcoin_order_draft
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.execution_bindings import build_strategy_instance_id
from telegram_kol_research.execution_bindings import reconcile_deepcoin_execution_bindings
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.models import ExecutionBinding, MediaAsset, RawMessage, SignalCandidate, Source, StrategyLifecycle
from telegram_kol_research.price_normalization import extract_normalized_prices
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_live_submit import process_trade_signal_live
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


def auto_process_message_trade_signal(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    group_config: GroupConfig,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    processed_at: datetime | None = None,
) -> dict[str, Any]:
    """Turn one fresh entry SignalCandidate into a queued and submitted live order."""

    now = processed_at or datetime.now(UTC)
    settings = load_trading_settings(session_factory)
    if not settings.auto_trade_enabled:
        return {"status": "skipped", "reason": "auto_trade_disabled"}

    loaded = _load_best_entry_candidate(session_factory, raw_message_id=raw_message_id)
    if loaded is None:
        return _auto_process_management_signal(
            session_factory,
            raw_message_id=raw_message_id,
            group_config=group_config,
            deepcoin_client=deepcoin_client,
            settings=settings,
            processed_at=now,
        )
    raw_message, candidate, source, has_media = loaded
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
        max_loss_usdt=_resolve_signal_max_loss_usdt(runtime_config, symbol=symbol),
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
        max_deviation_pct=settings.max_market_entry_deviation_pct,
    ):
        auto_draft = _build_auto_hybrid_deepcoin_draft(
            signal=signal,
            runtime_kol_id=str(runtime_config["kol_id"]),
            entry_execution_type=entry_execution_type,
            reference_price=reference_price,
            stop_loss_text=candidate.stop_loss_text,
            take_profit_text=candidate.take_profit_text,
            take_profit_allocations=settings.take_profit_allocations,
            max_market_entry_deviation_pct=settings.max_market_entry_deviation_pct,
            contract_spec_provider=contract_spec_provider,
        )
        execution_plan = "range_hybrid_market_half_limit_half"
    if auto_draft is not None:
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
    return {"status": "submitted", "entry_execution_type": entry_execution_type, "result": submit_result}


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


def _auto_process_management_signal(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    group_config: GroupConfig,
    deepcoin_client: DeepcoinTradingClientProtocol,
    settings,
    processed_at: datetime,
) -> dict[str, Any]:
    loaded = _load_best_management_candidate(session_factory, raw_message_id=raw_message_id)
    if loaded is None:
        return {"status": "skipped", "reason": "no_entry_signal_candidate"}
    raw_message, candidate, source, has_media = loaded
    runtime_group_config = apply_trading_settings_to_group_config(group_config, settings)
    runtime_config = _resolve_runtime_config(
        runtime_group_config,
        raw_message=raw_message,
        source=source,
    )
    if runtime_config is None:
        return {"status": "skipped", "reason": "group_not_configured_for_auto_trade"}
    if runtime_config["trading_mode"] != "auto_trade":
        return {"status": "skipped", "reason": "kol_or_group_auto_trade_disabled"}
    symbol = (candidate.symbol or "").upper()
    side = (candidate.side or "").lower()
    if not symbol or symbol not in {item.upper() for item in settings.allowed_symbols}:
        return {"status": "skipped", "reason": "symbol_not_allowed", "symbol": symbol}
    if candidate.confidence < settings.min_ai_confidence:
        return {"status": "skipped", "reason": "confidence_below_minimum"}
    if has_media and not settings.allow_vision_auto_trade:
        return {"status": "skipped", "reason": "vision_auto_trade_disabled"}

    if hasattr(deepcoin_client, "list_open_orders"):
        reconcile_deepcoin_execution_bindings(
            session_factory,
            client=deepcoin_client,
            recovered_at=processed_at,
        )
    partial_close_fraction = _extract_partial_close_fraction(raw_message.text)
    if (
        candidate.event_type == "position_update"
        and partial_close_fraction is not None
        and _requests_breakeven_protection(raw_message.text)
    ):
        bindings = _load_active_execution_bindings(
            session_factory,
            chat_id=raw_message.chat_id,
            kol_id=str(runtime_config["kol_id"]),
            symbol=symbol,
            side=side,
        )
        if not bindings:
            return {"status": "skipped", "reason": "no_execution_binding"}
        action = "partial_close_and_move_stop_to_entry"
        action_payload: dict[str, Any] = {
            "targets": [
                {"binding_id": binding.id, "fraction": partial_close_fraction}
                for binding in bindings
            ]
        }
        strategy_instance_id = None
    else:
        binding = _load_active_execution_binding(
            session_factory,
            chat_id=raw_message.chat_id,
            kol_id=str(runtime_config["kol_id"]),
            symbol=symbol,
            side=side,
        )
        if binding is None and candidate.event_type == "close_signal":
            binding = _recover_exit_signal_execution_binding(
                session_factory,
                raw_message=raw_message,
                candidate=candidate,
                runtime_kol_id=str(runtime_config["kol_id"]),
                deepcoin_client=deepcoin_client,
                recovered_at=processed_at,
            )
        if binding is None:
            return {"status": "skipped", "reason": "no_execution_binding"}

        action_payload = {"binding_id": binding.id}
        strategy_instance_id = binding.strategy_instance_id
        if candidate.event_type == "close_signal":
            action = "close_position" if binding.pos_id else "cancel_entry"
        else:
            if partial_close_fraction is not None:
                action = "close_position"
                action_payload["fraction"] = partial_close_fraction
                action_payload["partial_close_reason"] = "partial_take_profit"
            else:
                reference_price = _safe_ticker_price(
                    deepcoin_client,
                    inst_id=_to_deepcoin_swap_instrument(symbol),
                )
                stop_loss = _first_price(
                    candidate.stop_loss_text,
                    symbol=symbol,
                    reference_price=reference_price,
                )
                take_profit = _first_price(
                    candidate.take_profit_text,
                    symbol=symbol,
                    reference_price=reference_price,
                )
                if stop_loss is not None:
                    action_payload["stop_loss"] = stop_loss
                if take_profit is not None:
                    action_payload["take_profit"] = take_profit
                if stop_loss is not None and take_profit is not None:
                    action = "adjust_position_tpsl"
                elif stop_loss is not None:
                    action = "adjust_stop_loss"
                elif take_profit is not None:
                    action = "adjust_take_profit"
                else:
                    return {"status": "skipped", "reason": "no_tpsl_update"}

    trade_signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id=str(runtime_config["kol_id"]),
        chat_id=raw_message.chat_id,
        message_id=raw_message.message_id,
        symbol=symbol,
        side=side,
        action=action,
        payload={
            "source": {
                "chat_id": raw_message.chat_id,
                "message_id": raw_message.message_id,
                "candidate_id": candidate.id,
                "event_type": candidate.event_type,
            },
            **action_payload,
        },
        strategy_instance_id=strategy_instance_id,
        enqueued_at=processed_at,
    )
    submit_result = process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=deepcoin_client,
        processed_at=processed_at,
    )
    return {
        "status": "submitted",
        "management_action": action,
        "result": submit_result,
    }


def market_price_is_near_entry_edge(
    *,
    current_price: float | None,
    entry_range: tuple[float, float],
    side: str,
    max_deviation_pct: float,
) -> bool:
    if current_price is None:
        return False
    low, high = sorted((float(entry_range[0]), float(entry_range[1])))
    anchor = high if side.lower() == "long" else low
    if anchor <= 0:
        return False
    return abs(float(current_price) - anchor) / anchor * 100 <= float(max_deviation_pct)


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
    max_market_entry_deviation_pct: float,
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
            "entry_range_order_style": "hybrid",
            "current_price": reference_price,
            "max_market_entry_deviation_pct": max_market_entry_deviation_pct,
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


def _load_best_entry_candidate(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
) -> tuple[RawMessage, SignalCandidate, Source | None, bool] | None:
    with session_factory() as session:
        row = (
            session.query(RawMessage, SignalCandidate, Source)
            .join(SignalCandidate, SignalCandidate.raw_message_id == RawMessage.id)
            .outerjoin(Source, SignalCandidate.source_id == Source.id)
            .filter(RawMessage.id == raw_message_id)
            .filter(SignalCandidate.event_type == "entry_signal")
            .order_by(SignalCandidate.confidence.desc(), SignalCandidate.id.asc())
            .first()
        )
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
) -> tuple[RawMessage, SignalCandidate, Source | None, bool] | None:
    with session_factory() as session:
        row = (
            session.query(RawMessage, SignalCandidate, Source)
            .join(SignalCandidate, SignalCandidate.raw_message_id == RawMessage.id)
            .outerjoin(Source, SignalCandidate.source_id == Source.id)
            .filter(RawMessage.id == raw_message_id)
            .filter(SignalCandidate.event_type.in_(["close_signal", "position_update"]))
            .order_by(SignalCandidate.confidence.desc(), SignalCandidate.id.asc())
            .first()
        )
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


def _recover_exit_signal_execution_binding(
    session_factory: sessionmaker,
    *,
    raw_message: RawMessage,
    candidate: SignalCandidate,
    runtime_kol_id: str,
    deepcoin_client: DeepcoinTradingClientProtocol,
    recovered_at: datetime,
) -> ExecutionBinding | None:
    if not hasattr(deepcoin_client, "list_positions"):
        return None
    symbol = (candidate.symbol or "").upper()
    side = (candidate.side or "").lower()
    if not symbol or not side:
        return None
    inst_id = _to_deepcoin_swap_instrument(symbol)
    positions = deepcoin_client.list_positions(inst_id=inst_id)
    lifecycle = _load_exit_signal_lifecycle(
        session_factory,
        raw_message=raw_message,
        symbol=symbol,
        side=side,
    )
    if lifecycle is None:
        return None
    match = _select_unique_position_for_exit_lifecycle(
        session_factory,
        lifecycle=lifecycle,
        positions=positions,
        inst_id=inst_id,
    )
    if match is None:
        return None

    pos_id = _first_position_string(match, "posId", "pos_id", "id")
    if not pos_id:
        return None
    strategy_instance_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=lifecycle.chat_id,
        message_id=lifecycle.message_id,
        symbol=lifecycle.symbol,
        side=lifecycle.side,
    )
    with session_factory() as session:
        binding = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.chat_id == lifecycle.chat_id)
            .filter(ExecutionBinding.message_id == lifecycle.message_id)
            .filter(ExecutionBinding.symbol == lifecycle.symbol.upper())
            .filter(ExecutionBinding.side == lifecycle.side.lower())
            .one_or_none()
        )
        if binding is None:
            binding = ExecutionBinding(
                kol_id=runtime_kol_id,
                chat_id=lifecycle.chat_id,
                message_id=lifecycle.message_id,
                symbol=lifecycle.symbol.upper(),
                side=lifecycle.side.lower(),
                venue="deepcoin",
            )
            session.add(binding)
            session.flush()

        binding.strategy_instance_id = strategy_instance_id
        binding.kol_id = runtime_kol_id
        binding.pos_id = str(pos_id)
        binding.margin_mode = "cross"
        binding.position_mode = "split"
        binding.status = "active"
        binding.last_exchange_status = "position_active_recovered_for_exit_signal"
        binding.updated_at = recovered_at

        lifecycle_row = session.get(StrategyLifecycle, lifecycle.id)
        if lifecycle_row is not None:
            lifecycle_row.execution_binding_id = binding.id
            lifecycle_row.updated_at = recovered_at

        session.commit()
        session.refresh(binding)
        session.expunge(binding)
        return binding


def _load_exit_signal_lifecycle(
    session_factory: sessionmaker,
    *,
    raw_message: RawMessage,
    symbol: str,
    side: str,
) -> StrategyLifecycle | None:
    with session_factory() as session:
        row = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
            .filter(StrategyLifecycle.symbol == symbol.upper())
            .filter(StrategyLifecycle.side == side.lower())
            .filter(StrategyLifecycle.exit_signal_message_id == raw_message.message_id)
            .filter(StrategyLifecycle.exit_reason == "kol_signal")
            .order_by(StrategyLifecycle.entered_at.desc().nullslast(), StrategyLifecycle.id.desc())
            .first()
        )
        if row is None:
            return None
        session.expunge(row)
        return row


def _select_unique_position_for_exit_lifecycle(
    session_factory: sessionmaker,
    *,
    lifecycle: StrategyLifecycle,
    positions: list[dict[str, Any]],
    inst_id: str,
) -> dict[str, Any] | None:
    candidates = [
        position
        for position in positions
        if _position_matches_exit_lifecycle(
            lifecycle=lifecycle,
            position=position,
            inst_id=inst_id,
        )
    ]
    available = [
        position
        for position in candidates
        if _position_is_available_for_exit_recovery(
            session_factory,
            lifecycle=lifecycle,
            position=position,
        )
    ]
    return available[0] if len(available) == 1 else None


def _position_matches_exit_lifecycle(
    *,
    lifecycle: StrategyLifecycle,
    position: dict[str, Any],
    inst_id: str,
) -> bool:
    if str(position.get("instId") or "").upper() != inst_id.upper():
        return False
    if _normalize_position_side(position.get("posSide") or position.get("side")) != lifecycle.side.lower():
        return False
    if _position_size_from_payload(position) <= 0:
        return False
    position_price = _position_entry_price(position)
    if position_price is None:
        return False
    return _price_matches_lifecycle_entry(lifecycle, position_price)


def _position_is_available_for_exit_recovery(
    session_factory: sessionmaker,
    *,
    lifecycle: StrategyLifecycle,
    position: dict[str, Any],
) -> bool:
    pos_id = _first_position_string(position, "posId", "pos_id", "id")
    if not pos_id:
        return False
    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.symbol == lifecycle.symbol.upper())
            .filter(ExecutionBinding.side == lifecycle.side.lower())
            .all()
        )
    for row in rows:
        if str(pos_id) not in _split_csv_ids(row.pos_id):
            continue
        if row.chat_id == lifecycle.chat_id and row.message_id == lifecycle.message_id:
            continue
        if row.status in {"open", "active"}:
            if _binding_has_active_lifecycle(session_factory, row):
                return False
            continue
        if row.status == "stale" and row.last_exchange_status == "expired_pending_entry_not_attributed":
            continue
        return False
    return True


def _binding_has_active_lifecycle(
    session_factory: sessionmaker,
    binding: ExecutionBinding,
) -> bool:
    with session_factory() as session:
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.chat_id == binding.chat_id)
            .filter(StrategyLifecycle.message_id == binding.message_id)
            .filter(StrategyLifecycle.symbol == binding.symbol)
            .filter(StrategyLifecycle.side == binding.side)
            .order_by(StrategyLifecycle.id.desc())
            .first()
        )
    if lifecycle is None:
        return True
    return lifecycle.lifecycle_status in {"pending_entry", "entered"}


def _price_matches_lifecycle_entry(lifecycle: StrategyLifecycle, price: float) -> bool:
    reference = lifecycle.entry_price_actual
    if reference is not None and _prices_close(price, reference):
        return True
    low = lifecycle.entry_range_low
    high = lifecycle.entry_range_high
    if low is None and high is None:
        return False
    if low is None or high is None or abs(float(low) - float(high)) < 1e-9:
        single = float(low if low is not None else high)
        return _prices_close(price, single)
    lower, upper = sorted((float(low), float(high)))
    tolerance = max(5.0, min(abs(lower), abs(upper)) * 0.0005)
    return lower - tolerance <= price <= upper + tolerance


def _prices_close(left: float, right: float) -> bool:
    tolerance = max(5.0, abs(float(right)) * 0.0005)
    return abs(float(left) - float(right)) <= tolerance


def _position_entry_price(position: dict[str, Any]) -> float | None:
    return _float_or_none(
        position.get("avgPx")
        or position.get("avgPrice")
        or position.get("openAvgPx")
        or position.get("entryPrice")
        or position.get("px")
    )


def _position_size_from_payload(position: dict[str, Any]) -> float:
    value = _float_or_none(position.get("pos") or position.get("size") or position.get("sz"))
    return abs(value or 0.0)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_position_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _normalize_position_side(value: Any) -> str:
    side = str(value or "").lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return side


def _split_csv_ids(value: str | None) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


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
