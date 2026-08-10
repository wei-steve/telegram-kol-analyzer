from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from telegram_kol_research.entry_draft_revisions import (
    EntryDraftRevisionError,
    revise_entry_draft,
    validate_entry_draft_revision,
)


NOW = datetime.now(UTC)


def _chen_two_leg_draft():
    return {
        "venue": "deepcoin",
        "strategy_instance_id": "deepcoin:chen:BTC:long",
        "instrument_id": "BTC-USDT-SWAP",
        "symbol": "BTC",
        "position_side": "long",
        "stop_loss": 63000,
        "take_profit_legs": [
            {"price": 66000, "allocation_pct": 50},
            {"price": 67000, "allocation_pct": 50},
        ],
        "risk_budget_usdt": 20,
        "execution_deadline_at": (NOW + timedelta(hours=1)).isoformat(),
        "order_legs": [
            {
                "order_type": "limit",
                "price": 64000,
                "client_order_id": "CHEN-ENTRY-1",
                "allocation_pct": 50,
                "risk_budget_usdt": 10,
                "quantity": 10,
                "estimated_stop_loss_usdt": 10,
                "side": "buy",
                "position_side": "long",
            },
            {
                "order_type": "limit",
                "price": 63800,
                "client_order_id": "CHEN-ENTRY-2",
                "allocation_pct": 50,
                "risk_budget_usdt": 10,
                "quantity": 12.5,
                "estimated_stop_loss_usdt": 10,
                "side": "buy",
                "position_side": "long",
            },
        ],
    }


def test_market_first_leg_preserves_chen_two_leg_economics():
    original = _chen_two_leg_draft()

    revised = revise_entry_draft(
        original,
        operation="market_first_leg",
        market_price=Decimal("63950"),
        authorized_leg_indices=(1,),
    )

    assert len(revised["order_legs"]) == 2
    assert revised["order_legs"][0]["order_type"] == "market"
    assert revised["order_legs"][1] == original["order_legs"][1]
    assert revised["risk_budget_usdt"] == original["risk_budget_usdt"]
    assert sum(leg["risk_budget_usdt"] for leg in revised["order_legs"]) <= 20
    assert revised["order_legs"][0]["client_order_id"] != (
        original["order_legs"][0]["client_order_id"]
    )
    assert revised["parent_draft_fingerprint"]


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda draft: draft["order_legs"].pop(), "leg_count_changed"),
        (
            lambda draft: draft["order_legs"][1].update(
                client_order_id=draft["order_legs"][0]["client_order_id"]
            ),
            "duplicate_client_order_id",
        ),
        (
            lambda draft: draft["order_legs"][0].update(risk_budget_usdt=11),
            "aggregate_risk_increased",
        ),
        (lambda draft: draft.update(stop_loss=62000), "stop_loss_changed"),
        (
            lambda draft: draft["take_profit_legs"][0].update(price=68000),
            "take_profit_changed",
        ),
    ],
)
def test_revision_validation_rejects_economic_drift(mutate, error):
    original = _chen_two_leg_draft()
    revised = revise_entry_draft(
        original,
        operation="market_first_leg",
        market_price=Decimal("63950"),
        authorized_leg_indices=(1,),
    )
    invalid = deepcopy(revised)
    mutate(invalid)

    with pytest.raises(EntryDraftRevisionError, match=error):
        validate_entry_draft_revision(
            original,
            invalid,
            authorized_leg_indices=(1,),
        )


def test_revision_rejects_second_leg_after_execution_deadline():
    original = _chen_two_leg_draft()
    original["execution_deadline_at"] = (NOW - timedelta(seconds=1)).isoformat()

    with pytest.raises(EntryDraftRevisionError, match="execution_deadline_expired"):
        revise_entry_draft(
            original,
            operation="market_due_legs",
            market_price=Decimal("63950"),
            authorized_leg_indices=(2,),
        )


def test_revision_rejects_missing_execution_deadline():
    original = _chen_two_leg_draft()
    original.pop("execution_deadline_at")

    with pytest.raises(EntryDraftRevisionError, match="execution_deadline_missing"):
        revise_entry_draft(
            original,
            operation="market_first_leg",
            market_price=Decimal("63950"),
            authorized_leg_indices=(1,),
        )


def test_revision_rejects_any_original_unknown_leg():
    original = _chen_two_leg_draft()
    original["order_legs"][1]["execution_outcome"] = "submit_unknown"

    with pytest.raises(EntryDraftRevisionError, match="unknown_leg_outcome"):
        revise_entry_draft(
            original,
            operation="market_first_leg",
            market_price=Decimal("63950"),
            authorized_leg_indices=(1,),
        )


def test_only_explicitly_authorized_legs_may_change():
    original = _chen_two_leg_draft()
    revised = revise_entry_draft(
        original,
        operation="market_first_leg",
        market_price=Decimal("63950"),
        authorized_leg_indices=(1,),
    )
    revised["order_legs"][1]["price"] = 63700

    with pytest.raises(EntryDraftRevisionError, match="unauthorized_leg_changed"):
        validate_entry_draft_revision(
            original,
            revised,
            authorized_leg_indices=(1,),
        )
