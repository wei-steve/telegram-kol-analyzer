"""Phase 6e/6f boundary: an adopted primary stop never becomes an exchange order."""

from datetime import UTC, datetime

import pytest

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
                                venue="deepcoin", pos_id="pos-1", status="active",
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


@pytest.mark.parametrize("released", sorted(ADOPTED_PRIMARY_BACKUP_RELEASED_POS_IDS))
def test_a_released_position_is_not_held(tmp_path, released):
    """The release is per position id, and it is the only thing that lifts the hold.

    Without this the hold could be made unconditional -- every adopted primary
    held forever -- and the test above would still pass while phase 6f did
    nothing at all.

    Parametrized over the whole set rather than one member of it. The first
    version took ``sorted(...)[0]``, which was the same id before and after the
    second position was released -- so adding an id to the constant would have
    changed production behaviour while the test carried on exercising only the
    one that was already live.
    """

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

    assert plan.status != "shadow_ready_adopted_primary"
