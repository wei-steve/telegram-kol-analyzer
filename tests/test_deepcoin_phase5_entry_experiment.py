"""Offline guards for the phase 5 controlled live-entry experiment harness.

Nothing here touches the network. The point is that the harness cannot silently
grow a retry, a wider write route, an oversized order, or a "close the position
for you" convenience.
"""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deepcoin_phase5_entry_experiment.py"

SPEC = {
    "instId": "ETH-USDT-SWAP",
    "state": "live",
    "ctVal": "0.1",
    "lotSz": "0.1",
    "minSz": "0.1",
    "tickSz": "0.01",
}
TICKER = {"instId": "ETH-USDT-SWAP", "last": "2492.45", "bidPx": "2492.43", "askPx": "2492.46"}
MARKET = {"spec": SPEC, "ticker": TICKER}


@pytest.fixture
def harness():
    assert SCRIPT.exists(), "phase 5 experiment harness is not implemented"
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location("phase5_entry_experiment", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPT.parent))


def test_every_planned_cell_builds_a_manifest(harness):
    for cell in harness.CELLS:
        manifest = harness.build_manifest(cell, MARKET, run_id="run0000000")
        assert manifest["cell"] == cell
        assert len(manifest["requests"]) == harness.CELLS[cell]["orders"]
        for request in manifest["requests"]:
            assert request["path"] == harness.ORDER_PATH
            assert request["method"] == "POST"


def test_limit_payload_field_set_is_exactly_the_documented_creation_contract(harness):
    body = harness.build_manifest("6a", MARKET, run_id="run0000000")["requests"][0]["body"]
    assert set(body) == {
        "instId", "tdMode", "mrgPosition", "side", "posSide",
        "ordType", "px", "sz", "tpTriggerPx", "slTriggerPx",
    }
    assert body["ordType"] == "limit"
    # The trigger-order vocabulary must never leak into the ordinary order.
    for forbidden in ("triggerPrice", "triggerPxType", "orderType", "price",
                      "isCrossMargin", "productGroup", "slOrdPx", "tpOrdPx"):
        assert forbidden not in body


def test_only_the_clordid_cells_carry_a_client_order_id(harness):
    for cell, plan in harness.CELLS.items():
        bodies = [
            request["body"]
            for request in harness.build_manifest(cell, MARKET, run_id="run0000000")["requests"]
        ]
        if plan["client_order_id"]:
            ids = [body["clOrdId"] for body in bodies]
            assert all(ids) and len(set(ids)) == len(ids)
            assert all(len(value) <= 20 and value.isalnum() for value in ids)
        else:
            assert all("clOrdId" not in body for body in bodies)


def test_concurrent_cells_submit_identical_economics(harness):
    for cell in ("6c", "6d"):
        bodies = [
            request["body"]
            for request in harness.build_manifest(cell, MARKET, run_id="run0000000")["requests"]
        ]
        assert len(bodies) == 2
        for field in ("side", "posSide", "px", "sz", "tpTriggerPx", "slTriggerPx"):
            assert bodies[0][field] == bodies[1][field]


def test_each_far_cell_uses_its_own_price(harness):
    prices = {}
    for cell, plan in harness.CELLS.items():
        if plan["kind"] != "far":
            continue
        prices[cell] = harness.build_manifest(cell, MARKET, run_id="run0000000")["requests"][0]["body"]["px"]
    assert len(set(prices.values())) == len(prices)


def test_far_cells_cannot_fill_and_protection_brackets_the_limit_price(harness):
    last = float(TICKER["last"])
    for cell, plan in harness.CELLS.items():
        if plan["kind"] != "far":
            continue
        body = harness.build_manifest(cell, MARKET, run_id="run0000000")["requests"][0]["body"]
        price = float(body["px"])
        assert abs(price - last) / last >= float(harness.FAR_PRICE_MIN_DISTANCE)
        assert float(body["slTriggerPx"]) < price < float(body["tpTriggerPx"])


def test_short_cell_protection_is_inverted(harness):
    body = harness.build_manifest("2", MARKET, run_id="run0000000")["requests"][0]["body"]
    price = float(body["px"])
    assert (body["side"], body["posSide"]) == ("sell", "short")
    assert float(body["tpTriggerPx"]) < price < float(body["slTriggerPx"])


def test_front_of_book_cell_rests_inside_the_spread(harness):
    body = harness.build_manifest("2", MARKET, run_id="run0000000")["requests"][0]["body"]
    assert float(TICKER["bidPx"]) < float(body["px"]) < float(TICKER["askPx"])


def test_quantity_cap_is_enforced(harness):
    harness.CELLS["6a"] = dict(harness.CELLS["6a"], contracts="0.4")
    with pytest.raises(ValueError):
        harness.build_manifest("6a", MARKET, run_id="run0000000")


def test_notional_cap_is_enforced(harness):
    expensive = {"spec": SPEC, "ticker": dict(TICKER, last="9000", bidPx="8999.99", askPx="9000.01")}
    harness.CELLS["6a"] = dict(harness.CELLS["6a"], contracts="0.2")
    with pytest.raises(ValueError):
        harness.build_manifest("6a", expensive, run_id="run0000000")


def test_a_price_that_is_not_far_enough_is_refused(harness):
    harness.CELLS["6a"] = dict(harness.CELLS["6a"], px_factor="0.99")
    with pytest.raises(ValueError):
        harness.build_manifest("6a", MARKET, run_id="run0000000")


def test_write_route_allowlist_rejects_anything_but_order_and_cancel(harness, tmp_path):
    with pytest.raises(ValueError):
        harness.post_transport("/deepcoin/trade/trigger-order", {}, timeout=1.0)
    with pytest.raises(ValueError):
        harness.write_once(
            {"method": "POST", "path": "/deepcoin/trade/set-position-sltp", "body": {}},
            tmp_path, "bad", timeout=1.0,
        )
    with pytest.raises(ValueError):
        harness.write_once(
            {"method": "GET", "path": harness.ORDER_PATH, "body": {}}, tmp_path, "bad2", timeout=1.0,
        )


def test_write_once_persists_the_request_before_sending(harness, tmp_path, monkeypatch):
    seen = {}

    def fake_post(path, body, *, timeout):
        seen["request_file_exists"] = (tmp_path / "probe-request.json").exists()
        return 200, json.dumps({"code": "0", "data": [{"sCode": "0", "ordId": "123"}]})

    monkeypatch.setattr(harness, "post_transport", fake_post)
    record = harness.write_once(
        {"method": "POST", "path": harness.ORDER_PATH, "body": {"instId": "ETH-USDT-SWAP"}},
        tmp_path, "probe", timeout=15.0,
    )
    assert seen["request_file_exists"] is True
    assert record["outcome"] == "accepted" and record["ordId"] == "123"


def test_outer_code_zero_with_scode_14_is_a_rejection_not_a_success(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(
        harness, "post_transport",
        lambda path, body, *, timeout: (
            200, json.dumps({"code": "0", "data": [{"sCode": "14", "sMsg": "DuplicateAction", "ordId": ""}]})
        ),
    )
    record = harness.write_once(
        {"method": "POST", "path": harness.ORDER_PATH, "body": {}}, tmp_path, "dup", timeout=15.0
    )
    assert record["outcome"] == "rejected"
    assert record["sCode"] == "14" and record["sMsg"] == "DuplicateAction"
    assert record["ordId"] is None


def test_transport_failure_is_unknown_and_is_sent_exactly_once(harness, tmp_path, monkeypatch):
    calls = []

    def failing(path, body, *, timeout):
        calls.append(timeout)
        raise TimeoutError("response never arrived")

    monkeypatch.setattr(harness, "post_transport", failing)
    record = harness.write_once(
        {"method": "POST", "path": harness.ORDER_PATH, "body": {}}, tmp_path, "lost", timeout=0.05
    )
    assert record["outcome"] == "unknown_exchange_outcome"
    assert record["error_type"] == "TimeoutError"
    assert calls == [0.05], "an unknown outcome must never be resent"


def test_a_malformed_body_is_never_upgraded_to_success(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(
        harness, "post_transport",
        lambda path, body, *, timeout: (200, "not json at all"),
    )
    record = harness.write_once(
        {"method": "POST", "path": harness.ORDER_PATH, "body": {}}, tmp_path, "junk", timeout=15.0
    )
    assert record["outcome"] == "unknown_exchange_outcome"


def test_recovery_candidates_exclude_baseline_and_require_every_field(harness):
    body = {"instId": "ETH-USDT-SWAP", "side": "buy", "posSide": "long", "px": "2143.51", "sz": "0.1"}
    mine = {"ordId": "900", "instId": "ETH-USDT-SWAP", "side": "buy", "posSide": "long",
            "px": "2143.51", "sz": "0.1"}
    older = dict(mine, ordId="800")
    other_price = dict(mine, ordId="901", px="2143.52")
    other_side = dict(mine, ordId="902", side="sell")
    rows = [mine, older, other_price, other_side]
    matches = harness.recovery_candidates(rows, body=body, baseline={"800"})
    assert [row["ordId"] for row in matches] == ["900"]


def test_cell_eleven_forces_a_timeout_short_enough_to_lose_the_response(harness):
    assert harness.CELLS["11"]["force_timeout_seconds"] < 1.0


def test_harness_never_closes_a_position_and_writes_nowhere_else(harness):
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("reduceOnly", "close-position", "closePosition",
                      "/deepcoin/trade/set-position-sltp",
                      "/deepcoin/trade/cancel-position-sltp",
                      "/deepcoin/trade/modify-position-sltp",
                      "/deepcoin/trade/replace-order-sltp",
                      "/deepcoin/trade/trigger-order\"",
                      "/deepcoin/trade/cancel-trigger-order"):
        assert forbidden not in source, f"{forbidden} must not appear in the experiment harness"
    assert harness.WRITE_PATHS == frozenset({harness.ORDER_PATH, harness.CANCEL_PATH})


def test_cancel_exact_requires_a_numeric_id_and_never_scans(harness, tmp_path):
    with pytest.raises(ValueError):
        harness.cancel_exact(tmp_path, "not-an-id", execute=False)
    source = SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("def cancel_exact("):source.index("def main()")]
    # The target may only come from the operator, never from a listing.
    # (trigger-orders-pending is read for evidence, not to pick a target.)
    assert '"/deepcoin/trade/orders-pending"' not in body
    assert "orders-history" not in body
    assert "recovery_candidates" not in body


def test_cancel_exact_refuses_anything_that_is_not_a_live_unfilled_order(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "load_worker_credentials", lambda: {"worker_pid": "1"})
    calls = []
    monkeypatch.setattr(harness, "write_once",
                        lambda *a, **k: calls.append(a) or {"outcome": "accepted"})

    def reader(rows):
        return lambda path, params=None, **kw: rows if path == "/deepcoin/trade/order" else []

    monkeypatch.setattr(harness, "signed_get", reader([{"ordId": "1", "state": "filled", "accFillSz": "0.1"}]))
    assert harness.cancel_exact(tmp_path / "a", "1", execute=True) == 1
    monkeypatch.setattr(harness, "signed_get", reader([{"ordId": "1", "state": "live", "accFillSz": "0.1"}]))
    assert harness.cancel_exact(tmp_path / "b", "1", execute=True) == 1
    monkeypatch.setattr(harness, "signed_get", reader([
        {"ordId": "1", "state": "live", "accFillSz": "0"},
        {"ordId": "1", "state": "live", "accFillSz": "0"},
    ]))
    assert harness.cancel_exact(tmp_path / "c", "1", execute=True) == 1
    assert calls == [], "no cancel may be sent for a non-live, partly filled or ambiguous read"


def test_a_dry_run_cancel_does_not_block_the_real_one(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "load_worker_credentials", lambda: {"worker_pid": "1"})
    monkeypatch.setattr(
        harness, "signed_get",
        lambda path, params=None, **kw: (
            [{"ordId": "1", "state": "live", "accFillSz": "0"}]
            if path == "/deepcoin/trade/order" else []
        ),
    )
    assert harness.cancel_exact(tmp_path, "1", execute=False) == 0
    # The second call must not collide with the first call's evidence directory.
    assert harness.cancel_exact(tmp_path, "1", execute=False) == 0
    assert len(list(tmp_path.glob("manual-cancel-1-*"))) == 2


def test_an_empty_read_back_after_cancel_is_unresolved_not_success(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "load_worker_credentials", lambda: {"worker_pid": "1"})
    monkeypatch.setattr(harness, "write_once",
                        lambda *a, **k: {"outcome": "accepted", "ordId": "1"})
    monkeypatch.setattr(harness.time, "sleep", lambda *a: None)
    reads = iter([[{"ordId": "1", "state": "live", "accFillSz": "0"}], [], [], []])
    monkeypatch.setattr(
        harness, "signed_get",
        lambda path, params=None, **kw: next(reads) if path == "/deepcoin/trade/order" else [],
    )
    # The exact-id read is transiently empty right after a state change.
    assert harness.cancel_exact(tmp_path, "1", execute=True) == 1


def test_a_confirmed_canceled_read_back_is_success(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "load_worker_credentials", lambda: {"worker_pid": "1"})
    monkeypatch.setattr(harness, "write_once",
                        lambda *a, **k: {"outcome": "accepted", "ordId": "1"})
    monkeypatch.setattr(harness.time, "sleep", lambda *a: None)
    reads = iter([
        [{"ordId": "1", "state": "live", "accFillSz": "0"}],
        [{"ordId": "1", "state": "canceled", "accFillSz": "0"}],
    ])
    monkeypatch.setattr(
        harness, "signed_get",
        lambda path, params=None, **kw: next(reads) if path == "/deepcoin/trade/order" else [],
    )
    assert harness.cancel_exact(tmp_path, "1", execute=True) == 0
