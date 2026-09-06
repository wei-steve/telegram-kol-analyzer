import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def live():
    path = SCRIPTS / "deepcoin_trigger_lineage_live.py"
    assert path.exists(), "user-operated live experiment module missing"
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("eth_live_lab", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def manifest(live):
    return live.lab.build_manifest(
        {"instId": live.lab.INST, "state": "live", "ctVal": "0.1", "minSz": "0.1", "lotSz": "0.1", "tickSz": "0.01"},
        {"instId": live.lab.INST, "last": "2453.67", "ts": "1788617950000"},
        now_ms=1788617951000, run_id="test123", variant="both")


def test_minimum_pair_passes_and_size_increase_fails(live):
    m = manifest(live)
    live.validate_pair(m, now_ms=1788617951000)
    m["requests"][0]["body"]["sz"] = "1"
    with pytest.raises(ValueError):
        live.validate_pair(m, now_ms=1788617951000)


def test_exchange_minimum_increase_stops(live):
    m = manifest(live)
    m["instrument_spec"]["minSz"] = "0.2"
    for request in m["requests"]:
        request["body"]["sz"] = "0.2"
    m.update(quantity_eth="0.02", quantity_contracts="0.2")
    with pytest.raises(ValueError):
        live.validate_pair(m, now_ms=1788617951000)


@pytest.mark.parametrize("mutate", [
    lambda m: m["requests"][0]["body"].pop("slTriggerPx"),
    lambda m: m["requests"][0]["body"].update(side="sell"),
    lambda m: m["requests"][0]["body"].update(price="2453"),
    lambda m: m["requests"][0]["body"].update(slOrdPx="2442.67"),
    lambda m: m["requests"][0]["body"].update(ordId="123"),
])
def test_confirmed_trade_semantics_cannot_be_changed(live, mutate):
    m = manifest(live)
    mutate(m)
    with pytest.raises(ValueError):
        live.validate_pair(m, now_ms=1788617951000)


def test_quote_expiry_and_notional_cap(live):
    m = manifest(live)
    with pytest.raises(ValueError):
        live.validate_pair(m, now_ms=1788617970000)
    m["ticker"]["last"] = "6000"
    with pytest.raises(ValueError):
        live.validate_pair(m, now_ms=1788617951000)


@pytest.mark.parametrize("status,raw,outcome", [
    (200, '{"code":"0","data":{"ordId":"123","sCode":"0"}}', "accepted"),
    (200, '{"code":"0","data":{"ordId":"","sCode":"1001"}}', "rejected"),
    (200, '{"code":"0","data":{}}', "unknown_exchange_outcome"),
    (200, '{"code":"0","data":[{"ordId":"123","sCode":"0"}]}', "accepted"),
    (500, '{"code":"1"}', "unknown_exchange_outcome"),
    (200, 'not-json', "unknown_exchange_outcome"),
])
def test_post_outcome_and_raw_evidence(live, tmp_path, status, raw, outcome):
    calls = []
    def send(path, body):
        calls.append((path, body))
        assert (tmp_path / "submit-long-request.json").exists()
        return status, raw
    request = manifest(live)["requests"][0]
    result = live.write_once(request, tmp_path, "submit-long", send=send)
    assert len(calls) == 1
    assert result["outcome"] == outcome
    record = json.loads((tmp_path / "submit-long-response.json").read_text())
    assert record["raw_body"] == raw
    assert record["started_at"] <= record["finished_at"]


def test_network_timeout_is_not_retried(live, tmp_path):
    calls = []
    def fail(*args):
        calls.append(args)
        raise TimeoutError()
    result = live.write_once(manifest(live)["requests"][0], tmp_path, "submit-long", send=fail)
    assert len(calls) == 1 and result["outcome"] == "unknown_exchange_outcome"


def test_cleanup_selects_only_exact_owned_conditional_ids(live):
    rows = [
        {"instId": live.lab.INST, "ordId": "123", "triggerOrderType": "Conditional"},
        {"instId": live.lab.INST, "ordId": "456", "triggerOrderType": "TPSL"},
        {"instId": live.lab.INST, "ordId": "789", "triggerOrderType": "Conditional"},
    ]
    assert live.cancel_targets(rows, {"123", "456"}) == ["123"]


def test_pair_submits_exactly_two_and_never_retries_rejection(live, tmp_path):
    calls = []
    def send(path, body):
        calls.append(body["side"])
        return 200, '{"code":"0","data":{"sCode":"123","ordId":""}}'
    results = live.submit_pair(manifest(live), tmp_path, now_ms=1788617951000, send=send)
    assert sorted(calls) == ["buy", "sell"]
    assert [r["outcome"] for r in results] == ["rejected", "rejected"]


def test_one_shot_marker_prevents_second_run(live, tmp_path):
    live.claim_once(tmp_path, "run1")
    with pytest.raises(FileExistsError):
        live.claim_once(tmp_path, "run2")


def test_post_route_does_not_allow_market_entry_or_bulk_cancel(live, tmp_path):
    for path in ["/deepcoin/trade/order", "/deepcoin/trade/swap/cancel-trigger-all"]:
        with pytest.raises(ValueError):
            live.write_once({"method": "POST", "path": path, "body": {}}, tmp_path, "blocked")


def test_deadline_stops_before_read(live, tmp_path, monkeypatch):
    monkeypatch.setattr(live.time, "monotonic", lambda: 100)
    monkeypatch.setattr(live.lab, "read_evidence", lambda *a, **kw: pytest.fail("read after deadline"))
    with pytest.raises(TimeoutError):
        live.read_get(tmp_path, "/deepcoin/account/positions", {}, deadline=99)


def test_snapshot_ignores_old_page_and_captures_transient_stop(live, tmp_path, monkeypatch):
    calls = []
    def get(out, path, params, **kwargs):
        calls.append((path, params))
        if path.endswith("orders-history") and "trigger-" not in path:
            return [{"ordId": str(i), "cTime": "1700000000000"} for i in range(31)]
        if path.endswith("trigger-orders-history") and "ordId" not in params:
            return [{"ordId": "777", "instId": live.lab.INST, "triggerOrderType": "TPSL", "uTime": "1788617950000"}]
        return []
    monkeypatch.setattr(live, "read_get", get)
    frame = live.snapshot(tmp_path, parent_ids=set(), baseline_ids=set(), started_ms=1788617950000)
    assert frame["new_regular_ids"] == []
    assert any(path.endswith("trigger-orders-history") and params.get("ordId") == "777" for path, params in calls)


def test_failed_parent_cancel_is_not_success(live):
    for value in [
        {"attempts": [{"outcome": "rejected"}], "parents_still_pending": []},
        {"attempts": [{"outcome": "unknown_exchange_outcome"}], "parents_still_pending": []},
        {"attempts": [], "parents_still_pending": ["123"]},
    ]:
        assert live.cleanup_unresolved(value)
    assert not live.cleanup_unresolved({"attempts": [], "parents_still_pending": []})


def test_partial_submission_still_collects_without_resubmission(live, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(live, "worker_credentials", lambda: {"loaded_artifact_verified": True})
    monkeypatch.setattr(live, "read_get", lambda *a, **kw: [])
    monkeypatch.setattr(live.time, "time_ns", lambda: 1788617951000 * 1_000_000)
    monkeypatch.setattr(live.time, "monotonic", lambda: 0)
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    monkeypatch.setattr(live.signal, "signal", lambda *args: None)
    monkeypatch.setattr(live.lab, "prepare", lambda out, variant: live.lab.dump(out / "manifest.json", manifest(live)))
    def submit(*args, **kwargs):
        calls.append("submit")
        return [{"label": "long", "outcome": "accepted", "ordId": "123"},
                {"label": "short", "outcome": "unknown_exchange_outcome", "ordId": None}]
    monkeypatch.setattr(live, "submit_pair", submit)
    def snapshot(*args, **kwargs):
        calls.append("snapshot")
        monkeypatch.setattr(live.time, "monotonic", lambda: 10000)
        return {"new_regular_ids": [], "positions": []}
    monkeypatch.setattr(live, "snapshot", snapshot)
    monkeypatch.setattr(live, "cleanup_parents", lambda *args: {"attempts": [], "parents_still_pending": []})
    assert live.run_live(tmp_path) == 1
    assert calls == ["submit", "snapshot"]
