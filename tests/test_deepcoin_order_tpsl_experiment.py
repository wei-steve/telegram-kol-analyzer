import importlib.util
from pathlib import Path
import sys
import json

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def probe():
    path = ROOT / "scripts/deepcoin_order_tpsl_experiment.py"
    assert path.exists(), "ordinary order experiment is not implemented"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("ordinary_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


def make_manifest(probe):
    return probe.build_manifest(
        {"instId": "ETH-USDT-SWAP", "state": "live", "ctVal": "0.1", "lotSz": "0.1", "minSz": "0.1", "tickSz": "0.01"},
        {"instId": "ETH-USDT-SWAP", "last": "2457.52", "ts": "1788621246000"},
        now_ms=1788621247000, run_id="ordinary1")


def test_short_only_manifest_contains_one_sell_request(probe):
    manifest = probe.build_manifest(
        {"instId": "ETH-USDT-SWAP", "state": "live", "ctVal": "0.1", "lotSz": "0.1", "minSz": "0.1", "tickSz": "0.01"},
        {"instId": "ETH-USDT-SWAP", "last": "2457.52", "ts": "1788621246000"},
        now_ms=1788621247000, run_id="shortonly1", selection="short")
    assert manifest["selection"] == "short"
    assert len(manifest["requests"]) == 1
    body = manifest["requests"][0]["body"]
    assert (body["side"], body["posSide"]) == ("sell", "short")
    assert (body["px"], body["tpTriggerPx"], body["slTriggerPx"]) == ("2458.52", "2448.52", "2468.52")
    assert body["clOrdId"] == "EOshortonly1S"


def test_short_without_client_id_changes_only_that_field(probe):
    with_client = probe.build_manifest(
        {"instId": "ETH-USDT-SWAP", "state": "live", "ctVal": "0.1", "lotSz": "0.1", "minSz": "0.1", "tickSz": "0.01"},
        {"instId": "ETH-USDT-SWAP", "last": "2457.52", "ts": "1788621246000"},
        now_ms=1788621247000, run_id="shortonly1", selection="short")
    without_client = probe.build_manifest(
        with_client["instrument_spec"], with_client["ticker"],
        now_ms=1788621247000, run_id="shortonly1", selection="short_no_clordid")
    assert without_client["selection"] == "short_no_clordid"
    assert len(without_client["requests"]) == 1
    expected = dict(with_client["requests"][0]["body"])
    expected.pop("clOrdId")
    assert without_client["requests"][0]["body"] == expected


def test_documented_order_fields_and_tp_sl_direction(probe):
    m = make_manifest(probe)
    long, short = [r["body"] for r in m["requests"]]
    assert set(long) == {"instId", "tdMode", "mrgPosition", "side", "posSide", "ordType", "px", "sz", "clOrdId", "tpTriggerPx", "slTriggerPx"}
    assert (long["px"], long["tpTriggerPx"], long["slTriggerPx"]) == ("2456.52", "2466.52", "2446.52")
    assert (short["px"], short["tpTriggerPx"], short["slTriggerPx"]) == ("2458.52", "2448.52", "2468.52")
    assert long["ordType"] == short["ordType"] == "limit"
    assert long["tdMode"] == short["tdMode"] == "cross"
    assert long["sz"] == short["sz"] == "0.1"
    assert m["quantity_eth"] == "0.01"
    assert long["clOrdId"] != short["clOrdId"]
    assert all(r["path"] == "/deepcoin/trade/order" for r in m["requests"])


@pytest.mark.parametrize("field,value", [("sz", "1"), ("ordType", "market"), ("slTriggerPx", "0"), ("tpTriggerPx", "0"), ("triggerPrice", "2456.52"), ("tag", "test")])
def test_changes_to_confirmed_pair_rejected(probe, field, value):
    m = make_manifest(probe)
    m["requests"][0]["body"][field] = value
    with pytest.raises(ValueError):
        probe.validate_pair(m, now_ms=1788621247000)


def test_stale_price_and_minimum_above_cap(probe):
    m = make_manifest(probe)
    with pytest.raises(ValueError):
        probe.validate_pair(m, now_ms=1788621277000)
    spec = dict(m["instrument_spec"], minSz="1")
    m = probe.build_manifest(spec, m["ticker"], now_ms=1788621247000, run_id="ordinary1")
    with pytest.raises(ValueError):
        probe.validate_pair(m, now_ms=1788621247000)


def test_send_pair_saves_correct_route_and_never_retries(probe, tmp_path):
    calls = []
    def send(path, body):
        calls.append(path)
        raise TimeoutError()
    results = probe.submit_pair(make_manifest(probe), tmp_path, now_ms=1788621247000, send=send)
    assert calls == ["/deepcoin/trade/order", "/deepcoin/trade/order"]
    assert all(r["outcome"] == "unknown_exchange_outcome" for r in results)
    for path in tmp_path.glob("submit-*-request.json"):
        assert json.loads(path.read_text())["request"]["path"] == "/deepcoin/trade/order"


def test_send_short_only_calls_order_once_without_retry(probe, tmp_path):
    calls = []
    manifest = probe.build_manifest(
        {"instId": "ETH-USDT-SWAP", "state": "live", "ctVal": "0.1", "lotSz": "0.1", "minSz": "0.1", "tickSz": "0.01"},
        {"instId": "ETH-USDT-SWAP", "last": "2457.52", "ts": "1788621246000"},
        now_ms=1788621247000, run_id="shortonly1", selection="short")
    def send(path, body):
        calls.append((path, body["posSide"]))
        raise TimeoutError()
    results = probe.submit_pair(manifest, tmp_path, now_ms=1788621247000, send=send)
    assert calls == [("/deepcoin/trade/order", "short")]
    assert [result["outcome"] for result in results] == ["unknown_exchange_outcome"]


def test_cleanup_only_cancels_owned_ordinary_order(probe, tmp_path, monkeypatch):
    snapshots = [[{"ordId": "123", "instId": "ETH-USDT-SWAP"}, {"ordId": "999", "instId": "ETH-USDT-SWAP"}], [{"ordId": "999", "instId": "ETH-USDT-SWAP"}]]
    monkeypatch.setattr(probe.core, "read_get", lambda *a, **kw: snapshots.pop(0))
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    writes = []
    def send(path, body):
        writes.append((path, body))
        return 200, '{"code":"0","data":{"sCode":"0","ordId":"123"}}'
    result = probe.cleanup_entries(tmp_path, {"123"}, send=send)
    assert writes == [("/deepcoin/trade/cancel-order", {"instId": "ETH-USDT-SWAP", "ordId": "123"})]
    assert result["entries_still_pending"] == []


def test_default_trigger_io_still_blocks_ordinary_route(probe, tmp_path):
    with pytest.raises(ValueError):
        probe.core.write_once(make_manifest(probe)["requests"][0], tmp_path, "blocked")


@pytest.mark.parametrize("invalid", [
    [{}],
    [{"ordId": "bad", "instId": "ETH-USDT-SWAP"}],
    [{"ordId": "123", "instId": "BTC-USDT-SWAP"}],
    [{"ordId": "123", "instId": "ETH-USDT-SWAP"}] * 2,
])
def test_cleanup_bad_readback_is_not_empty_success(probe, tmp_path, monkeypatch, invalid):
    values = [[], invalid]
    monkeypatch.setattr(probe.core, "read_get", lambda *a, **kw: values.pop(0))
    with pytest.raises(ValueError):
        probe.cleanup_entries(tmp_path, {"123"})


def test_no_flag_does_not_load_credentials_or_trade(probe, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["probe.py"])
    monkeypatch.setattr(probe, "run_live", lambda *a: pytest.fail("live without explicit flag"))
    assert probe.main() == 0


def test_short_flag_selects_short_live_mode(probe, monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "argv", ["probe.py", "--execute-order-tpsl-short"])
    monkeypatch.setattr(probe, "run_live", lambda root, selection="pair": seen.append(selection) or 0)
    assert probe.main() == 0
    assert seen == ["short"]


def test_short_without_client_id_flag_selects_exact_mode(probe, monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "argv", ["probe.py", "--execute-order-tpsl-short-no-clordid"])
    monkeypatch.setattr(probe, "run_live", lambda root, selection="pair": seen.append(selection) or 0)
    assert probe.main() == 0
    assert seen == ["short_no_clordid"]


def test_short_live_mode_treats_one_accepted_order_as_complete_observation(probe, tmp_path, monkeypatch):
    manifest = probe.build_manifest(
        {"instId": "ETH-USDT-SWAP", "state": "live", "ctVal": "0.1", "lotSz": "0.1", "minSz": "0.1", "tickSz": "0.01"},
        {"instId": "ETH-USDT-SWAP", "last": "2457.52", "ts": "1788621246000"},
        now_ms=1788621247000, run_id="shortonly1", selection="short")
    monkeypatch.setattr(probe.core, "worker_credentials", lambda: {"loaded_artifact_verified": True})
    monkeypatch.setattr(probe.core, "read_get", lambda *a, **kw: [])
    monkeypatch.setattr(probe.time, "time_ns", lambda: 1788621247000 * 1_000_000)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 0)
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    monkeypatch.setattr(probe.signal, "signal", lambda *a: None)
    monkeypatch.setattr(probe, "prepare", lambda *a: manifest)
    monkeypatch.setattr(probe, "submit_pair", lambda *a, **kw: [
        {"label": "short", "outcome": "accepted", "ordId": "124"}])
    def observe(*args, **kwargs):
        monkeypatch.setattr(probe.time, "monotonic", lambda: 10000)
        return {"entry_states": {}, "positions": [], "tpsl_candidates": [], "coverage_gaps": [],
                "attached_protection_status": "unverified"}
    monkeypatch.setattr(probe, "observe_frame", observe)
    monkeypatch.setattr(probe, "cleanup_entries", lambda *a, **kw: {"attempts": [], "entries_still_pending": []})
    assert probe.run_live(tmp_path, selection="short") == 0
    summary = json.loads(next(tmp_path.glob("live-*/live-summary.json")).read_text())
    assert summary["status"] == "observed_protection_requires_review"
    assert [item["label"] for item in summary["submissions"]] == ["short"]


def test_short_without_client_id_live_mode_does_not_require_client_readback(probe, tmp_path, monkeypatch):
    manifest = probe.build_manifest(
        {"instId": "ETH-USDT-SWAP", "state": "live", "ctVal": "0.1", "lotSz": "0.1", "minSz": "0.1", "tickSz": "0.01"},
        {"instId": "ETH-USDT-SWAP", "last": "2457.52", "ts": "1788621246000"},
        now_ms=1788621247000, run_id="shortonly1", selection="short_no_clordid")
    monkeypatch.setattr(probe.core, "worker_credentials", lambda: {"loaded_artifact_verified": True})
    monkeypatch.setattr(probe.core, "read_get", lambda *a, **kw: [])
    monkeypatch.setattr(probe.time, "time_ns", lambda: 1788621247000 * 1_000_000)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 0)
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    monkeypatch.setattr(probe.signal, "signal", lambda *a: None)
    monkeypatch.setattr(probe, "prepare", lambda *a: manifest)
    monkeypatch.setattr(probe, "submit_pair", lambda *a, **kw: [
        {"label": "short", "outcome": "rejected", "ordId": None}])
    seen_clients = []
    def observe(out, owned_ids, clients, *args):
        seen_clients.extend(clients)
        return {"entry_states": {}, "positions": [], "tpsl_candidates": [], "coverage_gaps": [],
                "attached_protection_status": "unverified"}
    monkeypatch.setattr(probe, "observe_frame", observe)
    monkeypatch.setattr(probe, "cleanup_entries", lambda *a, **kw: {"attempts": [], "entries_still_pending": []})
    assert probe.run_live(tmp_path, selection="short_no_clordid") == 1
    assert seen_clients == []


@pytest.mark.parametrize("second_outcome,leftover,expected_status", [
    ("accepted", [], "observed_protection_requires_review"),
    ("unknown_exchange_outcome", [], "partial_or_unknown_submission"),
    ("accepted", ["123"], "cleanup_unresolved"),
])
def test_simulated_run_collects_and_keeps_failure_state(probe, tmp_path, monkeypatch, second_outcome, leftover, expected_status):
    calls = []
    monkeypatch.setattr(probe.core, "worker_credentials", lambda: {"loaded_artifact_verified": True})
    monkeypatch.setattr(probe.core, "read_get", lambda *a, **kw: [])
    monkeypatch.setattr(probe.time, "time_ns", lambda: 1788621247000 * 1_000_000)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 0)
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    monkeypatch.setattr(probe.signal, "signal", lambda *a: None)
    monkeypatch.setattr(probe, "prepare", lambda *a: make_manifest(probe))
    def submit(*a, **kw):
        calls.append("submit")
        return [{"label": "long", "outcome": "accepted", "ordId": "123"},
                {"label": "short", "outcome": second_outcome, "ordId": "124" if second_outcome == "accepted" else None}]
    monkeypatch.setattr(probe, "submit_pair", submit)
    def observe(*a, **kw):
        calls.append("observe")
        monkeypatch.setattr(probe.time, "monotonic", lambda: 10000)
        return {"entry_states": {}, "positions": [], "tpsl_candidates": [], "coverage_gaps": [], "attached_protection_status": "unverified"}
    monkeypatch.setattr(probe, "observe_frame", observe)
    def cleanup(*a, **kw):
        calls.append("cleanup")
        return {"attempts": [], "entries_still_pending": leftover}
    monkeypatch.setattr(probe, "cleanup_entries", cleanup)
    result = probe.run_live(tmp_path)
    assert result == (0 if expected_status == "observed_protection_requires_review" else 1)
    summary_path = next(tmp_path.glob("live-*/live-summary.json"))
    summary = json.loads(summary_path.read_text())
    assert summary["status"] == expected_status
    assert calls == ["submit", "observe", "cleanup"]
