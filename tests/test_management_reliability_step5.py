"""A-5: partial take-profit explanation, stop resize, and the two timeouts.

The incident behind every case here is convergence 222 (2026-09-04): TP1 filled
at 08:34:43Z, the audit could not name the reduction, it froze
``convergence_partial_position_unexplained``, and the stop stayed at ten lots
over a five-lot position. Management batch 158 then correctly refused, landed
in ``recovery_required``, and -- because nothing times that state out -- held
the strategy frozen for three days.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_events import (
    NON_EXCHANGE_WRITING_EXECUTION_ACTIONS,
)
from telegram_kol_research.management_recovery_timeout import (
    POSITION_GONE_REASON,
    RECOVERY_TIMEOUT_REASON,
    expire_stuck_management_recoveries,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    SourceMessageDeletionExit,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TelegramSourceMessageEvent,
)
from telegram_kol_research.partial_take_profit_explanation import (
    PARTIAL_TAKE_PROFIT_FILLED,
    explain_partial_position_reduction,
)
from telegram_kol_research.source_deletion_exit_timeout import (
    POSITION_GONE_REASON as EXIT_POSITION_GONE_REASON,
    ExchangeAbsenceProof,
    build_exchange_absence_reader,
    expire_stuck_source_deletion_exits,
)
from telegram_kol_research.stop_loss_size_convergence import (
    execute_stop_loss_resize,
    plan_stop_loss_resizes,
)
from telegram_kol_research.trigger_take_profit_convergence_executor import (
    _allocate_sizes,
)


NOW = datetime(2026, 9, 4, 8, 35, tzinfo=UTC)


class _TakeProfitRow:
    """The two attributes the judgement reads, and nothing else."""

    def __init__(self, *, order_id, size_text, binding_id=337, pos_id="pos-222"):
        self.order_id = order_id
        self.size_text = size_text
        self.execution_binding_id = binding_id
        self.pos_id = pos_id
        self.status = "active"
        self.trigger_price = "81100"


def _triggered_history(order_id="tp1", pos_id="pos-222"):
    return [
        {
            "ordId": order_id,
            "posId": pos_id,
            "triggerTime": "1757003683000",
            "errorCode": "0",
            "state": "filled",
        }
    ]


def _explain(**overrides):
    kwargs = {
        "execution_binding_id": 337,
        "pos_id": "pos-222",
        "planned_size": Decimal("10"),
        "live_size": Decimal("5"),
        "take_profit_orders": [
            _TakeProfitRow(order_id="tp1", size_text="5"),
            _TakeProfitRow(order_id="tp2", size_text="3"),
            _TakeProfitRow(order_id="tp3", size_text="2"),
        ],
        "trigger_history": _triggered_history(),
    }
    kwargs.update(overrides)
    return explain_partial_position_reduction(**kwargs)


# --------------------------------------------------------------------------
# Task 1: all three criteria, and each one missing on its own
# --------------------------------------------------------------------------


def test_reduction_matching_a_triggered_owned_take_profit_is_explained():
    result = _explain()

    assert result.explained is True
    assert result.reason_code == PARTIAL_TAKE_PROFIT_FILLED
    assert (result.order_id, result.filled_size, result.remaining_size) == (
        "tp1",
        "5",
        "5",
    )
    # The verdict carries the fields it was read from (A-5 task 4).
    assert result.evidence["planned_size"] == "10"
    assert result.evidence["live_size"] == "5"
    assert result.evidence["trigger_history"]["triggerTime"] == "1757003683000"


def test_reduction_of_a_different_size_is_not_explained():
    """Criterion 1 missing: 4 lots gone, no owned order is 4 lots."""

    result = _explain(live_size=Decimal("6"))

    assert result.explained is False
    assert result.reason_code == "partial_reduction_size_matches_no_owned_take_profit"
    assert result.evidence["reduction_size"] == "4"
    assert result.evidence["owned_take_profit_sizes"] == ["5", "3", "2"]


def test_reduction_matching_another_bindings_order_is_not_explained():
    """Criterion 2 missing: right size, wrong ledger."""

    result = _explain(
        take_profit_orders=[
            _TakeProfitRow(order_id="tp1", size_text="5", binding_id=999),
        ]
    )

    assert result.explained is False
    assert result.reason_code == "partial_reduction_size_matches_no_owned_take_profit"


def test_reduction_whose_take_profit_never_triggered_is_not_explained():
    """Criterion 3 missing: the exchange says the order never fired."""

    history = _triggered_history()
    history[0]["triggerTime"] = "0"

    result = _explain(trigger_history=history)

    assert result.explained is False
    assert result.reason_code == "partial_reduction_take_profit_not_triggered"
    assert result.order_id == "tp1"


def test_reduction_whose_take_profit_is_absent_from_history_is_not_explained():
    """A-5c: with no order-history inputs supplied, form (ii) is undecidable.

    The outcome is unchanged -- the reduction stays unexplained -- but the
    reason now names the half of criterion 3 that could not be evaluated.
    """

    result = _explain(trigger_history=[])

    assert result.explained is False
    assert result.reason_code == "partial_reduction_close_evidence_inputs_missing"


def test_reduction_whose_trigger_failed_is_not_explained():
    history = _triggered_history()
    history[0]["errorCode"] = "51004"

    result = _explain(trigger_history=history)

    assert result.explained is False
    assert result.reason_code == "partial_reduction_take_profit_trigger_failed"


def test_two_equally_sized_owned_take_profits_stay_unexplained():
    result = _explain(
        take_profit_orders=[
            _TakeProfitRow(order_id="tp1", size_text="5"),
            _TakeProfitRow(order_id="tp2", size_text="5"),
        ]
    )

    assert result.explained is False
    assert result.reason_code == "partial_reduction_take_profit_ambiguous"
    assert result.evidence["candidate_order_ids"] == ["tp1", "tp2"]
    # A-5c: trigger history naming exactly one of them does NOT break the tie.
    # tp1 is corroborated by trigger history but has an equal-sized sibling, so
    # form (i) may not claim it; tp2 has no evidence of any kind.
    assert result.evidence["per_candidate_reason_codes"] == [
        "partial_reduction_close_evidence_inputs_missing",
        "partial_reduction_take_profit_ambiguous",
    ]


# --------------------------------------------------------------------------
# Task 2: the stop shrinks to the live size, once, with read-back
# --------------------------------------------------------------------------


def _binding_fixture(tmp_path, *, ledger_size="10", explained_size="5"):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                id=337,
                venue="deepcoin",
                strategy_instance_id="strategy-222",
                kol_id=7,
                chat_id=-100,
                message_id=15,
                symbol="BTC",
                side="long",
                status="active",
                pos_id="pos-222",
                margin_mode="cross",
                position_mode="split",
            )
        )
        session.add(
            ExecutionOrderLeg(
                id=555,
                execution_binding_id=337,
                venue="deepcoin",
                purpose="entry",
                leg_index=1,
                status="active",
                # ``order_id == pos_id`` is the ordinary-order identity
                # equation, which is what makes this leg authoritative for a
                # position write (docs/ARCHITECTURE.md section 4.7).
                order_id="pos-222",
                pos_id="pos-222",
                attribution_status="verified",
                strategy_instance_id="strategy-222",
            )
        )
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=337,
                execution_order_leg_id=555,
                strategy_instance_id="strategy-222",
                pos_id="pos-222",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="stop-1",
                purpose="stop_loss",
                trigger_price="78500",
                size_text=ledger_size,
                status="verified",
                evidence_source="test",
            )
        )
        if explained_size is not None:
            session.add(
                PositionTakeProfitOrder(
                    venue="deepcoin",
                    execution_binding_id=337,
                    execution_order_leg_id=555,
                    pos_id="pos-222",
                    order_id="tp1",
                    trigger_price="81100",
                    size_text=explained_size,
                    status="completed",
                    evidence_json=(
                        '{"partial_take_profit_fill":{"order_id":"tp1"}}'
                    ),
                )
            )
        session.commit()
    return session_factory


def test_resize_is_planned_only_for_an_explained_shortfall(tmp_path):
    session_factory = _binding_fixture(tmp_path)

    plans = plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "5"}]
    )

    assert len(plans) == 1
    plan = plans[0]
    assert (plan.current_size, plan.new_size) == ("10", "5")
    assert plan.idempotency_key == "sl-resize:337:pos-222:5"
    assert plan.explained_order_ids == ("tp1",)


def test_resize_is_not_planned_when_the_shortfall_is_unexplained(tmp_path):
    """Three lots gone, only five explained: this is a freeze, not a resize."""

    session_factory = _binding_fixture(tmp_path)

    plans = plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "7"}]
    )

    assert plans == []


def test_resize_is_not_planned_when_the_ledger_already_matches(tmp_path):
    """The idempotency of the whole pass: after a success it plans nothing."""

    session_factory = _binding_fixture(tmp_path, ledger_size="5")

    plans = plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "5"}]
    )

    assert plans == []


def test_resize_is_not_replanned_from_the_pre_resize_ledger_row(tmp_path):
    """``set-position-sltp`` answers with a new order id, so both rows exist.

    Without judging per position, the stale ten-lot row would keep proposing
    the same shrink round after round.
    """

    session_factory = _binding_fixture(tmp_path)
    with session_factory() as session:
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=337,
                execution_order_leg_id=555,
                strategy_instance_id="strategy-222",
                pos_id="pos-222",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="stop-2",
                purpose="stop_loss",
                trigger_price="78500",
                size_text="5",
                status="verified",
                evidence_source="position_mutation_intent_readback",
            )
        )
        session.commit()

    plans = plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "5"}]
    )

    assert plans == []


def test_resize_is_skipped_when_two_verified_stops_disagree(tmp_path):
    session_factory = _binding_fixture(tmp_path)
    with session_factory() as session:
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=337,
                execution_order_leg_id=555,
                strategy_instance_id="strategy-222",
                pos_id="pos-222",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="stop-3",
                purpose="stop_loss",
                trigger_price="78000",
                size_text="8",
                status="verified",
                evidence_source="test",
            )
        )
        session.commit()

    plans = plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "5"}]
    )

    assert plans == []


def test_resize_never_grows_the_stop(tmp_path):
    session_factory = _binding_fixture(tmp_path, ledger_size="4")

    plans = plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "9"}]
    )

    assert plans == []


class _ResizeClient:
    """Accepts one set-position-sltp and reports it back on the pending list."""

    def __init__(self, *, readback: bool = True):
        self.calls: list[dict] = []
        self._readback = readback

    def _set_position_sltp_unchecked(self, payload):
        self.calls.append(dict(payload))
        return {"ordId": "stop-2", "sCode": "0"}

    def set_position_tpsl(self, payload):  # pragma: no cover - alias guard
        return self._set_position_sltp_unchecked(payload)

    def list_trigger_orders_pending(self, *, inst_id):
        if not self._readback:
            return []
        return [
            {
                "ordId": "stop-2",
                "posId": "pos-222",
                "posSide": "long",
                "instId": inst_id,
                "slTriggerPx": "78500",
                "sz": "5",
            }
        ]

    def list_positions(self, *, inst_id=None):
        return [
            {
                "posId": "pos-222",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "5",
                "avgPx": "80000",
                "mgnMode": "cross",
                "mrgPosition": "split",
                "slTriggerPx": "78500",
            }
        ]


def test_resize_submits_the_live_size_and_moves_the_ledger(tmp_path):
    session_factory = _binding_fixture(tmp_path)
    plan = plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "5"}]
    )[0]
    client = _ResizeClient()

    result = execute_stop_loss_resize(
        session_factory,
        plan=plan,
        deepcoin_client=client,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == "succeeded"
    assert len(client.calls) == 1
    call = client.calls[0]
    # Size shrinks; the trigger price is re-sent unchanged and no take-profit
    # field appears anywhere in the payload.
    assert call["sz"] == "5"
    assert call["slTriggerPx"] == "78500"
    assert "tpTriggerPx" not in call
    with session_factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id="stop-2")
            .one()
        )
        assert row.size_text == "5"


def test_resize_readback_mismatch_keeps_the_old_size_and_does_not_retry(tmp_path):
    session_factory = _binding_fixture(tmp_path)
    plan = plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "5"}]
    )[0]
    client = _ResizeClient(readback=False)

    first = execute_stop_loss_resize(
        session_factory,
        plan=plan,
        deepcoin_client=client,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )
    second = execute_stop_loss_resize(
        session_factory,
        plan=plan,
        deepcoin_client=client,
        executed_at=NOW + timedelta(minutes=1),
        live_execution_gate=lambda: True,
    )

    assert first.reason_code == "resize_readback_mismatch"
    assert second.status != "succeeded"
    # The idempotency key is the whole no-retry mechanism: the second attempt
    # reuses the same intent instead of sending a second exchange write.
    assert len(client.calls) == 1
    with session_factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id="stop-1")
            .one()
        )
        assert row.size_text == "10"
        from telegram_kol_research.models import PositionProtectionIncident

        incident = session.query(PositionProtectionIncident).one()
        assert incident.incident_type == "stop_loss_resize_readback_mismatch"


# --------------------------------------------------------------------------
# Task 3: the management batch timeout
# --------------------------------------------------------------------------


def _batch_fixture(tmp_path, *, updated_at, pos_id="pos-222"):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            StrategyManagementBatch(
                id=158,
                idempotency_fingerprint="f" * 64,
                raw_message_id=1,
                recognition_decision_id=1,
                recognition_generation="g1",
                target_lifecycle_id=1074,
                strategy_instance_id="strategy-222",
                execution_binding_id=337,
                intent="reduce",
                effective_action="partial_close",
                status="recovery_required",
                reason_code="final_check_refused",
                target_fingerprint="t" * 64,
                planned_at=updated_at.replace(tzinfo=None),
                updated_at=updated_at.replace(tzinfo=None),
                created_at=updated_at.replace(tzinfo=None),
            )
        )
        session.add(
            StrategyManagementLeg(
                management_batch_id=158,
                execution_order_leg_id=555,
                pos_id=pos_id,
                leg_index=1,
                status="planned",
            )
        )
        session.commit()
    return session_factory


def test_batch_past_the_timeout_is_blocked_and_reported(tmp_path):
    session_factory = _batch_fixture(tmp_path, updated_at=NOW - timedelta(hours=3))
    captured: list[dict] = []

    result = expire_stuck_management_recoveries(
        session_factory,
        now=NOW,
        timeout_minutes=60,
        positions=[{"posId": "pos-222"}],
        capture=lambda **kwargs: captured.append(kwargs),
    )

    assert result.blocked == (158,)
    assert result.resolved == ()
    assert len(captured) == 1
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, 158)
        assert (batch.status, batch.reason_code) == ("blocked", RECOVERY_TIMEOUT_REASON)
        # ``blocked`` is outside the active-batch predicate, so the freeze on
        # this strategy is gone.
        assert batch.status in {"succeeded", "blocked", "resolved"}


def test_batch_whose_position_is_gone_is_resolved_without_an_alert(tmp_path):
    session_factory = _batch_fixture(tmp_path, updated_at=NOW - timedelta(hours=3))
    captured: list[dict] = []

    result = expire_stuck_management_recoveries(
        session_factory,
        now=NOW,
        timeout_minutes=60,
        positions=[{"posId": "someone-else"}],
        capture=lambda **kwargs: captured.append(kwargs),
    )

    assert result.resolved == (158,)
    assert captured == []
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, 158)
        assert (batch.status, batch.reason_code) == ("resolved", POSITION_GONE_REASON)


def test_batch_inside_the_timeout_is_left_alone(tmp_path):
    session_factory = _batch_fixture(tmp_path, updated_at=NOW - timedelta(minutes=10))

    result = expire_stuck_management_recoveries(
        session_factory, now=NOW, timeout_minutes=60, positions=[]
    )

    assert (result.blocked, result.resolved) == ((), ())
    with session_factory() as session:
        assert session.get(StrategyManagementBatch, 158).status == "recovery_required"


def test_unreadable_positions_never_resolve_a_batch(tmp_path):
    """An unreadable snapshot is unknown, not "the position is gone"."""

    session_factory = _batch_fixture(tmp_path, updated_at=NOW - timedelta(hours=3))

    def broken_loader():
        raise RuntimeError("deepcoin down")

    result = expire_stuck_management_recoveries(
        session_factory,
        now=NOW,
        timeout_minutes=60,
        position_loader=broken_loader,
        capture=lambda **kwargs: None,
    )

    assert result.resolved == ()
    assert result.blocked == (158,)


# --------------------------------------------------------------------------
# Task 5: tier count shrinks to what the position can fill
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quantity,expected",
    [
        (1, ["1"]),
        (2, ["1", "1"]),
        (3, ["1", "2"]),
        (5, ["2", "1", "2"]),
    ],
)
def test_small_position_tiering_shrinks_to_allocatable_lots(quantity, expected):
    sizes = _allocate_sizes(
        Decimal(quantity),
        [Decimal("50"), Decimal("30"), Decimal("20")],
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
    )

    assert [str(size) for size in sizes] == expected


# --------------------------------------------------------------------------
# Task 6: no gate, no delivery
# --------------------------------------------------------------------------


def test_all_three_channels_are_silent_until_their_gate_lands(tmp_path):
    import asyncio

    from telegram_kol_research.models import (
        PositionAttributionAudit,
        PositionProtectionIncident,
        StrategyManagementNotification,
    )
    from telegram_kol_research.system_operator_bot import (
        SystemOperatorBotConfig,
        deliver_pending_position_attribution_incidents,
        deliver_pending_position_protection_incidents,
        deliver_strategy_management_notifications,
    )

    session_factory = _binding_fixture(tmp_path)
    with session_factory() as session:
        session.add(
            PositionAttributionAudit(
                venue="deepcoin",
                pos_id="pos-222",
                event_type="attribution_conflict",
                new_state="attribution_conflict",
                fingerprint="a" * 64,
                evidence_json="{}",
                notification_status="pending",
            )
        )
        session.add(
            PositionProtectionIncident(
                venue="deepcoin",
                execution_binding_id=337,
                execution_order_leg_id=555,
                pos_id="pos-222",
                incident_type="protection_missing",
                fingerprint="b" * 64,
                evidence_json="{}",
                delivery_status="pending",
            )
        )
        session.add(
            StrategyManagementNotification(
                management_batch_id=158,
                state="recovery_required",
                payload_fingerprint="c" * 64,
                payload_json='{"batch_id":158}',
                status="pending",
            )
        )
        session.commit()

    config = SystemOperatorBotConfig(bot_token="token", chat_id="chat")

    # Default settings carry no gate for any channel.
    assert asyncio.run(
        deliver_pending_position_attribution_incidents(
            session_factory, config=config
        )
    ) == 0
    assert asyncio.run(
        deliver_pending_position_protection_incidents(session_factory, config=config)
    ) == 0
    assert asyncio.run(
        deliver_strategy_management_notifications(session_factory, config=config)
    ) == 0
    with session_factory() as session:
        assert session.query(PositionAttributionAudit).one().notification_status == (
            "pending"
        )
        assert session.query(PositionProtectionIncident).one().delivery_status == (
            "pending"
        )


def test_a_landed_gate_skips_the_backlog_and_delivers_only_newer_rows(tmp_path):
    import asyncio

    from telegram_kol_research.models import PositionProtectionIncident
    from telegram_kol_research import system_operator_bot as operator_bot_module

    session_factory = _binding_fixture(tmp_path)
    with session_factory() as session:
        for index in range(1, 4):
            session.add(
                PositionProtectionIncident(
                    id=index,
                    venue="deepcoin",
                    execution_binding_id=337,
                    execution_order_leg_id=555,
                    pos_id="pos-222",
                    incident_type="protection_missing",
                    fingerprint=str(index) * 64,
                    evidence_json="{}",
                    delivery_status="pending",
                )
            )
        session.commit()

    sent: list[str] = []

    async def fake_send(**kwargs):
        sent.append(kwargs["text"])

    original = operator_bot_module.send_system_operator_bot_message
    operator_bot_module.send_system_operator_bot_message = fake_send
    try:
        delivered = asyncio.run(
            operator_bot_module.deliver_pending_position_protection_incidents(
                session_factory,
                config=operator_bot_module.SystemOperatorBotConfig("token", "chat"),
                delivery_after_id=2,
            )
        )
    finally:
        operator_bot_module.send_system_operator_bot_message = original

    assert delivered == 1
    assert len(sent) == 1
    with session_factory() as session:
        statuses = {
            row.id: row.delivery_status
            for row in session.query(PositionProtectionIncident).all()
        }
    assert statuses == {1: "pending", 2: "pending", 3: "delivered"}


# --------------------------------------------------------------------------
# Task 7: the stuck source-deletion exit
# --------------------------------------------------------------------------


def _exit_fixture(tmp_path, *, updated_at, binding_id=337):
    session_factory = _binding_fixture(tmp_path)
    with session_factory() as session:
        session.add(
            TelegramSourceMessageEvent(
                id=9001,
                chat_id=-100,
                message_id=15,
                event_type="deleted",
                event_fingerprint="e" * 64,
                occurred_at=updated_at.replace(tzinfo=None),
            )
        )
        session.add(
            SourceMessageDeletionExit(
                id=231,
                source_event_id=9001,
                execution_binding_id=binding_id,
                target_lifecycle_id=1074,
                state="recovery_required",
                last_reason="reconcile_incomplete",
                updated_at=updated_at.replace(tzinfo=None),
                created_at=updated_at.replace(tzinfo=None),
            )
        )
        with session.no_autoflush:
            leg = session.get(ExecutionOrderLeg, 555)
            leg.order_id = "entry-1"
        session.commit()
    return session_factory


def test_stuck_exit_alerts_and_releases_when_the_exchange_is_empty(tmp_path):
    session_factory = _exit_fixture(tmp_path, updated_at=NOW - timedelta(hours=5))
    captured: list[dict] = []

    result = expire_stuck_source_deletion_exits(
        session_factory,
        now=NOW,
        timeout_minutes=120,
        exchange_reader=build_exchange_absence_reader(
            positions_loader=lambda: [],
            resting_orders_loader=lambda: [],
        ),
        capture=lambda **kwargs: captured.append(kwargs),
    )

    assert result.alerted == (231,)
    assert result.released == (231,)
    assert captured[0]["lane_released"] is True
    with session_factory() as session:
        row = session.get(SourceMessageDeletionExit, 231)
        # ``succeeded`` is the only state the deferral barrier stops holding on.
        assert (row.state, row.last_reason) == (
            "succeeded",
            EXIT_POSITION_GONE_REASON,
        )


def test_stuck_exit_with_a_live_position_alerts_but_stays_held(tmp_path):
    session_factory = _exit_fixture(tmp_path, updated_at=NOW - timedelta(hours=5))
    captured: list[dict] = []

    result = expire_stuck_source_deletion_exits(
        session_factory,
        now=NOW,
        timeout_minutes=120,
        exchange_reader=build_exchange_absence_reader(
            positions_loader=lambda: [{"posId": "pos-222", "pos": "5"}],
            resting_orders_loader=lambda: [],
        ),
        capture=lambda **kwargs: captured.append(kwargs),
    )

    assert result.alerted == (231,)
    assert result.released == ()
    assert captured[0]["release_reason"] == "position_still_open"
    with session_factory() as session:
        assert session.get(SourceMessageDeletionExit, 231).state == "recovery_required"


def test_stuck_exit_with_a_resting_order_stays_held(tmp_path):
    session_factory = _exit_fixture(tmp_path, updated_at=NOW - timedelta(hours=5))

    result = expire_stuck_source_deletion_exits(
        session_factory,
        now=NOW,
        timeout_minutes=120,
        exchange_reader=build_exchange_absence_reader(
            positions_loader=lambda: [],
            resting_orders_loader=lambda: [{"ordId": "entry-1"}],
        ),
        capture=lambda **kwargs: None,
    )

    assert (result.alerted, result.released) == ((231,), ())


def test_stuck_exit_is_never_released_without_a_usable_snapshot(tmp_path):
    session_factory = _exit_fixture(tmp_path, updated_at=NOW - timedelta(hours=5))

    def broken():
        raise RuntimeError("deepcoin down")

    result = expire_stuck_source_deletion_exits(
        session_factory,
        now=NOW,
        timeout_minutes=120,
        exchange_reader=build_exchange_absence_reader(
            positions_loader=broken,
            resting_orders_loader=lambda: [],
        ),
        capture=lambda **kwargs: None,
    )

    assert (result.alerted, result.released) == ((231,), ())


def test_exit_inside_the_timeout_is_left_alone(tmp_path):
    session_factory = _exit_fixture(tmp_path, updated_at=NOW - timedelta(minutes=30))

    result = expire_stuck_source_deletion_exits(
        session_factory, now=NOW, timeout_minutes=120
    )

    assert result == type(result)()


def test_absence_reader_refuses_an_exit_with_no_known_position():
    reader = build_exchange_absence_reader(
        positions_loader=lambda: [], resting_orders_loader=lambda: []
    )

    proof = reader((), ())

    assert proof == ExchangeAbsenceProof(False, "exit_has_no_known_position")


# --------------------------------------------------------------------------
# Task 8: the four production events that froze exits 109/128/201/231
# --------------------------------------------------------------------------


PRODUCTION_SKIPPED_EVENT_IDS = (3501, 3589, 3796, 3967)


def _deletion_exit_with_events(tmp_path, events):
    """One unbound exit whose chat/message carries the given events."""

    from telegram_kol_research.source_message_deletion_worker import (
        run_source_message_deletion_worker_tick,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            TelegramSourceMessageEvent(
                id=9002,
                chat_id=-100,
                message_id=15,
                event_type="deleted",
                event_fingerprint="f" * 64,
                occurred_at=NOW.replace(tzinfo=None),
            )
        )
        for row in events:
            session.add(row)
        session.commit()
    return session_factory, run_source_message_deletion_worker_tick


def _skipped_event(event_id: int) -> ExecutionEvent:
    """One of the four production rows, field for field.

    ``auto_trade_skipped`` / ``skipped``, a request payload explaining why the
    system chose not to trade, and NULL order, client-order and position ids.
    """

    return ExecutionEvent(
        id=event_id,
        venue="deepcoin",
        action="auto_trade_skipped",
        status="skipped",
        chat_id=-100,
        message_id=15,
        request_json='{"reason":"auto_trade_disabled_for_chat"}',
        response_json=None,
        order_id=None,
        client_order_id=None,
        pos_id=None,
        created_at=NOW.replace(tzinfo=None),
    )


@pytest.mark.parametrize("event_id", PRODUCTION_SKIPPED_EVENT_IDS)
def test_skipped_auto_trade_event_is_not_a_hazardous_action(event_id, tmp_path):
    from sqlalchemy import or_

    from telegram_kol_research.execution_events import (
        execution_event_has_exchange_identity,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(_skipped_event(event_id))
        session.commit()

    with session_factory() as session:
        hazardous = (
            session.query(ExecutionEvent.id)
            .filter(
                ExecutionEvent.chat_id == -100,
                ExecutionEvent.message_id == 15,
                or_(
                    execution_event_has_exchange_identity(),
                    ExecutionEvent.action.not_in(
                        NON_EXCHANGE_WRITING_EXECUTION_ACTIONS
                    ),
                ),
            )
            .first()
        )

    assert hazardous is None


def test_an_event_carrying_an_exchange_order_id_is_still_hazardous(tmp_path):
    from sqlalchemy import or_

    from telegram_kol_research.execution_events import (
        execution_event_has_exchange_identity,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            ExecutionEvent(
                id=4001,
                venue="deepcoin",
                action="set_position_tpsl",
                status="submitted",
                chat_id=-100,
                message_id=15,
                order_id="1000123",
                created_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()

    with session_factory() as session:
        hazardous = (
            session.query(ExecutionEvent.id)
            .filter(
                ExecutionEvent.chat_id == -100,
                ExecutionEvent.message_id == 15,
                or_(
                    execution_event_has_exchange_identity(),
                    ExecutionEvent.action.not_in(
                        NON_EXCHANGE_WRITING_EXECUTION_ACTIONS
                    ),
                ),
            )
            .first()
        )

    assert hazardous is not None


def test_an_unknown_action_without_identity_stays_hazardous(tmp_path):
    """The whitelist is a denylist on purpose: new actions fail closed."""

    from sqlalchemy import or_

    from telegram_kol_research.execution_events import (
        execution_event_has_exchange_identity,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            ExecutionEvent(
                id=4002,
                venue="deepcoin",
                action="some_future_write_nobody_listed",
                status="submitted",
                chat_id=-100,
                message_id=15,
                created_at=NOW.replace(tzinfo=None),
            )
        )
        session.commit()

    with session_factory() as session:
        hazardous = (
            session.query(ExecutionEvent.id)
            .filter(
                ExecutionEvent.chat_id == -100,
                ExecutionEvent.message_id == 15,
                or_(
                    execution_event_has_exchange_identity(),
                    ExecutionEvent.action.not_in(
                        NON_EXCHANGE_WRITING_EXECUTION_ACTIONS
                    ),
                ),
            )
            .first()
        )

    assert hazardous is not None


# --------------------------------------------------------------------------
# Task 1 + 4, end to end through the convergence audit
# --------------------------------------------------------------------------


def _convergence_fixture(tmp_path):
    """Convergence 222's shape: a ten-lot plan whose TP1 has just triggered."""

    from telegram_kol_research.models import TriggerTakeProfitConvergence

    session_factory = _binding_fixture(tmp_path, explained_size=None)
    with session_factory() as session:
        session.add(
            TriggerTakeProfitConvergence(
                id=222,
                venue="deepcoin",
                execution_binding_id=337,
                execution_order_leg_id=555,
                desired_take_profits_json="[]",
                status="submitted",
                pos_id="pos-222",
            )
        )
        for index, (order_id, size, price) in enumerate(
            (("tp1", "5", "81100"), ("tp2", "3", "82000"), ("tp3", "2", "83000")),
            start=1,
        ):
            session.add(
                PositionTakeProfitOrder(
                    venue="deepcoin",
                    execution_binding_id=337,
                    execution_order_leg_id=555,
                    trigger_take_profit_convergence_id=222,
                    pos_id="pos-222",
                    order_id=order_id,
                    trigger_price=price,
                    size_text=size,
                    status="active",
                    # The ladder is placed before it can fill; form (ii) of
                    # criterion 3 refuses a close that predates its stage.
                    created_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
                )
            )
        session.commit()
    return session_factory


def _reconcile(session_factory, *, trigger_history, live_size="5"):
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    positions = [
        {
            "posId": "pos-222",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "pos": live_size,
        }
    ]
    with session_factory() as session:
        reconcile_trigger_take_profit_order_history(
            session,
            positions=positions,
            pending_orders=[{"ordId": "tp2"}, {"ordId": "tp3"}],
            trigger_history=trigger_history,
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
        )
        session.commit()


def _triggered_but_not_terminal(order_id="tp1"):
    """A triggered order whose ``state`` is not in the terminal vocabulary.

    This is the shape that produced the incident: ``_terminal_order_status``
    reads ``state`` and returns nothing, so the row stayed ``active`` and the
    plan still looked like ten lots.
    """

    return [
        {
            "ordId": order_id,
            "posId": "pos-222",
            "triggerTime": "1757003683000",
            "errorCode": "0",
            "state": "effective",
        }
    ]


def test_convergence_no_longer_freezes_on_its_own_take_profit_fill(tmp_path):
    from telegram_kol_research.models import TriggerTakeProfitConvergence

    session_factory = _convergence_fixture(tmp_path)

    _reconcile(session_factory, trigger_history=_triggered_but_not_terminal())

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, 222)
        assert convergence.status == "submitted"
        assert convergence.reason_code != "convergence_partial_position_unexplained"
        tp1 = (
            session.query(PositionTakeProfitOrder).filter_by(order_id="tp1").one()
        )
        assert tp1.status == "completed"
        evidence = json.loads(tp1.evidence_json)["partial_take_profit_fill"]
        assert evidence["reason_code"] == "partial_take_profit_filled"
        assert evidence["order_id"] == "tp1"
        assert evidence["filled_size"] == "5"
        assert evidence["trigger_history"]["triggerTime"] == "1757003683000"
        # The other two stages are untouched: this step never changes a plan.
        assert {
            row.order_id: row.status
            for row in session.query(PositionTakeProfitOrder).all()
        } == {"tp1": "completed", "tp2": "active", "tp3": "active"}


def test_convergence_is_idempotent_across_repeated_reconciles(tmp_path):
    from telegram_kol_research.models import TriggerTakeProfitConvergence

    session_factory = _convergence_fixture(tmp_path)

    _reconcile(session_factory, trigger_history=_triggered_but_not_terminal())
    _reconcile(session_factory, trigger_history=_triggered_but_not_terminal())

    with session_factory() as session:
        assert session.get(TriggerTakeProfitConvergence, 222).status == "submitted"
        assert (
            session.query(PositionTakeProfitOrder).filter_by(order_id="tp1").one().status
            == "completed"
        )


def test_convergence_still_freezes_on_an_unexplained_reduction(tmp_path):
    """Same position, no trigger evidence at all: the freeze must survive."""

    from telegram_kol_research.models import TriggerTakeProfitConvergence

    session_factory = _convergence_fixture(tmp_path)

    _reconcile(session_factory, trigger_history=[])

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, 222)
        assert (convergence.status, convergence.reason_code) == (
            "conflicted",
            "convergence_partial_position_unexplained",
        )
        # A-5 task 4: the freeze records the fields it judged, not just a verdict.
        error = json.loads(convergence.error_json)["partial_position_unexplained"]
        assert error["reason_code"] == "partial_reduction_close_evidence_inputs_missing"
        assert (error["planned_size"], error["live_size"]) == ("10", "5")
        assert error["reduction_size"] == "5"
        assert error["order_id"] == "tp1"
        assert error["binding_take_profit_order_ids"] == ["tp1", "tp2", "tp3"]


# --------------------------------------------------------------------------
# A-5c: criterion 3 form (ii) -- a filled close from orders-history
# --------------------------------------------------------------------------


def _close_row(**overrides):
    """The shape production actually returned for conv 237's TP1 close."""

    row = {
        "ordId": "close-1",
        "posSide": "short",
        "side": "buy",
        "sz": "5",
        "fillSz": "5",
        "avgPx": "81100",
        "state": "filled",
        "ordType": "market",
        # 2026-09-04T08:34:43Z -- the real trigger instant from convergence 222.
        "cTime": "1788510883000",
    }
    row.update(overrides)
    return row


def _explain_with_close(**overrides):
    stage = _TakeProfitRow(order_id="tp1", size_text="5")
    stage.created_at = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    kwargs = {
        "execution_binding_id": 337,
        "pos_id": "pos-222",
        "planned_size": Decimal("10"),
        "live_size": Decimal("5"),
        "take_profit_orders": [stage],
        # Form (i) has nothing: this is the production reality after
        # 2026-09-08T01:23Z.
        "trigger_history": [],
        "order_history": [_close_row()],
        "position_side": "short",
        "price_tick": Decimal("0.1"),
        "reduction_observed_at_ms": 1788510890000,
    }
    kwargs.update(overrides)
    return explain_partial_position_reduction(**kwargs)


def test_a_matching_filled_close_explains_the_reduction():
    result = _explain_with_close()

    assert result.explained is True
    assert result.order_id == "tp1"
    assert result.evidence["evidence_form"] == "orders_history"
    assert result.evidence["close_order"]["ordId"] == "close-1"
    assert result.evidence["close_order"]["price_delta"] == "0"


def test_a_close_on_the_same_side_as_the_position_is_not_evidence():
    result = _explain_with_close(order_history=[_close_row(side="sell")])

    assert result.explained is False
    assert result.reason_code == "partial_reduction_close_order_missing"


def test_a_close_of_a_different_size_is_not_evidence():
    result = _explain_with_close(order_history=[_close_row(sz="4", fillSz="4")])

    assert result.explained is False
    assert result.reason_code == "partial_reduction_close_order_missing"


def test_an_unfilled_close_is_not_evidence():
    result = _explain_with_close(order_history=[_close_row(state="canceled")])

    assert result.explained is False
    assert result.reason_code == "partial_reduction_close_order_missing"


@pytest.mark.parametrize(
    "avg_price,explained",
    [
        ("81100", True),         # exact
        ("81116.22", True),      # +2 basis points exactly, the boundary
        ("81083.78", True),      # -2 basis points exactly: slippage both ways
        ("81116.23", False),     # a hair past 2 basis points
        ("81200", False),        # plainly a different fill
    ],
)
def test_the_price_tolerance_is_two_basis_points_when_ticks_are_fine(
    avg_price, explained
):
    """A-5c ruling: tolerance is max(2 ticks, 2 basis points).

    Two ticks alone allows 0.008% on an instrument whose tick is 0.01, which
    is tighter than an ordinary market fill. Convergence 237's TP1 filled
    2470.03 against a 2470 trigger -- three ticks, but 1.2 basis points.
    """

    result = _explain_with_close(order_history=[_close_row(avgPx=avg_price)])

    assert result.explained is explained


@pytest.mark.parametrize(
    "avg_price,explained",
    [
        ("100", True),      # exact
        ("102", True),      # +2 ticks, which is far wider than 2 bp here
        ("98", True),       # -2 ticks
        ("102.5", False),   # past both floors
    ],
)
def test_the_tick_floor_wins_when_the_instrument_is_coarse(avg_price, explained):
    """The other half of ``max``: a coarse tick on a cheap instrument.

    Two basis points of 100 is 0.02, far below one tick, so the two-tick floor
    is what applies.
    """

    stage = _TakeProfitRow(order_id="tp1", size_text="5")
    stage.trigger_price = "100"
    stage.created_at = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)

    result = _explain_with_close(
        take_profit_orders=[stage],
        price_tick=Decimal("1"),
        order_history=[_close_row(avgPx=avg_price)],
    )

    assert result.explained is explained


def test_a_close_older_than_the_stage_is_not_evidence():
    """A fill that predates the order cannot be that order's fill."""

    result = _explain_with_close(order_history=[_close_row(cTime="1788500000000")])

    assert result.explained is False
    assert result.reason_code == "partial_reduction_close_order_missing"


def test_a_close_after_the_reduction_was_observed_is_not_evidence():
    result = _explain_with_close(reduction_observed_at_ms=1788510800000)

    assert result.explained is False
    assert result.reason_code == "partial_reduction_close_order_missing"


def test_a_close_already_spent_on_another_stage_is_not_reused():
    result = _explain_with_close(used_close_order_ids=["close-1"])

    assert result.explained is False
    assert result.reason_code == "partial_reduction_close_order_missing"


def test_two_equal_stages_are_separated_by_the_close_price():
    """The one case where equal-sized stages stop being ambiguous."""

    near = _TakeProfitRow(order_id="tp-near", size_text="5")
    near.trigger_price = "81100"
    near.created_at = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    far = _TakeProfitRow(order_id="tp-far", size_text="5")
    far.trigger_price = "82000"
    far.created_at = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)

    result = _explain_with_close(take_profit_orders=[near, far])

    assert result.explained is True
    assert result.order_id == "tp-near"


def test_two_equal_stages_stay_ambiguous_when_the_close_fits_neither():
    near = _TakeProfitRow(order_id="tp-near", size_text="5")
    near.trigger_price = "83000"
    near.created_at = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    far = _TakeProfitRow(order_id="tp-far", size_text="5")
    far.trigger_price = "84000"
    far.created_at = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)

    result = _explain_with_close(take_profit_orders=[near, far])

    assert result.explained is False
    assert result.reason_code == "partial_reduction_take_profit_ambiguous"


def test_form_one_alone_cannot_separate_two_equal_stages():
    """Trigger history is incomplete for recent orders, so absence proves nothing."""

    near = _TakeProfitRow(order_id="tp-near", size_text="5")
    far = _TakeProfitRow(order_id="tp-far", size_text="5")

    result = _explain_with_close(
        take_profit_orders=[near, far],
        trigger_history=_triggered_history(order_id="tp-near"),
        order_history=[],
    )

    assert result.explained is False
    assert result.reason_code == "partial_reduction_take_profit_ambiguous"


def test_a_missing_price_tick_refuses_rather_than_guessing_a_tolerance():
    result = _explain_with_close(price_tick=None)

    assert result.explained is False
    assert result.reason_code == "partial_reduction_close_evidence_inputs_missing"


# --------------------------------------------------------------------------
# A-5c task 3: a frozen convergence over a live position is re-judged
# --------------------------------------------------------------------------


class _SpecProvider:
    def get_contract_spec(self, instrument_id):
        class _Spec:
            price_tick = Decimal("0.1")

        return _Spec()


def _reconcile_5c(session_factory, *, order_history, live_size="5"):
    from telegram_kol_research.position_take_profit_orders import (
        reconcile_trigger_take_profit_order_history,
    )

    positions = [
        {
            "posId": "pos-222",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "pos": live_size,
        }
    ]
    with session_factory() as session:
        reconcile_trigger_take_profit_order_history(
            session,
            positions=positions,
            pending_orders=[{"ordId": "tp2"}, {"ordId": "tp3"}],
            trigger_history=[],
            order_history=order_history,
            observed_at=NOW,
            position_snapshot_complete=True,
            pending_snapshot_complete_by_instrument={"BTC-USDT-SWAP": True},
            contract_spec_provider=_SpecProvider(),
        )
        session.commit()


def _freeze(session_factory):
    from telegram_kol_research.models import TriggerTakeProfitConvergence

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, 222)
        convergence.status = "conflicted"
        convergence.reason_code = "convergence_partial_position_unexplained"
        convergence.completed_at = NOW
        session.commit()


def test_a_frozen_convergence_over_a_live_position_is_re_judged(tmp_path):
    """The A-5b backlog would never have formed if this ran every round."""

    from telegram_kol_research.models import TriggerTakeProfitConvergence

    session_factory = _convergence_fixture(tmp_path)
    _freeze(session_factory)

    _reconcile_5c(
        session_factory,
        order_history=[
            {
                "ordId": "close-1",
                "posSide": "long",
                "side": "sell",
                "sz": "5",
                "fillSz": "5",
                "avgPx": "81100",
                "state": "filled",
                "cTime": "1788510883000",
            }
        ],
    )

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, 222)
        # The freeze is lifted back to the state the online scan writes before
        # it promotes a row; production decides readiness, not this pass.
        assert convergence.status == "waiting_backup_stop"
        assert convergence.reason_code is None
        assert convergence.completed_at is None
        tp1 = session.query(PositionTakeProfitOrder).filter_by(order_id="tp1").one()
        assert tp1.status == "completed"
        evidence = json.loads(tp1.evidence_json)["partial_take_profit_fill"]
        assert evidence["evidence_form"] == "orders_history"
        assert evidence["close_order"]["ordId"] == "close-1"


def test_a_frozen_convergence_without_evidence_stays_frozen(tmp_path):
    from telegram_kol_research.models import TriggerTakeProfitConvergence

    session_factory = _convergence_fixture(tmp_path)
    _freeze(session_factory)

    _reconcile_5c(session_factory, order_history=[])

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, 222)
        assert (convergence.status, convergence.reason_code) == (
            "conflicted",
            "convergence_partial_position_unexplained",
        )
        assert (
            session.query(PositionTakeProfitOrder).filter_by(order_id="tp1").one().status
            == "active"
        )


# --------------------------------------------------------------------------
# A-5c ruling: one reduction may cover several stages, if exactly one set fits
# --------------------------------------------------------------------------


def _stage(order_id, size, trigger_price):
    row = _TakeProfitRow(
        order_id=order_id, size_text=size, binding_id=345, pos_id="pos-237"
    )
    row.trigger_price = trigger_price
    row.created_at = datetime(2026, 9, 8, 3, 11, tzinfo=UTC)
    return row


def _fill(order_id, size, price, when_ms):
    return {
        "ordId": order_id,
        "posSide": "short",
        "side": "buy",
        "sz": size,
        "fillSz": size,
        "avgPx": price,
        "state": "filled",
        "ordType": "market",
        "cTime": str(when_ms),
    }


def test_convergence_237_is_explained_by_both_stages_together():
    """The production case, field for field.

    ETH short: entered 2.1 at 03:11:20Z, TP1 (1 @2470) filled 06:02:29Z at
    2470.03, TP2 (0.6 @2450) filled 13:39:12Z at 2450, and the first look at it
    saw a single 1.6-lot drop. Neither stage equals 1.6; together they do.
    """

    result = explain_partial_position_reduction(
        execution_binding_id=345,
        pos_id="pos-237",
        planned_size=Decimal("2.1"),
        live_size=Decimal("0.5"),
        take_profit_orders=[
            _stage("tp1", "1", "2470"),
            _stage("tp2", "0.6", "2450"),
            _stage("tp3", "0.5", "2430"),
        ],
        trigger_history=[],
        order_history=[
            _fill("1001125181469680", "1", "2470.03", 1788847349000),
            _fill("1001125186263340", "0.6", "2450", 1788874752000),
        ],
        position_side="short",
        price_tick=Decimal("0.01"),
        reduction_observed_at_ms=1788874754000,
    )

    assert result.explained is True
    assert [order_id for order_id, _ in result.explained_orders] == ["tp1", "tp2"]
    assert result.order_id is None
    assert result.filled_size == "1.6"
    assert result.remaining_size == "0.5"
    assert result.evidence["evidence_form"] == "multi_stage"
    assert [stage["close_order"]["ordId"] for stage in result.evidence["stages"]] == [
        "1001125181469680",
        "1001125186263340",
    ]
    # TP3 is still live and untouched by the judgement.
    assert "tp3" not in {order_id for order_id, _ in result.explained_orders}


def test_two_different_sets_summing_to_the_reduction_stay_ambiguous():
    """1 + 0.6 and 1.6 both fit, so nothing is attributed."""

    result = explain_partial_position_reduction(
        execution_binding_id=345,
        pos_id="pos-237",
        planned_size=Decimal("3.2"),
        live_size=Decimal("1.6"),
        take_profit_orders=[
            _stage("tp1", "1", "2470"),
            _stage("tp2", "0.6", "2450"),
            _stage("tp3", "1.6", "2430"),
        ],
        trigger_history=[],
        order_history=[
            _fill("close-a", "1", "2470", 1788847349000),
            _fill("close-b", "0.6", "2450", 1788874752000),
            _fill("close-c", "1.6", "2430", 1788874753000),
        ],
        position_side="short",
        price_tick=Decimal("0.01"),
        reduction_observed_at_ms=1788874754000,
    )

    assert result.explained is False
    assert result.reason_code == "partial_reduction_take_profit_ambiguous"
    assert len(result.evidence["matching_combinations"]) == 2


def test_a_set_is_only_formed_from_stages_that_each_have_their_own_fill():
    """1 + 0.6 sums correctly, but TP2 has no fill of its own."""

    result = explain_partial_position_reduction(
        execution_binding_id=345,
        pos_id="pos-237",
        planned_size=Decimal("2.1"),
        live_size=Decimal("0.5"),
        take_profit_orders=[
            _stage("tp1", "1", "2470"),
            _stage("tp2", "0.6", "2450"),
            _stage("tp3", "0.5", "2430"),
        ],
        trigger_history=[],
        order_history=[_fill("1001125181469680", "1", "2470.03", 1788847349000)],
        position_side="short",
        price_tick=Decimal("0.01"),
        reduction_observed_at_ms=1788874754000,
    )

    assert result.explained is False
    assert result.reason_code == "partial_reduction_size_matches_no_owned_take_profit"


def test_one_fill_cannot_be_spent_on_two_stages_of_the_same_size():
    """Two 0.8-lot stages, one 0.8-lot fill: 1.6 is not explained by it twice."""

    result = explain_partial_position_reduction(
        execution_binding_id=345,
        pos_id="pos-237",
        planned_size=Decimal("2.1"),
        live_size=Decimal("0.5"),
        take_profit_orders=[
            _stage("tp-a", "0.8", "2470"),
            _stage("tp-b", "0.8", "2470"),
            _stage("tp3", "0.5", "2430"),
        ],
        trigger_history=[],
        order_history=[_fill("close-a", "0.8", "2470", 1788847349000)],
        position_side="short",
        price_tick=Decimal("0.01"),
        reduction_observed_at_ms=1788874754000,
    )

    assert result.explained is False
