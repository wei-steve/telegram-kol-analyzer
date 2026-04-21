from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, SignalCandidate


def test_review_command_reads_and_updates_database_candidates(tmp_path):
    database_path = tmp_path / "research.db"
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
                confidence=0.5,
                review_status="pending",
            )
        )
        session.commit()

    runner = CliRunner()
    list_result = runner.invoke(
        app,
        [
            "review",
            "--database-path",
            str(database_path),
        ],
    )
    assert list_result.exit_code == 0
    assert "Pending candidates: 1" in list_result.stdout
    assert "Alice Trader" in list_result.stdout

    update_result = runner.invoke(
        app,
        [
            "review",
            "--database-path",
            str(database_path),
            "--candidate-id",
            "1",
            "--decision",
            "confirmed",
            "--note",
            "validated from chart",
        ],
    )
    assert update_result.exit_code == 0

    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()

    assert candidate.review_status == "confirmed"
    assert candidate.review_note == "validated from chart"
