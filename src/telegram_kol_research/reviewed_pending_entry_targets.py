"""Canonical reviewed Deepcoin pending-entry targets for one-time cleanup."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryTarget:
    order_id: str
    instrument_id: str
    lifecycle_id: int
    execution_binding_id: int
    execution_order_leg_id: int
    chat_id: int
    message_id: int
    strategy_instance_id: str
    trigger_price: str
    size: str
    embedded_stop_price: str
    request_fingerprint: str


REVIEWED_PENDING_ENTRY_TARGETS = (
    ReviewedPendingEntryTarget(
        order_id="1001124718697641",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=780,
        execution_binding_id=271,
        execution_order_leg_id=479,
        chat_id=-1002370796392,
        message_id=3485,
        strategy_instance_id="deepcoin:-1002370796392:3485:ETH:long",
        trigger_price="1827",
        size="3",
        embedded_stop_price="1795",
        request_fingerprint="7f9f86c10c30936a062984b6a5839b5db293f9dcbd0222d45a85b90c37f06130",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124718698413",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=780,
        execution_binding_id=271,
        execution_order_leg_id=480,
        chat_id=-1002370796392,
        message_id=3485,
        strategy_instance_id="deepcoin:-1002370796392:3485:ETH:long",
        trigger_price="1812",
        size="3",
        embedded_stop_price="1795",
        request_fingerprint="a05cae373185d2b221b47297b23c25cd854affc402310588ed4a19e3f8ffb3e6",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124760022605",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=812,
        execution_binding_id=281,
        execution_order_leg_id=494,
        chat_id=-1002370796392,
        message_id=3507,
        strategy_instance_id="deepcoin:-1002370796392:3507:BTC:long",
        trigger_price="61890",
        size="13",
        embedded_stop_price="60900",
        request_fingerprint="fa3c307a5da05743b1bfc861757bab70713ed0b642699726ff86a8d516d982b0",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124760022650",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=812,
        execution_binding_id=281,
        execution_order_leg_id=495,
        chat_id=-1002370796392,
        message_id=3507,
        strategy_instance_id="deepcoin:-1002370796392:3507:BTC:long",
        trigger_price="61390",
        size="14",
        embedded_stop_price="60900",
        request_fingerprint="ca8806acf87c2b8d34354aea4e0538f71e952196fdf7f443effed7ec4654c401",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124898942178",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=911,
        execution_binding_id=308,
        execution_order_leg_id=532,
        chat_id=-1002409877375,
        message_id=8798,
        strategy_instance_id="deepcoin:-1002409877375:8798:ETH:long",
        trigger_price="2250",
        size="2.3",
        embedded_stop_price="2186",
        request_fingerprint="1f5a6157ee1fbc697c69ba164ff8bfc23f11a0def0916aabfaa5dca62579f99a",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124905627977",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=914,
        execution_binding_id=309,
        execution_order_leg_id=533,
        chat_id=-1003825498321,
        message_id=604,
        strategy_instance_id="deepcoin:-1003825498321:604:BTC:long",
        trigger_price="73690",
        size="8",
        embedded_stop_price="72300",
        request_fingerprint="a1838c649c7b17d2368c71d035719915700c7cd0e759c694c442134c49b787d6",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124905628046",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=914,
        execution_binding_id=309,
        execution_order_leg_id=534,
        chat_id=-1003825498321,
        message_id=604,
        strategy_instance_id="deepcoin:-1003825498321:604:BTC:long",
        trigger_price="73390",
        size="8",
        embedded_stop_price="72300",
        request_fingerprint="a33495361faf3ea1e7a90436a2cd8f6b716d3477a394f4628d2c7a7d47d11786",
    ),
)
