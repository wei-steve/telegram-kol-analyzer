from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_execution_queue import (
    list_recovery_execution_previews,
)
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.trading_settings import save_trading_settings


def _persist_reviewed_recovery_decision(
    session_factory,
    *,
    action,
    review_status,
    symbol="BTC",
    side="long",
    entry_range=(68000.0, 68200.0),
    stop_loss_text="67500",
):
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol=symbol,
                    side=side,
                    entry_range=entry_range,
                    stop_loss_text=stop_loss_text,
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action=action,
                    reason_codes=["recovery_checks_passed"],
                    entry_range=entry_range,
                    max_loss_usdt=100.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol=symbol,
        side=side,
        review_status=review_status,
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )


def test_recovery_execution_preview_lists_approved_limit_orders_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="eligible_for_recovery_limit_order",
        review_status="approved_for_order",
    )

    rows = list_recovery_execution_previews(session_factory)

    assert len(rows) == 1
    row = rows[0]
    assert row["review_status"] == "approved_for_order"
    assert row["execution_status"] == "pending_execution"
    assert row["payload_preview"]["market_leg_threshold"] == "200"
    assert row["payload_preview"]["first_limit_offset"] == "90"
    assert row["payload_preview"]["second_limit_offset"] == "90"
    assert "max_market_entry_deviation_pct" not in row["payload_preview"]
    assert "entry_range_order_style" not in row["payload_preview"]
    assert row["deepcoin_order_draft"]["blocking_reason_codes"] == ["contract_size_unverified"]
    assert [leg["price"] for leg in row["deepcoin_order_draft"]["order_legs"]] == [
        68290.0,
        68090.0,
    ]
    assert [leg["quantity"] for leg in row["deepcoin_order_draft"]["order_legs"]] == [
        0.072464,
        0.072464,
    ]


def test_recovery_execution_preview_uses_eth_fixed_entry_offsets(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="eligible_for_recovery_limit_order",
        review_status="approved_for_order",
        symbol="ETH",
        entry_range=(1500.0, 1600.0),
        stop_loss_text="1400",
    )

    row = list_recovery_execution_previews(session_factory)[0]

    assert row["payload_preview"]["market_leg_threshold"] == "4"
    assert row["payload_preview"]["first_limit_offset"] == "2"
    assert row["payload_preview"]["second_limit_offset"] == "2"
    assert [leg["price"] for leg in row["deepcoin_order_draft"]["order_legs"]] == [
        1602.0,
        1502.0,
    ]


def test_recovery_execution_preview_uses_zero_offsets_for_unconfigured_symbol(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(
        session_factory,
        {
            "allowed_symbols": ["SOL"],
            "symbol_entry_thresholds": {},
        },
    )
    _persist_reviewed_recovery_decision(
        session_factory,
        action="eligible_for_recovery_limit_order",
        review_status="approved_for_order",
        symbol="SOL",
        entry_range=(100.0, 110.0),
        stop_loss_text="90",
    )

    row = list_recovery_execution_previews(session_factory)[0]

    assert row["payload_preview"]["market_leg_threshold"] == "0"
    assert row["payload_preview"]["first_limit_offset"] == "0"
    assert row["payload_preview"]["second_limit_offset"] == "0"
    assert [leg["price"] for leg in row["deepcoin_order_draft"]["order_legs"]] == [
        110.0,
        100.0,
    ]


def test_recovery_execution_preview_ignores_non_approved_or_manual_review_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="manual_review",
        review_status="approved_for_order",
    )

    rows = list_recovery_execution_previews(session_factory)

    assert rows == []


def test_recovery_execution_preview_excludes_already_bound_open_orders(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="eligible_for_recovery_limit_order",
        review_status="approved_for_order",
    )
    upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="alice",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            order_id="existing-order",
            status="open",
        ),
    )

    rows = list_recovery_execution_previews(session_factory)

    assert rows == []


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        assert instrument_id == "BTC-USDT-SWAP"
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


def test_recovery_execution_preview_applies_contract_specs_when_available(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="eligible_for_recovery_limit_order",
        review_status="approved_for_order",
    )

    rows = list_recovery_execution_previews(
        session_factory,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    draft = rows[0]["deepcoin_order_draft"]
    assert rows[0]["contract_spec_status"] == {
        "code": "verified",
        "label": "已应用合约规格",
        "detail": "contracts",
        "quantity_unit": "contracts",
    }
    assert draft["blocking_reason_codes"] == []
    assert draft["contract_spec"]["instrument_id"] == "BTC-USDT-SWAP"
    assert draft["order_legs"][0]["quantity"] == 72.0
    assert draft["order_legs"][0]["quantity_unit"] == "contracts"
    assert draft["order_legs"][1]["quantity"] == 72.0
