from telegram_kol_research.analytics import compute_summary_metrics


def test_compute_summary_metrics_returns_win_rate_and_profit_factor():
    trades = [
        {"status": "win", "pnl": 2.0},
        {"status": "loss", "pnl": -1.0},
    ]
    summary = compute_summary_metrics(trades)
    assert summary.win_rate == 0.5
    assert summary.profit_factor == 2.0
