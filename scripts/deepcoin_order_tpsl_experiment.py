"""User-run ETH ordinary limit orders with attached TP/SL. Default: no trading.

Only --execute-order-tpsl-pair enables real order submission. No position protection
replacement, position close, bulk cancellation, or production routing changes.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import time
import uuid

import deepcoin_trigger_lineage_live as core

lab = core.lab
ORDER = "/deepcoin/trade/order"
CANCEL = "/deepcoin/trade/cancel-order"
WRITE_PATHS = frozenset({ORDER, CANCEL})


def build_manifest(spec, ticker, *, now_ms, run_id, selection="pair"):
    if selection not in {"pair", "short", "short_no_clordid"}:
        raise ValueError("unsupported order selection")
    source = lab.build_manifest(spec, ticker, now_ms=now_ms, run_id=run_id, variant="client")
    requests = []
    for request in source["requests"]:
        old = request["body"]
        price = lab.number(old["price"])
        tp = price + (10 if old["posSide"] == "long" else -10)
        tick = lab.number(spec["tickSz"])
        if tp <= 0 or tp % tick:
            raise ValueError("take profit price incompatible with tick")
        body = {
            "instId": lab.INST, "tdMode": "cross", "mrgPosition": "split",
            "side": old["side"], "posSide": old["posSide"], "ordType": "limit",
            "px": old["price"], "sz": old["sz"],
            "clOrdId": "EO" + run_id + ("L" if old["posSide"] == "long" else "S"),
            "tpTriggerPx": lab.decimal_text(tp), "slTriggerPx": old["slTriggerPx"],
        }
        requests.append({"label": request["label"], "method": "POST", "path": ORDER, "body": body})
    if selection in {"short", "short_no_clordid"}:
        requests = [request for request in requests if request["label"] == "short"]
    if selection == "short_no_clordid":
        requests[0]["body"].pop("clOrdId")
    return {**source, "variant": "ordinary_order_attached_tpsl", "requests": requests,
            "selection": selection,
            "unknown_fields_under_test": [], "stop_trigger_type": "not_specified_in_order_creation_contract",
            "attached_execution_type": "requires_exchange_readback", "take_profit_distance": "10",
            "stop_distance": "10", "stop_basis": "requested_limit_price_not_actual_fill"}


def validate_pair(manifest, *, now_ms):
    expected = build_manifest(manifest["instrument_spec"], manifest["ticker"], now_ms=now_ms,
                              run_id=manifest["run_id"], selection=manifest.get("selection", "pair"))
    if manifest["requests"] != expected["requests"]:
        raise ValueError("ordinary order requests differ from confirmed plan")
    quantity = lab.number(expected["quantity_eth"])
    if quantity > Decimal("0.01"):
        raise ValueError("minimum quantity exceeds 0.01 ETH; stopped without increasing size")
    if any(quantity * lab.number(r["body"]["px"]) > Decimal("50") for r in expected["requests"]):
        raise ValueError("per-side notional exceeds 50 USDT")


def post(path, body):
    return core.post_transport(path, body, allowed_paths=WRITE_PATHS)


def submit_pair(manifest, out, *, now_ms, send=post):
    validate_pair(manifest, now_ms=now_ms)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(core.write_once, request, out, "submit-" + request["label"],
                               send=send, allowed_paths=WRITE_PATHS) for request in manifest["requests"]]
        return [f.result() for f in futures]


def prepare(out, run_id, selection="pair"):
    specs = lab.read_evidence("/deepcoin/market/instruments", {"instType": "SWAP"}, out / "public.jsonl")
    tickers = lab.read_evidence("/deepcoin/market/tickers", {"instType": "SWAP", "instId": lab.INST}, out / "public.jsonl")
    specs = [x for x in specs if x.get("instId") == lab.INST]
    tickers = [x for x in tickers if x.get("instId") == lab.INST]
    if len(specs) != 1 or len(tickers) != 1:
        raise ValueError("missing or duplicate instrument/quote")
    manifest = build_manifest(specs[0], tickers[0], now_ms=time.time_ns() // 1_000_000,
                              run_id=run_id, selection=selection)
    validate_pair(manifest, now_ms=time.time_ns() // 1_000_000)
    core.durable_json(out / "manifest.json", manifest)
    return manifest


def validate_pending(rows):
    ids = [str(r.get("ordId", "")) for r in rows]
    if (len(rows) >= 100 or len(ids) != len(set(ids)) or any(not x.isdigit() for x in ids)
            or any(r.get("instId") != lab.INST for r in rows)):
        raise ValueError("ordinary pending snapshot incomplete")
    return set(ids)


def cleanup_entries(out, owned_ids, *, send=post):
    params = {"instType": "SWAP", "instId": lab.INST, "limit": 100}
    rows = core.read_get(out, "/deepcoin/trade/orders-pending", params)
    validate_pending(rows)
    targets = {str(r["ordId"]) for r in rows if r.get("instId") == lab.INST} & owned_ids
    results = []
    for oid in sorted(targets):
        result = core.write_once({"method": "POST", "path": CANCEL, "body": {"instId": lab.INST, "ordId": oid}},
                                 out, "cancel-entry-" + oid, send=send, allowed_paths=WRITE_PATHS)
        results.append(result)
        time.sleep(1)
    after = core.read_get(out, "/deepcoin/trade/orders-pending", params)
    after_ids = validate_pending(after)
    return {"attempts": results, "entries_still_pending": sorted(after_ids & owned_ids)}


def observe_frame(out, owned_ids, clients, baseline_ids, started_ms, deadline):
    frame = core.snapshot(out, parent_ids=set(), baseline_ids=baseline_ids, started_ms=started_ms, deadline=deadline)
    details = {}
    for oid in sorted(owned_ids):
        details[oid] = core.read_get(out, ORDER, {"instId": lab.INST, "ordId": oid}, deadline=deadline)
    by_client = {}
    for cid in clients:
        by_client[cid] = core.read_get(out, ORDER, {"instId": lab.INST, "clOrdId": cid}, deadline=deadline)
    # Price matches are candidates, not identity proof; preserve every TPSL field.
    protection_candidates = [r for r in frame["trigger-orders-pending"]
                             if str(r.get("ordId", "")) not in baseline_ids and r.get("triggerOrderType") == "TPSL"]
    entry_states = {oid: [{k: r.get(k) for k in ("ordId", "clOrdId", "state", "accFillSz", "tpTriggerPx", "slTriggerPx")} for r in rows]
                    for oid, rows in details.items()}
    event = {"at": lab.utc(), "entry_states": entry_states, "by_client": by_client,
             "tpsl_candidates": protection_candidates, "positions": frame["positions"],
             "attached_protection_status": "unverified_until_raw_fields_and_identity_reviewed",
             "coverage_gaps": frame["coverage_gaps"]}
    lab.append_json(out / "order-tpsl-frames.jsonl", event)
    return event


def run_live(root, selection="pair"):
    if selection not in {"pair", "short", "short_no_clordid"}:
        raise ValueError("unsupported live selection")
    if (root / "LIVE-ATTEMPT.json").exists():
        raise ValueError("This one-shot test has already been attempted. Do not repeat; inspect its evidence.")
    run_id = uuid.uuid4().hex[:12]
    out = root / ("live-" + run_id)
    out.mkdir(mode=0o700)
    summary = {"run_id": run_id, "output": str(out), "started_at": lab.utc(), "status": "preflight", "submissions": []}
    owned, baseline = set(), set()
    attempted = False
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        summary["worker_identity"] = core.worker_credentials()
        base = {"instType": "SWAP", "instId": lab.INST}
        positions = core.read_get(out, "/deepcoin/account/positions", base)
        if any(Decimal(str(r.get("pos", "NaN"))) != 0 for r in positions):
            raise ValueError("Existing ETH position; no new test order submitted")
        for name in ("orders-pending", "orders-history", "trigger-orders-pending", "trigger-orders-history"):
            rows = core.read_get(out, "/deepcoin/trade/" + name, {**base, "limit": 100})
            if name.endswith("pending") and len(rows) >= 100:
                raise ValueError("baseline active orders incomplete")
            baseline.update(str(r["ordId"]) for r in rows if r.get("ordId"))
        summary["baseline_ids"] = sorted(baseline)
        manifest = prepare(out, run_id, selection)
        clients = [r["body"]["clOrdId"] for r in manifest["requests"] if r["body"].get("clOrdId")]
        print(json.dumps({"event": "REAL_ORDINARY_LIMIT_" + selection.upper(), "quantity_eth_each": manifest["quantity_eth"],
                          "requests": manifest["requests"], "evidence": str(out),
                          "notice": "附带TP/SL是否正确生成及其执行方式正在实测；成交后不会自动平仓。"}, ensure_ascii=False), flush=True)
        if stopping:
            raise ValueError("interrupted before submission")
        core.claim_once(root, run_id)
        attempted = True
        started_ms = time.time_ns() // 1_000_000
        results = submit_pair(manifest, out, now_ms=started_ms)
        summary["submissions"] = results
        accepted = [r["ordId"] for r in results if r["outcome"] == "accepted"]
        owned = set(accepted) - baseline
        if set(accepted) & baseline or len(accepted) != len(set(accepted)):
            raise ValueError("accepted order identity conflict")
        core.durable_json(out / "ids.json", {"ordIds": sorted(owned), "clOrdIds": clients, "posIds": []})
        print(json.dumps({"event": "SUBMIT_RESULTS", "results": [{k: r.get(k) for k in ("label", "outcome", "ordId")} for r in results]}), flush=True)
        partial = len(owned) != len(manifest["requests"])
        deadline = time.monotonic() + (30 if partial else 300)
        last_state = None
        gaps = set()
        while not stopping and time.monotonic() < deadline:
            try:
                frame = observe_frame(out, owned, clients, baseline, started_ms, deadline)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    summary["last_frame_cut_by_deadline"] = True
                    break
                raise
            gaps.update(frame["coverage_gaps"])
            state = {"entry_states": frame["entry_states"], "position_count": len(frame["positions"]),
                     "new_tpsl_candidate_count": len(frame["tpsl_candidates"]),
                     "protection": frame["attached_protection_status"]}
            if state != last_state:
                print(json.dumps({"event": "OBSERVATION_CHANGED", **state}, ensure_ascii=False), flush=True)
                last_state = state
            if all(r["outcome"] == "rejected" for r in results):
                break
            time.sleep(min(5, max(0, deadline-time.monotonic())))
        summary["coverage_gaps"] = sorted(gaps)
        summary["status"] = "partial_or_unknown_submission" if partial else "observed_protection_requires_review"
    except Exception as exc:
        summary.update(status="incomplete_manual_review" if attempted else "stopped_before_submit", error_type=type(exc).__name__)
        if isinstance(exc, ValueError):
            summary["reason"] = str(exc)
    finally:
        if attempted:
            for path in out.glob("submit-*-response.json"):
                try:
                    receipt = json.loads(path.read_text())
                    oid = str(receipt.get("ordId", ""))
                    if receipt.get("outcome") == "accepted" and oid.isdigit() and oid not in baseline:
                        owned.add(oid)
                except Exception:
                    summary["receipt_recovery_incomplete"] = True
            try:
                cleanup = cleanup_entries(out, owned)
                summary["cleanup"] = cleanup
                if cleanup["entries_still_pending"] or any(r["outcome"] != "accepted" for r in cleanup["attempts"]):
                    summary["status"] = "cleanup_unresolved"
                base = {"instType": "SWAP", "instId": lab.INST}
                positions = core.read_get(out, "/deepcoin/account/positions", base)
                summary["remaining_eth_positions"] = [{k: r.get(k) for k in ("posId", "posSide", "pos", "avgPx", "tpTriggerPx", "slTriggerPx")} for r in positions]
                # Preserve protection persistence after cancellation of an unfilled or partial order.
                tpsl_rows = core.read_get(out, "/deepcoin/trade/trigger-orders-pending", {**base, "limit": 100})
                if len(tpsl_rows) >= 100:
                    summary.setdefault("coverage_gaps", []).append("final_protection_snapshot_capped")
                    if summary["status"] == "observed_protection_requires_review":
                        summary["status"] = "protection_snapshot_incomplete"
                summary["notice"] = "仅处理本次普通入场挂单的未成交余量；已成交仓位及TP/SL未自动平仓或撤销，请在Deepcoin核对处理。"
            except Exception as exc:
                summary.update(status="cleanup_unresolved", cleanup_error_type=type(exc).__name__)
                summary["notice"] = "收尾未能确认；请立即检查Deepcoin挂单、仓位和保护，不要重复下单命令。"
        summary["finished_at"] = lab.utc()
        core.durable_json(out / "live-summary.json", summary)
        terminal = {k: v for k, v in summary.items() if k not in {"baseline_ids", "submissions", "cleanup"}}
        terminal["submissions"] = [{k: r.get(k) for k in ("label", "outcome", "ordId")} for r in summary["submissions"]]
        if "cleanup" in summary:
            terminal["cleanup"] = {"entries_still_pending": summary["cleanup"]["entries_still_pending"],
                                   "cancel_results": [{k: r.get(k) for k in ("label", "outcome", "ordId")} for r in summary["cleanup"]["attempts"]]}
        print(json.dumps(terminal, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] == "observed_protection_requires_review" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--execute-order-tpsl-pair", action="store_true")
    group.add_argument("--execute-order-tpsl-short", action="store_true")
    group.add_argument("--execute-order-tpsl-short-no-clordid", action="store_true")
    args = parser.parse_args()
    if not any((args.execute_order_tpsl_pair, args.execute_order_tpsl_short,
                args.execute_order_tpsl_short_no_clordid)):
        print("No orders submitted. Only a user's explicit execute flag starts real trading.")
        return 0
    previous_umask = os.umask(0o077)
    try:
        selection = ("short_no_clordid" if args.execute_order_tpsl_short_no_clordid
                     else "short" if args.execute_order_tpsl_short else "pair")
        return run_live(Path(__file__).resolve().parent, selection=selection)
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
