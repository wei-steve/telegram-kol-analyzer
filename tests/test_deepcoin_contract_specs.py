from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from threading import Event
from threading import Thread

import pytest

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import (
    RefreshableDeepcoinContractSpecProvider,
)
from telegram_kol_research.deepcoin_contract_specs import load_deepcoin_contract_specs


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _row(instrument_id: str, **overrides: str) -> dict[str, str]:
    row = {
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


def test_load_deepcoin_contract_specs_returns_provider_for_yaml_entries(tmp_path):
    config_path = tmp_path / "deepcoin_contract_specs.yaml"
    config_path.write_text(
        "contracts:\n"
        "  - instrument_id: BTC-USDT-SWAP\n"
        "    contract_value: 0.001\n"
        "    quantity_step: 1\n"
        "    min_quantity: 1\n"
        "    price_tick: 0.1\n",
        encoding="utf-8",
    )

    provider = load_deepcoin_contract_specs(config_path)

    assert provider.get_contract_spec("btc-usdt-swap") == DeepcoinContractSpec(
        instrument_id="BTC-USDT-SWAP",
        contract_value=0.001,
        quantity_step=1,
        min_quantity=1,
        price_tick=0.1,
    )
    assert provider.get_contract_spec("ETH-USDT-SWAP") is None


def test_project_deepcoin_contract_specs_include_verified_btc_and_eth_minimums():
    provider = load_deepcoin_contract_specs(Path("config/deepcoin_contract_specs.yaml"))

    btc = provider.get_contract_spec("BTC-USDT-SWAP")
    eth = provider.get_contract_spec("ETH-USDT-SWAP")

    assert btc == DeepcoinContractSpec(
        instrument_id="BTC-USDT-SWAP",
        contract_value=0.001,
        quantity_step=1,
        min_quantity=1,
        price_tick=0.1,
    )
    assert eth == DeepcoinContractSpec(
        instrument_id="ETH-USDT-SWAP",
        contract_value=0.1,
        quantity_step=0.1,
        min_quantity=0.1,
        price_tick=0.01,
    )


def test_load_deepcoin_contract_specs_allows_missing_optional_file(tmp_path):
    provider = load_deepcoin_contract_specs(
        tmp_path / "missing.yaml",
        required=False,
    )

    assert provider.get_contract_spec("BTC-USDT-SWAP") is None


def test_load_deepcoin_contract_specs_rejects_invalid_numeric_values(tmp_path):
    config_path = tmp_path / "deepcoin_contract_specs.yaml"
    config_path.write_text(
        "contracts:\n"
        "  - instrument_id: BTC-USDT-SWAP\n"
        "    contract_value: 0\n"
        "    quantity_step: 1\n"
        "    min_quantity: 1\n"
        "    price_tick: 0.1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contract_value must be positive"):
        load_deepcoin_contract_specs(config_path)


def test_refreshable_provider_refreshes_publishes_and_exposes_bounded_metadata(tmp_path):
    cache_path = tmp_path / "specs.json"
    provider = RefreshableDeepcoinContractSpecProvider(
        cache_path=cache_path,
        instrument_loader=lambda: [_row("SOL-USDT-SWAP")],
        ttl=timedelta(hours=24),
        now_provider=lambda: NOW,
    )

    assert provider.get_contract_spec("SOL-USDT-SWAP") is None

    assert provider.refresh() is True

    assert provider.get_contract_spec("sol-usdt-swap") == DeepcoinContractSpec(
        instrument_id="SOL-USDT-SWAP",
        contract_value=1,
        quantity_step=1,
        min_quantity=1,
        price_tick=0.001,
    )
    assert provider.metadata.last_success_at == NOW
    assert provider.metadata.expires_at == NOW + timedelta(hours=24)
    assert provider.metadata.last_error is None
    assert cache_path.exists()


def test_refreshable_provider_reloads_after_atomic_cache_replacement(tmp_path):
    from telegram_kol_research.deepcoin_contract_spec_cache import (
        publish_deepcoin_contract_spec_snapshot,
        validate_deepcoin_instrument_snapshot,
    )

    cache_path = tmp_path / "specs.json"
    btc = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=timedelta(hours=24)
    )
    sol = validate_deepcoin_instrument_snapshot(
        [_row("SOL-USDT-SWAP")], fetched_at=NOW, ttl=timedelta(hours=24)
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, btc, now=NOW)
    provider = RefreshableDeepcoinContractSpecProvider(
        cache_path=cache_path,
        instrument_loader=lambda: [],
        ttl=timedelta(hours=24),
        now_provider=lambda: NOW,
    )

    assert provider.get_contract_spec("BTC-USDT-SWAP") is not None
    publish_deepcoin_contract_spec_snapshot(cache_path, sol, now=NOW)

    assert provider.reload() is True
    assert provider.get_contract_spec("BTC-USDT-SWAP") is None
    assert provider.get_contract_spec("SOL-USDT-SWAP") is not None


def test_refreshable_provider_fails_closed_when_reloaded_cache_is_corrupt(tmp_path):
    cache_path = tmp_path / "specs.json"
    provider = RefreshableDeepcoinContractSpecProvider(
        cache_path=cache_path,
        instrument_loader=lambda: [_row("BTC-USDT-SWAP")],
        ttl=timedelta(hours=24),
        now_provider=lambda: NOW,
        max_error_length=40,
    )
    provider.refresh()
    cache_path.write_text("{broken", encoding="utf-8")

    assert provider.reload() is False
    assert provider.get_contract_spec("BTC-USDT-SWAP") is None
    assert provider.metadata.last_success_at == NOW
    assert provider.metadata.expires_at == NOW + timedelta(hours=24)
    assert provider.metadata.last_error is not None
    assert len(provider.metadata.last_error) <= 40


def test_refreshable_provider_coalesces_concurrent_refreshes_with_one_bounded_lock(
    tmp_path,
):
    entered = Event()
    release = Event()
    calls = []

    def load_instruments():
        calls.append("called")
        entered.set()
        assert release.wait(timeout=1)
        return [_row("BTC-USDT-SWAP")]

    provider = RefreshableDeepcoinContractSpecProvider(
        cache_path=tmp_path / "specs.json",
        instrument_loader=load_instruments,
        ttl=timedelta(hours=24),
        now_provider=lambda: NOW,
        refresh_lock_timeout_seconds=0.5,
    )
    results = []
    first = Thread(target=lambda: results.append(provider.refresh()))
    second = Thread(target=lambda: results.append(provider.refresh()))

    first.start()
    assert entered.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert results == [True, True]
    assert calls == ["called"]


def test_refreshable_provider_refresh_failure_keeps_fresh_snapshot_and_bounds_error(
    tmp_path,
):
    responses = [[_row("BTC-USDT-SWAP")], RuntimeError("x" * 1_000)]

    def load_instruments():
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    provider = RefreshableDeepcoinContractSpecProvider(
        cache_path=tmp_path / "specs.json",
        instrument_loader=load_instruments,
        ttl=timedelta(hours=24),
        now_provider=lambda: NOW,
        max_error_length=64,
    )
    assert provider.refresh() is True

    assert provider.refresh() is False

    assert provider.get_contract_spec("BTC-USDT-SWAP") is not None
    assert provider.metadata.last_success_at == NOW
    assert len(provider.metadata.last_error or "") == 64
