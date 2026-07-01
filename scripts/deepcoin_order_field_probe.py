r"""Probe DeepCoin order/fill/position id relationships with tiny ETH long tests.

Default mode is dry-run. Live mode submits real DeepCoin orders and requires:

    $env:DEEPCOIN_FIELD_PROBE_CONFIRM="ETH_0.1_FIELD_PROBE_LIVE"
    .\.venv\Scripts\python.exe scripts\deepcoin_order_field_probe.py --live

The live probe opens and closes two ETH long positions:

1. market buy, then close by the resulting ``posId``.
2. marketable limit buy, then close by the resulting ``posId``.

Each step writes raw request/response snapshots to ``data/deepcoin_field_probe``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env
from telegram_kol_research.deepcoin_contract_specs import load_deepcoin_contract_specs


CONFIRM_VALUE = "ETH_0.1_FIELD_PROBE_LIVE"
INSTRUMENT_ID = "ETH-USDT-SWAP"


@dataclass(slots=True)
class ProbeConfig:
    live: bool
    quantity_eth: float
    output_dir: Path
    keep_positions: bool
    client: Any | None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Submit real DeepCoin orders.")
    parser.add_argument("--quantity-eth", type=float, default=0.1)
    parser.add_argument("--keep-positions", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "deepcoin_field_probe",
    )
    args = parser.parse_args()

    if args.live and os.environ.get("DEEPCOIN_FIELD_PROBE_CONFIRM") != CONFIRM_VALUE:
        print(
            "Live trading blocked. Set "
            f"DEEPCOIN_FIELD_PROBE_CONFIRM={CONFIRM_VALUE!r} to run real ETH orders.",
            file=sys.stderr,
        )
        return 2

    spec_provider = load_deepcoin_contract_specs(
        PROJECT_ROOT / "config" / "deepcoin_contract_specs.yaml",
        required=True,
    )
    spec = spec_provider.get_contract_spec(INSTRUMENT_ID)
    if spec is None:
        raise RuntimeError(f"missing contract spec for {INSTRUMENT_ID}")
    quantity_contracts = _round_down_to_step(
        args.quantity_eth / spec.contract_value,
        spec.quantity_step,
    )
    if quantity_contracts < spec.min_quantity:
        raise RuntimeError(
            f"{args.quantity_eth:g} ETH maps to {quantity_contracts:g} contracts, "
            f"below minimum {spec.min_quantity:g}"
        )

    output_dir = args.output_dir / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = ProbeConfig(
        live=args.live,
        quantity_eth=args.quantity_eth,
        output_dir=output_dir,
        keep_positions=args.keep_positions,
        client=build_deepcoin_client_from_env() if args.live else None,
    )

    print(f"mode={'LIVE' if cfg.live else 'DRY_RUN'}")
    print(f"output_dir={output_dir}")
    print(f"instrument={INSTRUMENT_ID} quantity_eth={args.quantity_eth:g} contracts={quantity_contracts:g}")

    market_result = run_entry_probe(
        cfg,
        label="market-long",
        order_payload={
            "instId": INSTRUMENT_ID,
            "tdMode": "cross",
            "side": "buy",
            "posSide": "long",
            "ordType": "market",
            "sz": _number_text(quantity_contracts),
            "mrgPosition": "split",
            "clOrdId": "TKFLDPROBEMKT1",
        },
    )

    price = _ticker_price(cfg) or 2500.0
    limit_price = _round_to_tick(price * 1.01, spec.price_tick)
    limit_result = run_entry_probe(
        cfg,
        label="marketable-limit-long",
        order_payload={
            "instId": INSTRUMENT_ID,
            "tdMode": "cross",
            "side": "buy",
            "posSide": "long",
            "ordType": "limit",
            "px": _number_text(limit_price),
            "sz": _number_text(quantity_contracts),
            "mrgPosition": "split",
            "clOrdId": "TKFLDPROBELMT1",
        },
    )

    summary = {
        "instrument_id": INSTRUMENT_ID,
        "quantity_eth": cfg.quantity_eth,
        "quantity_contracts": quantity_contracts,
        "market": market_result,
        "marketable_limit": limit_result,
    }
    write_json(cfg, "summary", summary)
    print_json("summary", summary)
    return 0


def run_entry_probe(
    cfg: ProbeConfig,
    *,
    label: str,
    order_payload: dict[str, Any],
) -> dict[str, Any]:
    print_step(label)
    before = snapshot_account(cfg, f"{label}-before")
    response = submit(cfg, "place_order", order_payload)
    write_json(cfg, f"{label}-place-order", {"request": order_payload, "response": response})
    after_submit = snapshot_account(cfg, f"{label}-after-submit")

    order_id = first_string(response, "ordId", "orderId", "order_id", "orderSysID", "id")
    client_order_id = str(order_payload.get("clOrdId") or "")
    if cfg.live:
        position = wait_for_new_position(
            cfg,
            before_position_ids=set(before["position_ids"]),
            attempts=12,
            delay_seconds=0.75,
        )
    else:
        position = {
            "posId": f"dry-run-{label}-pos",
            "instId": INSTRUMENT_ID,
            "posSide": "long",
            "pos": order_payload.get("sz"),
        }
    after_position = snapshot_account(cfg, f"{label}-after-position")
    pos_id = first_string(position or {}, "posId", "pos_id", "id")

    close_response = None
    if cfg.live and pos_id and not cfg.keep_positions:
        close_payload = {
            "instId": INSTRUMENT_ID,
            "tdMode": "cross",
            "side": "sell",
            "posSide": "long",
            "ordType": "market",
            "sz": str(position.get("pos") or order_payload.get("sz")),
            "mrgPosition": "split",
            "closePosId": pos_id,
        }
        close_response = submit(cfg, "place_order", close_payload)
        write_json(cfg, f"{label}-close-position", {"request": close_payload, "response": close_response})
        time.sleep(0.75)
        snapshot_account(cfg, f"{label}-after-close")

    result = {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "position_id": pos_id,
        "order_id_equals_position_id": bool(order_id and pos_id and str(order_id) == str(pos_id)),
        "position": position,
        "close_order_id": first_string(close_response or {}, "ordId", "orderId", "order_id", "id"),
    }
    print_json(label, result)
    return result


def snapshot_account(cfg: ProbeConfig, label: str) -> dict[str, Any]:
    if cfg.live:
        positions = cfg.client.list_positions(inst_id=INSTRUMENT_ID)
        open_orders = cfg.client.list_open_orders(inst_id=INSTRUMENT_ID)
        order_history = cfg.client.list_order_history(inst_id=INSTRUMENT_ID)
        fills = cfg.client.list_trade_fills(inst_id=INSTRUMENT_ID)
        trigger_orders = cfg.client.list_trigger_orders_pending(inst_id=INSTRUMENT_ID)
    else:
        positions = []
        open_orders = []
        order_history = []
        fills = []
        trigger_orders = []
    snapshot = {
        "captured_at": datetime.now(UTC).isoformat(),
        "positions": positions,
        "open_orders": open_orders,
        "order_history": order_history[:20],
        "fills": fills[:20],
        "trigger_orders": trigger_orders,
        "position_ids": [
            value
            for value in (first_string(item, "posId", "pos_id", "id") for item in positions)
            if value
        ],
    }
    write_json(cfg, label, snapshot)
    print(
        f"{label}: positions={len(positions)} open_orders={len(open_orders)} "
        f"history={len(order_history)} fills={len(fills)} triggers={len(trigger_orders)}"
    )
    return snapshot


def wait_for_new_position(
    cfg: ProbeConfig,
    *,
    before_position_ids: set[str],
    attempts: int,
    delay_seconds: float,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        positions = cfg.client.list_positions(inst_id=INSTRUMENT_ID)
        matches = [
            item
            for item in positions
            if str(item.get("instId") or "").upper() == INSTRUMENT_ID
            and str(item.get("posSide") or item.get("side") or "").lower() == "long"
            and first_string(item, "posId", "pos_id", "id") not in before_position_ids
            and _float(item.get("pos") or item.get("size")) > 0
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DeepcoinClientError(f"ambiguous new ETH long positions: {matches}")
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return None


def submit(cfg: ProbeConfig, method_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    print_json(f"{method_name}.request", payload)
    if not cfg.live:
        return {
            "code": "0",
            "data": {
                "ordId": f"dry-run-{method_name}-order",
                "clOrdId": payload.get("clOrdId"),
            },
        }
    return getattr(cfg.client, method_name)(payload)


def _ticker_price(cfg: ProbeConfig) -> float | None:
    if not cfg.live:
        return None
    return cfg.client.get_ticker_price(inst_id=INSTRUMENT_ID)


def write_json(cfg: ProbeConfig, name: str, payload: dict[str, Any]) -> None:
    path = cfg.output_dir / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def first_string(payload: dict[str, Any], *keys: str) -> str | None:
    data = payload.get("data")
    payloads = [payload]
    if isinstance(data, dict):
        payloads.append(data)
    for item in payloads:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _round_down_to_step(value: float, step: float) -> float:
    return int(value / step) * step


def _round_to_tick(value: float, tick: float) -> float:
    return round(round(value / tick) * tick, 8)


def _number_text(value: float | str | None) -> str:
    return f"{float(value):g}" if value is not None else ""


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def print_step(name: str) -> None:
    print(f"\n== {name} ==")


def print_json(label: str, payload: dict[str, Any]) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    raise SystemExit(main())
