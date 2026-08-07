from telegram_kol_research.reporting import (
    format_entry_assembly_summary,
    format_entry_preamble_assembly_summary,
    format_entry_revision_summary,
    render_leaderboard_rows,
)


def test_format_v2_entry_assembly_summary_is_bounded():
    summary = format_entry_assembly_summary(
        {
            "mode": "live",
            "status": "assembled",
            "configured_risk_budget_usdt": 20,
            "risk_multiplier": "0.5",
            "effective_risk_budget_usdt": 10,
            "strategy_message_id": 9902,
            "fragment_ids": [11, 12],
            "entry_allocations": ["0.5", "0.5"],
            "supplemental_entry_prices": ["63400"],
            "secret": "must-not-render",
        }
    )

    assert summary["risk_calculation"] == (
        "配置20U × 50% = 实际风险预算10U"
    )
    assert summary["allocation_summary"] == "整单100%；两档各50%"
    assert summary["supplemental_summary"] == "补仓价 63400"
    assert summary["source_summary"] == "策略消息 9902 · 片段 11/12"
    assert "secret" not in summary


def test_format_entry_revision_summary_is_truthful_and_bounded():
    planned = format_entry_revision_summary(
        {
            "status": "planned",
            "reason_code": None,
            "market_snapshot": {"risk_decision": {"remaining_risk_usdt": "4.2"}},
            "replacement_count": 2,
            "raw_response": {"token": "secret"},
        }
    )
    succeeded = format_entry_revision_summary(
        {"status": "succeeded", "replacement_count": 2}
    )
    recovery = format_entry_revision_summary(
        {
            "status": "recovery_required",
            "reason_code": "entry_revision_verified_stop_missing",
        }
    )

    assert planned["label"] == "等待执行入场修订"
    assert planned["remaining_headroom"] == "剩余风险余量 4.2U"
    assert planned["orders_changed"] is False
    assert succeeded["label"] == "入场修订已读回确认"
    assert succeeded["orders_changed"] is True
    assert recovery["label"] == "入场修订需要人工处理"
    assert recovery["reason_code"] == "entry_revision_verified_stop_missing"
    assert "secret" not in str(planned)


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


def test_shadow_summary_distinguishes_proposed_from_applied_multiplier():
    summary = format_entry_preamble_assembly_summary(
        {
            "mode": "shadow",
            "configured_risk_budget_usdt": 20,
            "risk_multiplier": "0.5",
            "applied_risk_multiplier": "1",
            "effective_risk_budget_usdt": 20,
            "preamble_message_id": 9901,
            "strategy_message_id": 9902,
        }
    )

    assert summary["risk_calculation"] == (
        "基础风险预算 20 USDT × 实际倍率 100% = 实际风险预算 20 USDT"
    )
    assert summary["proposed_risk_calculation"] == (
        "影子建议：基础风险预算 20 USDT × 仓位倍率 50% = 建议风险预算 10 USDT"
    )
