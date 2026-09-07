"""Phase 5 controlled live experiment for the ordinary-``order`` limit entry.

Answers one question only: **which field combination of a limit
``POST /deepcoin/trade/order`` is usable on this account.**  Every cell is a
separate one-shot run with its own evidence directory, its own lock, no retry
of any kind, and a private WebSocket subscription established *before* the
write so the ``TriggerOrder.TU`` transition cannot be missed.

Nothing is submitted unless an explicit ``--execute`` flag is given.  The
default prints the exact payload that would be sent and exits.

Cells (see docs/plans/2026-09-06-deepcoin-rest-ws/phase-5-order-entry-cutover.md):

======  ==========================================================
cell    what it isolates
======  ==========================================================
3       unfilled cancel: does cancelling the entry remove the TPSL
6a      single order, no clOrdId          (known accepted 2026-09-05)
6b      single order, with clOrdId        (known DuplicateAction 2026-09-05)
6c      two concurrent identical, no clOrdId          (never tested)
6d      two concurrent identical, distinct clOrdIds   (never tested at
        same side/size/price)
11      lost REST response: forced client timeout, no resend, then a
        time-window recovery read
1       limit long that intends to fill
2       passive order that may be taken in more than one trade
======  ==========================================================

Cells 3, 6a-6d and 11 use prices far below the market so nothing can fill;
each cancels exactly the ordIds this run received.  Cells 1 and 2 intend to
fill and never auto-close a filled position.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_DOWN, Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "https://api.deepcoin.com"
INST = "ETH-USDT-SWAP"
ORDER_PATH = "/deepcoin/trade/order"
CANCEL_PATH = "/deepcoin/trade/cancel-order"
LISTENKEY_PATH = "/deepcoin/listenkey/acquire"
WS_URL = "wss://stream.deepcoin.com/v1/private"
WS_TABLES = ("Order", "Trade", "Position", "TriggerOrder")
WRITE_PATHS = frozenset({ORDER_PATH, CANCEL_PATH})
WORKER_HEALTH = "http://127.0.0.1:8002"

MAX_CONTRACTS = Decimal("0.2")
MAX_NOTIONAL_USDT = Decimal("60")
FAR_PRICE_MIN_DISTANCE = Decimal("0.05")  # a "far" cell must sit >=5% from last

# Each far cell gets its own price so a price-scoped exchange dedupe key cannot
# make one cell look like a duplicate of an earlier cell.
CELLS: dict[str, dict] = {
    "3": {"kind": "far", "orders": 1, "client_order_id": False, "px_factor": "0.91",
          "contracts": "0.1", "side": "buy", "pos_side": "long", "observe_seconds": 45,
          "question": "does cancelling an unfilled entry also remove its attached TPSL"},
    "6a": {"kind": "far", "orders": 1, "client_order_id": False, "px_factor": "0.90",
           "contracts": "0.1", "side": "buy", "pos_side": "long", "observe_seconds": 20,
           "question": "single limit order without clOrdId"},
    "6b": {"kind": "far", "orders": 1, "client_order_id": True, "px_factor": "0.89",
           "contracts": "0.1", "side": "buy", "pos_side": "long", "observe_seconds": 20,
           "question": "single limit order with clOrdId"},
    "6c": {"kind": "far", "orders": 2, "client_order_id": False, "px_factor": "0.88",
           "contracts": "0.1", "side": "buy", "pos_side": "long", "observe_seconds": 20,
           "question": "two concurrent identical limit orders without clOrdId"},
    "6d": {"kind": "far", "orders": 2, "client_order_id": True, "px_factor": "0.87",
           "contracts": "0.1", "side": "buy", "pos_side": "long", "observe_seconds": 20,
           "question": "two concurrent identical limit orders with distinct clOrdIds"},
    "11": {"kind": "far", "orders": 1, "client_order_id": False, "px_factor": "0.86",
           "contracts": "0.1", "side": "buy", "pos_side": "long", "observe_seconds": 20,
           "force_timeout_seconds": 0.05,
           "question": "REST response lost while the exchange may have accepted"},
    "1": {"kind": "passive_fill", "orders": 1, "client_order_id": False,
          "px_offset": "-1", "contracts": "0.1", "side": "buy", "pos_side": "long",
          "observe_seconds": 300,
          "question": "long limit entry, symmetric to the accepted 2026-09-05 short"},
    "2": {"kind": "front_of_book", "orders": 1, "client_order_id": False,
          "contracts": "0.2", "side": "sell", "pos_side": "short",
          "observe_seconds": 900,
          "question": "partial or repeated fills on one order"},
}
FILLING_CELLS = frozenset({"1", "2"})


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%03dZ" % (
        (time.time_ns() // 1_000_000) % 1000
    )


def durable_json(path: Path, value, *, exclusive: bool = False) -> None:
    with path.open("x" if exclusive else "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def append_json(path: Path, value) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102 - refuse silently
        return None


# --------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------


def _worker_main_pid() -> str:
    pid = subprocess.check_output(
        ["systemctl", "show", "telegram-kol-worker.service", "--property=MainPID", "--value"],
        text=True,
    ).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise ValueError("worker is not running")
    return pid


def _local_json(path: str, timeout: float = 15.0):
    with urllib.request.urlopen(WORKER_HEALTH + path, timeout=timeout) as response:
        return json.load(response)


def load_worker_credentials() -> dict:
    """Borrow the worker's Deepcoin credentials from its own process env.

    The retired immutable-release flow no longer sets ``TELEGRAM_KOL_RELEASE_COMMIT``,
    so ``loaded_artifact_verified`` is permanently false in production (see the
    ``identity-note`` entry in docs/rest-ws-trading-status.md).  The precondition
    here is therefore the one later phases agreed on: the worker process is alive,
    the identity endpoint answers from that same pid in the worker role, and the
    private WebSocket is healthy.
    """

    pid = _worker_main_pid()
    identity = _local_json("/api/runtime/deployment-identity")
    if str(identity.get("pid")) != pid:
        raise ValueError("deployment identity pid does not match the worker MainPID")
    if str(identity.get("runtime_role")) != "worker":
        raise ValueError("deployment identity is not the worker role")
    environ = dict(
        item.split(b"=", 1)
        for item in Path("/proc/" + pid + "/environ").read_bytes().split(b"\0")
        if b"=" in item
    )
    for name in ("DEEPCOIN_API_KEY", "DEEPCOIN_API_SECRET", "DEEPCOIN_API_PASSPHRASE"):
        value = environ.get(name.encode())
        if not value:
            raise ValueError("worker credential is absent")
        os.environ[name] = value.decode()
    environ.clear()
    if _worker_main_pid() != pid:
        raise ValueError("worker restarted while credentials were being loaded")
    return {
        "worker_pid": pid,
        "runtime_role": identity.get("runtime_role"),
        "observed_at": identity.get("observed_at"),
    }


def require_ws_health() -> dict:
    """Refuse to submit unless the production private stream is fully converged."""

    health = _local_json("/api/runtime/deepcoin-ws-health")
    problems = []
    if health.get("state") != "healthy":
        problems.append("state=" + str(health.get("state")))
    if health.get("connected") is not True:
        problems.append("connected=false")
    if health.get("permits_new_entry") is not True:
        problems.append("permits_new_entry=false:" + str(health.get("permits_new_entry_reason")))
    if int(health.get("open_gap_count") or 0) != 0:
        problems.append("open_gap_count=" + str(health.get("open_gap_count")))
    if health.get("last_resync_outcome") != "converged":
        problems.append("last_resync_outcome=" + str(health.get("last_resync_outcome")))
    if problems:
        raise ValueError("production WebSocket is not healthy: " + ", ".join(problems))
    return {
        key: health.get(key)
        for key in (
            "state", "connected", "permits_new_entry", "open_gap_count",
            "last_resync_outcome", "reconnect_count", "counts_by_processed_state",
        )
    }


# --------------------------------------------------------------------------
# signed transport
# --------------------------------------------------------------------------


def _sign(timestamp: str, method: str, path: str, body: bytes) -> str:
    return base64.b64encode(
        hmac.new(
            os.environ["DEEPCOIN_API_SECRET"].encode(),
            timestamp.encode() + method.encode() + path.encode() + body,
            hashlib.sha256,
        ).digest()
    ).decode()


def _headers(timestamp: str, signature: str, *, json_body: bool) -> dict[str, str]:
    headers = {
        "DC-ACCESS-KEY": os.environ["DEEPCOIN_API_KEY"],
        "DC-ACCESS-SIGN": signature,
        "DC-ACCESS-TIMESTAMP": timestamp,
        "DC-ACCESS-PASSPHRASE": os.environ["DEEPCOIN_API_PASSPHRASE"],
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


# Every signed read is logged raw so an incomplete read can never be silently
# read back later as "nothing there".
_RAW_LOG: Path | None = None


def set_raw_log(path: Path | None) -> None:
    global _RAW_LOG
    _RAW_LOG = path


def signed_get(path: str, params: dict | None = None, *, timeout: float = 15.0):
    query = "?" + urllib.parse.urlencode(params) if params else ""
    timestamp = utc()
    signature = _sign(timestamp, "GET", path + query, b"")
    request = urllib.request.Request(
        BASE + path + query, headers=_headers(timestamp, signature, json_body=False), method="GET"
    )
    record = {"at": utc(), "method": "GET", "path": path, "params": params or {},
              "status": "incomplete"}
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8")
        record.update(http_status=status, raw_body=raw)
        payload = json.loads(raw)
        if status != 200 or not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise ValueError(f"read rejected: {path}")
        data = payload.get("data")
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"unexpected response schema: {path}")
        record["status"] = "response_received"
        return rows
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        raise
    finally:
        if _RAW_LOG is not None:
            append_json(_RAW_LOG, record)


def public_get(path: str, params: dict, *, timeout: float = 15.0) -> list:
    query = "?" + "&".join(f"{key}={value}" for key, value in params.items())
    with urllib.request.build_opener(NoRedirect).open(
        urllib.request.Request(BASE + path + query, method="GET"), timeout=timeout
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise ValueError(f"public read rejected: {path}")
    data = payload.get("data")
    return data if isinstance(data, list) else []


def post_transport(path: str, body: dict, *, timeout: float) -> tuple[int, str]:
    if path not in WRITE_PATHS:
        raise ValueError("write route not allowed")
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = utc()
    signature = _sign(timestamp, "POST", path, encoded)
    request = urllib.request.Request(
        BASE + path, data=encoded, headers=_headers(timestamp, signature, json_body=True),
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def write_once(request: dict, out: Path, label: str, *, timeout: float) -> dict:
    """Send exactly one write.  Never retries, whatever the outcome."""

    if request.get("method") != "POST" or request.get("path") not in WRITE_PATHS:
        raise ValueError("write route not allowed")
    started = utc()
    # Reaches durable storage before the request can reach the exchange.
    durable_json(
        out / (label + "-request.json"),
        {"started_at": started, "request": request, "timeout_seconds": timeout},
        exclusive=True,
    )
    record = {
        "label": label,
        "started_at": started,
        "outcome": "unknown_exchange_outcome",
        "ordId": None,
        "sCode": None,
        "sMsg": None,
    }
    try:
        status, raw = post_transport(request["path"], request["body"], timeout=timeout)
        record.update(http_status=status, raw_body=raw)
        payload = json.loads(raw)
        record["payload"] = payload
        if isinstance(payload, dict) and status == 200:
            code = str(payload.get("code", ""))
            data = payload.get("data")
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
            if code == "0" and isinstance(data, dict):
                # Outer code=0 is never success on its own: read data[].sCode.
                sub_code = str(data.get("sCode", ""))
                order_id = str(data.get("ordId") or "")
                record["sCode"] = sub_code
                record["sMsg"] = data.get("sMsg")
                if sub_code == "0" and order_id.isdigit():
                    record.update(outcome="accepted", ordId=order_id)
                elif sub_code.isdigit() and sub_code != "0":
                    record["outcome"] = "rejected"
            elif code.isdigit() and code != "0":
                record.update(outcome="rejected", sCode=code, sMsg=payload.get("msg"))
    except Exception as exc:
        record["error_type"] = type(exc).__name__
    finally:
        record["finished_at"] = utc()
        durable_json(out / (label + "-response.json"), record, exclusive=True)
    return record


# --------------------------------------------------------------------------
# private WebSocket capture
# --------------------------------------------------------------------------


class PrivateWsCapture:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.ready = threading.Event()
        self.stop_requested = threading.Event()
        self.thread: threading.Thread | None = None
        self.error_type: str | None = None
        self.frames = 0

    def start(self, listen_key: str) -> None:
        self.thread = threading.Thread(
            target=self._run, args=(listen_key,), name="phase5-private-ws", daemon=True
        )
        self.thread.start()

    def _run(self, listen_key: str) -> None:
        try:
            from websockets.sync.client import connect

            # Never persist or print this URL: it carries the listen key.
            with connect(
                WS_URL + "?listenKey=" + listen_key,
                open_timeout=15, close_timeout=5, ping_interval=10, ping_timeout=10,
                max_size=2_000_000,
            ) as websocket:
                websocket.send(json.dumps({"action": "subscribe", "tables": list(WS_TABLES)}))
                append_json(
                    self.out / "ws-status.jsonl",
                    {"at": utc(), "status": "connected_subscribe_sent", "tables": list(WS_TABLES)},
                )
                self.ready.set()
                while not self.stop_requested.is_set():
                    try:
                        raw = websocket.recv(timeout=1)
                    except TimeoutError:
                        continue
                    received_ms = time.time_ns() // 1_000_000
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        payload = {"unparsed_type": type(raw).__name__}
                    append_json(
                        self.out / "ws-events.jsonl",
                        {"received_at": utc(), "received_ms": received_ms, "payload": payload},
                    )
                    self.frames += 1
        except Exception as exc:
            self.error_type = type(exc).__name__
            append_json(
                self.out / "ws-status.jsonl",
                {"at": utc(), "status": "failed", "error_type": self.error_type},
            )
        finally:
            self.ready.set()

    def stop(self) -> None:
        self.stop_requested.set()
        if self.thread is not None:
            self.thread.join(timeout=8)


def acquire_listen_key() -> str:
    rows = signed_get(LISTENKEY_PATH)
    for row in rows:
        if isinstance(row, dict) and str(row.get("listenkey") or ""):
            return str(row["listenkey"])
    raise ValueError("listenkey missing from response")


def read_ws_rows(out: Path, *, after_ms: int = 0) -> list[dict]:
    path = out / "ws-events.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or int(event.get("received_ms") or 0) < after_ms:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        result = payload.get("result")
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            continue
        for item in result:
            if isinstance(item, dict) and isinstance(item.get("data"), dict):
                rows.append(
                    {
                        "received_ms": int(event.get("received_ms") or 0),
                        "action": payload.get("action"),
                        "table": str(item.get("table") or ""),
                        "data": item["data"],
                    }
                )
    return rows


def ws_order_id(row: dict) -> str:
    data = row.get("data")
    return str(data.get("OS") or "") if isinstance(data, dict) else ""


def ws_trade_unit(row: dict) -> str:
    data = row.get("data")
    return str(data.get("TU") or "") if isinstance(data, dict) else ""


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


def quantize(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def read_market() -> dict:
    specs = [
        row for row in public_get("/deepcoin/market/instruments", {"instType": "SWAP"})
        if isinstance(row, dict) and row.get("instId") == INST
    ]
    tickers = [
        row for row in public_get("/deepcoin/market/tickers", {"instType": "SWAP"})
        if isinstance(row, dict) and row.get("instId") == INST
    ]
    if len(specs) != 1 or len(tickers) != 1:
        raise ValueError("missing or duplicate instrument/quote")
    return {"spec": specs[0], "ticker": tickers[0]}


def build_manifest(cell: str, market: dict, *, run_id: str) -> dict:
    plan = CELLS[cell]
    spec, ticker = market["spec"], market["ticker"]
    tick = Decimal(str(spec["tickSz"]))
    lot = Decimal(str(spec["lotSz"]))
    contract_value = Decimal(str(spec["ctVal"]))
    last = Decimal(str(ticker["last"]))
    bid = Decimal(str(ticker["bidPx"]))
    ask = Decimal(str(ticker["askPx"]))
    contracts = Decimal(plan["contracts"])
    if contracts > MAX_CONTRACTS or contracts < Decimal(str(spec["minSz"])) or contracts % lot:
        raise ValueError("contract quantity outside the agreed experiment bounds")

    if plan["kind"] == "far":
        price = quantize(last * Decimal(plan["px_factor"]), tick)
        if abs(price - last) / last < FAR_PRICE_MIN_DISTANCE:
            raise ValueError("far-cell price is not far enough from the market")
    elif plan["kind"] == "passive_fill":
        price = quantize(last + Decimal(plan["px_offset"]), tick)
        # Passive means it rests instead of crossing: a buy stays below the ask,
        # a sell stays above the bid.  It must still be close enough to the
        # market that it can realistically fill inside the observation window.
        crosses = price >= ask if plan["side"] == "buy" else price <= bid
        if crosses:
            raise ValueError("passive fill price would cross the book")
        if abs(price - last) / last > Decimal("0.02"):
            raise ValueError("passive fill price is too far from the market to fill")
    else:  # front_of_book
        price = ask - tick if plan["side"] == "sell" else bid + tick
        price = quantize(price, tick)
        if not bid < price < ask:
            raise ValueError("front-of-book price does not sit inside the spread")

    if price <= 0 or price % tick:
        raise ValueError("price is not a valid tick multiple")
    notional = contracts * contract_value * price
    if notional > MAX_NOTIONAL_USDT:
        raise ValueError(f"notional {notional} exceeds the agreed cap")

    # TP and SL are anchored on the submitted limit price, 10 USDT either way,
    # exactly as the accepted 2026-09-05 short did.
    if plan["pos_side"] == "long":
        take_profit, stop_loss = price + 10, price - 10
    else:
        take_profit, stop_loss = price - 10, price + 10
    if stop_loss <= 0 or take_profit <= 0:
        raise ValueError("protection price is not positive")

    requests = []
    for index in range(int(plan["orders"])):
        body = {
            "instId": INST,
            "tdMode": "cross",
            "mrgPosition": "split",
            "side": plan["side"],
            "posSide": plan["pos_side"],
            "ordType": "limit",
            "px": f"{price:f}",
            "sz": f"{contracts:f}",
            "tpTriggerPx": f"{take_profit:f}",
            "slTriggerPx": f"{stop_loss:f}",
        }
        if plan["client_order_id"]:
            body["clOrdId"] = "P5" + run_id + chr(ord("A") + index)
        requests.append(
            {"label": f"{cell}-{index + 1}", "method": "POST", "path": ORDER_PATH, "body": body}
        )
    return {
        "cell": cell,
        "question": plan["question"],
        "run_id": run_id,
        "built_at": utc(),
        "instrument_spec": spec,
        "ticker": ticker,
        "eth_each": f"{contracts * contract_value:f}",
        "notional_usdt_each": f"{notional:f}",
        "requests": requests,
        "force_timeout_seconds": plan.get("force_timeout_seconds"),
        "observe_seconds": plan["observe_seconds"],
        "cancels_unfilled_accepted_orders": True,
        "never_closes_a_filled_position": True,
    }


# --------------------------------------------------------------------------
# observation and cleanup
# --------------------------------------------------------------------------


def read_state(out: Path, owned: set[str], *, label: str) -> dict:
    base = {"instType": "SWAP", "instId": INST}
    frame = {
        "at": utc(),
        "label": label,
        "positions": signed_get("/deepcoin/account/positions", base),
        "orders_pending": signed_get("/deepcoin/trade/orders-pending", {**base, "limit": 100}),
        "trigger_orders_pending": signed_get(
            "/deepcoin/trade/trigger-orders-pending", {**base, "limit": 100}
        ),
        "owned_order_details": {
            order_id: signed_get("/deepcoin/trade/order", {"instId": INST, "ordId": order_id})
            for order_id in sorted(owned)
        },
    }
    append_json(out / "rest-frames.jsonl", frame)
    return frame


def cancel_owned(out: Path, owned: set[str], *, timeout: float) -> list[dict]:
    results = []
    for order_id in sorted(owned):
        results.append(
            write_once(
                {
                    "method": "POST",
                    "path": CANCEL_PATH,
                    "body": {"instId": INST, "ordId": order_id, "mrgPosition": "split"},
                },
                out,
                "cancel-" + order_id,
                timeout=timeout,
            )
        )
        time.sleep(1)
    return results


def recovery_candidates(rows: list[dict], *, body: dict, baseline: set[str]) -> list[dict]:
    """Rows that *could* be the lost write.  Explicitly not identity proof."""

    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        order_id = str(row.get("ordId") or "")
        if not order_id.isdigit() or order_id in baseline:
            continue
        if (
            str(row.get("instId") or "") == body["instId"]
            and str(row.get("side") or "").lower() == body["side"]
            and str(row.get("posSide") or "").lower() == body["posSide"]
            and Decimal(str(row.get("px") or row.get("price") or "0")) == Decimal(body["px"])
            and Decimal(str(row.get("sz") or "0")) == Decimal(body["sz"])
        ):
            matches.append(row)
    return matches


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def run_cell(root: Path, cell: str, *, execute: bool, confirm_cancel_candidate: bool) -> int:
    plan = CELLS[cell]
    cell_root = root / ("cell-" + cell)
    cell_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    market = read_market()
    run_id = uuid.uuid4().hex[:10]

    if not execute:
        manifest = build_manifest(cell, market, run_id="DRYRUN0000")
        print(json.dumps({"event": "DRY_RUN", "manifest": manifest}, ensure_ascii=False, indent=2))
        print("\nNo order submitted. Re-run with --execute to submit for real.")
        return 0

    lock = cell_root / "LIVE-ATTEMPT.json"
    if lock.exists():
        raise ValueError(
            f"cell {cell} has already been attempted; inspect its evidence instead of repeating it"
        )
    out = cell_root / ("live-" + run_id)
    out.mkdir(mode=0o700)
    set_raw_log(out / "raw.jsonl")
    summary: dict = {
        "cell": cell,
        "question": plan["question"],
        "run_id": run_id,
        "output": str(out),
        "started_at": utc(),
        "status": "preflight",
    }
    owned: set[str] = set()
    baseline: set[str] = set()
    capture: PrivateWsCapture | None = None
    submitted = False

    try:
        summary["worker"] = load_worker_credentials()
        summary["ws_health_before"] = require_ws_health()
        base = {"instType": "SWAP", "instId": INST}
        for name in ("orders-pending", "orders-history", "trigger-orders-pending",
                     "trigger-orders-history"):
            rows = signed_get("/deepcoin/trade/" + name, {**base, "limit": 100})
            if name.endswith("pending") and len(rows) >= 100:
                raise ValueError(f"baseline {name} snapshot is incomplete")
            baseline.update(str(row.get("ordId")) for row in rows if isinstance(row, dict) and row.get("ordId"))
        summary["baseline_order_id_count"] = len(baseline)
        durable_json(out / "baseline-ids.json", sorted(baseline))
        read_state(out, set(), label="baseline")

        manifest = build_manifest(cell, market, run_id=run_id)
        durable_json(out / "manifest.json", manifest)

        listen_key = acquire_listen_key()
        capture = PrivateWsCapture(out)
        capture.start(listen_key)
        del listen_key
        if not capture.ready.wait(timeout=20) or capture.error_type:
            raise ValueError("private WebSocket did not become ready; nothing was submitted")
        time.sleep(2)
        if capture.error_type:
            raise ValueError("private WebSocket failed before submission; nothing was submitted")
        summary["ws_subscribed_before_submit"] = True

        print(json.dumps({"event": "SUBMITTING", "cell": cell,
                          "requests": manifest["requests"]}, ensure_ascii=False), flush=True)
        durable_json(lock, {"cell": cell, "run_id": run_id, "claimed_at": utc(),
                            "notice": "Do not remove or rerun automatically."}, exclusive=True)
        submitted = True
        submission_ms = time.time_ns() // 1_000_000
        timeout = float(plan.get("force_timeout_seconds") or 15.0)
        requests = manifest["requests"]
        if len(requests) == 1:
            results = [write_once(requests[0], out, "submit-" + requests[0]["label"], timeout=timeout)]
        else:
            with ThreadPoolExecutor(max_workers=len(requests)) as pool:
                futures = [
                    pool.submit(write_once, request, out, "submit-" + request["label"], timeout=timeout)
                    for request in requests
                ]
                results = [future.result() for future in futures]
        summary["submissions"] = [
            {key: row.get(key) for key in ("label", "outcome", "ordId", "sCode", "sMsg", "error_type")}
            for row in results
        ]
        print(json.dumps({"event": "SUBMIT_RESULTS", "results": summary["submissions"]},
                         ensure_ascii=False), flush=True)
        owned = {str(row["ordId"]) for row in results if row["outcome"] == "accepted"}
        if owned & baseline:
            raise ValueError("an accepted order id collides with the baseline")
        durable_json(out / "owned-ids.json", sorted(owned))

        if cell == "11":
            unknown = [row for row in results if row["outcome"] == "unknown_exchange_outcome"]
            summary["forced_timeout_produced_unknown"] = bool(unknown)
            summary["resend_attempted"] = False
            time.sleep(5)
            pending = signed_get("/deepcoin/trade/orders-pending", {**base, "limit": 100})
            history = signed_get("/deepcoin/trade/orders-history", {**base, "limit": 100})
            candidates = recovery_candidates(
                pending + history, body=requests[0]["body"], baseline=baseline
            )
            durable_json(out / "recovery-candidates.json", candidates)
            summary["recovery_candidate_count"] = len(candidates)
            summary["recovery_candidate_ids"] = [str(row.get("ordId")) for row in candidates]
            summary["recovery_note"] = (
                "candidates matched on instId/side/posSide/px/sz at a price nothing else on this "
                "account uses; this is candidate evidence, not an identity proof"
            )
            if len(candidates) == 1 and confirm_cancel_candidate:
                owned.add(str(candidates[0]["ordId"]))

        deadline = time.monotonic() + float(plan["observe_seconds"])
        while time.monotonic() < deadline:
            time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
            read_state(out, owned, label="observe")
        final = read_state(out, owned, label="final-before-cleanup")
        summary["ws_rows_after_submission"] = len(read_ws_rows(out, after_ms=submission_ms))

        ws_rows = read_ws_rows(out, after_ms=submission_ms)
        summary["ws_tables_seen"] = sorted({row["table"] for row in ws_rows})
        summary["ws_trigger_tu_values"] = sorted(
            {ws_trade_unit(row) for row in ws_rows if row["table"] == "TriggerOrder" and ws_trade_unit(row)}
        )
        summary["ws_order_ids_seen"] = sorted({ws_order_id(row) for row in ws_rows if ws_order_id(row)})
        summary["tpsl_rows_before_cancel"] = [
            {key: row.get(key) for key in
             ("ordId", "side", "posSide", "sz", "tpTriggerPrice", "slTriggerPrice",
              "triggerOrderType", "cTime")}
            for row in final["trigger_orders_pending"]
            if isinstance(row, dict) and str(row.get("ordId")) not in baseline
        ]
        summary["status"] = "submitted_and_observed"
    except Exception as exc:
        summary["status"] = "incomplete_manual_review" if submitted else "stopped_before_submit"
        summary["error_type"] = type(exc).__name__
        summary["reason"] = str(exc)
    finally:
        # The WebSocket stays subscribed through cleanup: cancelling an entry
        # also moves its attached TPSL, and those frames are evidence too.
        if submitted:
            try:
                # Cancel only the ordIds this run received.  A filled position is
                # never closed here; only an unfilled remainder can be cancelled.
                summary["cancel_results"] = [
                    {key: row.get(key) for key in ("label", "outcome", "ordId", "sCode", "sMsg")}
                    for row in cancel_owned(out, owned, timeout=15.0)
                ]
                after = read_state(out, owned, label="after-cancel")
                summary["tpsl_rows_after_cancel"] = [
                    {key: row.get(key) for key in
                     ("ordId", "side", "posSide", "sz", "tpTriggerPrice", "slTriggerPrice",
                      "triggerOrderType")}
                    for row in after["trigger_orders_pending"]
                    if isinstance(row, dict) and str(row.get("ordId")) not in baseline
                ]
                summary["orders_still_pending_from_this_run"] = sorted(
                    {str(row.get("ordId")) for row in after["orders_pending"]
                     if isinstance(row, dict) and str(row.get("ordId")) in owned}
                )
                summary["eth_positions_after"] = [
                    {key: row.get(key) for key in
                     ("posId", "posSide", "pos", "avgPx", "tpTriggerPx", "slTriggerPx")}
                    for row in after["positions"] if isinstance(row, dict)
                ]
                if summary["orders_still_pending_from_this_run"]:
                    summary["status"] = "cleanup_unresolved_manual_review"
            except Exception as exc:
                summary["status"] = "cleanup_unresolved_manual_review"
                summary["cleanup_error_type"] = type(exc).__name__
                summary["cleanup_reason"] = str(exc)
            summary["notice"] = (
                "只撤销本次收到 ordId 的未成交挂单；已成交仓位与其 TP/SL 不自动平仓、不自动撤销。"
            )
        if capture is not None:
            # Give the cancellation frames a moment to arrive before closing.
            time.sleep(3)
            capture.stop()
            summary["ws_frames"] = capture.frames
            summary["ws_error_type"] = capture.error_type
            summary["ws_rows_total"] = len(read_ws_rows(out))
        summary["finished_at"] = utc()
        durable_json(out / "cell-summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] == "submitted_and_observed" else 1


def cancel_exact(root: Path, order_id: str, *, execute: bool) -> int:
    """Cancel exactly one order id the operator names.  Never scans for targets.

    Needed because ``GET /deepcoin/trade/orders-pending`` is blind to a live
    ordinary limit order (cell 11, 2026-09-07): a run whose response was lost
    can leave an order that no listing endpoint will ever show, so the id has to
    come from the operator, be read back by exact id, and be verified unfilled
    before anything is cancelled.
    """

    if not order_id.isdigit():
        raise ValueError("order id must be numeric")
    # One directory per invocation, so a dry run never blocks the real one.
    out = root / ("manual-cancel-" + order_id + "-" + uuid.uuid4().hex[:8])
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    set_raw_log(out / "raw.jsonl")
    summary = {"order_id": order_id, "started_at": utc(), "status": "preflight"}
    try:
        summary["worker"] = load_worker_credentials()
        rows = signed_get("/deepcoin/trade/order", {"instId": INST, "ordId": order_id})
        summary["order_before"] = [
            {key: row.get(key) for key in ("ordId", "state", "accFillSz", "px", "sz", "side", "posSide")}
            for row in rows
        ]
        if len(rows) != 1:
            raise ValueError("exact order read did not return exactly one row")
        state = str(rows[0].get("state") or "")
        filled = Decimal(str(rows[0].get("accFillSz") or "0"))
        if state != "live":
            raise ValueError(f"order is not live (state={state}); nothing cancelled")
        if filled != 0:
            raise ValueError(f"order already has fills (accFillSz={filled}); refusing to cancel")
        base = {"instType": "SWAP", "instId": INST}
        summary["tpsl_before"] = [
            {key: row.get(key) for key in ("ordId", "side", "posSide", "sz",
                                           "tpTriggerPrice", "slTriggerPrice")}
            for row in signed_get("/deepcoin/trade/trigger-orders-pending", {**base, "limit": 100})
        ]
        if not execute:
            summary["status"] = "dry_run"
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print("\nNothing cancelled. Re-run with --execute.")
            return 0
        summary["cancel"] = write_once(
            {"method": "POST", "path": CANCEL_PATH,
             "body": {"instId": INST, "ordId": order_id, "mrgPosition": "split"}},
            out, "cancel-" + order_id, timeout=15.0,
        )
        time.sleep(2)
        summary["order_after"] = [
            {key: row.get(key) for key in ("ordId", "state", "accFillSz")}
            for row in signed_get("/deepcoin/trade/order", {"instId": INST, "ordId": order_id})
        ]
        summary["tpsl_after"] = [
            {key: row.get(key) for key in ("ordId", "side", "posSide", "sz",
                                           "tpTriggerPrice", "slTriggerPrice")}
            for row in signed_get("/deepcoin/trade/trigger-orders-pending", {**base, "limit": 100})
        ]
        summary["status"] = (
            "cancelled"
            if summary["cancel"]["outcome"] == "accepted"
            and all(str(row.get("state")) == "canceled" for row in summary["order_after"])
            else "cancel_unresolved_manual_review"
        )
    except Exception as exc:
        summary["status"] = "stopped"
        summary["error_type"] = type(exc).__name__
        summary["reason"] = str(exc)
    finally:
        summary["finished_at"] = utc()
        durable_json(out / "manual-cancel-summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] in {"cancelled", "dry_run"} else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", choices=sorted(CELLS))
    parser.add_argument("--cancel-exact", metavar="ORDID",
                        help="cancel exactly this order id and nothing else")
    parser.add_argument("--execute", action="store_true",
                        help="submit for real; without it nothing is sent")
    parser.add_argument("--confirm-cancel-candidate", action="store_true",
                        help="cell 11 only: allow cancelling the single recovery candidate")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    args = parser.parse_args()
    if bool(args.cell) == bool(args.cancel_exact):
        parser.error("pass exactly one of --cell or --cancel-exact")
    previous_umask = os.umask(0o077)
    try:
        if args.cancel_exact:
            return cancel_exact(Path(args.root), args.cancel_exact, execute=args.execute)
        return run_cell(
            Path(args.root), args.cell,
            execute=args.execute,
            confirm_cancel_candidate=args.confirm_cancel_candidate,
        )
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
