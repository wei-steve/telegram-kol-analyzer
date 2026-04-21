import json

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage, SignalCandidate, TradeIdea


def test_import_llm_results_creates_and_updates_candidates(tmp_path):
    database_path = tmp_path / "research.db"
    result_path = tmp_path / "llm-result.json"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9001,
            message_id=201,
            sender_id=777,
            sender_name="Demo Sender",
            text="Btc direction long near 71500 stop 70800 target 72200",
            raw_payload="{}",
            archived_target_group=True,
        )
        existing_message = RawMessage(
            chat_id=9001,
            message_id=202,
            sender_id=777,
            sender_name="Demo Sender",
            text="existing candidate",
            raw_payload="{}",
            archived_target_group=True,
        )
        session.add_all([raw_message, existing_message])
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=existing_message.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                parse_source="text",
                confidence=0.4,
                review_status="pending",
            )
        )
        session.commit()

    result_payload = {
        "items": [
            {
                "raw_message_id": 1,
                "classification": "entry_signal",
                "signal_kind": "entry_signal",
                "confidence": 0.92,
                "needs_review": False,
                "reasoning_short": "Clear BTC long setup with entry, stop, and targets.",
                "normalized_signal": {
                    "symbol": "BTC",
                    "side": "long",
                    "entry_text": "71500附近",
                    "stop_loss_text": "70800",
                    "take_profit_text": "72200-72900",
                    "leverage_text": None,
                    "time_horizon": "intraday",
                },
            },
            {
                "raw_message_id": 2,
                "classification": "not_signal",
                "signal_kind": "market_commentary",
                "confidence": 0.88,
                "needs_review": False,
                "reasoning_short": "This reads like commentary rather than a new signal.",
                "normalized_signal": None,
            },
        ]
    }
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "import-llm-results",
            "--database-path",
            str(database_path),
            "--input-path",
            str(result_path),
            "--report-output-dir",
            str(tmp_path / "reports"),
        ],
    )

    assert result.exit_code == 0
    assert "Processed 2 LLM adjudication item(s)" in result.stdout

    with session_factory() as session:
        created_candidate = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == 1)
            .one()
        )
        updated_candidate = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == 2)
            .one()
        )

        assert created_candidate.parse_source == "llm"
        assert created_candidate.review_status == "confirmed"
        assert created_candidate.symbol == "BTC"
        assert created_candidate.entry_text == "71500附近"
        assert "Clear BTC long setup" in (created_candidate.review_note or "")

        assert updated_candidate.review_status == "rejected"
        assert updated_candidate.parse_source == "llm"
        assert "commentary" in (updated_candidate.review_note or "")


def test_import_llm_results_creates_trade_idea_for_confirmed_signal(tmp_path):
    database_path = tmp_path / "research.db"
    result_path = tmp_path / "llm-result.json"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=301,
                sender_id=888,
                sender_name="Strategy Lead",
                text="ETH short around 2200",
                raw_payload="{}",
                archived_target_group=True,
            )
        )
        session.commit()

    result_payload = {
        "items": [
            {
                "raw_message_id": 1,
                "classification": "entry_signal",
                "signal_kind": "entry_signal",
                "confidence": 0.9,
                "needs_review": False,
                "reasoning_short": "Actionable ETH short setup.",
                "normalized_signal": {
                    "symbol": "ETH",
                    "side": "short",
                    "entry_text": "2200附近",
                    "stop_loss_text": "2230",
                    "take_profit_text": "2155-2105",
                    "leverage_text": None,
                    "time_horizon": None,
                },
            }
        ]
    }
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "import-llm-results",
            "--database-path",
            str(database_path),
            "--input-path",
            str(result_path),
            "--report-output-dir",
            str(tmp_path / "reports"),
        ],
    )

    assert result.exit_code == 0

    with session_factory() as session:
        trade_ideas = session.query(TradeIdea).all()

    assert len(trade_ideas) == 1
    assert trade_ideas[0].symbol == "ETH"


def test_import_llm_results_refreshes_reports(tmp_path):
    database_path = tmp_path / "research.db"
    result_path = tmp_path / "llm-result.json"
    report_dir = tmp_path / "reports"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=401,
                sender_id=999,
                sender_name="Report Trader",
                text="BTC long around 70000",
                raw_payload="{}",
                archived_target_group=True,
            )
        )
        session.commit()

    result_payload = {
        "items": [
            {
                "raw_message_id": 1,
                "classification": "entry_signal",
                "signal_kind": "entry_signal",
                "confidence": 0.95,
                "needs_review": False,
                "reasoning_short": "Actionable BTC long setup.",
                "normalized_signal": {
                    "symbol": "BTC",
                    "side": "long",
                    "entry_text": "70000附近",
                    "stop_loss_text": "69500",
                    "take_profit_text": "71000-72000",
                    "leverage_text": None,
                    "time_horizon": None,
                },
            }
        ]
    }
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "import-llm-results",
            "--database-path",
            str(database_path),
            "--input-path",
            str(result_path),
            "--report-output-dir",
            str(report_dir),
        ],
    )

    assert result.exit_code == 0
    assert (report_dir / "leaderboard-strict.json").exists()
    assert (report_dir / "leaderboard-expanded.json").exists()

    strict_payload = json.loads((report_dir / "leaderboard-strict.json").read_text(encoding="utf-8"))
    expanded_payload = json.loads((report_dir / "leaderboard-expanded.json").read_text(encoding="utf-8"))

    assert strict_payload["mode"] == "strict"
    assert expanded_payload["mode"] == "expanded"
