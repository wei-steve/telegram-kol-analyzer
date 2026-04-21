import json

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, SignalCandidate


def test_report_command_reads_database_and_writes_leaderboard(tmp_path):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "leaderboard.json"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=77,
            sender_id=501,
            sender_name="Alice Trader",
            text="BTC long 68000-68200",
            raw_payload="{}",
            archived_target_group=True,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                symbol="BTC",
                side="long",
                parse_source="text",
                confidence=1.0,
                review_status="confirmed",
            )
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
    assert payload["rows"][0]["source"] == "Alice Trader"
    assert payload["rows"][0]["sample_size"] == 1
