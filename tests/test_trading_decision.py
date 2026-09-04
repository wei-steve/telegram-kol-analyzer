import pytest

from telegram_kol_research.trading_decision import (
    ActivePosition,
    TradingDecisionInput,
    evaluate_trading_decision,
)


def _signal(**overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 55,
        "symbol": "BTC",
        "side": "long",
        "entry_text": "68000-68200",
        "stop_loss_text": "67500",
        "take_profit_text": "69000 / 70000",
        "parse_source": "text",
        "confidence": 0.9,
        "trading_mode": "auto_trade",
        "symbol_whitelist": ["BTC", "ETH"],
    }
    values.update(overrides)
    return TradingDecisionInput(**values)


def test_notify_only_mode_blocks_auto_trading():
    decision = evaluate_trading_decision(_signal(trading_mode="notify_only"))

    assert decision.action == "notify_only"
    assert "notify_only_mode" in decision.reason_codes


def test_missing_stop_loss_requires_manual_review():
    decision = evaluate_trading_decision(_signal(stop_loss_text=None))

    assert decision.action == "manual_review"
    assert "missing_stop_loss" in decision.reason_codes


def test_symbol_outside_whitelist_requires_manual_review():
    decision = evaluate_trading_decision(_signal(symbol="SOL"))

    assert decision.action == "manual_review"
    assert "symbol_not_whitelisted" in decision.reason_codes


def test_image_or_vision_signal_requires_manual_review():
    decision = evaluate_trading_decision(_signal(parse_source="vision"))

    assert decision.action == "manual_review"
    assert "vision_requires_review" in decision.reason_codes


def test_auto_enabled_btc_signal_with_stop_loss_is_eligible_for_auto_trade():
    decision = evaluate_trading_decision(_signal())

    assert decision.action == "eligible_for_auto_trade"
    assert decision.reason_codes == ["risk_checks_passed"]
    assert decision.max_loss_usdt == 20.0


@pytest.mark.parametrize(
    "signal",
    [
        _signal(
            side="short",
            entry_text="69900",
            stop_loss_text="61600",
            take_profit_text="67900 / 66600",
        ),
        _signal(
            side="short",
            entry_text="79500-76500区域附近分批做空，均价78000附近",
            stop_loss_text="76000",
            take_profit_text="81500-83000-85000",
        ),
    ],
)
def test_real_wrong_geometry_candidates_require_manual_review(signal):
    decision = evaluate_trading_decision(signal)

    assert decision.action == "manual_review"
    assert decision.reason_codes == [
        "entry_price_geometry_stop_side_invalid"
    ]


def test_indeterminate_candidate_geometry_requires_manual_review():
    decision = evaluate_trading_decision(
        _signal(entry_text="现价入场", stop_loss_text="止损 2%")
    )

    assert decision.action == "manual_review"
    assert decision.reason_codes == ["entry_price_geometry_ambiguous"]


def test_existing_same_kol_position_requires_manual_review():
    decision = evaluate_trading_decision(
        _signal(),
        active_positions=[
            ActivePosition(
                kol_id="alice",
                chat_id=100,
                symbol="BTC",
                side="long",
                pos_id="deepcoin-pos-1",
            )
        ],
    )

    assert decision.action == "manual_review"
    assert "duplicate_active_position" in decision.reason_codes
