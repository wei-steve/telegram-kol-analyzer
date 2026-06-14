from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_live_submit_gate import validate_recovery_live_submit_gate
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


def _confirm_ready(session_factory):
    return confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 6, 12, 20, 0, tzinfo=UTC),
    )


def test_recovery_live_submit_gate_returns_would_submit_when_all_checks_pass(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)
    _confirm_ready(session_factory)

    result = validate_recovery_live_submit_gate(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["would_submit"] is True
    assert result["dry_run_only"] is True
    assert result["reason_codes"] == []
    assert result["checks"] == {
        "ready_confirmation": True,
        "execution_queue_item": True,
        "no_active_binding": True,
        "order_draft_ready": True,
    }
    assert result["deepcoin_order_draft"]["order_legs"][0]["quantity"] == 71.0


def test_recovery_live_submit_gate_blocks_without_ready_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)

    result = validate_recovery_live_submit_gate(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["would_submit"] is False
    assert "missing_ready_confirmation" in result["reason_codes"]
    assert result["checks"]["ready_confirmation"] is False
    assert result["checks"]["execution_queue_item"] is True


def test_recovery_live_submit_gate_blocks_when_binding_exists(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)
    _confirm_ready(session_factory)
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

    result = validate_recovery_live_submit_gate(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["would_submit"] is False
    assert "active_binding_exists" in result["reason_codes"]
    assert "execution_queue_item_not_found" in result["reason_codes"]
    assert result["checks"]["no_active_binding"] is False
    assert result["checks"]["execution_queue_item"] is False


def test_recovery_live_submit_gate_blocks_when_order_draft_is_no_longer_ready(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)
    _confirm_ready(session_factory)

    result = validate_recovery_live_submit_gate(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
    )

    assert result["would_submit"] is False
    assert "contract_size_unverified" in result["reason_codes"]
    assert result["checks"]["ready_confirmation"] is True
    assert result["checks"]["order_draft_ready"] is False
