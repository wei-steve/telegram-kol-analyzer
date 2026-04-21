import json

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage


def test_export_llm_pack_writes_dataset_prompt_and_schema(tmp_path):
    database_path = tmp_path / "research.db"
    output_dir = tmp_path / "llm-pack"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9001,
                message_id=101,
                sender_id=777,
                sender_name="Demo Sender",
                text="BTC long 68000-68200, SL 67500, TP 69000 / 70000",
                raw_payload="{}",
                archived_target_group=True,
            )
        )
        session.commit()

    result = CliRunner().invoke(
        app,
        [
            "export-llm-pack",
            "--database-path",
            str(database_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0

    dataset_path = output_dir / "dataset.jsonl"
    prompt_path = output_dir / "prompt.md"
    schema_path = output_dir / "schema.json"
    manifest_path = output_dir / "manifest.json"
    template_path = output_dir / "response-template.json"

    assert dataset_path.exists()
    assert prompt_path.exists()
    assert schema_path.exists()
    assert manifest_path.exists()
    assert template_path.exists()

    dataset_lines = dataset_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(dataset_lines) == 1
    first_record = json.loads(dataset_lines[0])
    assert first_record["message_id"] == 101

    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert "You are adjudicating Telegram trading messages" in prompt_text
    assert "raw_message_id" in prompt_text
    assert "needs_review" in prompt_text

    schema_payload = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema_payload["type"] == "object"
    assert "items" in schema_payload["properties"]

    template_payload = json.loads(template_path.read_text(encoding="utf-8"))
    assert template_payload["items"][0]["raw_message_id"] == first_record["raw_message_id"]
    assert template_payload["items"][0]["classification"] == "needs_review"

    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["record_count"] == 1
    assert manifest_payload["dataset_path"] == str(dataset_path)
    assert manifest_payload["response_template_path"] == str(template_path)


def test_export_llm_pack_defaults_to_review_signal_like_dataset(tmp_path):
    database_path = tmp_path / "research.db"
    output_dir = tmp_path / "llm-pack"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9001,
                    message_id=111,
                    sender_id=777,
                    sender_name="Demo Sender",
                    text="good morning everyone",
                    raw_payload="{}",
                    archived_target_group=True,
                ),
                RawMessage(
                    chat_id=9001,
                    message_id=112,
                    sender_id=777,
                    sender_name="Demo Sender",
                    text="BTC long 68000-68200, SL 67500, TP 69000 / 70000",
                    raw_payload="{}",
                    archived_target_group=True,
                ),
            ]
        )
        session.commit()

    result = CliRunner().invoke(
        app,
        [
            "export-llm-pack",
            "--database-path",
            str(database_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0

    dataset_lines = (output_dir / "dataset.jsonl").read_text(encoding="utf-8").strip().splitlines()
    exported_records = [json.loads(line) for line in dataset_lines]

    assert {record["message_id"] for record in exported_records} == {112}


def test_export_llm_submission_sample_writes_copy_ready_markdown(tmp_path):
    database_path = tmp_path / "research.db"
    pack_dir = tmp_path / "llm-pack"
    sample_path = tmp_path / "submit-sample.md"
    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9001,
                    message_id=121,
                    sender_id=777,
                    sender_name="Demo Sender",
                    text="BTC long 68000-68200, SL 67500, TP 69000 / 70000",
                    raw_payload="{}",
                    archived_target_group=True,
                ),
                RawMessage(
                    chat_id=9001,
                    message_id=122,
                    sender_id=777,
                    sender_name="Demo Sender",
                    text="ETH short 2200, SL 2230, TP 2150",
                    raw_payload="{}",
                    archived_target_group=True,
                ),
            ]
        )
        session.commit()

    CliRunner().invoke(
        app,
        [
            "export-llm-pack",
            "--database-path",
            str(database_path),
            "--output-dir",
            str(pack_dir),
        ],
    )

    result = CliRunner().invoke(
        app,
        [
            "export-llm-submit-sample",
            "--pack-dir",
            str(pack_dir),
            "--output-path",
            str(sample_path),
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0
    sample_text = sample_path.read_text(encoding="utf-8")

    assert "Copy-Ready LLM Submission" in sample_text
    assert "Process exactly 1 input record(s)" in sample_text
    assert "```jsonl" in sample_text
    assert '"message_id": 121' in sample_text
    assert '"message_id": 122' not in sample_text
    assert "response-template.json" in sample_text
