from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
    PositionProtectionLeg,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
)
from telegram_kol_research.web_queries import (
    _build_lifecycle_event_timeline,
    list_execution_strategy_overview,
    list_holding_strategies,
    load_home_event_rows,
)


def test_load_home_event_rows_merges_sources_newest_first(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=10,
                    message_id=20,
                    sender_name="Andy",
                    posted_at=datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
                    text="BTC 现价做多",
                ),
                StrategyLifecycle(
                    chat_id=10,
                    message_id=20,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 7, 12, 8, 1, tzinfo=UTC),
                ),
                ExecutionEvent(
                    action="submit_entry_order",
                    status="submitted",
                    kol_id="Andy",
                    chat_id=10,
                    message_id=20,
                    symbol="BTC",
                    side="long",
                    created_at=datetime(2026, 7, 12, 8, 2, tzinfo=UTC),
                ),
            ]
        )
        session.commit()

    rows = load_home_event_rows(session_factory)

    assert [row["kind"] for row in rows] == ["execution", "strategy", "message"]
    assert rows[0].keys() >= {
        "id",
        "kind",
        "occurred_at",
        "source_label",
        "title",
        "summary",
        "symbol",
        "side",
        "status",
        "destination",
    }
    assert rows[0]["destination"]["view"] == "positions"
    assert rows[1]["destination"]["view"] == "strategies"
    assert rows[2]["destination"] == {
        "view": "messages",
        "chat_id": 10,
        "message_id": 20,
    }


def test_load_home_event_rows_filters_kinds_and_applies_limit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=10,
                    message_id=message_id,
                    posted_at=datetime(2026, 7, 12, 8, message_id, tzinfo=UTC),
                    text=f"message {message_id}",
                )
                for message_id in (1, 2, 3)
            ]
        )
        session.commit()

    rows = load_home_event_rows(session_factory, kinds={"message"}, limit=2)

    assert [row["id"] for row in rows] == ["message:10:3", "message:10:2"]
    assert all(row["symbol"] is None and row["side"] is None for row in rows)


def test_lifecycle_timeline_projects_safe_trigger_protection_recovery(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-timeline.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="recovery-timeline",
            kol_id="10",
            chat_id=10,
            message_id=20,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="pos-exact-1",
            status="open",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            execution_binding_id=binding.id,
            chat_id=10,
            message_id=20,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="trigger",
            pos_id="pos-exact-1",
            attribution_status="verified",
            status="filled",
        )
        session.add_all([lifecycle, leg])
        session.flush()
        intent = TriggerProtectionIntent(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            request_fingerprint="b" * 64,
            pre_submit_tpsl_baseline_json='{"raw":"must-not-render"}',
            correlation_id="timeline-1",
            parent_trigger_order_id="parent-1",
            recovery_state="adopted",
            retry_attempts=3,
            adopted_order_id="tpsl-adopted-1",
        )
        session.add(intent)
        session.flush()
        session.add(
            TriggerProtectionStopRescue(
                trigger_protection_intent_id=intent.id,
                execution_binding_id=binding.id + 1,
                execution_order_leg_id=leg.id + 1,
                pos_id="pos-exact-1",
                status="submitted",
                reason_code="rescue_opaque_take_profit_present",
                request_json='{"raw":"must-not-render"}',
            )
        )
        session.commit()

        events = _build_lifecycle_event_timeline(session, lifecycle)

    recovery = next(event for event in events if event["kind"] == "trigger_protection_recovery")
    assert recovery["detail"] == (
        "parent_order_id=parent-1 · pos_id=pos-exact-1 · state=adopted · "
        "attempts=3 · adopted_tpsl_ids=tpsl-adopted-1 · "
        "refusal=- · stop_rescue=none"
    )
    assert "must-not-render" not in str(recovery)


def test_holding_dashboard_surfaces_critical_unprotected_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "dashboard-risk.db")
    started = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:10:20:BTC:long", kol_id="group:10",
            chat_id=10, message_id=20, symbol="BTC", side="long",
            venue="deepcoin", status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            execution_binding_id=binding.id, chat_id=10, message_id=20,
            symbol="BTC", side="long", lifecycle_status="entered",
            signal_at=started, entered_at=started,
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="market", pos_id="pos-risk",
            attribution_status="verified", status="active", last_verified_at=started,
            created_at=started, updated_at=started,
        )
        session.add_all([lifecycle, leg])
        session.flush()
        session.add(PositionProtectionLeg(
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id, role="primary_stop", leg_index=1,
            planned_trigger_price="67500", pos_id="pos-risk", status="planned",
        ))
        session.commit()

    overview = list_execution_strategy_overview(session_factory, status="holding")

    item = overview["items"][0]
    assert item["critical_unprotected"] is True
    assert item["unprotected_positions"] == [{
        "execution_order_leg_id": 1,
        "pos_id": "pos-risk",
        "planned_stop": "67500",
        "exposure_started_at": started.replace(tzinfo=None).isoformat(),
        "rescue_state": "not_planned",
    }]


def test_holding_projection_closes_fallback_lookup_session_connection(tmp_path):
    session_factory = create_session_factory(tmp_path / "holding-pool.db")
    started = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:10:20:BTC:long",
            kol_id="group:10",
            chat_id=10,
            message_id=20,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="pos-pool",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                execution_binding_id=binding.id,
                chat_id=10,
                message_id=20,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=started,
                entered_at=started,
            )
        )
        session.commit()

    rows = list_holding_strategies(session_factory)

    assert [row["pos_id"] for row in rows] == ["pos-pool"]
    assert session_factory.kw["bind"].pool.checkedout() == 0


def test_entered_lifecycle_without_binding_is_labeled_as_price_touch(tmp_path):
    session_factory = create_session_factory(tmp_path / "dashboard-price-touch.db")
    started = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=10,
                message_id=21,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=started,
                entered_at=started,
            )
        )
        session.commit()

    overview = list_execution_strategy_overview(session_factory, status="holding")

    item = overview["items"][0]
    assert item["execution_state"] == "price_touched"
    assert item["execution_state_label"] == "价格触发，未提交交易所订单"
    assert item["price_touched"] is True
    assert item["exchange_execution_verified"] is False
