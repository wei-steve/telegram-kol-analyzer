import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def probe():
    sys.path.insert(0, str(SCRIPTS))
    try:
        path = SCRIPTS / "deepcoin_rest_ws_tpsl_experiment.py"
        spec = importlib.util.spec_from_file_location("rest_ws_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def test_extracts_short_key_private_ws_rows(probe):
    events = [
        {
            "received_ms": 101,
            "payload": {
                "action": "PushTrade",
                "result": [{"table": "Trade", "data": {"OS": "123", "TI": "9"}}],
            },
        },
        {"received_ms": 99, "payload": {"result": [{"table": "Order", "data": {"OS": "old"}}]}},
    ]
    assert probe.extract_ws_rows(events, after_ms=100) == [
        {
            "received_ms": 101,
            "action": "PushTrade",
            "table": "Trade",
            "data": {"OS": "123", "TI": "9"},
        }
    ]


def test_exact_chain_requires_trade_pos_and_trigger_tu_match(probe):
    rows = [
        {"received_ms": 101, "table": "Order", "data": {"OS": "123", "L": "client1"}},
        {"received_ms": 102, "table": "Trade", "data": {"OS": "123", "TI": "fill1"}},
        {"received_ms": 103, "table": "Position", "data": {"I": "ETHUSDT"}},
        {"received_ms": 104, "table": "TriggerOrder", "data": {"OS": "456", "TU": "789"}},
    ]
    result = probe.build_correlation(
        ws_rows=rows,
        main_order_id="123",
        client_order_id="client1",
        baseline_ids={"111"},
        positions=[{"posId": "789", "posSide": "short", "pos": "0.1"}],
        trigger_rows=[{"ordId": "456"}],
    )
    assert result["exact_entry_position_tpsl_chain_observed"] is True
    assert result["trigger_ids_whose_TU_equals_rest_posId"] == ["456"]
    assert result["ws_and_rest_trigger_ids"] == ["456"]


def test_price_and_time_similarity_never_create_exact_chain(probe):
    result = probe.build_correlation(
        ws_rows=[
            {"received_ms": 101, "table": "Trade", "data": {"OS": "123"}},
            {"received_ms": 102, "table": "TriggerOrder", "data": {"OS": "456", "TU": "account-id"}},
        ],
        main_order_id="123",
        client_order_id="client1",
        baseline_ids=set(),
        positions=[{"posId": "789", "posSide": "short"}],
        trigger_rows=[{"ordId": "456", "slTriggerPx": "2500"}],
    )
    assert result["exact_entry_position_tpsl_chain_observed"] is False
    assert "do not infer ownership" in result["conclusion"]


def test_long_position_cannot_satisfy_short_experiment_chain(probe):
    result = probe.build_correlation(
        ws_rows=[
            {"received_ms": 101, "table": "Trade", "data": {"OS": "123"}},
            {"received_ms": 102, "table": "TriggerOrder", "data": {"OS": "456", "TU": "789"}},
        ],
        main_order_id="123",
        client_order_id="client1",
        baseline_ids=set(),
        positions=[{"posId": "789", "posSide": "long"}],
        trigger_rows=[{"ordId": "456"}],
    )
    assert result["rest_position_ids"] == []
    assert result["exact_entry_position_tpsl_chain_observed"] is False


def test_default_mode_never_loads_credentials_or_trades(probe, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["probe.py"])
    monkeypatch.setattr(probe, "run_live", lambda *args: pytest.fail("unexpected live run"))
    assert probe.main() == 0


def test_event_file_reader_ignores_incomplete_line(probe, tmp_path):
    path = tmp_path / "ws-events.jsonl"
    path.write_text(json.dumps({"received_ms": 1, "payload": {}}) + "\n{" + "\n")
    assert probe.read_ws_events(path) == [{"received_ms": 1, "payload": {}}]


def test_correlation_allows_missing_client_id_but_requires_main_id(probe):
    result = probe.build_correlation(
        ws_rows=[
            {"received_ms": 101, "table": "Trade", "data": {"OS": "123"}},
            {"received_ms": 102, "table": "TriggerOrder", "data": {"OS": "456", "TU": "789"}},
        ],
        main_order_id="123",
        client_order_id="",
        baseline_ids=set(),
        positions=[{"posId": "789", "posSide": "short"}],
        trigger_rows=[{"ordId": "456"}],
    )
    assert result["client_order_id"] == ""
    assert result["exact_entry_position_tpsl_chain_observed"] is True
