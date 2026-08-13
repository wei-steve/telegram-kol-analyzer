from datetime import UTC, datetime
import json

import pytest

from telegram_kol_research.deepcoin_client import (
    DeepcoinRequestOutcomeUnknown,
)
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
    PositionMutationIntentError,
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


def test_partially_filled_then_cancelled_close_becomes_terminal_for_delta_recovery(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=FakeDeepcoinClient(),
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
        size="5",
        client_order_id="close-partial-terminal",
        idempotency_key="component:close:partial-terminal",
    )
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, result.intent_id)
        intent.status = "recovery_required"
        intent.response_json = None
        session.commit()
    terminal = {
        "ordId": "ord-partial-terminal",
        "clOrdId": "close-partial-terminal",
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "closePosId": "pos-sister",
        "sz": "5",
        "status": "cancelled",
    }
    fill = {
        **terminal,
        "fillSz": "3",
        "status": "filled",
    }

    confirmed = reconcile_submitted_position_mutation_intents(
        session_factory,
        order_history=[terminal],
        trade_fills=[fill],
        reconciled_at=NOW,
    )

    assert confirmed == 1
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, result.intent_id)
        assert intent.status == "confirmed"
        assert intent.order_id == "ord-partial-terminal"


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


def test_protected_entry_writer_boundary_runs_only_after_final_gate(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    boundary_calls = []
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: False,
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
        idempotency_key="protected-entry:writer-boundary",
        before_exchange_submit=lambda intent_id: boundary_calls.append(
            intent_id
        ),
    )

    assert result.status == "blocked"
    assert result.reason == "live_execution_disabled"
    assert boundary_calls == []
    assert client.set_position_sltp_calls == []


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


def test_submitting_backup_stop_recovers_with_persisted_role(tmp_path):
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
        ledger_purpose="backup_stop",
        trigger_price="61000",
        size="10",
        idempotency_key="component:backup:crash-window",
    )
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, result.intent_id)
        assert json.loads(intent.request_json)["_ledger_purpose"] == "backup_stop"
        intent.status = "submitting"
        intent.response_json = None
        session.commit()

    confirmed = reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=[{
            "ordId": "ord-new-stop",
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-sister",
            "posSide": "long",
            "slTriggerPx": "61000",
            "sz": "10",
        }],
        reconciled_at=NOW,
    )

    assert confirmed == 1
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, result.intent_id)
        assert intent.status == "confirmed"
        ledger = session.query(PositionProtectionLedger).filter_by(
            order_id="ord-new-stop"
        ).one()
        assert ledger.purpose == "backup_stop"


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


def test_protected_entry_market_protection_readback_polls_bounded_without_repeating_writer(
    tmp_path,
):
    session_factory, _, _, _, _ = _seed(tmp_path)

    class Clock:
        def __init__(self):
            self.now = 100.0
            self.sleeps = []

        def __call__(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    class DelayedReadbackClient(FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.readback_calls = 0

        def list_trigger_orders_pending(self, *, inst_id):
            self.readback_calls += 1
            if self.readback_calls < 3:
                return []
            return [{
                "ordId": "ord-new-stop",
                "instId": inst_id,
                "posId": "pos-sister",
                "posSide": "long",
                "slTriggerPx": "62000",
                "sz": "0",
            }]

    clock = Clock()
    client = DelayedReadbackClient()

    response = submit_exact_position_sltp(
        session_factory=session_factory,
        deepcoin_client=client,
        pos_id="pos-sister",
        payload={
            "instId": "BTC-USDT-SWAP",
            "slTriggerPx": "62000",
            "sz": "0",
        },
        idempotency_key="protected-entry:readback:bounded",
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        require_readback=True,
        readback_deadline_monotonic=110.0,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
    )

    assert response["data"]["ordId"] == "ord-new-stop"
    assert len(client.set_position_sltp_calls) == 1
    assert client.readback_calls == 3
    # The first read captures the immutable pre-submit TPSL baseline; only the
    # remaining two reads belong to the bounded post-submit poll.
    assert clock.sleeps == [0.5]


def test_protected_entry_readback_requires_complete_position_identity(
    tmp_path,
):
    session_factory, _, _, _, _ = _seed(tmp_path)

    class IncompleteReadbackClient(FakeDeepcoinClient):
        def list_trigger_orders_pending(self, *, inst_id):
            return [{
                "ordId": "ord-new-stop",
                "slTriggerPx": "62000",
                "sz": "0",
            }]

    client = IncompleteReadbackClient()

    with pytest.raises(
        DeepcoinRequestOutcomeUnknown,
        match="position_sltp_pending_readback",
    ):
        submit_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=client,
            pos_id="pos-sister",
            payload={
                "instId": "BTC-USDT-SWAP",
                "slTriggerPx": "62000",
                "sz": "0",
            },
            idempotency_key="protected-entry:strict-readback",
            live_execution_gate=lambda: True,
            now_provider=lambda: NOW,
            require_readback=True,
            require_complete_readback_identity=True,
        )

    assert len(client.set_position_sltp_calls) == 1


def test_protected_entry_get_only_recovery_does_not_reconcile_other_intents(
    tmp_path,
):
    class SimulatedCrash(BaseException):
        pass

    session_factory, binding_id, leg_id, other_binding_id, other_leg_id = (
        _seed(tmp_path)
    )
    unrelated = reserve_position_mutation_intent(
        session_factory,
        idempotency_key="unrelated-cancel-submitting",
        operation="cancel_position_sltp",
        strategy_instance_id="strategy-other",
        execution_binding_id=other_binding_id,
        execution_order_leg_id=other_leg_id,
        pos_id="pos-other",
        order_id="unrelated-stop",
        authority_fingerprint="a" * 64,
        request_fingerprint="b" * 64,
        request={
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-other",
            "ordId": "unrelated-stop",
        },
        reserved_at=NOW,
    )
    transition_position_mutation_intent(
        session_factory,
        unrelated.id,
        expected_statuses={"reserved"},
        new_status="submitting",
        transitioned_at=NOW,
    )

    class CrashClient(FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.pending = []

        def set_position_sltp(self, payload):
            self.set_position_sltp_calls.append(dict(payload))
            if len(self.set_position_sltp_calls) == 1:
                self.pending = [{
                    "ordId": "recovered-stop",
                    "instId": payload["instId"],
                    "posId": payload["posId"],
                    "posSide": payload["posSide"],
                    "slTriggerPx": payload["slTriggerPx"],
                    "sz": payload.get("sz", "0"),
                }]
                raise SimulatedCrash()
            raise AssertionError("protection writer repeated")

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.pending)

    client = CrashClient()
    kwargs = dict(
        session_factory=session_factory,
        deepcoin_client=client,
        pos_id="pos-sister",
        payload={
            "instId": "BTC-USDT-SWAP",
            "slTriggerPx": "62000",
            "sz": "0",
        },
        idempotency_key="protected-entry:single-intent-recovery",
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        require_readback=True,
        require_complete_readback_identity=True,
    )

    with pytest.raises(SimulatedCrash):
        submit_exact_position_sltp(**kwargs)
    response = submit_exact_position_sltp(**kwargs)

    assert response["data"]["ordId"] == "recovered-stop"
    assert len(client.set_position_sltp_calls) == 1
    with session_factory() as session:
        assert session.get(PositionMutationIntent, unrelated.id).status == (
            "submitting"
        )
        recovered = session.query(PositionMutationIntent).filter(
            PositionMutationIntent.id != unrelated.id
        ).one()
        assert recovered.status == "confirmed"


@pytest.mark.parametrize("tamper_baseline", [False, True])
def test_protected_entry_recovery_never_claims_preexisting_matching_stop(
    tmp_path,
    tamper_baseline,
):
    class SimulatedCrash(BaseException):
        pass

    session_factory, _, _, _, _ = _seed(tmp_path)

    class CrashBeforePostClient(FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.pending = [{
                "ordId": "preexisting-manual-stop",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-sister",
                "posSide": "long",
                "slTriggerPx": "62000",
                "sz": "0",
            }]

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.pending)

        def set_position_sltp(self, payload):
            self.set_position_sltp_calls.append(dict(payload))
            raise SimulatedCrash()

    client = CrashBeforePostClient()
    kwargs = dict(
        session_factory=session_factory,
        deepcoin_client=client,
        pos_id="pos-sister",
        payload={
            "instId": "BTC-USDT-SWAP",
            "slTriggerPx": "62000",
            "sz": "0",
        },
        idempotency_key="protected-entry:preexisting-stop",
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        require_readback=True,
        require_complete_readback_identity=True,
    )

    with pytest.raises(SimulatedCrash):
        submit_exact_position_sltp(**kwargs)
    if tamper_baseline:
        with session_factory() as session:
            intent = session.query(PositionMutationIntent).one()
            request = json.loads(intent.request_json)
            request["_pre_submit_order_refs"] = []
            intent.request_json = json.dumps(request, sort_keys=True)
            session.commit()
        assert reconcile_submitted_position_mutation_intents(
            session_factory,
            pending_trigger_orders=client.pending,
            reconciled_at=NOW,
        ) == 0
    if tamper_baseline:
        with pytest.raises(
            PositionMutationIntentError,
            match="position_mutation_intent_conflict",
        ):
            submit_exact_position_sltp(**kwargs)
    else:
        with pytest.raises(
            DeepcoinRequestOutcomeUnknown,
            match=(
                "writer_outcome_unknown|position_sltp_pending_readback|"
                "position_mutation_submitting"
            ),
        ):
            submit_exact_position_sltp(**kwargs)

    assert len(client.set_position_sltp_calls) == 1
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "submitting"
        assert intent.order_id is None
        assert "preexisting-manual-stop" not in intent.request_json
        if not tamper_baseline:
            baseline_refs = json.loads(intent.request_json)[
                "_pre_submit_order_refs"
            ]
            assert len(baseline_refs) == 1
            assert len(baseline_refs[0]) == 64
        assert session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id
            == "preexisting-manual-stop"
        ).count() == 0


@pytest.mark.parametrize(
    "tamper_kind",
    ["trigger", "purpose_removed", "purpose_changed"],
)
def test_protected_entry_recovery_rejects_tampered_request_before_writing_ledger(
    tmp_path,
    tamper_kind,
):
    class SimulatedCrash(BaseException):
        pass

    session_factory, _, _, _, _ = _seed(tmp_path)

    class TamperedReadbackClient(FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.pending = []

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.pending)

        def set_position_sltp(self, payload):
            self.set_position_sltp_calls.append(dict(payload))
            raise SimulatedCrash()

    client = TamperedReadbackClient()
    kwargs = dict(
        session_factory=session_factory,
        deepcoin_client=client,
        pos_id="pos-sister",
        payload={
            "instId": "BTC-USDT-SWAP",
            "slTriggerPx": "62000",
            "sz": "0",
        },
        idempotency_key="protected-entry:tampered-request",
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        require_readback=True,
        require_complete_readback_identity=True,
    )

    with pytest.raises(SimulatedCrash):
        submit_exact_position_sltp(**kwargs)
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        request = json.loads(intent.request_json)
        if tamper_kind == "trigger":
            request["slTriggerPx"] = "63000"
        elif tamper_kind == "purpose_removed":
            request.pop("_ledger_purpose")
        else:
            request["_ledger_purpose"] = "backup_stop"
        intent.request_json = json.dumps(request, sort_keys=True)
        session.commit()
    client.pending = [{
        "ordId": "unrelated-tampered",
        "instId": "BTC-USDT-SWAP",
        "posId": "pos-sister",
        "posSide": "long",
        "slTriggerPx": (
            "63000" if tamper_kind == "trigger" else "62000"
        ),
        "sz": "0",
    }]

    assert reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=client.pending,
        reconciled_at=NOW,
    ) == 0

    with pytest.raises(Exception):
        submit_exact_position_sltp(**kwargs)

    assert len(client.set_position_sltp_calls) == 1
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "submitting"
        assert intent.order_id is None
        assert session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id == "unrelated-tampered"
        ).count() == 0


def test_protected_entry_refuses_incomplete_pre_submit_tpsl_baseline(
    tmp_path,
):
    session_factory, _, _, _, _ = _seed(tmp_path)

    class PaginatedBaselineClient(FakeDeepcoinClient):
        def read_trigger_orders_pending(self, *, inst_id):
            return {"data": [], "nextPageCursor": "page-2"}

    client = PaginatedBaselineClient()
    with pytest.raises(Exception, match="snapshot_pagination_incomplete"):
        submit_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=client,
            pos_id="pos-sister",
            payload={
                "instId": "BTC-USDT-SWAP",
                "slTriggerPx": "62000",
                "sz": "0",
            },
            idempotency_key="protected-entry:paginated-baseline",
            live_execution_gate=lambda: True,
            now_provider=lambda: NOW,
            require_readback=True,
            require_complete_readback_identity=True,
        )

    assert client.set_position_sltp_calls == []
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "reserved"
        assert "_pre_submit_order_refs" not in json.loads(
            intent.request_json
        )


def test_protected_entry_persists_only_bounded_success_response(
    tmp_path,
):
    session_factory, _, _, _, _ = _seed(tmp_path)

    class SensitiveSuccessClient(FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.pending = []

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.pending)

        def set_position_sltp(self, payload):
            self.set_position_sltp_calls.append(dict(payload))
            self.pending = [{
                "ordId": "safe-stop-id",
                "instId": payload["instId"],
                "posId": payload["posId"],
                "posSide": payload["posSide"],
                "slTriggerPx": payload["slTriggerPx"],
                "sz": payload.get("sz", "0"),
            }]
            return {
                "code": "0",
                "data": {"ordId": "safe-stop-id"},
                "message": "Authorization: Bearer TOPSECRET",
            }

    client = SensitiveSuccessClient()
    response = submit_exact_position_sltp(
        session_factory=session_factory,
        deepcoin_client=client,
        pos_id="pos-sister",
        payload={
            "instId": "BTC-USDT-SWAP",
            "slTriggerPx": "62000",
            "sz": "0",
        },
        idempotency_key="protected-entry:safe-success",
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        require_readback=True,
        require_complete_readback_identity=True,
    )

    assert response == {"code": "0", "data": {"ordId": "safe-stop-id"}}
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert "TOPSECRET" not in (intent.response_json or "")
        assert json.loads(intent.response_json) == response


def test_protected_entry_rejects_credential_shaped_success_order_id(
    tmp_path,
):
    session_factory, _, _, _, _ = _seed(tmp_path)

    class HostileIdClient(FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.pending = []

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.pending)

        def set_position_sltp(self, payload):
            self.set_position_sltp_calls.append(dict(payload))
            return {
                "code": "0",
                "data": {"ordId": "DC-ACCESS-KEY:TOPSECRET"},
            }

    client = HostileIdClient()
    with pytest.raises(
        DeepcoinRequestOutcomeUnknown,
        match="position_sltp_response_missing_order_id",
    ):
        submit_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=client,
            pos_id="pos-sister",
            payload={
                "instId": "BTC-USDT-SWAP",
                "slTriggerPx": "62000",
                "sz": "0",
            },
            idempotency_key="protected-entry:hostile-success-id",
            live_execution_gate=lambda: True,
            now_provider=lambda: NOW,
            require_readback=True,
            require_complete_readback_identity=True,
        )

    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert "TOPSECRET" not in (intent.response_json or "")


def test_require_readback_is_restart_idempotent_after_confirmation(tmp_path):
    session_factory, _, _, _, _ = _seed(tmp_path)

    class ReadbackClient(FakeDeepcoinClient):
        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "ordId": "ord-new-stop", "instId": inst_id,
                    "posId": "pos-sister", "posSide": "long",
                    "slTriggerPx": "62000", "sz": "0",
                }
            ]

    client = ReadbackClient()
    kwargs = dict(
        session_factory=session_factory,
        deepcoin_client=client,
        pos_id="pos-sister",
        payload={"instId": "BTC-USDT-SWAP", "slTriggerPx": "62000", "sz": "0"},
        idempotency_key="management:readback:restart",
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        require_readback=True,
    )

    first = submit_exact_position_sltp(**kwargs)
    second = submit_exact_position_sltp(**kwargs)

    assert first == second
    assert len(client.set_position_sltp_calls) == 1


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


def test_close_blocks_when_another_position_mutation_outcome_is_unresolved(tmp_path):
    session_factory, binding_id, leg_id, _, _ = _seed(tmp_path)
    client = FakeDeepcoinClient()
    authority = _authority(
        binding_id=binding_id,
        leg_id=leg_id,
        position=SISTER_POSITION,
        strategy="strategy-sister",
    )
    prior = reserve_position_mutation_intent(
        session_factory,
        idempotency_key="existing-unknown-close",
        operation="close_position",
        strategy_instance_id="strategy-sister",
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id="pos-sister",
        order_id=None,
        authority_fingerprint="prior-authority",
        request_fingerprint="prior-request",
        request={"closePosId": "pos-sister", "sz": "1"},
        reserved_at=NOW,
    )
    transition_position_mutation_intent(
        session_factory,
        prior.id,
        expected_statuses={"reserved"},
        new_status="recovery_required",
        transitioned_at=NOW,
    )
    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    result = gateway.close_exact_position(
        authority=authority,
        size="1",
        client_order_id="close-new",
        idempotency_key="new-close",
    )

    assert result.status == "blocked"
    assert result.reason == "position_mutation_unresolved"
    assert client.place_order_calls == []


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
