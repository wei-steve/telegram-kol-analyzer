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
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, posted_at TEXT, text TEXT);
        CREATE TABLE signal_candidates (id INTEGER PRIMARY KEY, raw_message_id INTEGER, event_type TEXT, symbol TEXT, side TEXT);
        CREATE TABLE entry_strategy_fragments (
          id INTEGER PRIMARY KEY, raw_message_id INTEGER,
          target_strategy_raw_message_id INTEGER, chat_id INTEGER, symbol TEXT, side TEXT, fragment_kind TEXT,
          payload_json TEXT, source_relationship TEXT, status TEXT
        );
        INSERT INTO raw_messages VALUES
          (1, 1, 4154, '2026-08-01 00:00:00', 'BTC策略'), (2, 1, 4155, '2026-08-01 00:01:00', '补仓63400附近'),
          (3, 2, 9901, '2026-08-01 01:00:00', '半仓操作'), (4, 2, 9902, '2026-08-01 01:01:00', 'BTC策略'),
          (5, 2, 9935, '2026-08-01 02:00:00', '正常仓位操作'), (6, 2, 9936, '2026-08-01 02:01:00', 'BTC策略'),
          (7, 3, 558, '2026-08-01 03:00:00', 'BTC策略'), (8, 3, 559, '2026-08-01 03:01:00', '50%仓位'),
          (9, 3, 538, '2026-08-01 04:00:00', 'BTC策略'), (10, 3, 539, '2026-08-01 04:01:00', '50%仓位');
        INSERT INTO signal_candidates VALUES
          (1, 1, 'entry_signal', 'BTCUSDT', 'long'),
          (2, 4, 'entry_signal', 'BTCUSDT', 'long'),
          (3, 6, 'entry_signal', 'BTCUSDT', 'long'),
          (4, 7, 'entry_signal', 'BTCUSDT', 'long'),
          (5, 9, 'entry_signal', 'BTCUSDT', 'long');
        INSERT INTO entry_strategy_fragments VALUES
          (1, 2, 1, 1, 'BTCUSDT', 'long', 'supplemental_entry', '{"entry_price":"63400"}', 'after_strategy', 'pending'),
          (2, 3, 4, 2, 'BTCUSDT', 'long', 'risk_multiplier', '{"risk_multiplier":"0.5"}', 'before_strategy', 'consumed'),
          (3, 5, 6, 2, 'BTCUSDT', 'long', 'risk_multiplier', '{"risk_multiplier":"1"}', 'before_strategy', 'assembled'),
          (4, 8, 7, 3, 'BTCUSDT', 'long', 'risk_multiplier', '{"risk_multiplier":"0.5"}', 'after_strategy', 'pending'),
          (5, 10, 9, 3, 'BTCUSDT', 'long', 'risk_multiplier', '{"risk_multiplier":"0.5"}', 'after_strategy', 'pending');
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


def test_adjacent_entry_replay_can_reconstruct_unbackfilled_text_evidence(tmp_path):
    database = tmp_path / "historical.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, posted_at TEXT, text TEXT);
        CREATE TABLE signal_candidates (id INTEGER PRIMARY KEY, raw_message_id INTEGER, event_type TEXT, symbol TEXT, side TEXT);
        CREATE TABLE entry_strategy_fragments (id INTEGER PRIMARY KEY, raw_message_id INTEGER, target_strategy_raw_message_id INTEGER, chat_id INTEGER, symbol TEXT, side TEXT, fragment_kind TEXT, payload_json TEXT, source_relationship TEXT, status TEXT);
        INSERT INTO raw_messages VALUES
          (1, 2, 9901, '2026-08-01 01:00:00', '半仓操作'),
          (2, 2, 9902, '2026-08-01 01:01:00', 'BTC策略');
        INSERT INTO signal_candidates VALUES (1, 2, 'entry_signal', 'BTCUSDT', 'long');
        """
    )
    connection.commit()
    connection.close()
    script = Path(__file__).parents[1] / "scripts" / "replay_adjacent_entry_assembly.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--database-path", str(database), "--message-id", "9902"],
        check=True, capture_output=True, text=True,
    )
    record = json.loads(completed.stdout)["records"][0]
    assert record["effective_risk_budget_usdt"] == "10"
    assert record["source_message_ids"] == [9901]


def test_adjacent_entry_replay_script_has_no_exchange_write_dependency():
    script = Path(__file__).parents[1] / "scripts" / "replay_adjacent_entry_assembly.py"
    source = script.read_text()
    assert "deepcoin" not in source.lower()
    assert "exchange_client" not in source.lower()
