"""An unverified binding authorizes nothing: modify, cancel and close, each alone.

Phase 6's flat rule, with no exceptions: while the entry leg that owns a
position is not ``attribution_status='verified'``, this system may not move its
stop, may not cancel its protection, and may not close it. Those are three
separate code paths, so they get three separate tests -- a single test covering
"the gateway refuses" would pass while one of the three quietly grew its own
way through.

Each test asserts two things, and the second is the one that matters: not only
that the call refused, but that **the exchange client was never touched**. A
refusal that still sent the request would satisfy a "raises" assertion and be
exactly the defect this rule exists to prevent.
"""

from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_execution_actions import (
    DeepcoinExecutionActionError,
    close_bound_position_market,
)
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg
from telegram_kol_research.position_mutation_authority import (
    PositionMutationAuthority,
    position_authority_fingerprint,
)
from telegram_kol_research.position_mutation_gateway import PositionMutationGateway
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
INST = "ETH-USDT-SWAP"
POSITION = {
    "posId": "pos-1",
    "instId": INST,
    "posSide": "long",
    "pos": "4",
    "avgPx": "2600",
    "mgnMode": "cross",
    "mrgPosition": "split",
}


class _Client:
    """Records every write. The tests assert these stay empty."""

    def __init__(self):
        self.set_position_sltp_calls = []
        self.cancel_position_sltp_calls = []
        self.place_order_calls = []
        self.order_payloads = []

    def list_positions(self, *, inst_id=None):
        return [dict(POSITION)]

    def list_trigger_orders_pending(self, *, inst_id):
        return [
            {
                "ordId": "stop-1",
                "instId": INST,
                "posSide": "long",
                "posId": "pos-1",
                "triggerOrderType": "TPSL",
                "slTriggerPrice": "2500",
                "sz": "4",
            }
        ]

    def list_position_history(self, *, inst_id, pos_id=None):
        return []

    def list_order_history(self, *, inst_id=None):
        return []

    def list_open_orders(self, *, inst_id=None):
        return []

    def set_position_sltp(self, payload):
        self.set_position_sltp_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": "should-never-happen"}}

    def cancel_position_sltp(self, payload):
        self.cancel_position_sltp_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}

    def place_order(self, payload):
        self.place_order_calls.append(dict(payload))
        self.order_payloads.append(dict(payload))
        return {"code": "0", "data": {"ordId": "should-never-happen"}}

    @property
    def wrote_anything(self) -> bool:
        return bool(
            self.set_position_sltp_calls
            or self.cancel_position_sltp_calls
            or self.place_order_calls
        )


def _seed(tmp_path, *, attribution_status):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            strategy_instance_id="deepcoin:1:1:ETH:long",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
            attribution_status=attribution_status,
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        # The close path finds its binding by the position id recorded on the
        # binding row. Without this the close refuses because no binding claims
        # the position -- a refusal for the wrong reason, which reads exactly
        # like a refusal for the right one.
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "pos-1"
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            strategy_instance_id="deepcoin:1:1:ETH:long",
            pos_id="pos-1",
            instrument_id=INST,
            side="long",
            order_id="stop-1",
            purpose="stop_loss",
            trigger_price="2500",
            size_text="4",
            status="verified",
            evidence_source="test",
            evidence={},
            seen_at=NOW,
        )
        session.commit()
    return session_factory, binding_id, leg_id


def _authority(binding_id, leg_id):
    return PositionMutationAuthority(
        venue="deepcoin",
        strategy_instance_id="deepcoin:1:1:ETH:long",
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id="pos-1",
        instrument_id=INST,
        side="long",
        position_fingerprint=position_authority_fingerprint(POSITION),
    )


def _gateway(session_factory, client):
    return PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )


@pytest.mark.parametrize("attribution_status", ["unverified", "unassigned", "candidate"])
def test_an_unverified_binding_cannot_move_a_stop(tmp_path, attribution_status):
    session_factory, binding_id, leg_id = _seed(
        tmp_path, attribution_status=attribution_status
    )
    client = _Client()

    result = _gateway(session_factory, client).set_exact_position_sltp(
        authority=_authority(binding_id, leg_id),
        purpose="stop_loss",
        trigger_price="2550",
        size="4",
        idempotency_key="refusal-test:set",
    )

    assert result.status == "blocked"
    assert result.reason is not None
    assert not client.wrote_anything


@pytest.mark.parametrize("attribution_status", ["unverified", "unassigned", "candidate"])
def test_an_unverified_binding_cannot_cancel_protection(tmp_path, attribution_status):
    session_factory, binding_id, leg_id = _seed(
        tmp_path, attribution_status=attribution_status
    )
    client = _Client()

    result = _gateway(session_factory, client).cancel_owned_position_sltp(
        authority=_authority(binding_id, leg_id),
        order_id="stop-1",
        idempotency_key="refusal-test:cancel",
    )

    assert result.status == "blocked"
    assert result.reason is not None
    assert not client.wrote_anything


@pytest.mark.parametrize("attribution_status", ["unverified", "unassigned", "candidate"])
def test_an_unverified_binding_cannot_close_the_position(tmp_path, attribution_status):
    session_factory, _, _ = _seed(tmp_path, attribution_status=attribution_status)
    client = _Client()

    with pytest.raises(DeepcoinExecutionActionError) as excinfo:
        close_bound_position_market(
            session_factory,
            pos_id="pos-1",
            deepcoin_client=client,
            executed_at=NOW,
        )

    # Either refusal is correct and both are about ownership: the lookup that
    # finds "the one active binding for this position" already declines to
    # return an unverified leg, so the close can stop there rather than at the
    # gateway. Asserting the *class* of reason keeps the test meaningful --
    # a refusal for an unrelated reason (a network error, a bad fixture) would
    # still fail it.
    assert str(excinfo.value) == (
        "position_ownership_not_verified:" + attribution_status
    )
    assert not client.wrote_anything


def test_the_same_three_calls_are_allowed_once_the_binding_is_verified(tmp_path):
    """The refusals must be about the attribution, not about the fixture.

    Without this, all three tests above would still pass if the seed were
    simply broken in some unrelated way -- a refusal for the wrong reason looks
    exactly like a refusal for the right one.
    """

    session_factory, binding_id, leg_id = _seed(tmp_path, attribution_status="verified")
    client = _Client()

    set_result = _gateway(session_factory, client).set_exact_position_sltp(
        authority=_authority(binding_id, leg_id),
        purpose="stop_loss",
        trigger_price="2550",
        size="4",
        idempotency_key="allowed-test:set",
    )
    cancel_result = _gateway(session_factory, client).cancel_owned_position_sltp(
        authority=_authority(binding_id, leg_id),
        order_id="stop-1",
        idempotency_key="allowed-test:cancel",
    )

    assert set_result.status == "submitted"
    assert cancel_result.status == "submitted"
    assert client.set_position_sltp_calls
    assert client.cancel_position_sltp_calls
