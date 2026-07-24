import pytest

from telegram_kol_research.trigger_backup_stop import BackupStopError
from telegram_kol_research.trigger_backup_stop import build_backup_stop_trigger_payload
from telegram_kol_research.trigger_backup_stop import calculate_backup_stop_price
from telegram_kol_research.trading_settings import TradingSettings
from telegram_kol_research.trading_settings import trading_settings_from_payload


@pytest.mark.parametrize(
    ("side", "expected"), [("long", "1909.4"), ("short", "1928.6")]
)
def test_calculate_backup_stop_price_applies_50_bps_buffer_and_rounds_away_from_risk(side, expected):
    assert calculate_backup_stop_price(
        primary_stop="1919", side=side, price_tick="0.1", buffer_bps=50
    ) == expected


def test_calculate_backup_stop_price_rejects_invalid_values_and_wrong_risk_side():
    with pytest.raises(BackupStopError, match="side"):
        calculate_backup_stop_price(primary_stop="1919", side="flat", price_tick="0.1")
    with pytest.raises(BackupStopError, match="positive"):
        calculate_backup_stop_price(primary_stop="0", side="long", price_tick="0.1")
    with pytest.raises(BackupStopError, match="risk side"):
        build_backup_stop_trigger_payload(
            instrument_id="ETH-USDT-SWAP",
            side="long",
            margin_mode="cross",
            pos_id="pos-1",
            primary_stop="1919",
            backup_stop="1920",
            liquidation_price="1800",
            client_order_id="TKBACKUP1",
        )


def test_build_backup_stop_trigger_payload_requires_safe_liquidation_boundary():
    with pytest.raises(BackupStopError, match="liquidation"):
        build_backup_stop_trigger_payload(
            instrument_id="ETH-USDT-SWAP",
            side="long",
            margin_mode="cross",
            pos_id="pos-1",
            primary_stop="1919",
            backup_stop="1909.4",
            liquidation_price="1910",
            client_order_id="TKBACKUP1",
        )


def test_build_backup_stop_trigger_payload_closes_only_the_exact_split_position_at_market():
    payload = build_backup_stop_trigger_payload(
        instrument_id="ETH-USDT-SWAP",
        side="short",
        margin_mode="cross",
        pos_id="pos-1",
        primary_stop="1919",
        backup_stop="1928.6",
        liquidation_price="2000",
        client_order_id="TKBACKUP1",
    )

    assert payload == {
        "instType": "SWAP",
        "instId": "ETH-USDT-SWAP",
        "side": "buy",
        "posSide": "short",
        "mrgPosition": "split",
        "tdMode": "cross",
        "closePosId": "pos-1",
        "ordType": "market",
        "triggerPx": "1928.6",
        "triggerPxType": "last",
        "ordPx": "-1",
        "clOrdId": "TKBACKUP1",
    }


def test_backup_stop_buffer_settings_default_to_50_bps_and_validate_positive_values():
    assert TradingSettings().trigger_backup_stop_buffer_bps == 50
    assert trading_settings_from_payload({"trigger_backup_stop_buffer_bps": "25"}).trigger_backup_stop_buffer_bps == 25
    assert trading_settings_from_payload({"trigger_backup_stop_buffer_bps": 0}).trigger_backup_stop_buffer_bps == 50
