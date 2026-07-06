from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


def _persist_approved_recovery(session_factory):
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol="BTC",
                    side="long",
                    entry_range=(68000.0, 68200.0),
                    stop_loss_text="67500",
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action="eligible_for_recovery_limit_order",
                    reason_codes=["recovery_checks_passed"],
                    entry_range=(68000.0, 68200.0),
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
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )


def test_confirm_recovery_order_dry_run_marks_verified_queue_item_ready(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)

    result = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["ready_for_live_order"] is True
    assert result["dry_run_only"] is True
    assert result["reason_codes"] == []
    assert result["contract_spec_status"]["code"] == "verified"
    assert result["deepcoin_order_draft"]["order_legs"][0]["quantity_unit"] == "contracts"
    assert [leg["quantity"] for leg in result["deepcoin_order_draft"]["order_legs"]] == [
        71.0,
        83.0,
    ]


def test_confirm_recovery_order_dry_run_blocks_without_contract_spec(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)

    result = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
    )

    assert result["ready_for_live_order"] is False
    assert result["reason_codes"] == ["contract_size_unverified"]
    assert result["contract_spec_status"]["code"] == "missing"


def test_confirm_recovery_order_dry_run_raises_when_item_is_not_in_execution_queue(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)
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

    with pytest.raises(LookupError, match="recovery execution item not found"):
        confirm_recovery_order_dry_run(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            contract_spec_provider=_StaticContractSpecProvider(),
        )
