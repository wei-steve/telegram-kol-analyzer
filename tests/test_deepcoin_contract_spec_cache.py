from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

import pytest

from telegram_kol_research.deepcoin_contract_spec_cache import (
    DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION,
)
from telegram_kol_research.deepcoin_contract_spec_cache import (
    DeepcoinContractSpecSnapshot,
)
from telegram_kol_research.deepcoin_contract_spec_cache import (
    validate_deepcoin_instrument_snapshot,
)
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
TTL = timedelta(hours=24)


def _row(instrument_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "instType": "SWAP",
        "instId": instrument_id,
        "ctVal": "1",
        "lotSz": "1",
        "minSz": "1",
        "tickSz": "0.001",
        "state": "live",
    }
    row.update(overrides)
    return row


def test_validate_builds_immutable_complete_snapshot_with_exact_specs():
    rows = [
        _row("BTC-USDT-SWAP", ctVal="0.001", tickSz="0.1"),
        _row("ETH-USDT-SWAP", ctVal="0.1", lotSz="0.1", minSz="0.1", tickSz="0.01"),
        _row("SOL-USDT-SWAP"),
    ]

    snapshot = validate_deepcoin_instrument_snapshot(rows, fetched_at=NOW, ttl=TTL)

    assert isinstance(snapshot, DeepcoinContractSpecSnapshot)
    assert snapshot.schema_version == DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION
    assert snapshot.venue == "deepcoin"
    assert snapshot.source_path == "/deepcoin/market/instruments?instType=SWAP"
    assert snapshot.fetched_at == NOW
    assert snapshot.expires_at == NOW + TTL
    assert snapshot.specs_by_instrument_id["SOL-USDT-SWAP"] == DeepcoinContractSpec(
        instrument_id="SOL-USDT-SWAP",
        contract_value=1,
        quantity_step=1,
        min_quantity=1,
        price_tick=0.001,
    )
    assert snapshot.states_by_instrument_id == {
        "BTC-USDT-SWAP": "live",
        "ETH-USDT-SWAP": "live",
        "SOL-USDT-SWAP": "live",
    }
    assert len(snapshot.source_digest_sha256) == 64
    with pytest.raises(TypeError):
        snapshot.specs_by_instrument_id["DOGE-USDT-SWAP"] = snapshot.specs_by_instrument_id[
            "SOL-USDT-SWAP"
        ]
    with pytest.raises(FrozenInstanceError):
        snapshot.venue = "other"


def test_validate_retains_non_live_capability_but_excludes_it_from_live_specs():
    snapshot = validate_deepcoin_instrument_snapshot(
        [
            _row("BTC-USDT-SWAP"),
            _row("SOL-USDT-SWAP", state="suspend"),
            _row("DOGE-USDT-SWAP", state="preopen"),
        ],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert set(snapshot.specs_by_instrument_id) == {"BTC-USDT-SWAP"}
    assert snapshot.states_by_instrument_id == {
        "BTC-USDT-SWAP": "live",
        "DOGE-USDT-SWAP": "preopen",
        "SOL-USDT-SWAP": "suspend",
    }


@pytest.mark.parametrize(
    "missing_field",
    ["instType", "instId", "ctVal", "lotSz", "minSz", "tickSz", "state"],
)
def test_validate_rejects_missing_required_fields(missing_field):
    row = _row("BTC-USDT-SWAP")
    del row[missing_field]

    with pytest.raises(ValueError, match=missing_field):
        validate_deepcoin_instrument_snapshot([row], fetched_at=NOW, ttl=TTL)


@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
def test_validate_rejects_boolean_numeric_fields(field):
    with pytest.raises(ValueError, match=field):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: True})],
            fetched_at=NOW,
            ttl=TTL,
        )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
def test_validate_rejects_non_finite_numeric_fields(field, value):
    with pytest.raises(ValueError, match=field):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: value})],
            fetched_at=NOW,
            ttl=TTL,
        )


@pytest.mark.parametrize("value", ["0", "-0.1"])
@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
def test_validate_rejects_non_positive_numeric_fields(field, value):
    with pytest.raises(ValueError, match=field):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: value})],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_rejects_duplicate_instrument_ids_even_when_identical():
    row = _row("BTC-USDT-SWAP")

    with pytest.raises(ValueError, match="duplicate.*BTC-USDT-SWAP"):
        validate_deepcoin_instrument_snapshot([row, dict(row)], fetched_at=NOW, ttl=TTL)


def test_validate_rejects_conflicting_duplicate_instrument_ids():
    with pytest.raises(ValueError, match="duplicate.*BTC-USDT-SWAP"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP"), _row("BTC-USDT-SWAP", lotSz="0.1")],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_rejects_non_swap_product_in_swap_response():
    with pytest.raises(ValueError, match="instType"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", instType="SPOT")],
            fetched_at=NOW,
            ttl=TTL,
        )


@pytest.mark.parametrize(
    "instrument_id",
    ["BTC_USDT_SWAP", "BTC-USDT", "-USDT-SWAP", "btc-usdt-swap", " BTC-USDT-SWAP"],
)
def test_validate_rejects_malformed_instrument_ids(instrument_id):
    with pytest.raises(ValueError, match="instId"):
        validate_deepcoin_instrument_snapshot(
            [_row(instrument_id)],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_ignores_well_formed_non_usdt_swap_instruments():
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP"), _row("BTC-USDC-SWAP")],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert set(snapshot.states_by_instrument_id) == {"BTC-USDT-SWAP"}


def test_validate_rejects_minimum_quantity_incompatible_with_quantity_step():
    with pytest.raises(ValueError, match="minSz.*lotSz"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", lotSz="0.3", minSz="1")],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_uses_decimal_before_converting_to_existing_spec_type():
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", lotSz="0.1", minSz="0.3")],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert snapshot.specs_by_instrument_id["BTC-USDT-SWAP"].min_quantity == 0.3


def test_validate_digest_is_deterministic_across_row_and_key_order():
    btc = _row("BTC-USDT-SWAP", ctVal="0.001")
    sol = _row("SOL-USDT-SWAP")
    reversed_btc = dict(reversed(list(btc.items())))

    first = validate_deepcoin_instrument_snapshot([btc, sol], fetched_at=NOW, ttl=TTL)
    second = validate_deepcoin_instrument_snapshot(
        [sol, reversed_btc], fetched_at=NOW, ttl=TTL
    )

    assert first.source_digest_sha256 == second.source_digest_sha256


def test_validate_high_precision_incompatible_minimum_is_not_rounded_to_multiple():
    with pytest.raises(ValueError, match="minSz.*lotSz"):
        validate_deepcoin_instrument_snapshot(
            [
                _row(
                    "BTC-USDT-SWAP",
                    lotSz="1",
                    minSz="10000000000000000000000000000.1",
                )
            ],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_high_precision_exact_multiple_is_accepted():
    snapshot = validate_deepcoin_instrument_snapshot(
        [
            _row(
                "BTC-USDT-SWAP",
                lotSz="0.12345678901234567890123456789",
                minSz="0.37037036703703703670370370367",
            )
        ],
        fetched_at=NOW,
        ttl=TTL,
    )

    capability = snapshot.capabilities_by_instrument_id["BTC-USDT-SWAP"]
    assert capability.quantity_step == Decimal("0.12345678901234567890123456789")
    assert capability.min_quantity == Decimal("0.37037036703703703670370370367")


def test_validate_high_precision_values_do_not_collide_in_digest():
    first = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", ctVal="1.00000000000000000000000000001")],
        fetched_at=NOW,
        ttl=TTL,
    )
    second = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", ctVal="1.00000000000000000000000000002")],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert first.source_digest_sha256 != second.source_digest_sha256


def test_snapshot_canonical_rows_round_trip_losslessly_through_json():
    snapshot = validate_deepcoin_instrument_snapshot(
        [
            _row(
                "BTC-USDT-SWAP",
                ctVal="1.0000000000000000000000000000100",
                lotSz="0.1234567890123456789012345678900",
                minSz="0.3703703670370370367037037036700",
                tickSz="0.0000000000000000000000000000100",
                state=" SUSPEND ",
            )
        ],
        fetched_at=NOW,
        ttl=TTL,
    )

    serialized_rows = json.loads(json.dumps(snapshot.to_instrument_rows()))
    reloaded = validate_deepcoin_instrument_snapshot(
        serialized_rows,
        fetched_at=snapshot.fetched_at,
        ttl=snapshot.expires_at - snapshot.fetched_at,
        source_path=snapshot.source_path,
    )

    capability = snapshot.capabilities_by_instrument_id["BTC-USDT-SWAP"]
    assert capability.contract_value == Decimal("1.00000000000000000000000000001")
    assert capability.quantity_step == Decimal("0.12345678901234567890123456789")
    assert capability.min_quantity == Decimal("0.37037036703703703670370370367")
    assert capability.price_tick == Decimal("0.00000000000000000000000000001")
    assert reloaded.to_instrument_rows() == snapshot.to_instrument_rows()
    assert reloaded.source_digest_sha256 == snapshot.source_digest_sha256


@pytest.mark.parametrize(
    ("fetched_at", "ttl", "match"),
    [
        (datetime(2026, 8, 8, 12, 0), TTL, "timezone-aware"),
        (NOW, timedelta(0), "ttl"),
        (NOW, timedelta(seconds=-1), "ttl"),
    ],
)
def test_validate_rejects_invalid_snapshot_timing(fetched_at, ttl, match):
    with pytest.raises(ValueError, match=match):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP")],
            fetched_at=fetched_at,
            ttl=ttl,
        )


def test_validate_normalizes_fetched_at_to_utc_before_ttl_across_dst_fallback():
    # Vancouver falls back at 02:00 on 2026-11-01. Two elapsed hours after
    # 00:30 PDT is 01:30 PST, not 02:30 PST.
    local_fetched_at = datetime(
        2026, 11, 1, 0, 30, tzinfo=ZoneInfo("America/Vancouver")
    )

    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")],
        fetched_at=local_fetched_at,
        ttl=timedelta(hours=2),
    )

    assert snapshot.fetched_at == datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)
    assert snapshot.fetched_at.tzinfo is timezone.utc
    assert snapshot.expires_at == datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)
    assert snapshot.expires_at.tzinfo is timezone.utc


def test_validate_rejects_empty_or_non_list_snapshot():
    with pytest.raises(ValueError, match="non-empty list"):
        validate_deepcoin_instrument_snapshot([], fetched_at=NOW, ttl=TTL)

    with pytest.raises(ValueError, match="non-empty list"):
        validate_deepcoin_instrument_snapshot("not rows", fetched_at=NOW, ttl=TTL)


def test_validate_rejects_non_mapping_row():
    with pytest.raises(ValueError, match="row 0"):
        validate_deepcoin_instrument_snapshot(["not a mapping"], fetched_at=NOW, ttl=TTL)


def test_validate_rejects_malformed_non_live_required_row():
    with pytest.raises(ValueError, match="tickSz"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP"), _row("SOL-USDT-SWAP", state="suspend", tickSz="bad")],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_rejects_non_string_or_empty_state():
    for state in (None, "", "   ", True):
        with pytest.raises(ValueError, match="state"):
            validate_deepcoin_instrument_snapshot(
                [_row("BTC-USDT-SWAP", state=state)],
                fetched_at=NOW,
                ttl=TTL,
            )


def test_validate_normalizes_state_case_and_whitespace():
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", state=" LIVE ")],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert snapshot.states_by_instrument_id["BTC-USDT-SWAP"] == "live"
    assert "BTC-USDT-SWAP" in snapshot.specs_by_instrument_id


def test_validate_accepts_decimal_numeric_inputs_without_float_rounding():
    snapshot = validate_deepcoin_instrument_snapshot(
        [
            _row(
                "BTC-USDT-SWAP",
                ctVal=Decimal("0.001"),
                lotSz=Decimal("0.1"),
                minSz=Decimal("0.3"),
                tickSz=Decimal("0.01"),
            )
        ],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert snapshot.specs_by_instrument_id["BTC-USDT-SWAP"].quantity_step == 0.1


@pytest.mark.parametrize("value", ["1e10000", "1e-10000"])
def test_validate_rejects_value_that_cannot_convert_to_a_positive_finite_existing_spec(value):
    with pytest.raises(ValueError, match="ctVal"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", ctVal=value)],
            fetched_at=NOW,
            ttl=TTL,
        )
