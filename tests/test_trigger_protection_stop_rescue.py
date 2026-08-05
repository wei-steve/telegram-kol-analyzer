from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from telegram_kol_research.db import create_session_factory, init_db
from telegram_kol_research.execution_events import ExecutionEventRecord, record_execution_event
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
    PositionProtectionIncident,
    PositionProtectionLeg,
    BoundPositionCloseReservation,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.position_protection_legs import create_or_get_protection_leg
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)


class _Client:
    def __init__(self):
        self.calls = []
        self.pending = []

    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "BTC-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "2", "avgPx": "64000", "liqPx": "68000",
            "mgnMode": "cross", "posMode": "split",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

    def set_position_sltp(self, payload):
        self.calls.append(dict(payload))
        self.pending.append({
            "ordId": "rescue-sl-1",
            "instId": payload["instId"],
            "posId": payload["posId"],
            "posSide": payload["posSide"],
            "triggerOrderType": "TPSL",
            "slTriggerPx": payload["slTriggerPx"],
            "slOrdPx": payload["slOrdPx"],
        })
        return {"code": "0", "data": {"ordId": "rescue-sl-1"}}


def _saved_deferred_intent(session_factory, *, verified=True):
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=1, text="entry", posted_at=NOW)
        session.add(raw); session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id, input_kind="text", authoritative_model="mimo",
            authoritative_status="策略", authoritative_payload_json="{}",
            agreement_status="authoritative_only", differences_json="[]",
        )
        lifecycle = StrategyLifecycle(chat_id=1, message_id=1, symbol="BTC", side="short", lifecycle_status="entered", signal_at=NOW)
        session.add_all([decision, lifecycle]); session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:1:BTC:short", kol_id="kol", chat_id=1,
            message_id=1, symbol="BTC", side="short", venue="deepcoin", margin_mode="cross",
            position_mode="split", pos_id="pos-1", status="active", last_exchange_status="positions_verified",
        )
        session.add(binding); session.flush(); lifecycle.execution_binding_id = binding.id
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="trigger_limit", order_id="entry-1",
            pos_id="pos-1", venue="deepcoin", attribution_status=("verified" if verified else "unassigned"),
            attribution_evidence_json='{"policy_version":2}', status="active",
            request_json='{"sz":"2"}',
        )
        session.add(leg); session.flush()
        intent = TriggerProtectionIntent(
            venue="deepcoin", execution_binding_id=binding.id, execution_order_leg_id=leg.id,
            request_fingerprint="a" * 64, pre_submit_tpsl_baseline_json="[]",
            correlation_id="correlation-1", parent_trigger_order_id="entry-1", recovery_state="retrying",
        )
        session.add(intent); session.flush()
        record_execution_event(session_factory, ExecutionEventRecord(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            action="create_trigger_entry", order_id="entry-1", pos_id="pos-1", symbol="BTC", side="short",
            request={"slTriggerPx": "65000", "slTriggerPxType": "last", "slOrdPx": "-1"}, created_at=NOW,
        ), session=session)
        session.commit()
        return intent.id


def test_rescue_submits_stop_only_and_persists_exact_order_before_retry(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue
    from telegram_kol_research.strategy_management_executor import execute_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    with session_factory() as session:
        intent = session.get(TriggerProtectionIntent, intent_id)
        create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(intent.execution_order_leg_id),
            role="primary_stop",
            leg_index=1,
            planned_trigger_price="65000",
            planned_size=None,
        )
        session.commit()
    client = _Client()

    planned = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )
    assert planned.status == "ready"
    result = execute_trigger_protection_stop_rescue(
        session_factory, rescue_id=planned.rescue_id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "verified"
    assert client.calls == [{
        "instType": "SWAP", "instId": "BTC-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "mrgPosition": "split", "tdMode": "cross", "slTriggerPx": "65000",
        "slTriggerPxType": "last", "slOrdPx": "-1",
    }]
    assert not any(key.startswith("tp") for key in client.calls[0])
    retry = execute_trigger_protection_stop_rescue(
        session_factory, rescue_id=planned.rescue_id, deepcoin_client=client, executed_at=NOW
    )
    assert retry["status"] == "verified"
    assert len(client.calls) == 1
    with session_factory() as session:
        primary = session.query(PositionProtectionLeg).one()
        rescue = session.get(TriggerProtectionStopRescue, planned.rescue_id)
    assert primary.status == "verified"
    assert primary.pos_id == "pos-1"
    assert primary.exchange_order_id == "rescue-sl-1"
    assert rescue.status == "verified"


def test_rescue_refuses_unverified_legacy_position_without_exchange_write(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory, verified=False)
    client = _Client()

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "blocked"
    assert result.reason_code == "rescue_position_not_verified"
    assert client.calls == []


def test_rescue_is_noop_when_exact_position_already_has_ledger_managed_stop(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    with session_factory() as session:
        intent = session.get(TriggerProtectionIntent, intent_id)
        leg = session.get(ExecutionOrderLeg, intent.execution_order_leg_id)
        upsert_protection_ledger_row(
            session, venue="deepcoin", execution_binding_id=intent.execution_binding_id,
            execution_order_leg_id=leg.id, strategy_instance_id=leg.strategy_instance_id,
            pos_id="pos-1", instrument_id="BTC-USDT-SWAP", side="short", order_id="managed-sl",
            purpose="stop_loss", trigger_price="65000", size_text=None, status="verified",
            evidence_source="test", evidence={}, seen_at=NOW,
        )
        session.commit()
    client = _Client()

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "noop"
    assert result.reason_code == "rescue_managed_stop_already_present"
    assert client.calls == []


def test_rescue_blocks_opaque_take_profit_without_exchange_write(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    client = _Client()
    client.pending = [{"instId": "BTC-USDT-SWAP", "posId": "pos-1", "tpTriggerPx": "62000"}]

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "blocked"
    assert result.reason_code == "rescue_opaque_take_profit_present"
    assert client.calls == []


def test_rescue_blocks_stale_binding_position_before_any_exchange_write(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    with session_factory() as session:
        intent = session.get(TriggerProtectionIntent, intent_id)
        binding = session.get(ExecutionBinding, intent.execution_binding_id)
        binding.pos_id = "pos-stale"
        session.commit()
    client = _Client()

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "blocked"
    assert result.reason_code == "rescue_binding_position_mismatch"
    assert client.calls == []


def test_rescue_is_noop_when_exchange_already_has_exact_pending_stop(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "pending-stop.db")
    intent_id = _saved_deferred_intent(session_factory)
    client = _Client()
    client.pending = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "short",
            "slTriggerPx": "65000",
            "ordId": "existing-stop",
        }
    ]

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "noop"
    assert result.reason_code == "rescue_exchange_stop_already_present"
    assert client.calls == []


def test_rescue_blocks_when_exact_position_close_is_reserved(tmp_path):
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "close-wins.db")
    intent_id = _saved_deferred_intent(session_factory)
    with session_factory() as session:
        intent = session.get(TriggerProtectionIntent, intent_id)
        session.add(
            BoundPositionCloseReservation(
                pos_id="pos-1",
                execution_binding_id=intent.execution_binding_id,
                status="reserved",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    client = _Client()

    result = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )

    assert result.status == "blocked"
    assert result.reason_code == "rescue_close_in_progress"
    assert client.calls == []


def test_rescue_keeps_exchange_response_when_later_ledger_write_fails(monkeypatch, tmp_path):
    import telegram_kol_research.strategy_management_executor as executor
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    client = _Client()
    planned = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )
    monkeypatch.setattr(
        executor, "upsert_protection_ledger_row", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ledger offline"))
    )

    with pytest.raises(RuntimeError, match="ledger offline"):
        executor.execute_trigger_protection_stop_rescue(
            session_factory, rescue_id=planned.rescue_id, deepcoin_client=client, executed_at=NOW
        )

    with session_factory() as session:
        rescue = session.get(TriggerProtectionStopRescue, planned.rescue_id)
        assert rescue.status == "submitted"
        assert rescue.exchange_order_id == "rescue-sl-1"
        assert json.loads(rescue.response_json) == {"code": "0", "data": {"ordId": "rescue-sl-1"}}
    again = executor.execute_trigger_protection_stop_rescue(
        session_factory, rescue_id=planned.rescue_id, deepcoin_client=client, executed_at=NOW
    )
    assert again["order_id"] == "rescue-sl-1"
    assert len(client.calls) == 1


def test_rescue_persists_bounded_failure_diagnostics(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_trigger_protection_stop_rescue
    from telegram_kol_research.strategy_management_planner import plan_trigger_protection_stop_rescue

    class _FailingClient(_Client):
        def set_position_sltp(self, payload):
            self.calls.append(dict(payload))
            raise RuntimeError("x" * 1000)

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    client = _FailingClient()
    planned = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=client, planned_at=NOW
    )
    result = execute_trigger_protection_stop_rescue(
        session_factory, rescue_id=planned.rescue_id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "submit_unknown"
    with session_factory() as session:
        rescue = session.get(TriggerProtectionStopRescue, planned.rescue_id)
        diagnostics = json.loads(rescue.error_json)
    assert diagnostics["type"] == "DeepcoinRequestOutcomeUnknown"
    assert len(diagnostics["message"]) == 512


@pytest.mark.parametrize(
    ("mode", "auto_trade", "management_mode", "expected"),
    [
        ("disabled", True, "live", "disabled"),
        ("shadow", False, "disabled", "shadow"),
        ("live", True, "live", "live"),
        ("live", False, "live", "disabled"),
        ("live", True, "shadow", "disabled"),
    ],
)
def test_rescue_worker_obeys_separate_mode_and_shadow_never_writes(
    tmp_path,
    mode,
    auto_trade,
    management_mode,
    expected,
):
    from telegram_kol_research.trigger_protection_rescue_worker import (
        run_trigger_protection_rescue_tick,
    )

    session_factory = create_session_factory(tmp_path / f"rescue-worker-{mode}-{expected}.db")
    _saved_deferred_intent(session_factory)
    save_trading_settings(
        session_factory,
        {
            "trigger_protection_stop_rescue_mode": mode,
            "auto_trade_enabled": auto_trade,
            "management_execution_mode": management_mode,
        },
    )
    client = _Client()

    result = run_trigger_protection_rescue_tick(
        session_factory,
        deepcoin_client=client,
        processed_at=NOW,
    )

    assert result.mode == expected
    with session_factory() as session:
        rescues = session.query(TriggerProtectionStopRescue).all()
        incidents = session.query(PositionProtectionIncident).all()
    if expected == "disabled":
        assert result.evaluated == 0
        assert rescues == []
        assert incidents == []
        assert client.calls == []
    elif expected == "shadow":
        assert result.shadow_ready == 1
        assert rescues == []
        assert len(incidents) == 1
        assert incidents[0].incident_type == "stop_rescue_shadow_ready"
        assert client.calls == []
    else:
        assert result.planned == 1
        assert result.executed == 1
        assert len(rescues) == 1
        assert rescues[0].status == "verified"
        assert len(client.calls) == 1


def test_rescue_worker_is_idempotent_after_successful_live_tick(tmp_path):
    from telegram_kol_research.trigger_protection_rescue_worker import (
        run_trigger_protection_rescue_tick,
    )

    session_factory = create_session_factory(tmp_path / "rescue-worker-repeat.db")
    _saved_deferred_intent(session_factory)
    save_trading_settings(
        session_factory,
        {
            "trigger_protection_stop_rescue_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        },
    )
    client = _Client()

    first = run_trigger_protection_rescue_tick(
        session_factory,
        deepcoin_client=client,
        processed_at=NOW,
    )
    second = run_trigger_protection_rescue_tick(
        session_factory,
        deepcoin_client=client,
        processed_at=NOW,
    )

    assert first.executed == 1
    assert second.executed == 0
    assert len(client.calls) == 1
    with session_factory() as session:
        assert session.query(TriggerProtectionStopRescue).count() == 1


def test_rescue_worker_excludes_structured_manual_review_disposition(tmp_path):
    from telegram_kol_research.trigger_protection_rescue_worker import (
        run_trigger_protection_rescue_tick,
    )

    session_factory = create_session_factory(tmp_path / "rescue-worker-manual.db")
    intent_id = _saved_deferred_intent(session_factory)
    with session_factory() as session:
        intent = session.get(TriggerProtectionIntent, intent_id)
        intent.recovery_state = "failed"
        intent.recovery_disposition = "manual_review"
        session.commit()
    save_trading_settings(
        session_factory,
        {"trigger_protection_stop_rescue_mode": "shadow"},
    )

    result = run_trigger_protection_rescue_tick(
        session_factory,
        deepcoin_client=_Client(),
        processed_at=NOW,
    )

    assert result.discovered == 0
    assert result.evaluated == 0


def test_init_db_adds_rescue_error_json_to_prior_sqlite_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-rescue.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE trigger_protection_stop_rescues (
                id INTEGER PRIMARY KEY,
                trigger_protection_intent_id INTEGER NOT NULL,
                execution_binding_id INTEGER NOT NULL,
                execution_order_leg_id INTEGER NOT NULL,
                pos_id VARCHAR(255) NOT NULL,
                status VARCHAR(32) NOT NULL,
                reason_code VARCHAR(96),
                request_json TEXT,
                response_json TEXT,
                exchange_order_id VARCHAR(255),
                planned_at DATETIME NOT NULL,
                reserved_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))

    init_db(engine)

    assert "error_json" in {column["name"] for column in inspect(engine).get_columns("trigger_protection_stop_rescues")}
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO trigger_protection_stop_rescues (
                id, trigger_protection_intent_id, execution_binding_id, execution_order_leg_id,
                pos_id, status, planned_at, created_at, updated_at, error_json
            ) VALUES (1, 1, 1, 1, 'pos-1', 'submit_unknown', :now, :now, :now, :error)
        """), {"now": NOW, "error": '{"type":"RuntimeError"}'})
        assert connection.execute(text("SELECT error_json FROM trigger_protection_stop_rescues WHERE id = 1")).scalar_one() == '{"type":"RuntimeError"}'
