import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_shadow_replay_is_read_only_and_reports_proposed_budget(tmp_path):
    database = tmp_path / "replay.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE raw_messages (
          id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER,
          posted_at TEXT
        );
        CREATE TABLE entry_preambles (
          id INTEGER PRIMARY KEY, raw_message_id INTEGER, chat_id INTEGER,
          message_id INTEGER, symbol TEXT, side TEXT, risk_multiplier TEXT,
          status TEXT
        );
        CREATE TABLE signal_candidates (
          id INTEGER PRIMARY KEY, raw_message_id INTEGER, symbol TEXT,
          side TEXT, event_type TEXT
        );
        INSERT INTO raw_messages VALUES
          (9334, 9, 9901, '2026-08-05 01:00:00'),
          (9335, 9, 9902, '2026-08-05 01:01:00');
        INSERT INTO entry_preambles VALUES
          (1, 9334, 9, 9901, 'BTCUSDT', 'long', '0.5', 'pending');
        INSERT INTO signal_candidates VALUES
          (1, 9335, 'BTCUSDT', 'long', 'entry_signal');
        """
    )
    connection.commit()
    connection.close()
    before = database.read_bytes()
    script = Path(__file__).parents[1] / "scripts" / "replay_entry_preamble_shadow.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--database-path",
            str(database),
            "--preamble-raw-message-id",
            "9334",
            "--strategy-raw-message-id",
            "9335",
            "--configured-risk-usdt",
            "20",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "configured_risk_budget_usdt": "20",
        "decision": "proposed",
        "mode": "shadow",
        "preamble_message_id": 9901,
        "proposed_effective_risk_budget_usdt": "10",
        "reason_codes": [],
        "risk_multiplier": "0.5",
        "side": "long",
        "strategy_message_id": 9902,
        "symbol": "BTCUSDT",
    }
    assert database.read_bytes() == before
