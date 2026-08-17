import sqlite3
from pathlib import Path

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_RUNTIME_PATHS = (
    "src/telegram_kol_research/mimo_contract_circuit.py",
    "src/telegram_kol_research/mimo_recognition_runs.py",
    "src/telegram_kol_research/mimo_v2_contract.py",
    "src/telegram_kol_research/mimo_v2_execution_adapter.py",
    "src/telegram_kol_research/mimo_v2_replay.py",
)
FORBIDDEN_RUNTIME_MARKERS = {
    "src/telegram_kol_research/authoritative_recognition.py": (
        "v2_live_adapter",
        "infer_mimo_authoritative_v2",
    ),
    "src/telegram_kol_research/cli.py": ("mimo-v2-replay",),
    "src/telegram_kol_research/trading_settings.py": (
        "mimo_contract_mode",
        "mimo_v2_activation_after_raw_message_id",
    ),
    "src/telegram_kol_research/web_app.py": ("mimo-v2", "v2_live_adapter"),
}


def test_mimo_v2_runtime_modules_are_retired():
    present = [path for path in FORBIDDEN_RUNTIME_PATHS if (ROOT / path).exists()]
    assert present == []


def test_mimo_v2_activation_surfaces_are_retired():
    found = []
    for relative_path, markers in FORBIDDEN_RUNTIME_MARKERS.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        found.extend(
            f"{relative_path}:{marker}" for marker in markers if marker in source
        )
    assert found == []


def test_pre_v2_runtime_ignores_additive_retired_mimo_schema(tmp_path):
    database_path = tmp_path / "retired-mimo-schema.db"
    factory = create_session_factory(database_path)
    with factory() as session:
        session.add(RawMessage(chat_id=1, message_id=1, text="baseline"))
        session.commit()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE message_evidence_versions "
            "ADD COLUMN mimo_recognition_run_id INTEGER"
        )
        connection.execute(
            "CREATE TABLE mimo_recognition_runs "
            "(id INTEGER PRIMARY KEY, run_kind TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE mimo_recognition_attempts "
            "(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE mimo_contract_circuit_state "
            "(id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO mimo_recognition_runs (id, run_kind) "
            "VALUES (1, 'v1_authoritative')"
        )
        connection.execute(
            "INSERT INTO mimo_recognition_attempts (id, run_id) VALUES (1, 1)"
        )

    reopened = create_session_factory(database_path)
    with reopened() as session:
        assert session.query(RawMessage).count() == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT run_kind FROM mimo_recognition_runs WHERE id = 1"
        ).fetchone() == ("v1_authoritative",)
