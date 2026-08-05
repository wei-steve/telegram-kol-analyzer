from telegram_kol_research.reporting import (
    format_entry_preamble_assembly_summary,
    render_leaderboard_rows,
)


def test_render_leaderboard_rows_orders_by_quality_adjusted_rank():
    rows = render_leaderboard_rows(
        [
            {"source": "Alice", "win_rate": 0.6, "quality_score": 0.9},
            {"source": "Bob", "win_rate": 0.7, "quality_score": 0.4},
        ]
    )
    assert rows[0]["source"] == "Alice"


def test_format_entry_preamble_assembly_summary_is_bounded_and_human_readable():
    summary = format_entry_preamble_assembly_summary(
        {
            "mode": "live",
            "configured_risk_budget_usdt": 20,
            "risk_multiplier": "0.5",
            "effective_risk_budget_usdt": 10,
            "preamble_message_id": 9901,
            "strategy_message_id": 9902,
            "prompt": "must-not-render",
        }
    )

    assert summary == {
        "mode": "live",
        "risk_calculation": "基础风险预算 20 USDT × 仓位倍率 50% = 实际风险预算 10 USDT",
        "message_pair": "前置消息 9901 / 策略消息 9902",
        "configured_risk_budget_usdt": 20.0,
        "risk_multiplier": 0.5,
        "effective_risk_budget_usdt": 10.0,
        "preamble_message_id": 9901,
        "strategy_message_id": 9902,
    }


def test_format_entry_preamble_assembly_summary_rejects_malformed_evidence():
    assert format_entry_preamble_assembly_summary({"risk_multiplier": "secret"}) is None
