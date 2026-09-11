"""Phase 6e/6f boundary: an adopted primary stop never becomes an exchange order."""

from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import (
    DeepcoinContractSpec,
    StaticDeepcoinContractSpecProvider,
)
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
import telegram_kol_research.trigger_backup_stop_executor as backup_stop_module
from telegram_kol_research.trigger_backup_stop_executor import (
    ADOPTED_PRIMARY_BACKUP_RELEASED_POS_IDS,
    _plan_submission,
)


NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)


def _spec_provider():
    return StaticDeepcoinContractSpecProvider({
        "BTC-USDT-SWAP": DeepcoinContractSpec(
            instrument_id="BTC-USDT-SWAP", contract_value=0.001, quantity_step=1,
            min_quantity=1, price_tick=0.1,
        )
    })
INST = "BTC-USDT-SWAP"


class _Client:
    def __init__(self, pos_id="pos-1"):
        self.writes = []
        self.pos_id = pos_id

    def list_positions(self, *, inst_id=None):
        return [{"instId": INST, "posId": self.pos_id, "posSide": "long", "pos": "15",
                 "avgPx": "77000", "liqPx": "68116.6", "lever": "125",
                 "mgnMode": "cross", "mrgPosition": "split"}]

    def read_trigger_orders_pending(self, *, inst_id):
        return {"code": "0", "data": [{
            "ordId": "adopted-stop", "instId": INST, "posSide": "long",
            "triggerOrderType": "TPSL", "slTriggerPrice": "75700",
            "slTriggerPx": "75700", "posId": self.pos_id, "sz": "15",
        }]}

    def list_trigger_orders_pending(self, *, inst_id):
        return self.read_trigger_orders_pending(inst_id=inst_id)["data"]

    def set_position_sltp(self, payload):
        self.writes.append(dict(payload))
        raise AssertionError("an adopted primary must not trigger a backup stop order")


def _seed(tmp_path, *, evidence_source, pos_id="pos-1"):
    """Binding, entry leg and adopted primary stop, all on the same position.

    ``pos_id`` has to reach the *leg* as well as the ledger row: the first
    thing ``_plan_submission`` does is refuse with ``binding_or_leg_unavailable``
    when ``leg.pos_id`` is not the position it was asked about. A seed that
    left the leg on "pos-1" produced that refusal for every caller passing a
    different id -- and a refusal for a fixture's reason reads exactly like a
    refusal for the right one.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(kol_id="k", chat_id=1, message_id=1, symbol="BTC",
                               side="long", venue="deepcoin", margin_mode="cross",
                               position_mode="split", status="active"),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(execution_binding_id=binding_id, leg_index=1,
                                purpose="entry", order_kind="limit",
                                strategy_instance_id="deepcoin:1:1:BTC:long",
                                venue="deepcoin", pos_id=pos_id, status="active",
                                attribution_status="verified"),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        upsert_protection_ledger_row(
            session, venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg_id, strategy_instance_id=None, pos_id=pos_id,
            instrument_id=INST, side="long", order_id="adopted-stop",
            purpose="stop_loss", trigger_price="75700", size_text="15",
            status="verified", evidence_source=evidence_source, evidence={}, seen_at=NOW,
        )
        session.commit()
    return session_factory, binding_id, leg_id


def test_an_adopted_primary_stop_yields_a_recorded_plan_and_no_order(tmp_path):
    """6e writes the ledger row; only 6f may turn it into an exchange write."""

    session_factory, binding_id, leg_id = _seed(
        tmp_path, evidence_source="exchange_adopted_by_tu"
    )
    client = _Client()

    with session_factory() as session:
        plan = _plan_submission(
            session, binding_id=binding_id, leg_id=leg_id, pos_id="pos-1",
            client=client, contract_spec_provider=_spec_provider(),
            backup_stop_buffer_bps=20.0, submitted_at=NOW,
        )

    assert plan.status == "shadow_ready_adopted_primary"
    assert plan.reason_code == "primary_stop_adopted_from_exchange"
    assert plan.primary_order_id == "adopted-stop"
    assert plan.primary_stop == "75700"
    # Phase 6f: the held plan carries what it would have sent. A record saying
    # only "held" cannot be approved by anyone.
    assert plan.backup_stop == "75548.6"
    assert plan.payload["slTriggerPx"] == "75548.6"
    assert plan.payload["posSide"] == "long"
    # The payload carries no ``sz``: a position-bound TPSL with ``slOrdPx=-1``
    # closes whatever the position holds. The size is validated on the way in
    # and deliberately not sent, so this stop follows the position rather than
    # pinning a quantity.
    assert "sz" not in plan.payload
    assert client.writes == []


def test_a_normally_recorded_primary_is_not_held_back(tmp_path):
    """The hold-back is keyed to adoption, not to every primary stop.

    Without this, a change that held everything back would pass the test above
    and quietly stop backup stops for every position.
    """

    session_factory, binding_id, leg_id = _seed(
        tmp_path, evidence_source="position_mutation_intent_readback"
    )
    client = _Client()

    with session_factory() as session:
        plan = _plan_submission(
            session, binding_id=binding_id, leg_id=leg_id, pos_id="pos-1",
            client=client, contract_spec_provider=_spec_provider(),
            backup_stop_buffer_bps=20.0, submitted_at=NOW,
        )

    assert plan.status != "shadow_ready_adopted_primary"


def test_a_released_position_is_not_held(tmp_path, monkeypatch):
    """The release is per position id, and it is the only thing that lifts the hold.

    Without this the hold could be made unconditional -- every adopted primary
    held forever -- and the test above would still pass while phase 6f did
    nothing at all.

    **Released id supplied by the test, not read from the constant.** Two
    earlier versions read it: the first took ``sorted(...)[0]``, so adding an
    id changed production while the test kept exercising the one already live;
    the second parametrized over the whole set, which was right until the set
    went back to empty on 2026-09-11 -- and then pytest reported
    ``got empty parameter set``, **a skip that reads as green**. A property
    this test exists to hold does not stop being true when nothing is
    currently released, so it must not stop being checked either.

    **Asserted positively, and against the same fixture held.** The third
    version of this test asserted only ``status != "shadow_ready_adopted_primary"``
    and hard-wiring the gate shut did not turn it red: the fixture's leg
    carried a different ``pos_id`` than the one under test, so every run
    refused at ``binding_or_leg_unavailable`` long before reaching the gate.
    A refusal for the fixture's reason is indistinguishable from a refusal for
    the right one, so the pair below is the assertion: one position, one
    difference -- the gate -- and two different outcomes.
    """

    released = "pos-released-for-this-test"
    monkeypatch.setattr(
        backup_stop_module,
        "ADOPTED_PRIMARY_BACKUP_RELEASED_POS_IDS",
        frozenset({released}),
    )
    session_factory, binding_id, leg_id = _seed(
        tmp_path, evidence_source="exchange_adopted_by_tu", pos_id=released
    )
    client = _Client(pos_id=released)

    with session_factory() as session:
        plan = _plan_submission(
            session, binding_id=binding_id, leg_id=leg_id, pos_id=released,
            client=client, contract_spec_provider=_spec_provider(),
            backup_stop_buffer_bps=20.0, submitted_at=NOW,
        )

    # Positive: the release lets the plan through to a real submission plan.
    # "not held" is not enough -- a block reads the same way.
    assert plan.status == "ready", (plan.status, plan.reason_code)
    assert plan.payload["slTriggerPx"] == "75548.6"
    assert client.writes == []

    # Negative, same position, same fixture, gate closed. Only the gate moved.
    monkeypatch.setattr(
        backup_stop_module,
        "ADOPTED_PRIMARY_BACKUP_RELEASED_POS_IDS",
        frozenset(),
    )
    held_factory, held_binding_id, held_leg_id = _seed(
        tmp_path / "held", evidence_source="exchange_adopted_by_tu", pos_id=released
    )
    with held_factory() as session:
        held = _plan_submission(
            session, binding_id=held_binding_id, leg_id=held_leg_id, pos_id=released,
            client=_Client(pos_id=released), contract_spec_provider=_spec_provider(),
            backup_stop_buffer_bps=20.0, submitted_at=NOW,
        )

    assert held.status == "shadow_ready_adopted_primary", (held.status, held.reason_code)
    assert held.reason_code == "primary_stop_adopted_from_exchange"


def test_the_release_set_is_empty_in_production():
    """And it is empty right now, asserted separately from the behaviour above.

    Split deliberately. The behavioural test must keep running whatever the
    constant holds; this one records what it holds, and will need a deliberate
    edit the next time a position is approved -- which is the point, since
    releasing one is meant to cost a code change.
    """

    assert ADOPTED_PRIMARY_BACKUP_RELEASED_POS_IDS == frozenset()
