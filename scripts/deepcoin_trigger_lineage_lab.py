"""Prepare ETH trigger-order drafts and collect bounded, GET-only REST evidence.

This standalone tool has no order submission, amendment, cancellation or close API.
Private observation uses exported DEEPCOIN_API_KEY/API_SECRET/API_PASSPHRASE only.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


BASE = "https://api.deepcoin.com"
INST = "ETH-USDT-SWAP"
PUBLIC = {"/deepcoin/market/instruments", "/deepcoin/market/tickers"}
READS = PUBLIC | {
    "/deepcoin/trade/order", "/deepcoin/trade/orders-pending",
    "/deepcoin/trade/orders-history", "/deepcoin/trade/trigger-orders-pending",
    "/deepcoin/trade/trigger-orders-history", "/deepcoin/trade/fills",
    "/deepcoin/account/positions", "/deepcoin/account/positions-history",
}
QUERY_KEYS = {"instId", "instType", "ordId", "clOrdId", "posId", "mrgPosition", "limit", "before", "begin", "end"}


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("finite positive number required")
    return result


def decimal_text(value):
    return format(value, "f")


def build_manifest(spec, ticker, *, now_ms, run_id, variant):
    if spec.get("instId") != INST or ticker.get("instId") != INST or spec.get("state") != "live":
        raise ValueError("expected live ETH swap instrument")
    if not re.fullmatch(r"[A-Za-z0-9]{1,12}", run_id) or variant not in {"baseline", "client", "tag", "both"}:
        raise ValueError("invalid run id or variant")
    age = Decimal(str(now_ms)) - number(ticker["ts"])
    if not -2000 <= age <= 15000:
        raise ValueError("ticker stale or future dated")
    p = number(ticker["last"])
    step, minimum, tick, ctval = [number(spec[k]) for k in ("lotSz", "minSz", "tickSz", "ctVal")]
    size = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    requests = []
    for side, pos, sign, suffix in (("buy", "long", -1, "L"), ("sell", "short", 1, "S")):
        entry, stop = p + sign, p + sign * 11
        if min(entry, stop) <= 0 or entry % tick or stop % tick:
            raise ValueError("confirmed prices cannot be represented by tickSz")
        body = {
            "instId": INST, "productGroup": "Swap", "sz": decimal_text(size),
            "side": side, "posSide": pos, "price": decimal_text(entry),
            "triggerPrice": decimal_text(entry), "triggerPxType": "last",
            "orderType": "limit", "isCrossMargin": "1", "tdMode": "cross", "mrgPosition": "split",
            "slTriggerPx": decimal_text(stop), "slTriggerPxType": "last", "slOrdPx": "-1",
        }
        if variant in {"client", "both"}:
            body["clOrdId"] = "EL" + run_id + suffix
        if variant in {"tag", "both"}:
            body["tag"] = "ET" + run_id + suffix
        requests.append({"label": pos, "method": "POST", "path": "/deepcoin/trade/trigger-order", "body": body})
    return {
        "status": "DRAFT_NOT_SUBMITTED", "run_id": run_id, "variant": variant,
        "created_at": utc(), "reference_last": str(ticker["last"]), "ticker_ts": str(ticker["ts"]),
        "expires_at_ms": int(number(ticker["ts"])) + 15000,
        "instrument_spec": spec, "ticker": ticker,
        "quantity_contracts": decimal_text(size), "quantity_eth": decimal_text(size * ctval),
        "margin_mode_draft": "cross", "position_mode_draft": "split", "leverage": "account_setting_not_verified",
        "stop_basis": "requested_limit_price_not_actual_fill", "stop_trigger_type": "last",
        "unknown_fields_under_test": [k for k in ("clOrdId", "tag") if k in requests[0]["body"]],
        "requests": requests,
    }


def validate_read(path, params):
    if path not in READS or set(params) - QUERY_KEYS:
        raise ValueError("GET route or query not allowlisted")
    if params.get("instId", INST) != INST or params.get("instType", "SWAP") != "SWAP":
        raise ValueError("only ETH SWAP is in scope")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward signed headers to another endpoint or host.


def transport(path_query, private):
    headers = {"Accept": "application/json"}
    if private:
        names = ["DEEPCOIN_API_KEY", "DEEPCOIN_API_SECRET", "DEEPCOIN_API_PASSPHRASE"]
        if any(not os.environ.get(n) for n in names):
            raise ValueError("private observation requires exported credentials")
        timestamp = utc()
        signature = base64.b64encode(hmac.new(
            os.environ[names[1]].encode(), (timestamp + "GET" + path_query).encode(), hashlib.sha256,
        ).digest()).decode()
        headers.update({"DC-ACCESS-KEY": os.environ[names[0]], "DC-ACCESS-SIGN": signature,
                        "DC-ACCESS-TIMESTAMP": timestamp, "DC-ACCESS-PASSPHRASE": os.environ[names[2]]})
    request = urllib.request.Request(BASE + path_query, headers=headers, method="GET")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def append_json(path, value):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def read_evidence(path, params, log, send=transport, *, private=False):
    validate_read(path, params)
    if private != (path not in PUBLIC):
        raise ValueError("route authentication mismatch")
    request_path = path + ("?" + urllib.parse.urlencode(params) if params else "")
    record = {"method": "GET", "path": path, "params": params, "started_at": utc(), "status": "incomplete"}
    started = time.monotonic()
    try:
        status, raw = send(request_path, private)
        record.update(http_status=status, raw_body=raw)
        payload = json.loads(raw)
        record["payload"] = payload
        if status != 200 or not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise ValueError("HTTP or business query failure")
        data = payload.get("data")
        if isinstance(data, dict) and path == "/deepcoin/trade/order":
            data = [data] if data else []
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise ValueError("unexpected response schema")
        record["status"] = "response_received"  # This is not a completeness or lineage verdict.
        return data
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        raise
    finally:
        record.update(finished_at=utc(), elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        append_json(log, record)


def history_pages(getter, path, params, id_key, *, max_pages=3, window_start_ms=None, time_key=None):
    result, seen, cursor = [], set(), None
    for _ in range(max_pages):
        query = {**params, "limit": 100}
        if cursor is not None:
            query["before"] = cursor
        rows = getter(path, query)
        ids = [str(row.get(id_key, "")) for row in rows]
        result.extend(rows)
        if any(not x.isdigit() for x in ids) or len(ids) != len(set(ids)) or seen.intersection(ids):
            return result, False
        seen.update(ids)
        if len(rows) < 100:
            return result, True
        # Entire page predates the bounded observation window. Never infer this
        # from one old row or from an absent/invalid exchange timestamp.
        if window_start_ms is not None and time_key and all(
            str(row.get(time_key, "")).isdigit()
            and 1_000_000_000_000 <= int(row[time_key]) < window_start_ms for row in rows
        ):
            return result, True
        cursor = min(ids, key=int)
    return result, False


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def prepare(out, variant):
    rows = read_evidence("/deepcoin/market/instruments", {"instType": "SWAP"}, out / "public.jsonl")
    specs = [r for r in rows if r.get("instId") == INST]
    rows = read_evidence("/deepcoin/market/tickers", {"instType": "SWAP", "instId": INST}, out / "public.jsonl")
    tickers = [r for r in rows if r.get("instId") == INST]
    if len(specs) != 1 or len(tickers) != 1:
        raise ValueError("missing or duplicate ETH specification/quote")
    manifest = build_manifest(specs[0], tickers[0], now_ms=time.time_ns() // 1_000_000,
                              run_id=uuid.uuid4().hex[:12], variant=variant)
    dump(out / "manifest.json", manifest)
    dump(out / "ids.json", {"ordIds": [], "posIds": [], "clOrdIds": [r["body"]["clOrdId"] for r in manifest["requests"] if "clOrdId" in r["body"]]})
    dump(out / "submission-record-template.json", {
        "status": "NOT_SUBMITTED", "warning": "Fill from actual submitting client; never store signed headers or secrets.",
        "events": [{"label": r["label"], "local_sent_at_utc": None, "local_received_at_utc": None,
                    "request": r, "http_status": None, "response_raw_body": None} for r in manifest["requests"]],
    })
    return {k: manifest[k] for k in ("status", "reference_last", "ticker_ts", "quantity_contracts", "quantity_eth", "requests")}


def read_ids(path):
    value = json.loads(path.read_text(encoding="utf-8")) if path else {}
    result = {}
    for key in ("ordIds", "posIds", "clOrdIds"):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 100 or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9]{1,40}", x) for x in items):
            raise ValueError("invalid bounded ID list")
        result[key] = items
    return result


def observe(out, ids_path, seconds, interval):
    deadline = time.monotonic() + seconds
    window_start_ms = time.time_ns() // 1_000_000 - 60_000
    summary = {"started_at": utc(), "status": "in_progress", "frames": 0,
               "window_start_ms": window_start_ms,
               "limitations": ["REST polling is not a lossless event stream", "No parent-child attribution inferred", "Submission receipts must be captured by submitting client", "History scan ends at a full page older than observation start minus 60 seconds; not a full account history audit"]}
    base = {"instType": "SWAP", "instId": INST}
    discovered = set()

    def get(path, params):
        for attempt in range(2):
            if time.monotonic() >= deadline:
                raise TimeoutError("observation deadline")
            try:
                return read_evidence(path, params, out / "raw.jsonl", private=True)
            except Exception:
                if attempt:
                    raise
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            finally:
                time.sleep(min(0.75, max(0, deadline - time.monotonic())))

    try:
        while time.monotonic() < deadline:
            started = time.monotonic()
            ids = read_ids(ids_path)
            frame = {"frame": summary["frames"], "started_at": utc(), "incomplete": [], "counts": {}}
            try:
                for name in ("orders-pending", "trigger-orders-pending", "trigger-orders-history"):
                    rows = get("/deepcoin/trade/" + name, {**base, "limit": 100})
                    frame["counts"][name] = len(rows)
                    if len(rows) >= 100:
                        frame["incomplete"].append(name + ":limit_reached")
                    if name == "orders-pending":
                        discovered.update(str(r["ordId"]) for r in rows if r.get("ordId"))
                for name, key in (("orders-history", "ordId"), ("fills", "billId")):
                    time_key = "cTime" if name == "orders-history" else "ts"
                    params = base if name == "orders-history" else {**base, "begin": window_start_ms}
                    rows, complete = history_pages(get, "/deepcoin/trade/" + name, params, key,
                                                   window_start_ms=window_start_ms, time_key=time_key)
                    frame["counts"][name] = len(rows)
                    if not complete:
                        frame["incomplete"].append(name + ":history_not_exhausted")
                    for row in rows:
                        stamp = str(row.get(time_key, ""))
                        if not stamp.isdigit() or int(stamp) < 1_000_000_000_000:
                            frame["incomplete"].append(name + ":invalid_exchange_time")
                        elif int(stamp) >= window_start_ms and row.get("ordId"):
                            discovered.add(str(row["ordId"]))
                positions = get("/deepcoin/account/positions", base)
                frame["counts"]["positions"] = len(positions)
                # Parent IDs are separately queried against trigger history; GET order may legitimately be empty.
                for oid in ids["ordIds"]:
                    get("/deepcoin/trade/trigger-orders-history", {**base, "ordId": oid, "limit": 100})
                candidates = sorted(discovered | set(ids["ordIds"]))
                if len(candidates) > 100:
                    frame["incomplete"].append("detail_candidate_cap")
                    candidates = sorted(set(ids["ordIds"])) + sorted(discovered - set(ids["ordIds"]))[:max(0, 100-len(set(ids["ordIds"])))]
                for oid in candidates:
                    get("/deepcoin/trade/order", {"instId": INST, "ordId": oid})
                for oid in ids["ordIds"]:
                    _, complete = history_pages(get, "/deepcoin/trade/fills", {**base, "ordId": oid}, "billId")
                    if not complete:
                        frame["incomplete"].append("exact_fills:" + oid)
                for cid in ids["clOrdIds"]:
                    get("/deepcoin/trade/order", {"instId": INST, "clOrdId": cid})
                for pid in ids["posIds"]:
                    get("/deepcoin/account/positions-history", {**base, "posId": pid, "mrgPosition": "split", "limit": 100})
            except Exception as exc:
                frame["incomplete"].append(type(exc).__name__)
                raise
            finally:
                frame["finished_at"] = utc()
                append_json(out / "frames.jsonl", frame)
            summary["frames"] += 1
            if frame["incomplete"]:
                summary["status"] = "incomplete"
                break
            time.sleep(min(max(0, interval-(time.monotonic()-started)), max(0, deadline-time.monotonic())))
        else:
            summary["status"] = "window_ended_lifecycle_unverified"
    except Exception as exc:
        summary.update(status="incomplete", error_type=type(exc).__name__)
    finally:
        summary["finished_at"] = utc()
        dump(out / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "observe"])
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory; must not already exist")
    parser.add_argument("--variant", choices=["baseline", "client", "tag", "both"], default="both")
    parser.add_argument("--ids", type=Path, help="JSON ID list reloaded each observation frame")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800 or not 5 <= args.interval <= 60:
        parser.error("seconds must be 1..1800; interval must be 5..60")
    previous_umask = os.umask(0o077)
    try:
        args.output.mkdir(parents=True, exist_ok=False)
        try:
            result = prepare(args.output, args.variant) if args.command == "prepare" else observe(args.output, args.ids, args.seconds, args.interval)
        except Exception as exc:
            result = {"status": "incomplete", "error_type": type(exc).__name__}
            dump(args.output / "summary.json", result)
        print(json.dumps({"output": str(args.output.resolve()), **result}, ensure_ascii=False, indent=2))
        return 1 if result["status"] == "incomplete" else 0
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
