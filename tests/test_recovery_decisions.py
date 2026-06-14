import sqlite3
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RecoveryDecisionRecord
from telegram_kol_research.recovery_decisions import (
    apply_recovery_review_decision,
    list_recovery_decisions,
    persist_recovery_evaluations,
)
from telegram_kol_research.recovery_scan import (
    RecoveryDecision,
    RecoveryEvaluation,
    RecoverySignal,
)


def _evaluation(**overrides):
    signal_values = {
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 55,
        "posted_at": datetime(2026, 6, 12, 8, 0),
        "symbol": "BTC",
        "side": "long",
        "entry_range": (68000.0, 68200.0),
        "stop_loss_text": "67500",
        "take_profit_text": "69000 / 70000",
        "trading_mode": "auto_trade",
        "max_loss_usdt": 100.0,
    }
    decision_values = {
        "action": "manual_review",
        "reason_codes": ["current_price_in_entry_range"],
        "entry_range": (68000.0, 68200.0),
        "max_loss_usdt": 100.0,
    }
    signal_values.update(overrides.pop("signal", {}))
    decision_values.update(overrides.pop("decision", {}))
    return RecoveryEvaluation(
        signal=RecoverySignal(**signal_values),
        decision=RecoveryDecision(**decision_values),
    )


def test_database_bootstrap_creates_recovery_decisions_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(recovery_decisions)").fetchall()
    }
    conn.close()

    assert "kol_id" in columns
    assert "action" in columns
    assert "reason_codes_json" in columns
    assert "run_at" in columns
    assert "review_status" in columns
    assert "reviewed_at" in columns
    assert "review_note" in columns


def test_persist_recovery_evaluations_upserts_by_strategy_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    run_at = datetime(2026, 6, 12, 18, 0, tzinfo=UTC)

    first = persist_recovery_evaluations(session_factory, [_evaluation()], run_at=run_at)
    second = persist_recovery_evaluations(
        session_factory,
        [
            _evaluation(
                decision={
                    "action": "eligible_for_recovery_limit_order",
                    "reason_codes": ["recovery_checks_passed"],
                }
            )
        ],
        run_at=run_at,
    )

    assert first == {"upserted": 1}
    assert second == {"upserted": 1}
    with session_factory() as session:
        stored = session.query(RecoveryDecisionRecord).one()

    assert stored.action == "eligible_for_recovery_limit_order"
    assert stored.reason_codes_json == '["recovery_checks_passed"]'
    assert stored.entry_range_text == "68000-68200"
    assert stored.stop_loss_text == "67500"


def test_list_recovery_decisions_returns_recent_rows_with_decoded_reason_codes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    persist_recovery_evaluations(
        session_factory,
        [_evaluation()],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )

    rows = list_recovery_decisions(session_factory, limit=10)

    assert rows == [
        {
            "kol_id": "alice",
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
            "action": "manual_review",
            "reason_codes": ["current_price_in_entry_range"],
            "entry_range_text": "68000-68200",
            "stop_loss_text": "67500",
            "max_loss_usdt": 100.0,
            "review_status": "pending",
            "reviewed_at": None,
            "review_note": None,
        }
    ]


def test_apply_recovery_review_decision_records_manual_audit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    persist_recovery_evaluations(
        session_factory,
        [_evaluation(decision={"action": "eligible_for_recovery_limit_order"})],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )

    reviewed = apply_recovery_review_decision(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="btc",
        side="LONG",
        review_status="approved_for_order",
        note="人工确认补挂单",
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )

    assert reviewed["review_status"] == "approved_for_order"
    assert reviewed["review_note"] == "人工确认补挂单"
    assert reviewed["reviewed_at"] == datetime(2026, 6, 12, 19, 0)
    rows = list_recovery_decisions(session_factory, limit=10)
    assert rows[0]["review_status"] == "approved_for_order"
    assert rows[0]["review_note"] == "人工确认补挂单"


def test_apply_recovery_review_decision_rejects_unknown_status(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    persist_recovery_evaluations(
        session_factory,
        [_evaluation()],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )

    try:
        apply_recovery_review_decision(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            review_status="place_order_now",
        )
    except ValueError as exc:
        assert "unsupported recovery review status" in str(exc)
    else:
        raise AssertionError("expected invalid recovery review status to fail")
