import json

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import Source, TradeIdea


def test_report_command_aggregates_trade_ideas_by_source(tmp_path):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "leaderboard.json"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        source = Source(
            telegram_sender_id=501,
            chat_id=9001,
            display_name="Alice Trader",
            custom_label="Alice",
        )
        session.add(source)
        session.flush()
        session.add_all(
            [
                TradeIdea(
                    source_id=source.id,
                    chat_id=9001,
                    symbol="BTC",
                    side="long",
                    status="win",
                    confidence=0.9,
                    pnl_r_multiple=2.0,
                ),
                TradeIdea(
                    source_id=source.id,
                    chat_id=9001,
                    symbol="ETH",
                    side="short",
                    status="loss",
                    confidence=0.8,
                    pnl_r_multiple=-1.0,
                ),
            ]
        )
        session.commit()

    result = CliRunner().invoke(
        app,
        [
            "report",
            "--output-path",
            str(output_path),
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["rows"][0]["source"] == "Alice"
    assert payload["rows"][0]["sample_size"] == 2
    assert payload["rows"][0]["win_rate"] == 0.5
