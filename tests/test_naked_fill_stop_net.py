"""Phase 6-pre-2: the stop a market fill never got.

A market entry whose position the identity equation cannot name gets its
protection write refused -- correctly, three times over. What is left is a
filled position with nothing bounding its loss but the liquidation price.
These tests pin the net under that: one stop, once, only when exactly one
unclaimed position can be it, and never anything else.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    RuntimeIncident,
)
from telegram_kol_research.naked_fill_stop_net import (
    NAKED_FILL_AUDIT_ACTION,
    NAKED_FILL_INCIDENT_TYPE,
    NAKED_FILL_STOP_ATTRIBUTION,
    naked_fill_idempotency_key,
    reconcile_naked_market_fills,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
FILLED_AT = NOW - timedelta(minutes=5)
INST = "ETH-USDT-SWAP"


def _position(pos_id, *, size="2", side="long", inst=INST):
    return {
        "posId": pos_id,
        "instId": inst,
        "posSide": side,
        "pos": size,
        "avgPx": "2800",
        "mgnMode": "cross",
        "mrgPosition": "split",
    }


class FakeClient:
    def __init__(self, positions, *, readback_pos_id=None, raise_positions=False):
        self.positions = positions
        self.set_position_sltp_calls = []
        self.raise_positions = raise_positions
        self._readback_pos_id = readback_pos_id

    def list_positions(self, *, inst_id=None):
        if self.raise_positions:
            raise RuntimeError("exchange unreachable")
        return list(self.positions)

    def set_position_sltp(self, payload):
        self.set_position_sltp_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": "ord-rescue-stop"}}

    def list_trigger_orders_pending(self, *, inst_id):
        return [
            {
                "ordId": "ord-rescue-stop",
                "instId": inst_id,
                "posId": self._readback_pos_id,
                "posSide": "long",
                "slTriggerPx": "2500.000",
            }
        ]


def _seed(tmp_path, *, attribution="unverified", stop_loss="2500", size="2",
          filled_at=FILLED_AT, name="naked.db"):
    session_factory = create_session_factory(tmp_path / name)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:55:ETH:long",
            kol_id="kol",
            chat_id=-1001,
            message_id=55,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
            payload_json=json.dumps(
                {
                    "draft": {
                        "instrument_id": INST,
                        "stop_loss": stop_loss,
                        "strategy_instance_id": "deepcoin:100:55:ETH:long",
                    }
                }
            ),
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id="deepcoin:100:55:ETH:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            venue="deepcoin",
            order_id="ord-entry-1",
            pos_id="pos-candidate",
            attribution_status=attribution,
            status="submitted",
            request_json=json.dumps(
                {"instId": INST, "posSide": "long", "sz": size}
            ),
            created_at=filled_at,
            updated_at=filled_at,
        )
        session.add(leg)
        session.commit()
        return session_factory, leg.id


# --- the one case it exists for -------------------------------------------


def test_a_single_unclaimed_candidate_gets_exactly_one_stop(tmp_path):
    session_factory, leg_id = _seed(tmp_path)
    client = FakeClient([_position("pos-candidate")], readback_pos_id="pos-candidate")

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.attached, result.alerted) == (1, 0)
    assert len(client.set_position_sltp_calls) == 1
    call = client.set_position_sltp_calls[0]
    assert call["slTriggerPx"] == "2500"
    assert call["posId"] == "pos-candidate"
    # Stop only. A take profit on a position nobody has proved is ours would be
    # an economic decision; the stop's worst case is bounded loss.
    assert "tpTriggerPx" not in call

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        # Marked, but never "verified": ownership is still not claimed.
        assert leg.attribution_status == NAKED_FILL_STOP_ATTRIBUTION
        assert leg.attribution_status != "verified"

        audit = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == NAKED_FILL_AUDIT_ACTION)
            .one()
        )
        assert audit.pos_id == "pos-candidate"
        assert audit.order_id == "ord-rescue-stop"
        before = json.loads(audit.before_json)
        assert before["candidate_pos_id"] == "pos-candidate"
        assert before["fill_size"] == "2"
        assert before["preconditions"] == ["a:pass", "b:pass", "c:pass", "d:pass"]
        after = json.loads(audit.after_json)
        assert after["ownership_claimed"] is False
        assert after["take_profit_attached"] is False

        incident = (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == NAKED_FILL_INCIDENT_TYPE)
            .one()
        )
        assert incident.severity == "critical"
        assert incident.source_record_id == "ord-entry-1"


def test_a_rescued_leg_is_never_written_twice(tmp_path):
    session_factory, _ = _seed(tmp_path)
    client = FakeClient([_position("pos-candidate")], readback_pos_id="pos-candidate")

    first = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )
    second = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=1)
    )

    assert first.attached == 1
    assert second.attached == 0
    assert len(client.set_position_sltp_calls) == 1


def test_the_idempotency_key_names_the_order_and_the_position():
    assert naked_fill_idempotency_key(order_id="o1", pos_id="p1") == (
        "naked-fill-sl:o1:p1"
    )


# --- every case where it must not act -------------------------------------


def test_two_candidates_only_alert(tmp_path):
    session_factory, leg_id = _seed(tmp_path)
    client = FakeClient(
        [_position("pos-a"), _position("pos-b")], readback_pos_id="pos-a"
    )

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.attached, result.alerted) == (0, 1)
    assert client.set_position_sltp_calls == []
    with session_factory() as session:
        incident = session.query(RuntimeIncident).one()
        assert json.loads(incident.redacted_summary)["reason_code"] == (
            "candidate_not_unique"
        )
        assert session.get(ExecutionOrderLeg, leg_id).attribution_status == (
            "unverified"
        )


def test_no_candidate_only_alerts(tmp_path):
    session_factory, _ = _seed(tmp_path)
    # Right instrument and side, wrong size: not this order's position.
    client = FakeClient([_position("pos-other", size="7")])

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.attached, result.alerted) == (0, 1)
    assert client.set_position_sltp_calls == []
    with session_factory() as session:
        assert json.loads(
            session.query(RuntimeIncident).one().redacted_summary
        )["reason_code"] == "no_unclaimed_candidate"


def test_an_unreadable_snapshot_never_becomes_no_candidate(tmp_path):
    """Hard rule 4: could-not-look and nothing-there are different answers."""

    session_factory, _ = _seed(tmp_path)
    client = FakeClient([], raise_positions=True)

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.attached, result.alerted) == (0, 1)
    assert client.set_position_sltp_calls == []
    with session_factory() as session:
        assert json.loads(
            session.query(RuntimeIncident).one().redacted_summary
        )["reason_code"] == "position_snapshot_incomplete"


def test_a_verified_leg_is_never_touched(tmp_path):
    session_factory, _ = _seed(tmp_path, attribution="verified")
    client = FakeClient([_position("pos-candidate")])

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.examined, result.attached, result.alerted) == (0, 0, 0)
    assert client.set_position_sltp_calls == []


def test_a_fill_inside_the_grace_period_is_left_alone(tmp_path):
    session_factory, _ = _seed(
        tmp_path, filled_at=NOW - timedelta(seconds=30)
    )
    client = FakeClient([_position("pos-candidate")])

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.examined, result.attached) == (0, 0)
    assert client.set_position_sltp_calls == []


def test_a_position_another_leg_already_speaks_for_is_not_a_candidate(tmp_path):
    """``execution_order_legs`` has a unique index on (venue, pos_id).

    So a position can be held by at most one leg, and the only way another leg
    can own the candidate is if the subject leg does not -- which is exactly
    the shape this builds. It is also why "unclaimed" has to exclude the
    subject leg itself: under the strict reading, the fallback ``pos_id`` that
    ``recovery_live_submit`` records would make the net unable to ever fire.
    """

    session_factory, leg_id = _seed(tmp_path)
    with session_factory() as session:
        session.get(ExecutionOrderLeg, leg_id).pos_id = None
        other = ExecutionOrderLeg(
            execution_binding_id=1,
            strategy_instance_id="someone-else",
            leg_index=2,
            purpose="entry",
            order_kind="market",
            venue="deepcoin",
            order_id="ord-other",
            pos_id="pos-candidate",
            attribution_status="verified",
            status="submitted",
        )
        session.add(other)
        session.commit()
    client = FakeClient([_position("pos-candidate")])

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.attached, result.alerted) == (0, 1)
    assert client.set_position_sltp_calls == []


def test_a_position_in_the_protection_ledger_is_not_a_candidate(tmp_path):
    session_factory, _ = _seed(tmp_path)
    with session_factory() as session:
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=1,
                execution_order_leg_id=1,
                pos_id="pos-candidate",
                instrument_id=INST,
                side="long",
                order_id="ord-existing-stop",
                purpose="stop_loss",
                status="verified",
                evidence_source="test",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        session.commit()
    client = FakeClient([_position("pos-candidate")])

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.attached, result.alerted) == (0, 1)
    assert client.set_position_sltp_calls == []


def test_a_draft_without_a_stop_price_cannot_be_rescued(tmp_path):
    session_factory, _ = _seed(tmp_path, stop_loss="")
    client = FakeClient([_position("pos-candidate")])

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.attached, result.alerted) == (0, 1)
    assert client.set_position_sltp_calls == []


# --- the writer's own discipline ------------------------------------------


def test_a_failed_readback_is_unknown_and_never_resent(tmp_path):
    """The venue may have acted. Hard rule 2: record unknown, never resend."""

    from telegram_kol_research.models import PositionMutationIntent

    session_factory, leg_id = _seed(tmp_path)

    class WrongReadback(FakeClient):
        def list_trigger_orders_pending(self, *, inst_id):
            return []  # the stop is not there

    client = WrongReadback([_position("pos-candidate")])

    first = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )
    # The tick catches it, counts it as skipped, and logs -- it must not
    # propagate into the operator loop.
    assert first.attached == 0
    assert len(client.set_position_sltp_calls) == 1
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "unknown"
        assert json.loads(intent.error_json)["reason"] == "naked_fill_pending_readback"
        # Ownership was never claimed and the leg keeps its unverified marker.
        assert session.get(ExecutionOrderLeg, leg_id).attribution_status == (
            "unverified"
        )

    second = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=1)
    )
    assert second.attached == 0
    # The reserved key is what stops the resend.
    assert len(client.set_position_sltp_calls) == 1


def test_a_candidate_claimed_between_deciding_and_writing_is_not_written(tmp_path):
    from telegram_kol_research.models import PositionMutationIntent

    session_factory, _ = _seed(tmp_path)
    client = FakeClient([_position("pos-candidate")], readback_pos_id="pos-candidate")
    original = client.list_positions
    state = {"calls": 0}

    def racing_list_positions(*, inst_id=None):
        state["calls"] += 1
        # The revalidation call sees the position already gone.
        if state["calls"] > 1:
            return []
        return original(inst_id=inst_id)

    client.list_positions = racing_list_positions

    result = reconcile_naked_market_fills(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result.attached == 0
    assert client.set_position_sltp_calls == []
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        assert intent.status == "blocked"
        assert json.loads(intent.error_json)["reason"] == (
            "naked_fill_revalidation_failed"
        )


def test_the_payload_can_only_ever_carry_a_stop(tmp_path):
    from telegram_kol_research.naked_fill_stop_net import (
        NakedFillStopAuthority,
        build_naked_fill_stop_payload,
    )

    session_factory, leg_id = _seed(tmp_path)
    with session_factory() as session:
        binding_id = session.get(ExecutionOrderLeg, leg_id).execution_binding_id
    authority = NakedFillStopAuthority(
        venue="deepcoin",
        strategy_instance_id="deepcoin:100:55:ETH:long",
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        pos_id="pos-candidate",
        instrument_id=INST,
        side="long",
        position_fingerprint="f",
    )

    payload = build_naked_fill_stop_payload(
        session_factory, authority=authority, stop_loss="2500"
    )

    assert payload["slTriggerPx"] == "2500"
    assert not [key for key in payload if key.lower().startswith("tp")]
    assert payload["tdMode"] == "cross"
    assert payload["mrgPosition"] == "split"
