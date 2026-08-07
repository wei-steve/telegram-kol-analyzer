import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_adjacent_entry_replay_covers_sanitized_pairs_without_writes(tmp_path):
    database = tmp_path / "adjacent-replay.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER);
        CREATE TABLE entry_strategy_fragments (
          id INTEGER PRIMARY KEY, raw_message_id INTEGER,
          target_strategy_raw_message_id INTEGER, fragment_kind TEXT,
          payload_json TEXT, source_relationship TEXT, status TEXT
        );
        INSERT INTO raw_messages VALUES
          (1, 1, 4154), (2, 1, 4155),
          (3, 2, 9901), (4, 2, 9902), (5, 2, 9935), (6, 2, 9936),
          (7, 3, 558), (8, 3, 559), (9, 3, 538), (10, 3, 539);
        INSERT INTO entry_strategy_fragments VALUES
          (1, 2, 1, 'supplemental_entry', '{"price":"63400"}', 'after_strategy', 'pending'),
          (2, 3, 4, 'risk_multiplier', '{"risk_multiplier":"0.5"}', 'before_strategy', 'consumed'),
          (3, 5, 6, 'risk_multiplier', '{"risk_multiplier":"1"}', 'before_strategy', 'assembled'),
          (4, 8, 7, 'risk_multiplier', '{"risk_multiplier":"0.5"}', 'after_strategy', 'pending'),
          (5, 10, 9, 'risk_multiplier', '{"risk_multiplier":"0.5"}', 'after_strategy', 'pending');
        """
    )
    connection.commit()
    connection.close()
    before = database.read_bytes()
    script = Path(__file__).parents[1] / "scripts" / "replay_adjacent_entry_assembly.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--database-path", str(database)],
        check=True, capture_output=True, text=True,
    )
    records = {
        (row["chat_id"], row["strategy_message_id"]): row
        for row in json.loads(completed.stdout)["records"]
    }

    assert records[(1, 4154)]["supplemental_entry_prices"] == ["63400"]
    assert records[(2, 9902)]["effective_risk_budget_usdt"] == "10"
    assert records[(2, 9936)]["effective_risk_budget_usdt"] == "20"
    assert records[(3, 558)]["effective_risk_budget_usdt"] == "10"
    assert records[(3, 538)]["effective_risk_budget_usdt"] == "10"
    assert database.read_bytes() == before


def test_adjacent_entry_replay_script_has_no_exchange_write_dependency():
    script = Path(__file__).parents[1] / "scripts" / "replay_adjacent_entry_assembly.py"
    source = script.read_text()
    assert "deepcoin" not in source.lower()
    assert "exchange_client" not in source.lower()
