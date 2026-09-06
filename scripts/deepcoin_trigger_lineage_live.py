"""User-operated, one-shot ETH minimum-size experiment. No writes without --execute-eth-minimum-pair.

The assistant may test this module with fake transports, or copy it to a server,
but must not execute the live entry point. Existing production services are untouched.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import hmac
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import deepcoin_trigger_lineage_lab as lab


TRIGGER = "/deepcoin/trade/trigger-order"
CANCEL = "/deepcoin/trade/cancel-trigger-order"
MAX_ETH = Decimal("0.01")
MAX_NOTIONAL = Decimal("50")


def durable_json(path, value, *, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def validate_pair(manifest, *, now_ms):
    expected = lab.build_manifest(manifest["instrument_spec"], manifest["ticker"], now_ms=now_ms,
                                  run_id=manifest["run_id"], variant="both")
    if manifest["requests"] != expected["requests"]:
        raise ValueError("trade parameters differ from the confirmed experiment")
    qty = lab.number(expected["quantity_eth"])
    if qty > MAX_ETH:
        raise ValueError("exchange minimum exceeds 0.01 ETH; no size escalation")
    if any(qty * lab.number(r["body"]["price"]) > MAX_NOTIONAL for r in expected["requests"]):
        raise ValueError("per-side notional exceeds 50 USDT")


def post_transport(path, body, *, allowed_paths=None):
    allowed = {TRIGGER, CANCEL} if allowed_paths is None else set(allowed_paths)
    if path not in allowed or not allowed <= {TRIGGER, CANCEL, "/deepcoin/trade/order", "/deepcoin/trade/cancel-order"}:
        raise ValueError("write route not allowed")
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = lab.utc()
    signature = base64.b64encode(hmac.new(
        os.environ["DEEPCOIN_API_SECRET"].encode(),
        timestamp.encode() + b"POST" + path.encode() + encoded, hashlib.sha256,
    ).digest()).decode()
    headers = {
        "Content-Type": "application/json", "DC-ACCESS-KEY": os.environ["DEEPCOIN_API_KEY"],
        "DC-ACCESS-SIGN": signature, "DC-ACCESS-TIMESTAMP": timestamp,
        "DC-ACCESS-PASSPHRASE": os.environ["DEEPCOIN_API_PASSPHRASE"],
    }
    request = urllib.request.Request(lab.BASE + path, data=encoded, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(lab.NoRedirect).open(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def write_once(request, out, label, *, send=post_transport, allowed_paths=None):
    allowed = {TRIGGER, CANCEL} if allowed_paths is None else set(allowed_paths)
    if request.get("method") != "POST" or request.get("path") not in allowed or not allowed <= {TRIGGER, CANCEL, "/deepcoin/trade/order", "/deepcoin/trade/cancel-order"}:
        raise ValueError("write route not allowed")
    started = lab.utc()
    # Must reach durable storage BEFORE the request could reach the exchange.
    durable_json(out / (label + "-request.json"), {"started_at": started, "request": request}, exclusive=True)
    record = {"label": label, "started_at": started, "outcome": "unknown_exchange_outcome", "ordId": None}
    try:
        status, raw = send(request["path"], request["body"])
        record.update(http_status=status, raw_body=raw)
        payload = json.loads(raw)
        record["payload"] = payload
        if isinstance(payload, dict) and status == 200:
            code = str(payload.get("code", ""))
            data = payload.get("data")
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
            if code == "0" and isinstance(data, dict):
                subcode = str(data.get("sCode", ""))
                oid = str(data.get("ordId") or "")
                if subcode == "0" and oid.isdigit():
                    record.update(outcome="accepted", ordId=oid)
                elif subcode.isdigit() and subcode != "0":
                    record["outcome"] = "rejected"
            elif code.isdigit() and code != "0":
                record["outcome"] = "rejected"
    except Exception as exc:
        record["error_type"] = type(exc).__name__
    finally:
        record["finished_at"] = lab.utc()
        durable_json(out / (label + "-response.json"), record, exclusive=True)
    return record


def submit_pair(manifest, out, *, now_ms, send=post_transport):
    validate_pair(manifest, now_ms=now_ms)
    # Two independent requests, no atomicity claim and no retry/fallback variant.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write_once, request, out, "submit-" + request["label"], send=send)
                   for request in manifest["requests"]]
        return [future.result() for future in futures]


def cancel_targets(rows, owned_ids):
    ids = [str(row.get("ordId", "")) for row in rows]
    if len(ids) != len(set(ids)) or len(rows) >= 100:
        raise ValueError("pending trigger snapshot ambiguous or incomplete")
    return sorted({str(row["ordId"]) for row in rows
                   if row.get("instId") == lab.INST
                   and str(row.get("ordId")) in owned_ids
                   and str(row.get("triggerOrderType", "")).lower() == "conditional"})


def claim_once(root, run_id):
    durable_json(root / "LIVE-ATTEMPT.json", {"run_id": run_id, "claimed_at": lab.utc(),
                 "notice": "Do not remove or retry automatically; inspect original outcomes first."}, exclusive=True)


def worker_credentials():
    pid = subprocess.check_output(["systemctl", "show", "telegram-kol-worker.service", "--property=MainPID", "--value"], text=True).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise ValueError("worker is not running")
    with urllib.request.urlopen("http://127.0.0.1:8002/api/runtime/deployment-identity", timeout=10) as response:
        identity = json.load(response)
    if identity.get("loaded_artifact_verified") is not True or str(identity.get("pid")) != pid:
        raise ValueError("worker loaded identity is not verified")
    env = dict(item.split(b"=", 1) for item in Path("/proc/" + pid + "/environ").read_bytes().split(b"\0") if b"=" in item)
    for name in ("DEEPCOIN_API_KEY", "DEEPCOIN_API_SECRET", "DEEPCOIN_API_PASSPHRASE"):
        value = env.get(name.encode())
        if not value:
            raise ValueError("worker credential is absent")
        os.environ[name] = value.decode()
    env.clear()
    if subprocess.check_output(["systemctl", "show", "telegram-kol-worker.service", "--property=MainPID", "--value"], text=True).strip() != pid:
        raise ValueError("worker changed during credential loading")
    return {k: identity.get(k) for k in ("pid", "runtime_role", "release_commit", "loaded_artifact_verified", "observed_at")}


def read_get(out, path, params, *, deadline=None):
    for attempt in range(2):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("observation window ended")
        try:
            return lab.read_evidence(path, params, out / "raw.jsonl", private=True)
        except Exception:
            if attempt:
                raise
            time.sleep(1)
        finally:
            time.sleep(0.75)


def snapshot(out, *, parent_ids, baseline_ids, started_ms, deadline=None):
    base = {"instType": "SWAP", "instId": lab.INST}
    frame = {"started_at": lab.utc(), "parent_ids": sorted(parent_ids), "coverage_gaps": [], "new_regular_ids": []}
    get = lambda path, params: read_get(out, path, params, deadline=deadline)
    try:
        for name in ("trigger-orders-pending", "orders-pending"):
            rows = get("/deepcoin/trade/" + name, {**base, "limit": 100})
            if len(rows) >= 100:
                raise ValueError("active order page reached cap")
            frame[name] = rows
        for name, key, stamp in (("orders-history", "ordId", "cTime"), ("fills", "billId", "ts")):
            params = base if name == "orders-history" else {**base, "begin": started_ms - 60_000}
            rows, complete = lab.history_pages(get, "/deepcoin/trade/" + name, params, key,
                                              window_start_ms=started_ms-60_000, time_key=stamp)
            frame[name] = rows
            if not complete:
                raise ValueError("history pagination incomplete")
        frame["positions"] = get("/deepcoin/account/positions", base)
        for oid in parent_ids:
            get("/deepcoin/trade/trigger-orders-history", {**base, "ordId": oid, "limit": 100})
        candidates = {str(row["ordId"]) for row in frame["orders-pending"]
                      if row.get("ordId") and str(row["ordId"]) not in baseline_ids}
        for name, key in (("orders-history", "cTime"), ("fills", "ts")):
            for row in frame[name]:
                stamp = str(row.get(key, ""))
                if not stamp.isdigit() or int(stamp) < 1_000_000_000_000:
                    raise ValueError("missing or invalid exchange time")
                if int(stamp) >= started_ms - 60_000 and row.get("ordId") and str(row["ordId"]) not in baseline_ids:
                    candidates.add(str(row["ordId"]))
        if len(candidates) > 30:
            raise ValueError("new ordinary order candidate cap exceeded")
        frame["new_regular_ids"] = sorted(candidates)
        for oid in sorted(candidates | parent_ids):
            get("/deepcoin/trade/order", {"instId": lab.INST, "ordId": oid})
        # Capture a stop that was created AND triggered between polling frames.
        recent_triggers = get("/deepcoin/trade/trigger-orders-history", {**base, "limit": 100})
        frame["trigger-orders-history"] = recent_triggers
        if len(recent_triggers) >= 100:
            frame["coverage_gaps"].append("trigger_history_latest_100_only_no_supported_pagination")
        new_trigger_ids = set()
        for row in frame["trigger-orders-pending"] + recent_triggers:
            oid = str(row.get("ordId", ""))
            if oid and oid not in baseline_ids and oid not in parent_ids:
                new_trigger_ids.add(oid)
        if len(new_trigger_ids) > 30:
            raise ValueError("new trigger candidate cap exceeded")
        for oid in sorted(new_trigger_ids):
            get("/deepcoin/trade/trigger-orders-history", {**base, "ordId": oid, "limit": 100})
        frame["status"] = "response_received_lineage_unproven"
        return frame
    except Exception as exc:
        frame.update(status="incomplete", error_type=type(exc).__name__)
        raise
    finally:
        frame["finished_at"] = lab.utc()
        lab.append_json(out / "frames.jsonl", frame)


def cleanup_parents(out, parent_ids):
    base = {"instType": "SWAP", "instId": lab.INST}
    rows = read_get(out, "/deepcoin/trade/trigger-orders-pending", {**base, "limit": 100})
    results = []
    for oid in cancel_targets(rows, parent_ids):
        result = write_once({"method": "POST", "path": CANCEL, "body": {"instId": lab.INST, "ordId": oid}},
                            out, "cancel-parent-" + oid)
        results.append(result)
        time.sleep(1)
    # A successful cancel response alone is not terminal-state proof.
    after = read_get(out, "/deepcoin/trade/trigger-orders-pending", {**base, "limit": 100})
    if len(after) >= 100:
        raise ValueError("cleanup readback incomplete")
    return {"attempts": results, "parents_still_pending": sorted({str(r.get("ordId")) for r in after} & parent_ids)}


def cleanup_unresolved(result):
    return bool(result["parents_still_pending"] or any(r["outcome"] != "accepted" for r in result["attempts"]))


def run_live(root):
    if (root / "LIVE-ATTEMPT.json").exists():
        raise ValueError("This one-shot experiment was already attempted. Inspect its evidence; do not resubmit.")
    run_id = uuid.uuid4().hex[:12]
    out = root / ("live-" + run_id)
    out.mkdir(mode=0o700)
    summary = {"status": "preflight", "output": str(out), "run_id": run_id, "started_at": lab.utc(), "order_results": []}
    parent_ids = set()
    attempted = False
    baseline_ids = set()
    stop_requested = False

    def stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        summary["worker_identity"] = worker_credentials()
        base = {"instType": "SWAP", "instId": lab.INST}
        positions = read_get(out, "/deepcoin/account/positions", base)
        if any(Decimal(str(r.get("pos", "NaN"))) != 0 for r in positions):
            raise ValueError("Existing ETH position: experiment stopped before submission")
        # Capture existing IDs so observations remain candidates, never inferred ownership.
        for name in ("trigger-orders-pending", "orders-pending", "orders-history", "trigger-orders-history"):
            rows = read_get(out, "/deepcoin/trade/" + name, {**base, "limit": 100})
            if name.endswith("pending") and len(rows) >= 100:
                raise ValueError("baseline active orders incomplete")
            baseline_ids.update(str(r["ordId"]) for r in rows if r.get("ordId"))
        summary["baseline_ids"] = sorted(baseline_ids)
        lab.prepare(out, "both")  # Fresh public spec and quote immediately before submission.
        manifest = json.loads((out / "manifest.json").read_text())
        validate_pair(manifest, now_ms=time.time_ns() // 1_000_000)
        print(json.dumps({"event": "SUBMITTING_REAL_ETH_PAIR", "quantity_eth_each": manifest["quantity_eth"],
                          "reference_last": manifest["reference_last"], "requests": manifest["requests"], "evidence": str(out)}, ensure_ascii=False), flush=True)
        if stop_requested:
            raise ValueError("interrupted before submission")
        claim_once(root, run_id)
        attempted = True
        started_ms = time.time_ns() // 1_000_000
        results = submit_pair(manifest, out, now_ms=started_ms)
        summary["order_results"] = results
        parent_ids = {r["ordId"] for r in results if r["outcome"] == "accepted"}
        if parent_ids & baseline_ids:
            parent_ids -= baseline_ids
            raise ValueError("returned parent ID conflicts with pre-existing order")
        durable_json(out / "ids.json", {"ordIds": sorted(parent_ids), "posIds": [],
                     "clOrdIds": [r["body"]["clOrdId"] for r in manifest["requests"]]})
        print(json.dumps({"event": "SUBMIT_RESULTS", "results": [{k: r.get(k) for k in ("label", "outcome", "ordId")} for r in results]}), flush=True)
        partial = len(parent_ids) != 2
        deadline = time.monotonic() + (30 if partial else 300)
        last_counts = None
        while not stop_requested and time.monotonic() < deadline:
            try:
                frame = snapshot(out, parent_ids=parent_ids, baseline_ids=baseline_ids, started_ms=started_ms, deadline=deadline)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    summary["last_frame_cut_by_deadline"] = True
                    break
                raise
            for request in manifest["requests"]:
                if time.monotonic() < deadline:
                    read_get(out, "/deepcoin/trade/order", {"instId": lab.INST, "clOrdId": request["body"]["clOrdId"]}, deadline=deadline)
            counts = {"new_regular_ids": frame["new_regular_ids"], "position_count": len(frame["positions"])}
            if counts != last_counts:
                print(json.dumps({"event": "OBSERVATION_CHANGED", **counts}), flush=True)
                last_counts = counts
            if all(r["outcome"] == "rejected" for r in results):
                break
            time.sleep(min(5, max(0, deadline-time.monotonic())))
        summary["status"] = "partial_or_unknown_submission_manual_review" if partial else "observation_ended_manual_review_required"
    except Exception as exc:
        summary.update(status="incomplete_manual_review" if attempted else "stopped_before_submit", error_type=type(exc).__name__)
        # Only controlled, non-sensitive exception messages are emitted here.
        if isinstance(exc, ValueError):
            summary["reason"] = str(exc)
    finally:
        if attempted:
            # Recover durable accepted receipts even if aggregation/storage failed.
            for path in out.glob("submit-*-response.json"):
                try:
                    receipt = json.loads(path.read_text())
                    if receipt.get("outcome") == "accepted" and str(receipt.get("ordId", "")).isdigit() and receipt["ordId"] not in baseline_ids:
                        parent_ids.add(receipt["ordId"])
                except Exception:
                    summary["receipt_recovery_incomplete"] = True
            try:
                summary["cleanup"] = cleanup_parents(out, parent_ids)
                if cleanup_unresolved(summary["cleanup"]):
                    summary["status"] = "cleanup_unresolved_manual_review"
                base = {"instType": "SWAP", "instId": lab.INST}
                positions = read_get(out, "/deepcoin/account/positions", base)
                orders = read_get(out, "/deepcoin/trade/orders-pending", {**base, "limit": 100})
                summary["remaining_eth_positions"] = [{k: r.get(k) for k in ("posId", "posSide", "pos", "slTriggerPx")} for r in positions]
                summary["new_pending_regular_candidates"] = [r.get("ordId") for r in orders if str(r.get("ordId")) not in baseline_ids]
                if len(orders) >= 100:
                    summary["status"] = "cleanup_unresolved_manual_review"
                    summary["final_pending_snapshot_incomplete"] = True
                summary["notice"] = "请查看父单撤销结果及剩余 ID；子单、仓位和附带止损没有自动撤销或平仓，请在 Deepcoin 检查。"
            except Exception as exc:
                summary["status"] = "cleanup_unresolved_manual_review"
                summary["cleanup_error_type"] = type(exc).__name__
                summary["notice"] = "收尾证据不完整，请立即在 Deepcoin 检查本次挂单和仓位；不要重复运行下单命令。"
        summary["finished_at"] = lab.utc()
        durable_json(out / "live-summary.json", summary)
        terminal = {k: v for k, v in summary.items() if k not in {"order_results", "baseline_ids", "cleanup"}}
        terminal["submissions"] = [{k: r.get(k) for k in ("label", "outcome", "ordId")} for r in summary["order_results"]]
        if "cleanup" in summary:
            terminal["cleanup"] = {"parents_still_pending": summary["cleanup"]["parents_still_pending"],
                                   "cancel_outcomes": [{k: r.get(k) for k in ("label", "outcome", "ordId")} for r in summary["cleanup"]["attempts"]]}
        print(json.dumps(terminal, ensure_ascii=False, indent=2), flush=True)
    return 1 if summary["status"] != "observation_ended_manual_review_required" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-eth-minimum-pair", action="store_true", help="USER-OPERATED ONLY: two real ETH trigger entries, attached stops, 300s observation, exact parent cancellation")
    args = parser.parse_args()
    if not args.execute_eth_minimum_pair:
        print("No orders submitted. Live execution requires --execute-eth-minimum-pair.")
        return 0
    previous_umask = os.umask(0o077)
    try:
        return run_live(Path(__file__).resolve().parent)
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
