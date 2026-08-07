from decimal import Decimal

import pytest


def test_revision_risk_below_target_uses_only_remaining_headroom():
    from telegram_kol_research.entry_revision_risk import assess_revision_risk

    result = assess_revision_risk(
        quantity="0.006",
        average_entry="64000",
        stop_loss="65000",
        contract_value="1",
        side="short",
        target_risk_usdt="10.2",
        quantity_step="0.001",
    )

    assert result.action == "retain_and_use_headroom"
    assert result.filled_risk_usdt == Decimal("6")
    assert result.remaining_risk_usdt == Decimal("4.2")
    assert result.reduce_quantity == Decimal("0")


def test_revision_risk_at_target_rebuilds_no_new_exposure():
    from telegram_kol_research.entry_revision_risk import assess_revision_risk

    result = assess_revision_risk(
        quantity="0.01",
        average_entry="64000",
        stop_loss="65000",
        contract_value="1",
        side="short",
        target_risk_usdt="10",
        quantity_step="0.001",
    )

    assert result.action == "retain_at_target"
    assert result.remaining_risk_usdt == Decimal("0")


def test_revision_risk_over_target_rounds_remaining_position_down():
    from telegram_kol_research.entry_revision_risk import assess_revision_risk

    result = assess_revision_risk(
        quantity="0.012",
        average_entry="64000",
        stop_loss="65000",
        contract_value="1",
        side="short",
        target_risk_usdt="10.5",
        quantity_step="0.001",
    )

    assert result.action == "reduce_to_target"
    assert result.target_quantity == Decimal("0.010")
    assert result.reduce_quantity == Decimal("0.002")


def test_revision_risk_rejects_wrong_side_stop():
    from telegram_kol_research.entry_revision_risk import (
        EntryRevisionRiskError,
        assess_revision_risk,
    )

    with pytest.raises(EntryRevisionRiskError, match="entry_revision_stop_side_invalid"):
        assess_revision_risk(
            quantity="0.01",
            average_entry="64000",
            stop_loss="63000",
            contract_value="1",
            side="short",
            target_risk_usdt="10",
            quantity_step="0.001",
        )
