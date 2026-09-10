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
from telegram_kol_research.trigger_backup_stop_executor import _plan_submission


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
    def __init__(self):
        self.writes = []

    def list_positions(self, *, inst_id=None):
        return [{"instId": INST, "posId": "pos-1", "posSide": "long", "pos": "15",
                 "avgPx": "76000", "mgnMode": "cross", "mrgPosition": "split"}]

    def read_trigger_orders_pending(self, *, inst_id):
        return {"code": "0", "data": [{
            "ordId": "adopted-stop", "instId": INST, "posSide": "long",
            "triggerOrderType": "TPSL", "slTriggerPrice": "75700", "sz": "15",
        }]}

    def list_trigger_orders_pending(self, *, inst_id):
        return self.read_trigger_orders_pending(inst_id=inst_id)["data"]

    def set_position_sltp(self, payload):
        self.writes.append(dict(payload))
        raise AssertionError("an adopted primary must not trigger a backup stop order")


def _seed(tmp_path, *, evidence_source):
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
            execution_order_leg_id=leg_id, strategy_instance_id=None, pos_id="pos-1",
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
    assert plan.payload is None
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
