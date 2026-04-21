import json

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MediaAsset, RawMessage, SignalCandidate, Source, TradeIdea


def test_export_dataset_writes_jsonl_with_reply_media_and_candidate_context(tmp_path):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "dataset.jsonl"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        source = Source(chat_id=9001, telegram_sender_id=501, display_name="Alice Trader")
        session.add(source)
        session.flush()

        seed_message = RawMessage(
            chat_id=9001,
            message_id=10,
            sender_id=501,
            sender_name="Alice Trader",
            text="Initial market context",
            raw_payload="{}",
            archived_target_group=True,
        )
        signal_message = RawMessage(
            chat_id=9001,
            message_id=11,
            sender_id=501,
            sender_name="Alice Trader",
            text="BTC long 68000-68200, SL 67500, TP 69000 / 70000",
            raw_payload="{}",
            reply_to_message_id=10,
            archived_target_group=True,
        )
        session.add_all([seed_message, signal_message])
        session.flush()

        session.add(
            MediaAsset(
                raw_message_id=signal_message.id,
                kind="photo",
                local_path="data/media/9001/11.jpg",
                ocr_text="BTC long 68000-68200 TP 69000 SL 67500",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=signal_message.id,
                source_id=source.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                parse_source="text+ocr",
                confidence=0.8,
                review_status="confirmed",
                entry_text="68000-68200",
                stop_loss_text="67500",
                take_profit_text="69000 / 70000",
            )
        )
        session.flush()
        session.add(
            TradeIdea(
                source_id=source.id,
                primary_signal_candidate_id=1,
                chat_id=9001,
                symbol="BTC",
                side="long",
                status="open",
                confidence=0.8,
            )
        )
        session.commit()

    from telegram_kol_research.dataset_export import export_dataset_jsonl

    written_path = export_dataset_jsonl(session_factory, output_path)
    lines = written_path.read_text(encoding="utf-8").strip().splitlines()

    assert len(lines) == 2
    exported_records = [json.loads(line) for line in lines]
    signal_record = next(record for record in exported_records if record["message_id"] == 11)

    assert signal_record["sender_name"] == "Alice Trader"
    assert signal_record["reply_context"]["message_id"] == 10
    assert signal_record["reply_context"]["text"] == "Initial market context"
    assert signal_record["media_assets"][0]["ocr_text"] == "BTC long 68000-68200 TP 69000 SL 67500"
    assert signal_record["candidate"]["symbol"] == "BTC"
    assert signal_record["candidate"]["side"] == "long"
    assert signal_record["trade_idea"]["status"] == "open"


def test_export_dataset_cli_writes_jsonl_file(tmp_path):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "dataset.jsonl"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=21,
                sender_id=777,
                sender_name="Demo Sender",
                text="demo message",
                raw_payload="{}",
                archived_target_group=True,
            )
        )
        session.commit()

    result = CliRunner().invoke(
        app,
        [
            "export-dataset",
            "--database-path",
            str(database_path),
            "--output-path",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    assert output_path.exists()
    assert "Dataset written to" in result.stdout


def test_export_dataset_can_limit_to_review_candidates(tmp_path):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "review-dataset.jsonl"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        source = Source(chat_id=9001, telegram_sender_id=501, display_name="Alice Trader")
        session.add(source)
        session.flush()

        raw_messages = [
            RawMessage(
                chat_id=9001,
                message_id=31,
                sender_id=501,
                sender_name="Alice Trader",
                text="unparsed message",
                raw_payload="{}",
                archived_target_group=True,
            ),
            RawMessage(
                chat_id=9001,
                message_id=32,
                sender_id=501,
                sender_name="Alice Trader",
                text="pending candidate",
                raw_payload="{}",
                archived_target_group=True,
            ),
            RawMessage(
                chat_id=9001,
                message_id=33,
                sender_id=501,
                sender_name="Alice Trader",
                text="confirmed candidate",
                raw_payload="{}",
                archived_target_group=True,
            ),
        ]
        session.add_all(raw_messages)
        session.flush()
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=raw_messages[1].id,
                    source_id=source.id,
                    symbol="BTC",
                    side="long",
                    event_type="entry_signal",
                    parse_source="text",
                    confidence=0.4,
                    review_status="pending",
                ),
                SignalCandidate(
                    raw_message_id=raw_messages[2].id,
                    source_id=source.id,
                    symbol="ETH",
                    side="long",
                    event_type="entry_signal",
                    parse_source="text",
                    confidence=0.9,
                    review_status="confirmed",
                ),
            ]
        )
        session.commit()

    from telegram_kol_research.dataset_export import export_dataset_jsonl

    written_path = export_dataset_jsonl(
        session_factory,
        output_path,
        review_only=True,
        confidence_threshold=0.8,
    )
    lines = written_path.read_text(encoding="utf-8").strip().splitlines()
    exported_records = [json.loads(line) for line in lines]
    message_ids = {record["message_id"] for record in exported_records}

    assert message_ids == {31, 32}


def test_export_dataset_cli_supports_review_only_mode(tmp_path):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "review-dataset.jsonl"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=41,
                sender_id=777,
                sender_name="Demo Sender",
                text="demo message",
                raw_payload="{}",
                archived_target_group=True,
            )
        )
        session.commit()

    result = CliRunner().invoke(
        app,
        [
            "export-dataset",
            "--database-path",
            str(database_path),
            "--output-path",
            str(output_path),
            "--review-only",
            "--confidence-threshold",
            "0.8",
        ],
    )

    assert result.exit_code == 0
    assert output_path.exists()


def test_export_dataset_can_pre_filter_signal_like_messages(tmp_path):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "signal-like.jsonl"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        raw_messages = [
            RawMessage(
                chat_id=9001,
                message_id=51,
                sender_id=777,
                sender_name="Demo Sender",
                text="good morning everyone",
                raw_payload="{}",
                archived_target_group=True,
            ),
            RawMessage(
                chat_id=9001,
                message_id=52,
                sender_id=777,
                sender_name="Demo Sender",
                text="BTC long 68000-68200",
                raw_payload="{}",
                archived_target_group=True,
            ),
            RawMessage(
                chat_id=9001,
                message_id=53,
                sender_id=777,
                sender_name="Demo Sender",
                text="#TAO bullish add more",
                raw_payload="{}",
                archived_target_group=True,
            ),
            RawMessage(
                chat_id=9001,
                message_id=54,
                sender_id=777,
                sender_name="Demo Sender",
                text="",
                raw_payload="{}",
                archived_target_group=True,
            ),
            RawMessage(
                chat_id=9001,
                message_id=55,
                sender_id=777,
                sender_name="Demo Sender",
                text="以太币 2055 附近第一波反弹出现了，后面重点关注2035附近吧",
                raw_payload="{}",
                archived_target_group=True,
            ),
        ]
        session.add_all(raw_messages)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_messages[3].id,
                kind="photo",
                local_path="data/media/9001/54.jpg",
                ocr_text=None,
            )
        )
        session.commit()

    from telegram_kol_research.dataset_export import export_dataset_jsonl

    written_path = export_dataset_jsonl(
        session_factory,
        output_path,
        review_only=True,
        signal_like_only=True,
    )
    lines = written_path.read_text(encoding="utf-8").strip().splitlines()
    exported_records = [json.loads(line) for line in lines]
    message_ids = {record["message_id"] for record in exported_records}

    assert message_ids == {52, 53, 55}


def test_export_dataset_signal_like_filter_prefers_structured_signals_over_brief_updates(
    tmp_path,
):
    database_path = tmp_path / "research.db"
    output_path = tmp_path / "signal-like.jsonl"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        raw_messages = [
            RawMessage(
                chat_id=9001,
                message_id=61,
                sender_id=777,
                sender_name="Demo Sender",
                text="73了可以全部止盈",
                raw_payload="{}",
                archived_target_group=True,
            ),
            RawMessage(
                chat_id=9001,
                message_id=62,
                sender_id=777,
                sender_name="Demo Sender",
                text=(
                    "Btc\n"
                    "方向：多\n"
                    "建仓：71500附近\n"
                    "止损：70800附近\n"
                    "止盈：72200-72900-73600"
                ),
                raw_payload="{}",
                archived_target_group=True,
            ),
            RawMessage(
                chat_id=9001,
                message_id=63,
                sender_id=777,
                sender_name="Demo Sender",
                text="抄底白银",
                raw_payload="{}",
                archived_target_group=True,
            ),
        ]
        session.add_all(raw_messages)
        session.commit()

    from telegram_kol_research.dataset_export import export_dataset_jsonl

    written_path = export_dataset_jsonl(
        session_factory,
        output_path,
        review_only=True,
        signal_like_only=True,
    )
    lines = written_path.read_text(encoding="utf-8").strip().splitlines()
    exported_records = [json.loads(line) for line in lines]
    message_ids = {record["message_id"] for record in exported_records}

    assert message_ids == {62, 63}
