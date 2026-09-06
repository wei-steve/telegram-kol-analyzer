import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deepcoin_trigger_lineage_lab.py"


@pytest.fixture
def lab():
    assert SCRIPT.exists(), "lineage lab implementation missing"
    spec = importlib.util.spec_from_file_location("lineage_lab", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inputs():
    return (
        {"instId": "ETH-USDT-SWAP", "state": "live", "ctVal": "0.1", "minSz": "0.1", "lotSz": "0.1", "tickSz": "0.01"},
        {"instId": "ETH-USDT-SWAP", "last": "2453.67", "ts": "1788617950000"},
    )


def test_confirmed_prices_size_and_server_generated_id(lab):
    m = lab.build_manifest(*inputs(), now_ms=1788617951000, run_id="abcd1234", variant="both")
    buy, sell = [x["body"] for x in m["requests"]]
    assert (buy["price"], buy["triggerPrice"], buy["slTriggerPx"]) == ("2452.67", "2452.67", "2442.67")
    assert (sell["price"], sell["triggerPrice"], sell["slTriggerPx"]) == ("2454.67", "2454.67", "2464.67")
    assert buy["sz"] == sell["sz"] == "0.1"
    assert m["quantity_eth"] == "0.01"
    assert buy["slOrdPx"] == sell["slOrdPx"] == "-1"
    # Live API rejected both legs with code=51 when tdMode was omitted,
    # even though isCrossMargin="1" was present.
    assert buy["tdMode"] == sell["tdMode"] == "cross"
    assert buy["isCrossMargin"] == sell["isCrossMargin"] == "1"
    assert "ordId" not in buy and "ordId" not in sell
    assert buy["clOrdId"] != sell["clOrdId"]
    assert buy["tag"] != sell["tag"]


@pytest.mark.parametrize("field,value", [("last", "NaN"), ("last", "10"), ("ts", "1788617900000")])
def test_bad_or_stale_quote_rejected(lab, field, value):
    spec, ticker = inputs()
    ticker[field] = value
    with pytest.raises(ValueError):
        lab.build_manifest(spec, ticker, now_ms=1788617951000, run_id="abcd1234", variant="both")


def test_size_rounds_up_and_tick_mismatch_stops(lab):
    spec, ticker = inputs()
    spec["minSz"] = "0.15"
    assert lab.build_manifest(spec, ticker, now_ms=1788617951000, run_id="a", variant="baseline")["requests"][0]["body"]["sz"] == "0.2"
    spec["tickSz"] = "0.1"
    with pytest.raises(ValueError):
        lab.build_manifest(spec, ticker, now_ms=1788617951000, run_id="a", variant="baseline")


@pytest.mark.parametrize("variant,fields", [("baseline", set()), ("client", {"clOrdId"}), ("tag", {"tag"}), ("both", {"clOrdId", "tag"})])
def test_diagnostic_variants(lab, variant, fields):
    m = lab.build_manifest(*inputs(), now_ms=1788617951000, run_id="a", variant=variant)
    assert set(m["requests"][0]["body"]) & {"clOrdId", "tag"} == fields


def test_read_routes_exclude_writes_and_unknown_queries(lab):
    for path in ["/deepcoin/trade/trigger-order", "https://example.com", "/deepcoin/trade/order?ordId=x", "/deepcoin/trade/cancel-order"]:
        with pytest.raises(ValueError):
            lab.validate_read(path, {})
    with pytest.raises(ValueError):
        lab.validate_read("/deepcoin/trade/order", {"body": "x"})
    lab.validate_read("/deepcoin/trade/order", {"instId": "ETH-USDT-SWAP", "ordId": "123"})


def test_raw_times_and_errors_are_preserved(lab, tmp_path):
    def transport(path, private):
        return 200, '{"code":"501","msg":"rejected","data":[]}'
    log = tmp_path / "raw.jsonl"
    with pytest.raises(ValueError):
        lab.read_evidence("/deepcoin/account/positions", {"instType": "SWAP", "instId": "ETH-USDT-SWAP"}, log, transport, private=True)
    record = json.loads(log.read_text())
    assert record["started_at"] <= record["finished_at"]
    assert record["payload"]["code"] == "501"
    assert record["status"] == "incomplete"
    assert "headers" not in record


def test_history_pagination_uses_before_and_marks_cap(lab):
    calls = []
    def getter(path, params):
        calls.append(dict(params))
        return [{"ordId": str(i)} for i in range(1, 101)]
    rows, complete = lab.history_pages(getter, "/deepcoin/trade/orders-history", {}, "ordId", max_pages=1)
    assert len(rows) == 100 and not complete
    batches = [[{"ordId": str(i)} for i in range(1, 101)], [{"ordId": "0"}]]
    def paged(path, params):
        calls.append(dict(params))
        return batches.pop(0)
    rows, complete = lab.history_pages(paged, "/deepcoin/trade/orders-history", {}, "ordId", max_pages=2)
    assert complete and calls[-1]["before"] == "1"


def test_duplicate_page_cannot_claim_complete(lab):
    rows, complete = lab.history_pages(lambda *_: [{"billId": str(i)} for i in range(100)], "/deepcoin/trade/fills", {}, "billId", max_pages=3)
    assert not complete


def test_history_stops_at_observation_window_not_all_account_history(lab):
    rows = [{"ordId": str(i), "cTime": "1788617900000"} for i in range(1, 101)]
    _, complete = lab.history_pages(lambda *_: rows, "/deepcoin/trade/orders-history", {}, "ordId", max_pages=1, window_start_ms=1788617950000, time_key="cTime")
    assert complete


def test_observer_stops_after_second_failed_get(lab, tmp_path, monkeypatch):
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(args)
        raise ValueError("query failed")
    monkeypatch.setattr(lab, "read_evidence", fail)
    monkeypatch.setattr(lab.time, "sleep", lambda _: None)
    result = lab.observe(tmp_path, None, 30, 5)
    assert len(attempts) == 2
    assert result["status"] == "incomplete"
    assert result["frames"] == 0
    frame = json.loads((tmp_path / "frames.jsonl").read_text())
    assert frame["incomplete"] == ["ValueError"]
