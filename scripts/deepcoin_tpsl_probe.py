r"""Probe Deepcoin TP/SL creation and replacement flows for ETH long orders.

Default mode is dry-run. Live mode places real Deepcoin orders and requires:

    $env:DEEPCOIN_LIVE_TPSL_TEST_CONFIRM="ETH_0.1_TPSL_TEST"
    .\.venv\Scripts\python.exe scripts\deepcoin_tpsl_probe.py --live

The script intentionally prints sanitized request/response summaries only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env
from telegram_kol_research.deepcoin_contract_specs import load_deepcoin_contract_specs
from telegram_kol_research.deepcoin_order_matching import pending_tpsl_order_ids_for_position
from telegram_kol_research.recovery_live_submit import build_deepcoin_market_order_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_order_sltp_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_position_sltp_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_place_order_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_trigger_order_payload


CONFIRM_VALUE = "ETH_0.1_TPSL_TEST"
INSTRUMENT_ID = "ETH-USDT-SWAP"


@dataclass(slots=True)
class ProbeContext:
    live: bool
    client: Any | None
    dry_run_responses: bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Submit real Deepcoin orders.")
    parser.add_argument(
        "--keep-position",
        action="store_true",
        help="Do not attempt to close the market-test position at the end.",
    )
    parser.add_argument(
        "--quantity-eth",
        type=float,
        default=0.1,
        help="Base ETH size to test. Default: 0.1 ETH.",
    )
    parser.add_argument(
        "--limit-price",
        type=float,
        default=1000.0,
        help="Far-away ETH limit-entry price. Default: 1000.",
    )
    args = parser.parse_args()

    if args.live and os.environ.get("DEEPCOIN_LIVE_TPSL_TEST_CONFIRM") != CONFIRM_VALUE:
        print(
            "Live trading blocked. Set "
            f"DEEPCOIN_LIVE_TPSL_TEST_CONFIRM={CONFIRM_VALUE!r} to run real orders.",
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
    quantity_contracts = _quantity_contracts(args.quantity_eth, spec.contract_value)
    if quantity_contracts < spec.min_quantity:
        raise RuntimeError("quantity is below Deepcoin configured minimum")

    client = build_deepcoin_client_from_env() if args.live else None
    ctx = ProbeContext(live=args.live, client=client, dry_run_responses=not args.live)
    current_price = _load_reference_price(ctx, fallback=2500.0)
    market_prices = _long_tpsl_prices(current_price)
    adjusted_market_prices = _long_tpsl_prices(current_price, widen=True)
    limit_initial_prices = {"tp": args.limit_price + 100.0, "sl": args.limit_price - 100.0}
    limit_adjusted_prices = {"tp": args.limit_price + 120.0, "sl": args.limit_price - 80.0}

    print(f"mode={'LIVE' if args.live else 'DRY_RUN'}")
    print(f"instrument={INSTRUMENT_ID} quantity_eth={args.quantity_eth:g} contracts={quantity_contracts:g}")
    print(f"reference_price={current_price:g}")

    market_position_id = run_market_position_tpsl_probe(
        ctx,
        quantity_contracts=quantity_contracts,
        initial_tp=market_prices["tp"],
        initial_sl=market_prices["sl"],
        adjusted_tp=adjusted_market_prices["tp"],
        adjusted_sl=adjusted_market_prices["sl"],
    )
    run_trigger_limit_order_tpsl_probe(
        ctx,
        quantity_contracts=quantity_contracts,
        limit_price=args.limit_price,
        initial_tp=limit_initial_prices["tp"],
        initial_sl=limit_initial_prices["sl"],
        adjusted_tp=limit_adjusted_prices["tp"],
        adjusted_sl=limit_adjusted_prices["sl"],
    )
    if args.live and market_position_id and not args.keep_position:
        print_step("market-cleanup-close-long")
        close_payload = {
            "instId": INSTRUMENT_ID,
            "tdMode": "cross",
            "side": "sell",
            "posSide": "long",
            "ordType": "market",
            "sz": str(quantity_contracts),
            "mrgPosition": "split",
            "closePosId": market_position_id,
        }
        response = submit(ctx, "place_order", close_payload)
        print_json("response", _summarize_response(response))
    elif args.live and market_position_id:
        print("market position left open because --keep-position was supplied")

    return 0


def run_market_position_tpsl_probe(
    ctx: ProbeContext,
    *,
    quantity_contracts: float,
    initial_tp: float,
    initial_sl: float,
    adjusted_tp: float,
    adjusted_sl: float,
) -> str | None:
    print_step("market-entry-eth-long")
    draft = _draft(stop_loss=initial_sl, take_profit=initial_tp)
    leg = {
        "side": "buy",
        "position_side": "long",
        "quantity": quantity_contracts,
        "client_order_id": "TKETHTPSLMKT1",
    }
    order_payload = build_deepcoin_market_order_payload(draft, leg)
    response = submit(ctx, "place_order", order_payload)
    print_json("response", _summarize_response(response))

    pos_id = None
    if ctx.live:
        pos_id = wait_for_position_id(ctx, attempts=10, delay_seconds=0.7)
        if not pos_id:
            raise DeepcoinClientError("market order submitted but ETH long posId was not found")
    else:
        pos_id = "dry-run-pos-eth-long"

    print_step("market-position-add-tpsl")
    protection_payload = build_deepcoin_position_sltp_payload(draft, pos_id=pos_id)
    response = dry_run_position_write(ctx, protection_payload)
    print_json("response", _summarize_response(response))

    print_step("market-position-adjust-tpsl")
    cancel_existing_position_tpsl(ctx, pos_id=pos_id)
    adjusted_payload = build_deepcoin_position_sltp_payload(
        _draft(stop_loss=adjusted_sl, take_profit=adjusted_tp),
        pos_id=pos_id,
    )
    response = dry_run_position_write(ctx, adjusted_payload)
    print_json("response", _summarize_response(response))
    return pos_id


def dry_run_position_write(
    ctx: ProbeContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Keep this diagnostic probe read-only for exact-position mutations."""

    print_json("request", payload)
    if ctx.live:
        raise DeepcoinClientError(
            "live exact-position TPSL writes must use PositionMutationGateway"
        )
    return {"code": "0", "data": [{"ordId": "dry-run-position-tpsl"}]}


def cancel_existing_position_tpsl(ctx: ProbeContext, *, pos_id: str) -> None:
    print_step("market-position-adjust-cancel-existing-tpsl")
    if not ctx.live:
        payload = {"instId": INSTRUMENT_ID, "ordId": "dry-run-existing-tpsl"}
        response = submit(ctx, "cancel_trigger_order", payload)
        print_json("response", _summarize_response(response))
        return

    positions = ctx.client.list_positions(inst_id=INSTRUMENT_ID)
    position = next(
        (
            item
            for item in positions
            if str(item.get("posId") or item.get("pos_id") or item.get("id") or "") == str(pos_id)
        ),
        None,
    )
    if position is None:
        raise DeepcoinClientError(f"position not found before TPSL adjustment: {pos_id}")
    pending_orders = ctx.client.list_trigger_orders_pending(inst_id=INSTRUMENT_ID)
    order_ids = pending_tpsl_order_ids_for_position(
        position=position,
        pending_trigger_orders=pending_orders,
    )
    if not order_ids:
        raise DeepcoinClientError(f"no existing TPSL orders found for position: {pos_id}")
    for order_id in order_ids:
        response = submit(
            ctx,
            "cancel_trigger_order",
            {"instId": INSTRUMENT_ID, "ordId": order_id},
        )
        print_json("response", _summarize_response(response))


def run_trigger_limit_order_tpsl_probe(
    ctx: ProbeContext,
    *,
    quantity_contracts: float,
    limit_price: float,
    initial_tp: float,
    initial_sl: float,
    adjusted_tp: float,
    adjusted_sl: float,
) -> None:
    print_step("trigger-limit-entry-eth-long-with-tpsl")
    draft = _draft(stop_loss=initial_sl, take_profit=initial_tp)
    leg = {
        "side": "buy",
        "position_side": "long",
        "price": limit_price,
        "quantity": quantity_contracts,
        "client_order_id": "TKETHTPSLTRG1",
    }
    order_payload = build_deepcoin_trigger_order_payload(draft, leg)
    response = submit(ctx, "trigger_order", order_payload)
    print_json("response", _summarize_response(response))
    order_id = _extract_order_id(response) or "dry-run-limit-order"

    print_step("trigger-limit-adjust-tpsl-cancel-old")
    response = submit(
        ctx,
        "cancel_trigger_order",
        {"instId": INSTRUMENT_ID, "ordId": order_id},
    )
    print_json("response", _summarize_response(response))

    print_step("trigger-limit-adjust-tpsl-recreate")
    adjusted_draft = _draft(stop_loss=adjusted_sl, take_profit=adjusted_tp)
    adjusted_leg = dict(leg)
    adjusted_leg["client_order_id"] = "TKETHTPSLTRG2"
    adjusted_payload = build_deepcoin_trigger_order_payload(adjusted_draft, adjusted_leg)
    adjusted_response = submit(ctx, "trigger_order", adjusted_payload)
    print_json("response", _summarize_response(adjusted_response))
    adjusted_order_id = _extract_order_id(adjusted_response) or "dry-run-adjusted-limit-order"

    print_step("trigger-limit-cleanup-cancel")
    response = submit(
        ctx,
        "cancel_trigger_order",
        {"instId": INSTRUMENT_ID, "ordId": adjusted_order_id},
    )
    print_json("response", _summarize_response(response))

def run_normal_limit_order_replace_sltp_probe(
    ctx: ProbeContext,
    *,
    quantity_contracts: float,
    limit_price: float,
    initial_tp: float,
    initial_sl: float,
) -> None:
    """Demonstrate the currently unsupported normal-limit replace-order-sltp path."""

    print_step("normal-limit-entry-eth-long-unsupported-replace-sltp")
    draft = _draft(stop_loss=initial_sl, take_profit=initial_tp)
    leg = {
        "side": "buy",
        "position_side": "long",
        "price": limit_price,
        "quantity": quantity_contracts,
        "client_order_id": "TKETHTPSLLMT1",
    }
    order_payload = build_deepcoin_place_order_payload(draft, leg)
    response = submit(ctx, "place_order", order_payload)
    print_json("response", _summarize_response(response))
    order_id = _extract_order_id(response) or "dry-run-limit-order"
    print_step("normal-limit-replace-sltp-expected-to-fail-on-deepcoin")
    sltp_payload = build_deepcoin_order_sltp_payload(draft, order_id=order_id)
    response = submit(ctx, "replace_order_sltp", sltp_payload)
    print_json("response", _summarize_response(response))
    print_step("normal-limit-cleanup-cancel")
    cancel_payload = {
        "instId": INSTRUMENT_ID,
        "mrgPosition": "split",
        "ordId": order_id,
    }
    response = submit(ctx, "cancel_order", cancel_payload)
    print_json("response", _summarize_response(response))


def submit(ctx: ProbeContext, method_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    print_json("request", payload)
    if not ctx.live:
        return {"code": "0", "data": _fake_response_data(method_name, payload)}
    method = getattr(ctx.client, method_name)
    return method(payload)


def wait_for_position_id(
    ctx: ProbeContext,
    *,
    attempts: int,
    delay_seconds: float,
) -> str | None:
    for attempt in range(attempts):
        positions = ctx.client.list_positions(inst_id=INSTRUMENT_ID)
        matches = [
            position
            for position in positions
            if str(position.get("instId") or "").upper() == INSTRUMENT_ID
            and str(position.get("posSide") or "").lower() == "long"
            and _float(position.get("pos") or position.get("size")) > 0
        ]
        if matches:
            matches.sort(
                key=lambda item: int(float(item.get("uTime") or item.get("cTime") or 0)),
                reverse=True,
            )
            pos_id = matches[0].get("posId") or matches[0].get("pos_id") or matches[0].get("id")
            if pos_id:
                return str(pos_id)
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return None


def _load_reference_price(ctx: ProbeContext, *, fallback: float) -> float:
    if not ctx.live:
        return fallback
    price = ctx.client.get_ticker_price(inst_id=INSTRUMENT_ID)
    if price is None:
        raise DeepcoinClientError("ETH ticker price unavailable")
    return price


def _draft(*, stop_loss: float, take_profit: float) -> dict[str, Any]:
    return {
        "instrument_id": INSTRUMENT_ID,
        "margin_mode": "cross",
        "position_mode": "split",
        "stop_loss": round(float(stop_loss), 2),
        "take_profit_legs": [{"price": round(float(take_profit), 2), "allocation_pct": 100.0}],
        "order_legs": [{"position_side": "long"}],
    }


def _long_tpsl_prices(reference_price: float, *, widen: bool = False) -> dict[str, float]:
    tp_pct = 0.012 if not widen else 0.018
    sl_pct = 0.012 if not widen else 0.006
    return {
        "tp": round(reference_price * (1 + tp_pct), 2),
        "sl": round(reference_price * (1 - sl_pct), 2),
    }


def _quantity_contracts(quantity_eth: float, contract_value: float) -> float:
    return round(quantity_eth / contract_value, 8)


def _fake_response_data(method_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if method_name == "place_order":
        return {"ordId": "dry-run-order", "clOrdId": payload.get("clOrdId")}
    if method_name == "trigger_order":
        return {"ordId": "dry-run-trigger-order", "clOrdId": payload.get("clOrdId")}
    if method_name == "set_position_sltp":
        return {"ordId": "dry-run-position-sltp", "posId": payload.get("posId")}
    if method_name == "replace_order_sltp":
        return {"orderSysID": payload.get("orderSysID")}
    if method_name == "cancel_order":
        return {"ordId": payload.get("ordId")}
    if method_name == "cancel_trigger_order":
        return {"ordId": payload.get("ordId")}
    return {}


def _summarize_response(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if isinstance(data, dict):
        return {"code": response.get("code"), "data": data}
    if isinstance(data, list):
        return {"code": response.get("code"), "data_count": len(data)}
    return {"code": response.get("code"), "msg": response.get("msg")}


def _extract_order_id(response: dict[str, Any]) -> str | None:
    payloads = [response]
    data = response.get("data")
    if isinstance(data, dict):
        payloads.append(data)
    for payload in payloads:
        for key in ("ordId", "orderId", "order_id", "orderSysID", "OrderSysID", "id"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return None


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
