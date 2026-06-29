from pathlib import Path

import pytest

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import load_deepcoin_contract_specs


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
