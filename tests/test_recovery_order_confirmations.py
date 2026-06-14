import sqlite3
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_order_confirmations import (
    has_ready_recovery_order_confirmation,
    list_recovery_order_confirmations,
)
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


def test_database_bootstrap_creates_recovery_order_confirmations_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(recovery_order_confirmations)").fetchall()
    }
    conn.close()

    assert "chat_id" in columns
    assert "message_id" in columns
    assert "status" in columns
    assert "confirmation_payload_json" in columns


def test_confirm_recovery_order_dry_run_records_ready_confirmation_when_requested(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)

    result = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 6, 12, 20, 0, tzinfo=UTC),
    )

    assert result["ready_for_live_order"] is True
    assert result["ready_confirmation"]["status"] == "ready_confirmed"
    assert has_ready_recovery_order_confirmation(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
    )
    rows = list_recovery_order_confirmations(session_factory)
    assert len(rows) == 1
    assert rows[0]["status"] == "ready_confirmed"
    assert rows[0]["deepcoin_order_draft"]["order_legs"][0]["quantity"] == 71.0


def test_confirm_recovery_order_dry_run_does_not_record_when_blocked(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)

    result = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        persist_ready_confirmation=True,
    )

    assert result["ready_for_live_order"] is False
    assert result["ready_confirmation"] is None
    assert list_recovery_order_confirmations(session_factory) == []


def test_confirm_recovery_order_dry_run_updates_existing_ready_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)

    first = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 6, 12, 20, 0, tzinfo=UTC),
    )
    second = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 6, 12, 20, 5, tzinfo=UTC),
    )

    assert first["ready_confirmation"]["id"] == second["ready_confirmation"]["id"]
    rows = list_recovery_order_confirmations(session_factory)
    assert len(rows) == 1
    assert rows[0]["confirmed_at"].isoformat() == "2026-06-12T20:05:00"
