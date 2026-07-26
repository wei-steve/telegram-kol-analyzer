from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.strategy_management_market_decisions import (
    BreakEvenMarketDecisionConflict,
    reserve_break_even_market_decision,
)


NOW = datetime(2026, 7, 26, 15, 0, tzinfo=UTC)


def _persist_batch(session_factory, *, effective_action="break_even_by_market"):
    with session_factory() as session:
        batch = StrategyManagementBatch(
            idempotency_fingerprint="a" * 64,
            raw_message_id=1,
            recognition_decision_id=1,
            recognition_generation="generation-1",
            target_lifecycle_id=1,
            strategy_instance_id="deepcoin:1:1:BTC:short",
            execution_binding_id=1,
            intent="move_stop_to_break_even",
            effective_action=effective_action,
            execution_mode="live",
            requested_fraction=None,
            effective_fraction=None,
            partial_round_before=0,
            status="executing",
            target_fingerprint="b" * 64,
            target_snapshot_json="{}",
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.flush()
        legs = [
            StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=11,
                pos_id="pos-b",
                leg_index=0,
                status="planned",
                preflight_size="16",
                avg_entry_price="64602.9",
                quantity_step="1",
                created_at=NOW,
                updated_at=NOW,
            ),
            StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=12,
                pos_id="pos-a",
                leg_index=1,
                status="planned",
                preflight_size="4",
                avg_entry_price="64700",
                quantity_step="1",
                created_at=NOW,
                updated_at=NOW,
            ),
        ]
        session.add_all(legs)
        session.commit()
        return batch.id, {leg.pos_id: leg.id for leg in legs}


def _decisions(leg_ids):
    return [
        {
            "management_leg_id": leg_ids["pos-b"],
            "execution_order_leg_id": 11,
            "pos_id": "pos-b",
            "side": "short",
            "entry_price": "64602.9",
            "comparison": "entry_below_market",
            "action": "full_exit",
        },
        {
            "management_leg_id": leg_ids["pos-a"],
            "execution_order_leg_id": 12,
            "pos_id": "pos-a",
            "side": "short",
            "entry_price": "64700",
            "comparison": "entry_above_market",
            "action": "set_break_even",
        },
    ]


def test_bootstrap_creates_unique_market_decision_table(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        columns = {
            row[1]
            for row in session.execute(
                text("PRAGMA table_info(strategy_management_market_decisions)")
            ).all()
        }
        indexes = {
            row[1]
            for row in session.execute(
                text("PRAGMA index_list(strategy_management_market_decisions)")
            ).all()
        }

    assert {
        "management_batch_id",
        "instrument_id",
        "quote_price",
        "quote_price_field",
        "observed_at",
        "decisions_json",
        "decision_fingerprint",
    } <= columns
    assert "uq_strategy_management_market_decisions_batch" in indexes


def test_reserve_market_decision_is_sorted_fingerprinted_and_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    batch_id, leg_ids = _persist_batch(session_factory)
    decisions = list(reversed(_decisions(leg_ids)))

    first = reserve_break_even_market_decision(
        session_factory,
        batch_id=batch_id,
        instrument_id="BTC-USDT-SWAP",
        quote_price="64688.6",
        quote_price_field="last",
        observed_at=NOW,
        decisions=decisions,
    )
    second = reserve_break_even_market_decision(
        session_factory,
        batch_id=batch_id,
        instrument_id="BTC-USDT-SWAP",
        quote_price="64688.6",
        quote_price_field="last",
        observed_at=NOW,
        decisions=decisions,
    )

    assert second.id == first.id
    assert first.instrument_id == "BTC-USDT-SWAP"
    assert first.quote_price == "64688.6"
    assert first.quote_price_field == "last"
    assert [row["pos_id"] for row in first.decisions] == ["pos-a", "pos-b"]
    assert len(first.decision_fingerprint) == 64


def test_reserve_market_decision_rejects_conflicting_second_choice(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    batch_id, leg_ids = _persist_batch(session_factory)
    decisions = _decisions(leg_ids)
    reserve_break_even_market_decision(
        session_factory,
        batch_id=batch_id,
        instrument_id="BTC-USDT-SWAP",
        quote_price="64688.6",
        quote_price_field="last",
        observed_at=NOW,
        decisions=decisions,
    )

    changed = [dict(row) for row in decisions]
    changed[0]["action"] = "set_break_even"
    with pytest.raises(BreakEvenMarketDecisionConflict):
        reserve_break_even_market_decision(
            session_factory,
            batch_id=batch_id,
            instrument_id="BTC-USDT-SWAP",
            quote_price="64688.6",
            quote_price_field="last",
            observed_at=NOW,
            decisions=changed,
        )


@pytest.mark.parametrize(
    "effective_action", ["move_stop_to_break_even", "full_exit", "partial_close"]
)
def test_reserve_market_decision_requires_exact_batch_action(
    tmp_path, effective_action
):
    session_factory = create_session_factory(tmp_path / "research.db")
    batch_id, leg_ids = _persist_batch(
        session_factory, effective_action=effective_action
    )

    with pytest.raises(BreakEvenMarketDecisionConflict):
        reserve_break_even_market_decision(
            session_factory,
            batch_id=batch_id,
            instrument_id="BTC-USDT-SWAP",
            quote_price="64688.6",
            quote_price_field="last",
            observed_at=NOW,
            decisions=_decisions(leg_ids),
        )


def test_reserve_market_decision_requires_exact_leg_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    batch_id, leg_ids = _persist_batch(session_factory)
    decisions = _decisions(leg_ids)
    decisions[0]["execution_order_leg_id"] = 999

    with pytest.raises(BreakEvenMarketDecisionConflict):
        reserve_break_even_market_decision(
            session_factory,
            batch_id=batch_id,
            instrument_id="BTC-USDT-SWAP",
            quote_price="64688.6",
            quote_price_field="last",
            observed_at=NOW,
            decisions=decisions,
        )


def test_reserve_market_decision_rejects_action_inconsistent_with_quote(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    batch_id, leg_ids = _persist_batch(session_factory)
    decisions = _decisions(leg_ids)
    decisions[0]["action"] = "set_break_even"

    with pytest.raises(BreakEvenMarketDecisionConflict):
        reserve_break_even_market_decision(
            session_factory,
            batch_id=batch_id,
            instrument_id="BTC-USDT-SWAP",
            quote_price="64688.6",
            quote_price_field="last",
            observed_at=NOW,
            decisions=decisions,
        )
