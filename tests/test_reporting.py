from telegram_kol_research.reporting import render_leaderboard_rows


def test_render_leaderboard_rows_orders_by_quality_adjusted_rank():
    rows = render_leaderboard_rows(
        [
            {"source": "Alice", "win_rate": 0.6, "quality_score": 0.9},
            {"source": "Bob", "win_rate": 0.7, "quality_score": 0.4},
        ]
    )
    assert rows[0]["source"] == "Alice"
