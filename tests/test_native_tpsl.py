from decimal import Decimal

from telegram_kol_research.native_tpsl import normalize_native_tpsl


def test_normalize_native_tpsl_accepts_official_position_id_fields():
    order = normalize_native_tpsl(
        {
            "triggerOrderType": "TPSL",
            "OrderSysID": "order-1",
            "PositionID": "pos-1",
            "InstrumentID": "BTC-USDT-SWAP",
            "PosiDirection": "0",
            "Volume": "3",
            "CreateTime": "1720000000000",
            "SLTriggerPrice": "62000",
            "TPTriggerPrice": "68000",
        }
    )

    assert order is not None
    assert order.ord_id == "order-1"
    assert order.pos_id == "pos-1"
    assert order.inst_id == "BTC-USDT-SWAP"
    assert order.size == Decimal("3")
    assert order.created_time == "1720000000000"
    assert order.stop_loss_trigger_price == Decimal("62000")
    assert order.take_profit_trigger_price == Decimal("68000")


def test_normalize_native_tpsl_rejects_official_conditional_row():
    assert (
        normalize_native_tpsl(
            {"triggerOrderType": "Conditional", "OrderSysID": "conditional-1"}
        )
        is None
    )
