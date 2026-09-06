"""One-shot Deepcoin REST command + private WebSocket event experiment.

The live flag submits one minimum-size ETH-USDT-SWAP short limit order through
REST with attached TP/SL and no client order ID. It captures Order, Trade, Position and TriggerOrder
events before and after submission, then reconciles them with REST.  It never
retries a write with an unknown outcome.
"""
from __future__ import annotations

import argparse
import base64
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import threading
import time
import urllib.error
import urllib.request
import uuid

import deepcoin_order_tpsl_experiment as order_probe


core = order_probe.core
lab = core.lab
LISTENKEY_PATH = "/deepcoin/listenkey/acquire"
WS_URL = "wss://stream.deepcoin.com/v1/private"
TABLES = ("Order", "Trade", "Position", "TriggerOrder")


def _signed_get_json(path: str) -> dict:
    timestamp = lab.utc()
    signature = base64.b64encode(
        hmac.new(
            os.environ["DEEPCOIN_API_SECRET"].encode(),
            (timestamp + "GET" + path).encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    request = urllib.request.Request(
        lab.BASE + path,
        headers={
            "DC-ACCESS-KEY": os.environ["DEEPCOIN_API_KEY"],
            "DC-ACCESS-SIGN": signature,
            "DC-ACCESS-TIMESTAMP": timestamp,
            "DC-ACCESS-PASSPHRASE": os.environ["DEEPCOIN_API_PASSPHRASE"],
        },
        method="GET",
    )
    with urllib.request.build_opener(lab.NoRedirect).open(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise ValueError("listenkey acquisition rejected")
    data = payload.get("data")
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict) or not str(data.get("listenkey") or ""):
        raise ValueError("listenkey missing from response")
    return data


def _append_event(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class PrivateWsCapture:
    def __init__(self, out: Path):
        self.out = out
        self.ready = threading.Event()
        self.stop_requested = threading.Event()
        self.thread: threading.Thread | None = None
        self.error_type: str | None = None
        self.frames = 0

    def start(self, listenkey: str) -> None:
        self.thread = threading.Thread(
            target=self._run,
            args=(listenkey,),
            name="deepcoin-private-ws-evidence",
            daemon=True,
        )
        self.thread.start()

    def _run(self, listenkey: str) -> None:
        try:
            from websockets.sync.client import connect

            # Never persist or print this URL because it contains the listen key.
            with connect(
                WS_URL + "?listenKey=" + listenkey,
                open_timeout=15,
                close_timeout=5,
                ping_interval=10,
                ping_timeout=10,
                max_size=2_000_000,
            ) as websocket:
                websocket.send(json.dumps({"action": "subscribe", "tables": list(TABLES)}))
                _append_event(
                    self.out / "ws-status.jsonl",
                    {"at": lab.utc(), "status": "connected_subscribe_sent", "tables": list(TABLES)},
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
                    _append_event(
                        self.out / "ws-events.jsonl",
                        {"received_at": lab.utc(), "received_ms": received_ms, "payload": payload},
                    )
                    self.frames += 1
        except Exception as exc:
            self.error_type = type(exc).__name__
            _append_event(
                self.out / "ws-status.jsonl",
                {"at": lab.utc(), "status": "failed", "error_type": self.error_type},
            )
        finally:
            self.ready.set()

    def stop(self) -> None:
        self.stop_requested.set()
        if self.thread is not None:
            self.thread.join(timeout=8)


def read_ws_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def extract_ws_rows(events: list[dict], *, after_ms: int = 0) -> list[dict]:
    rows: list[dict] = []
    for event in events:
        received_ms = int(event.get("received_ms") or 0)
        if received_ms < after_ms:
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
            if not isinstance(item, dict):
                continue
            table = str(item.get("table") or "")
            data = item.get("data")
            if isinstance(data, dict):
                rows.append(
                    {
                        "received_ms": received_ms,
                        "action": payload.get("action"),
                        "table": table,
                        "data": data,
                    }
                )
    return rows


def _ws_order_id(row: dict) -> str:
    data = row.get("data") if isinstance(row, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("OS") or data.get("ordId") or data.get("orderId") or "")


def _ws_position_id(row: dict) -> str:
    data = row.get("data") if isinstance(row, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("TU") or data.get("posId") or data.get("PositionID") or "")


def build_correlation(
    *,
    ws_rows: list[dict],
    main_order_id: str,
    client_order_id: str,
    baseline_ids: set[str],
    positions: list[dict],
    trigger_rows: list[dict],
) -> dict:
    order_events = [
        row for row in ws_rows if row.get("table") == "Order" and _ws_order_id(row) == main_order_id
    ]
    trade_events = [
        row for row in ws_rows if row.get("table") == "Trade" and _ws_order_id(row) == main_order_id
    ]
    position_events = [row for row in ws_rows if row.get("table") == "Position"]
    trigger_events = [
        row
        for row in ws_rows
        if row.get("table") == "TriggerOrder"
        and _ws_order_id(row)
        and _ws_order_id(row) not in baseline_ids
    ]
    pos_ids = sorted(
        {
            str(row.get("posId") or row.get("positionId") or "")
            for row in positions
            if str(row.get("posId") or row.get("positionId") or "")
            and str(row.get("posSide") or "").lower() == "short"
        }
    )
    trigger_ws = [
        {
            "ordId": _ws_order_id(row),
            "TU": _ws_position_id(row),
            "received_ms": row.get("received_ms"),
            "data": row.get("data"),
        }
        for row in trigger_events
    ]
    ws_tu_matches = sorted(
        {
            item["ordId"]
            for item in trigger_ws
            if item["TU"] and item["TU"] in pos_ids
        }
    )
    rest_trigger_ids = sorted(
        {
            str(row.get("ordId") or "")
            for row in trigger_rows
            if str(row.get("ordId") or "") and str(row.get("ordId")) not in baseline_ids
        }
    )
    ws_trigger_ids = sorted({item["ordId"] for item in trigger_ws})
    exact = bool(main_order_id and trade_events and pos_ids and ws_tu_matches)
    return {
        "main_order_id": main_order_id,
        "client_order_id": client_order_id,
        "ws_order_event_count": len(order_events),
        "ws_trade_event_count": len(trade_events),
        "ws_position_event_count": len(position_events),
        "ws_trigger_event_count": len(trigger_events),
        "rest_position_ids": pos_ids,
        "ws_trigger_orders": trigger_ws,
        "rest_trigger_order_ids": rest_trigger_ids,
        "ws_and_rest_trigger_ids": sorted(set(ws_trigger_ids) & set(rest_trigger_ids)),
        "trigger_ids_whose_TU_equals_rest_posId": ws_tu_matches,
        "exact_entry_position_tpsl_chain_observed": exact,
        "conclusion": (
            "observed main ordId -> Trade OS -> REST posId -> TriggerOrder OS with TU=posId"
            if exact
            else "required exact chain not fully observed; do not infer ownership from price/time proximity"
        ),
    }


def _has_nonzero_position(rows: list[dict]) -> bool:
    for row in rows:
        try:
            if Decimal(str(row.get("pos") or "0")) != 0:
                return True
        except Exception:
            return True
    return False


def _entry_filled(order_rows: list[dict]) -> bool:
    for row in order_rows:
        state = str(row.get("state") or row.get("status") or "").lower()
        try:
            filled = Decimal(str(row.get("accFillSz") or row.get("fillSz") or "0")) > 0
        except Exception:
            filled = False
        if filled or state in {"filled", "fully_filled", "2"}:
            return True
    return False


def run_live(root: Path) -> int:
    if (root / "LIVE-ATTEMPT.json").exists():
        raise ValueError("This one-shot REST+WebSocket experiment was already attempted")
    run_id = uuid.uuid4().hex[:12]
    out = root / ("live-" + run_id)
    out.mkdir(mode=0o700)
    summary: dict = {
        "run_id": run_id,
        "output": str(out),
        "started_at": lab.utc(),
        "status": "preflight",
    }
    baseline_ids: set[str] = set()
    owned_ids: set[str] = set()
    submitted = False
    stop_requested = False
    capture: PrivateWsCapture | None = None
    last_positions: list[dict] = []
    last_triggers: list[dict] = []
    main_order_id = ""
    client_order_id = ""
    submission_ms = 0

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, request_stop)

    try:
        summary["worker_identity"] = core.worker_credentials()
        base = {"instType": "SWAP", "instId": lab.INST}
        positions = core.read_get(out, "/deepcoin/account/positions", base)
        regular_pending = core.read_get(
            out, "/deepcoin/trade/orders-pending", {**base, "limit": 100}
        )
        trigger_pending = core.read_get(
            out, "/deepcoin/trade/trigger-orders-pending", {**base, "limit": 100}
        )
        if _has_nonzero_position(positions):
            raise ValueError("existing ETH position; stopped before submission")
        if regular_pending:
            raise ValueError("existing ETH regular pending order; stopped before submission")
        if len(trigger_pending) >= 100:
            raise ValueError("existing ETH trigger-order baseline is incomplete")
        trigger_pending_ids = [str(row.get("ordId") or "") for row in trigger_pending]
        if (
            any(not order_id.isdigit() for order_id in trigger_pending_ids)
            or len(trigger_pending_ids) != len(set(trigger_pending_ids))
        ):
            raise ValueError("existing ETH trigger-order baseline has invalid identities")
        baseline_ids.update(trigger_pending_ids)
        for name in ("orders-history", "trigger-orders-history"):
            rows = core.read_get(out, "/deepcoin/trade/" + name, {**base, "limit": 100})
            baseline_ids.update(str(row.get("ordId")) for row in rows if row.get("ordId"))
        # Deepcoin returned sCode=14 DuplicateAction for a unique alphanumeric
        # clOrdId in the immediately preceding controlled attempt.  The earlier
        # no-clOrdId ordinary-order experiment was accepted, so this variant
        # deliberately relies on the successful REST response ordId.
        manifest = order_probe.prepare(out, run_id, selection="short_no_clordid")
        order_probe.validate_pair(manifest, now_ms=time.time_ns() // 1_000_000)
        request = manifest["requests"][0]
        client_order_id = str(request["body"].get("clOrdId") or "")

        listenkey_data = _signed_get_json(LISTENKEY_PATH)
        listenkey = str(listenkey_data["listenkey"])
        capture = PrivateWsCapture(out)
        capture.start(listenkey)
        del listenkey
        if not capture.ready.wait(timeout=20) or capture.error_type:
            raise ValueError("private WebSocket did not become ready")
        # Allow the subscription acknowledgement and initial snapshot to arrive.
        time.sleep(2)
        if capture.error_type:
            raise ValueError("private WebSocket failed before REST submission")

        print(
            json.dumps(
                {
                    "event": "REST_WS_READY_TO_SUBMIT",
                    "quantity_eth": manifest["quantity_eth"],
                    "reference_last": manifest["reference_last"],
                    "request": request,
                    "evidence": str(out),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        core.claim_once(root, run_id)
        submission_ms = time.time_ns() // 1_000_000
        submitted = True
        result = order_probe.submit_pair(
            manifest, out, now_ms=submission_ms
        )[0]
        summary["submission"] = {
            key: result.get(key) for key in ("label", "outcome", "ordId")
        }
        if result.get("outcome") == "accepted":
            main_order_id = str(result["ordId"])
            if main_order_id in baseline_ids:
                raise ValueError("returned order ID conflicts with baseline")
            owned_ids.add(main_order_id)
        elif result.get("outcome") == "unknown_exchange_outcome":
            summary["status"] = "unknown_exchange_outcome"
        else:
            summary["status"] = "order_rejected"
        print(json.dumps({"event": "REST_SUBMIT_RESULT", **summary["submission"]}), flush=True)

        deadline = time.monotonic() + (60 if not main_order_id else 300)
        post_fill_observe_until: float | None = None
        last_printed = None
        while not stop_requested and time.monotonic() < deadline:
            if main_order_id:
                order_rows = core.read_get(
                    out, "/deepcoin/trade/order", {"instId": lab.INST, "ordId": main_order_id}, deadline=deadline
                )
            elif client_order_id:
                order_rows = core.read_get(
                    out, "/deepcoin/trade/order", {"instId": lab.INST, "clOrdId": client_order_id}, deadline=deadline
                )
                recovered = [str(row.get("ordId") or "") for row in order_rows if str(row.get("ordId") or "").isdigit()]
                if len(set(recovered)) == 1 and recovered[0] not in baseline_ids:
                    main_order_id = recovered[0]
                    owned_ids.add(main_order_id)
            else:
                # Without a confirmed ordId or supported clOrdId there is no
                # exact lookup key. Preserve unknown outcome and never scan by
                # symbol/time/price to manufacture ownership.
                order_rows = []
            last_positions = core.read_get(out, "/deepcoin/account/positions", base, deadline=deadline)
            pending = core.read_get(
                out, "/deepcoin/trade/trigger-orders-pending", {**base, "limit": 100}, deadline=deadline
            )
            history = core.read_get(
                out, "/deepcoin/trade/trigger-orders-history", {**base, "limit": 100}, deadline=deadline
            )
            last_triggers = pending + history
            ws_rows = extract_ws_rows(read_ws_events(out / "ws-events.jsonl"), after_ms=submission_ms)
            correlation = build_correlation(
                ws_rows=ws_rows,
                main_order_id=main_order_id,
                client_order_id=client_order_id,
                baseline_ids=baseline_ids,
                positions=last_positions,
                trigger_rows=last_triggers,
            )
            state = {
                "main_order_id": main_order_id,
                "filled": _entry_filled(order_rows),
                "posIds": correlation["rest_position_ids"],
                "ws_order": correlation["ws_order_event_count"],
                "ws_trade": correlation["ws_trade_event_count"],
                "ws_trigger": correlation["ws_trigger_event_count"],
                "exact_chain": correlation["exact_entry_position_tpsl_chain_observed"],
            }
            if state != last_printed:
                print(json.dumps({"event": "STATE_CHANGED", **state}, ensure_ascii=False), flush=True)
                last_printed = state
            if correlation["exact_entry_position_tpsl_chain_observed"]:
                if post_fill_observe_until is None:
                    post_fill_observe_until = time.monotonic() + 10
                elif time.monotonic() >= post_fill_observe_until:
                    break
            if summary["status"] == "order_rejected":
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))

        ws_rows = extract_ws_rows(read_ws_events(out / "ws-events.jsonl"), after_ms=submission_ms)
        correlation = build_correlation(
            ws_rows=ws_rows,
            main_order_id=main_order_id,
            client_order_id=client_order_id,
            baseline_ids=baseline_ids,
            positions=last_positions,
            trigger_rows=last_triggers,
        )
        core.durable_json(out / "correlation.json", correlation)
        summary["correlation"] = correlation
        if summary["status"] not in {"unknown_exchange_outcome", "order_rejected"}:
            summary["status"] = (
                "exact_chain_observed"
                if correlation["exact_entry_position_tpsl_chain_observed"]
                else "observation_incomplete_manual_review"
            )
    except Exception as exc:
        summary["status"] = "incomplete_manual_review" if submitted else "stopped_before_submit"
        summary["error_type"] = type(exc).__name__
        if isinstance(exc, ValueError):
            summary["reason"] = str(exc)
    finally:
        if capture is not None:
            capture.stop()
            summary["ws_frames"] = capture.frames
            summary["ws_error_type"] = capture.error_type
        if submitted:
            for path in out.glob("submit-*-response.json"):
                try:
                    receipt = json.loads(path.read_text())
                    oid = str(receipt.get("ordId") or "")
                    if receipt.get("outcome") == "accepted" and oid.isdigit() and oid not in baseline_ids:
                        owned_ids.add(oid)
                except Exception:
                    summary["receipt_recovery_incomplete"] = True
            try:
                cleanup = order_probe.cleanup_entries(out, owned_ids)
                summary["cleanup"] = {
                    "entries_still_pending": cleanup["entries_still_pending"],
                    "cancel_outcomes": [
                        {key: row.get(key) for key in ("label", "outcome", "ordId")}
                        for row in cleanup["attempts"]
                    ],
                }
                if cleanup["entries_still_pending"]:
                    summary["status"] = "cleanup_unresolved_manual_review"
                final_positions = core.read_get(
                    out, "/deepcoin/account/positions", {"instType": "SWAP", "instId": lab.INST}
                )
                summary["remaining_eth_positions"] = [
                    {
                        key: row.get(key)
                        for key in ("posId", "posSide", "pos", "avgPx", "tpTriggerPx", "slTriggerPx")
                    }
                    for row in final_positions
                ]
            except Exception as exc:
                summary["status"] = "cleanup_unresolved_manual_review"
                summary["cleanup_error_type"] = type(exc).__name__
        summary["finished_at"] = lab.utc()
        summary["notice"] = (
            "未成交入场余量仅按本次精确 ordId 撤销；已成交仓位和附带 TP/SL 不自动撤销或平仓。"
        )
        core.durable_json(out / "live-summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] == "exact_chain_observed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-rest-ws-short", action="store_true")
    args = parser.parse_args()
    if not args.execute_rest_ws_short:
        print("No order submitted. Live execution requires --execute-rest-ws-short.")
        return 0
    previous_umask = os.umask(0o077)
    try:
        return run_live(Path(__file__).resolve().parent)
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
