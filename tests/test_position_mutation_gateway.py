from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionLedger,
)
from telegram_kol_research.position_mutation_authority import (
    PositionMutationAuthority,
)
from telegram_kol_research.position_mutation_gateway import (
    PositionMutationGateway,
    position_authority_fingerprint,
    reconcile_submitted_position_mutation_intents,
    submit_exact_position_sltp,
)
from telegram_kol_research.position_mutation_intents import (
    reserve_position_mutation_intent,
    transition_position_mutation_intent,
)


NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)
SISTER_POSITION = {
    "posId": "pos-sister",
    "instId": "BTC-USDT-SWAP",
    "posSide": "long",
    "pos": "10",
    "avgPx": "63895.725",
    "mgnMode": "cross",
    "mrgPosition": "split",
    "slTriggerPx": "",
}
OTHER_POSITION = {
    "posId": "pos-other",
    "instId": "BTC-USDT-SWAP",
    "posSide": "long",
    "pos": "2",
    "avgPx": "63900",
    "mgnMode": "cross",
    "mrgPosition": "split",
    # The incident showed this foreign/aggregate value on another position.
    "slTriggerPx": "63895.725",
}


class FakeDeepcoinClient:
    def __init__(self):
        self.cancel_position_sltp_calls = []
        self.set_position_sltp_calls = []
        self.place_order_calls = []
        self.positions = [SISTER_POSITION, OTHER_POSITION]

    def list_positions(self, *, inst_id=None):
        return list(self.positions)

    def cancel_position_sltp(self, payload):
        self.cancel_position_sltp_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": payload["ordId"]}}

    def set_position_sltp(self, payload):
        self.set_position_sltp_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": "ord-new-stop"}}

    def place_order(self, payload):
        self.place_order_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": "ord-close"}}


def _seed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        sister_binding = ExecutionBinding(
            strategy_instance_id="strategy-sister",
            kol_id="sister",
            chat_id=-1001,
            message_id=1136,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        other_binding = ExecutionBinding(
            strategy_instance_id="strategy-other",
            kol_id="other",
            chat_id=-1002,
            message_id=1466,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add_all([sister_binding, other_binding])
        session.flush()
        sister_leg = ExecutionOrderLeg(
            execution_binding_id=sister_binding.id,
            strategy_instance_id="strategy-sister",
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            pos_id="pos-sister",
            venue="deepcoin",
            attribution_status="verified",
            response_json='{"data":{"posId":"pos-sister"}}',
            status="active",
        )
        other_leg = ExecutionOrderLeg(
            execution_binding_id=other_binding.id,
            strategy_instance_id="strategy-other",
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            pos_id="pos-other",
            venue="deepcoin",
            attribution_status="verified",
            response_json='{"data":{"posId":"pos-other"}}',
            status="active",
        )
        session.add_all([sister_leg, other_leg])
        session.flush()
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=sister_binding.id,
                execution_order_leg_id=sister_leg.id,
                strategy_instance_id="strategy-sister",
                pos_id="pos-sister",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="ord-sister-stop",
                purpose="stop_loss",
                trigger_price="63895.725",
                size_text="0",
                status="verified",
                evidence_source="management_tpsl_replacement",
                evidence_json="{}",
            )
        )
        session.commit()
        return (
            session_factory,
            sister_binding.id,
            sister_leg.id,
            other_binding.id,
            other_leg.id,
        )


def _authority(*, binding_id, leg_id, position, strategy):
    return PositionMutationAuthority(
        venue="deepcoin",
        strategy_instance_id=strategy,
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id=position["posId"],
        instrument_id=position["instId"],
        side=position["posSide"],
        position_fingerprint=position_authority_fingerprint(position),
    )


def test_foreign_stop_is_blocked_before_exchange_call(tmp_path):
    session_factory, _, _, other_binding_id, other_leg_id = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    result = gateway.cancel_owned_position_sltp(
        authority=_authority(
            binding_id=other_binding_id,
            leg_id=other_leg_id,
            position=OTHER_POSITION,
            strategy="strategy-other",
        ),
        order_id="ord-sister-stop",
        idempotency_key="management:71:55:cancel:ord-sister-stop",
    )

    assert result.status == "blocked"
    assert result.reason == "order_owner_mismatch"
    assert client.cancel_position_sltp_calls == []
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "blocked"


def test_exact_owner_cancellation_is_submitted_once(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )
    authority = _authority(
        binding_id=binding_id,
        leg_id=leg_id,
        position=SISTER_POSITION,
        strategy="strategy-sister",
    )

    first = gateway.cancel_owned_position_sltp(
        authority=authority,
        order_id="ord-sister-stop",
        idempotency_key="management:65:49:cancel:ord-sister-stop",
    )
    second = gateway.cancel_owned_position_sltp(
        authority=authority,
        order_id="ord-sister-stop",
        idempotency_key="management:65:49:cancel:ord-sister-stop",
    )

    assert first.status == "submitted"
    assert second.status == "submitted"
    assert client.cancel_position_sltp_calls == [
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "ordId": "ord-sister-stop",
        }
    ]


def test_cancel_and_close_intents_confirm_from_terminal_snapshots(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )
    authority = _authority(
        binding_id=binding_id,
        leg_id=leg_id,
        position=SISTER_POSITION,
        strategy="strategy-sister",
    )
    cancelled = gateway.cancel_owned_position_sltp(
        authority=authority,
        order_id="ord-sister-stop",
        idempotency_key="management:65:49:cancel:reconcile",
    )
    closed = gateway.close_exact_position(
        authority=authority,
        size="10",
        client_order_id="close-reconcile",
        idempotency_key="management:65:49:close:reconcile",
    )
    with session_factory() as session:
        close_intent = session.get(
            PositionMutationIntent, closed.intent_id
        )
        close_intent.status = "recovery_required"
        close_intent.response_json = None
        session.commit()

    assert (
        reconcile_submitted_position_mutation_intents(
            session_factory,
            pending_trigger_orders=None,
            order_history=[],
            trade_fills=[],
            reconciled_at=NOW,
        )
        == 0
    )
    with session_factory() as session:
        assert session.get(
            PositionMutationIntent, cancelled.intent_id
        ).status == "submitted"

    confirmed = reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=[],
        order_history=[
            {
                "ordId": "ord-close",
                "clOrdId": "close-reconcile",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "closePosId": "pos-sister",
                "sz": "10.0",
                "status": "filled",
            }
        ],
        trade_fills=[
            {
                "ordId": "ord-close",
                "clOrdId": "close-reconcile",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "closePosId": "pos-sister",
                "sz": "10.0",
            }
        ],
        reconciled_at=NOW,
    )

    assert confirmed == 2
    with session_factory() as session:
        assert session.get(
            PositionMutationIntent, cancelled.intent_id
        ).status == "confirmed"
        assert session.get(
            PositionMutationIntent, closed.intent_id
        ).status == "confirmed"
        assert (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.order_id == "ord-sister-stop"
            )
            .one()
            .status
            == "cancelled"
        )


def test_rejected_close_readback_never_confirms_intent(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )
    result = gateway.close_exact_position(
        authority=_authority(
            binding_id=binding_id,
            leg_id=leg_id,
            position=SISTER_POSITION,
            strategy="strategy-sister",
        ),
        size="10",
        client_order_id="close-rejected",
        idempotency_key="management:65:49:close:rejected",
    )
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, result.intent_id)
        intent.status = "recovery_required"
        intent.response_json = None
        session.commit()

    reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=None,
        order_history=[
            {
                "ordId": "ord-close-rejected",
                "clOrdId": "close-rejected",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "closePosId": "pos-sister",
                "sz": "10",
                "status": "rejected",
            }
        ],
        trade_fills=[],
        reconciled_at=NOW,
    )

    with session_factory() as session:
        intent = session.get(PositionMutationIntent, result.intent_id)
        assert intent.status == "rejected"
        assert intent.order_id == "ord-close-rejected"


def test_stale_position_fingerprint_blocks_write(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )
    authority = _authority(
        binding_id=binding_id,
        leg_id=leg_id,
        position={**SISTER_POSITION, "pos": "20"},
        strategy="strategy-sister",
    )

    result = gateway.cancel_owned_position_sltp(
        authority=authority,
        order_id="ord-sister-stop",
        idempotency_key="management:65:49:cancel:stale",
    )

    assert result.status == "blocked"
    assert result.reason == "position_fingerprint_changed"
    assert client.cancel_position_sltp_calls == []


def test_live_gate_is_checked_before_write(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: False,
        now_provider=lambda: NOW,
    )

    result = gateway.cancel_owned_position_sltp(
        authority=_authority(
            binding_id=binding_id,
            leg_id=leg_id,
            position=SISTER_POSITION,
            strategy="strategy-sister",
        ),
        order_id="ord-sister-stop",
        idempotency_key="management:65:49:cancel:gate",
    )

    assert result.status == "blocked"
    assert result.reason == "live_execution_disabled"
    assert client.cancel_position_sltp_calls == []


def test_terminal_leg_cannot_authorize_position_write(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    with session_factory() as session:
        session.get(ExecutionOrderLeg, leg_id).status = "closed"
        session.commit()
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    result = gateway.cancel_owned_position_sltp(
        authority=_authority(
            binding_id=binding_id,
            leg_id=leg_id,
            position=SISTER_POSITION,
            strategy="strategy-sister",
        ),
        order_id="ord-sister-stop",
        idempotency_key="management:65:49:cancel:terminal",
    )

    assert result.status == "blocked"
    assert result.reason == "position_ownership_terminal"
    assert client.cancel_position_sltp_calls == []


def test_inactive_protection_ledger_cannot_authorize_cancellation(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    with session_factory() as session:
        session.query(PositionProtectionLedger).one().status = "cancelled"
        session.commit()
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    result = gateway.cancel_owned_position_sltp(
        authority=_authority(
            binding_id=binding_id,
            leg_id=leg_id,
            position=SISTER_POSITION,
            strategy="strategy-sister",
        ),
        order_id="ord-sister-stop",
        idempotency_key="management:65:49:cancel:inactive-ledger",
    )

    assert result.status == "blocked"
    assert result.reason == "protection_order_not_active"
    assert client.cancel_position_sltp_calls == []


def test_set_sltp_uses_exact_position_and_verified_binding_modes(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    result = gateway.set_exact_position_sltp(
        authority=_authority(
            binding_id=binding_id,
            leg_id=leg_id,
            position=SISTER_POSITION,
            strategy="strategy-sister",
        ),
        purpose="stop_loss",
        trigger_price="62000",
        size="0",
        idempotency_key="management:65:49:set:stop",
    )

    assert result.status == "submitted"
    assert client.set_position_sltp_calls == [
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "posId": "pos-sister",
            "tdMode": "cross",
            "slTriggerPx": "62000",
            "sz": "0",
        }
    ]


def test_submitted_set_intent_is_confirmed_only_by_exact_pending_readback(
    tmp_path,
):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )
    result = gateway.set_exact_position_sltp(
        authority=_authority(
            binding_id=binding_id,
            leg_id=leg_id,
            position=SISTER_POSITION,
            strategy="strategy-sister",
        ),
        purpose="stop_loss",
        trigger_price="62000",
        size="0",
        idempotency_key="management:65:49:set:readback",
    )
    assert result.status == "submitted"

    confirmed = reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=[
            {
                "ordId": "ord-new-stop",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-sister",
                "posSide": "long",
                "slTriggerPx": "62000.000",
                "sz": "0",
            }
        ],
        reconciled_at=NOW,
    )

    assert confirmed == 1
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, result.intent_id)
        assert intent.status == "confirmed"
        assert intent.order_id == "ord-new-stop"
        ledger = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.order_id == "ord-new-stop")
            .one()
        )
        assert ledger.status == "verified"

    replay = gateway.set_exact_position_sltp(
        authority=_authority(
            binding_id=binding_id,
            leg_id=leg_id,
            position=SISTER_POSITION,
            strategy="strategy-sister",
        ),
        purpose="stop_loss",
        trigger_price="62000",
        size="0",
        idempotency_key="management:65:49:set:readback",
    )
    assert replay.status == "confirmed"
    assert len(client.set_position_sltp_calls) == 1


def test_require_readback_confirms_intent_and_ledger_atomically(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)

    class ReadbackClient(FakeDeepcoinClient):
        def list_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "ordId": "ord-new-stop",
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-sister",
                    "posSide": "long",
                    "slTriggerPx": "62000.000",
                    "sz": "0",
                }
            ]

    client = ReadbackClient()

    response = submit_exact_position_sltp(
        session_factory=session_factory,
        deepcoin_client=client,
        pos_id="pos-sister",
        payload={
            "instId": "BTC-USDT-SWAP",
            "slTriggerPx": "62000",
            "sz": "0",
        },
        idempotency_key="management:readback:atomic-ledger",
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        require_readback=True,
    )

    assert response["data"]["ordId"] == "ord-new-stop"
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        ledger = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.order_id == "ord-new-stop")
            .one()
        )
        assert intent.status == "confirmed"
        assert intent.order_id == "ord-new-stop"
        assert ledger.execution_binding_id == binding_id
        assert ledger.execution_order_leg_id == leg_id
        assert ledger.status == "verified"


def test_close_uses_exact_close_position_id(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    result = gateway.close_exact_position(
        authority=_authority(
            binding_id=binding_id,
            leg_id=leg_id,
            position=SISTER_POSITION,
            strategy="strategy-sister",
        ),
        size="10",
        client_order_id="close-sister-1",
        idempotency_key="management:65:49:close",
    )

    assert result.status == "submitted"
    assert client.place_order_calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "cross",
            "side": "sell",
            "posSide": "long",
            "ordType": "market",
            "sz": "10",
            "mrgPosition": "split",
            "closePosId": "pos-sister",
            "clOrdId": "close-sister-1",
        }
    ]


def test_failed_terminal_cas_returns_durable_current_status(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=FakeDeepcoinClient(),
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )
    authority = _authority(
        binding_id=binding_id,
        leg_id=leg_id,
        position=SISTER_POSITION,
        strategy="strategy-sister",
    )
    intent = reserve_position_mutation_intent(
        session_factory,
        idempotency_key="management:cas-race",
        operation="set_position_sltp",
        strategy_instance_id=authority.strategy_instance_id,
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id=authority.pos_id,
        order_id=None,
        authority_fingerprint="authority",
        request_fingerprint="request",
        request={},
        reserved_at=NOW,
    )
    assert transition_position_mutation_intent(
        session_factory,
        intent.id,
        expected_statuses={"reserved"},
        new_status="submitting",
        transitioned_at=NOW,
    )

    blocked = gateway._block(intent.id, "stale_worker")
    failed = gateway._finish_with_error(
        intent.id, "recovery_required", "stale_worker"
    )

    assert blocked.status == "submitting"
    assert failed.status == "recovery_required"
