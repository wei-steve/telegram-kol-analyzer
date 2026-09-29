"""M5 replay: #19597 (binding 385, lifecycle 1343) with the break-even target
already on the wrong side of the market.

Design: `docs/plans/2026-09-29-mia-management-verification-design.md` section 2
(M5). Real numbers, taken from the production snapshot fixture used for the
Mia review (not read from any scratchpad path here -- inlined verbatim):

- lifecycle 1343 / execution_binding 385: BTC long, 10 contracts @83800,
  strategy stop_loss 81800, single entry price (entry_range_low ==
  entry_range_high == 83800).
- raw_message_id 19597 (2026-09-28 13:39:44 UTC), text: "目前BTC现价83800，
  加仓后浮盈600点，相当于正常仓位1200点收益，加仓后仓位比较大，减50%仓位，
  剩余仓位止损位上移至83200！@Tarderfengge QQ:158241758". The model's
  authoritative payload gives management_action
  "partial_take_profit,move_stop_to_protect", side long, stop_loss "83200".

``resolve_management_directive`` (unchanged by this task) already turns this
into `partial_then_break_even` with an explicit stop of 83200. The planner's
existing price-plausibility module (`management_price_plausibility.py`,
already deployed, not part of this task) already discards 83200 as looser
than the strategy's own break-even reference and supersedes it with the
strategy price 83800 (`STRATEGY_SINGLE_PRICE`, since entry_range_low ==
entry_range_high). That is why the composite contract this fixture builds
carries `stop_mode="actual_entry_price"` and the leg's `planned_tpsl_json`
carries `break_even_reference_price="83800"` -- this is the exact contract
production would have built for this message on `6450ac67`/`2e442531`, not a
hypothetical one.

What was actually unverified before this test existed is what
`replace_remaining_protection` (`strategy_management_composite_executor.py`)
does once *execution* time arrives with the market already below 83800 (a
long stop above the market can never be armed there; the exchange would
trigger it instantly). Two scenarios below share the exact same batch/leg/
contract and differ only in the fake Deepcoin client's market price:

- 83900 (correct side for a long's stop at 83800): the ordinary path --
  reduce 5, arm the remaining 5's stop at 83800.
- 83700 (wrong side): the design's open question. This test proves the
  *current* code already retires the remainder at market instead of
  refusing or arming an instantly-triggering stop, exactly as
  `docs/ARCHITECTURE.md` section 4.8 ("有一种"改止损"最后不是改止损，而是
  平仓") already documents for `partial_then_break_even` in general, and as
  `tests/test_composite_remainder_market_close.py` already covers for
  synthetic prices. This file is the same mechanism pinned to the real
  binding-385 numbers so the Mia design's M5 question has a concrete,
  reproducible answer instead of an inferred one.

No exchange call, no AI call. Fake Deepcoin client only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
)
from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
    management_contract_fingerprint,
    serialize_management_contract,
)

NOW = datetime(2026, 9, 28, 13, 40, 0, tzinfo=UTC)

# The real text of raw_message_id 19597, verbatim.
RAW_19597_TEXT = (
    "目前BTC现价83800，加仓后浮盈600点，相当于正常仓位1200点收益，"
    "加仓后仓位比较大，减50%仓位，剩余仓位止损位上移至83200！\n"
    "@Tarderfengge QQ:158241758"
)

POS_ID = "pos-19597-385"
INSTRUMENT = "BTC-USDT-SWAP"
STRATEGY_ENTRY_PRICE = "83800"
STRATEGY_STOP_LOSS = "81800"
START_SIZE = "10"
REMAINING_SIZE = "5"


def _build_binding_385_component(session_factory):
    """The exact composite contract production would build for #19597.

    ``stop_mode="actual_entry_price"`` and the leg's break-even reference of
    83800 are the *result* of the already-deployed price-plausibility module
    discarding the message's explicit-but-looser 83200 -- not an assumption
    this test makes. Only the ``replace_remaining_protection`` component is
    left ``pending``; the take-profit-consumption and partial-close
    components are stamped ``confirmed`` because M5 is scoped to what happens
    once the reduction has already gone through and only the remaining
    position's protection is left to place.
    """

    contract = ManagementInstructionContract(
        version=2,
        target_lifecycle_id=1343,
        strategy_instance_id="deepcoin:-1003825498321:705:BTC:long",
        symbol="BTC",
        side="long",
        close_fraction="0.5",
        stop_mode="actual_entry_price",
        stop_price=None,
        stop_price_source=None,
        take_profit_consumption="consume_first_stage",
        cancel_deferred_entries=True,
        required_components=(
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        current_message_text=RAW_19597_TEXT,
    )
    contract_json = serialize_management_contract(contract)
    fingerprint = management_contract_fingerprint(contract)

    with session_factory() as session:
        raw = RawMessage(
            chat_id=-1003825498321, message_id=707, text=RAW_19597_TEXT, posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="非策略",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        lifecycle = StrategyLifecycle(
            id=1343,
            chat_id=-1003825498321,
            message_id=705,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            entry_range_low=83800.0,
            entry_range_high=83800.0,
            stop_loss=81800.0,
        )
        binding = ExecutionBinding(
            id=385,
            strategy_instance_id="deepcoin:-1003825498321:705:BTC:long",
            kol_id="mia",
            chat_id=-1003825498321,
            message_id=705,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id=POS_ID,
            status="active",
        )
        session.add_all([decision, lifecycle, binding])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        entry_leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            pos_id=POS_ID,
            venue="deepcoin",
            attribution_status="verified",
            response_json=json.dumps({"data": {"posId": POS_ID}}),
            status="active",
        )
        session.add(entry_leg)
        session.flush()

        batch = StrategyManagementBatch(
            idempotency_fingerprint="mia-19597-binding385",
            raw_message_id=raw.id,
            recognition_decision_id=decision.id,
            recognition_generation="mia-m5-replay",
            target_lifecycle_id=lifecycle.id,
            strategy_instance_id=binding.strategy_instance_id,
            execution_binding_id=binding.id,
            intent="partial_then_break_even",
            effective_action="partial_then_break_even",
            execution_mode="live",
            requested_fraction=0.5,
            effective_fraction=0.5,
            management_contract_json=contract_json,
            management_contract_fingerprint=fingerprint,
            contract_version=2,
            status="ready",
            target_fingerprint="mia-19597-target",
            target_snapshot_json=json.dumps(
                {
                    "identity": {
                        "target_lifecycle_id": lifecycle.id,
                        "deferred_entry_leg_ids": [],
                        "capability_deferred_entry_leg_ids": [],
                    },
                    "break_even_reference": {
                        "price": STRATEGY_ENTRY_PRICE,
                        "source": "strategy_single_price",
                    },
                    "positions": [
                        {
                            "pos_id": POS_ID,
                            "trusted_start_size": START_SIZE,
                            "target_remaining_size": REMAINING_SIZE,
                            "avg_entry_price": STRATEGY_ENTRY_PRICE,
                            "quantity_step": "1",
                            "min_quantity": "1",
                        }
                    ],
                }
            ),
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.flush()

        management_leg = StrategyManagementLeg(
            management_batch_id=batch.id,
            execution_order_leg_id=entry_leg.id,
            pos_id=POS_ID,
            leg_index=1,
            status="planned",
            preflight_size=START_SIZE,
            planned_close_size=REMAINING_SIZE,
            avg_entry_price=STRATEGY_ENTRY_PRICE,
            quantity_step="1",
            planned_tpsl_json=json.dumps(
                {
                    "intent": "partial_then_break_even",
                    "stop_loss_text": None,
                    # This is exactly what the price-plausibility module
                    # would have written for #19597: the message's 83200 was
                    # judged looser than 83800 and dropped.
                    "break_even_reference_price": STRATEGY_ENTRY_PRICE,
                    "break_even_reference_source": "strategy_single_price",
                }
            ),
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(management_leg)
        session.flush()

        components = []
        for sequence, kind in enumerate(contract.required_components):
            component = StrategyManagementComponent(
                management_batch_id=batch.id,
                strategy_management_leg_id=management_leg.id,
                strategy_management_leg_scope=management_leg.id,
                component_kind=kind,
                sequence=sequence,
                status="pending",
                idempotency_key=f"component:{kind}:mia-19597",
                desired_json=json.dumps(
                    {
                        "contract_fingerprint": fingerprint,
                        "pos_id": POS_ID,
                        "execution_order_leg_id": entry_leg.id,
                        "trusted_start_size": START_SIZE,
                        "target_remaining_size": REMAINING_SIZE,
                        "avg_entry_price": STRATEGY_ENTRY_PRICE,
                        "quantity_step": "1",
                        "min_quantity": "1",
                        "component_kind": kind,
                    },
                    sort_keys=True,
                ),
                evidence_json="[]",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(component)
            components.append(component)
        # The first two stages are already done: the reduction from 10 to 5
        # has already gone through the exchange. M5 is scoped to what the
        # third component does with the remainder's protection.
        components[0].status = "confirmed"
        components[0].completed_at = NOW
        components[1].status = "confirmed"
        components[1].completed_at = NOW
        session.flush()

        owner = {
            "venue": "deepcoin",
            "execution_binding_id": binding.id,
            "execution_order_leg_id": entry_leg.id,
            "strategy_instance_id": binding.strategy_instance_id,
            "pos_id": POS_ID,
            "instrument_id": INSTRUMENT,
            "side": "long",
            "evidence_source": "native_tpsl_pending_readback",
            "evidence_json": "{}",
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        }
        session.add_all(
            [
                PositionProtectionLedger(
                    **owner,
                    order_id="mia-tp-retained",
                    purpose="take_profit",
                    trigger_price="86000",
                    size_text=REMAINING_SIZE,
                    status="verified",
                ),
                PositionProtectionLedger(
                    **owner,
                    order_id="mia-stop-old",
                    purpose="stop_loss",
                    trigger_price=STRATEGY_STOP_LOSS,
                    size_text=REMAINING_SIZE,
                    status="verified",
                ),
            ]
        )
        session.commit()
        return batch.id, components[2].id


class _Binding385Client:
    """Read-only-until-written fake for the ``pos-19597-385`` position.

    ``mark_price`` is what the position row reports (what the executor reads
    first, cheaply); ``quote_price`` is the fresh, uncached ``last`` the
    fallback re-checks before closing anything (design 3.1 condition 6). Both
    default to the same value -- the two scenarios below only ever move both
    together, since a real quote and a real position mark do not disagree by
    hundreds of dollars in this replay.
    """

    def __init__(self, *, market_price: str):
        self.mark_price = market_price
        self.quote_price = market_price
        self.position_size = REMAINING_SIZE
        self.pending = [
            {
                "ordId": "mia-tp-retained", "posId": POS_ID, "instId": INSTRUMENT,
                "posSide": "long", "triggerOrderType": "TPSL",
                "tpTriggerPx": "86000", "sz": REMAINING_SIZE,
            },
            {
                "ordId": "mia-stop-old", "posId": POS_ID, "instId": INSTRUMENT,
                "posSide": "long", "triggerOrderType": "TPSL",
                "slTriggerPx": STRATEGY_STOP_LOSS, "sz": REMAINING_SIZE,
            },
        ]
        self.set_calls: list[dict] = []
        self.cancel_sltp_calls: list[dict] = []
        self.close_calls: list[dict] = []
        self.open_orders: list[dict] = []
        self.cancel_order_calls: list[dict] = []
        self.events: list[str] = []

    def list_positions(self, *, inst_id=None):
        if self.position_size in (None, "0"):
            return []
        return [
            {
                "posId": POS_ID, "instId": INSTRUMENT, "posSide": "long",
                "pos": self.position_size, "avgPx": STRATEGY_ENTRY_PRICE,
                "markPx": self.mark_price, "mgnMode": "cross",
                "mrgPosition": "split",
            }
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        self.events.append("readback")
        return list(self.pending)

    def list_open_orders(self, *, inst_id=None):
        return list(self.open_orders)

    def list_trigger_order_history(self, *, inst_id):
        return []

    def list_order_history(self, *, inst_id):
        return []

    def list_trade_fills(self, *, inst_id):
        return []

    def get_ticker_quote(self, *, inst_id):
        self.events.append("ticker")
        return {
            "instrument_id": INSTRUMENT, "price": self.quote_price,
            "price_field": "last",
        }

    def set_position_sltp(self, payload):
        self.set_calls.append(dict(payload))
        role = "primary" if len(self.set_calls) == 1 else "backup"
        self.events.append(f"set_{role}")
        order_id = f"mia-stop-new-{role}"
        self.pending.append(
            {
                "ordId": order_id, "posId": POS_ID, "instId": INSTRUMENT,
                "posSide": "long", "triggerOrderType": "TPSL",
                "slTriggerPx": payload["slTriggerPx"], "sz": payload["sz"],
            }
        )
        return {"code": "0", "data": {"ordId": order_id}}

    def cancel_position_sltp(self, payload):
        order_id = payload["ordId"]
        self.cancel_sltp_calls.append(dict(payload))
        self.events.append(f"cancel_{order_id}")
        self.pending = [row for row in self.pending if row["ordId"] != order_id]
        return {"code": "0", "data": {"ordId": order_id}}

    def cancel_trigger_order(self, payload):  # pragma: no cover - unused route
        raise AssertionError("this replay never cancels via cancel_trigger_order")

    def place_order(self, payload):
        self.close_calls.append(dict(payload))
        self.events.append("close")
        self.position_size = "0"
        return {"code": "0", "data": {"ordId": "mia-close-remainder-1"}}

    def cancel_order(self, payload):  # pragma: no cover - no deferred entries here
        self.cancel_order_calls.append(dict(payload))
        return {"code": "0", "data": {"ordId": "unused"}}


def _execute(session_factory, batch_id, component_id, client):
    from telegram_kol_research.strategy_management_composite_executor import (
        execute_protection_replacement_component,
    )

    return execute_protection_replacement_component(
        session_factory,
        batch_id=batch_id,
        component_id=component_id,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        price_tick="0.1",
        backup_buffer_bps="20",
    )


def test_binding_385_break_even_target_on_the_correct_side_arms_the_stop(tmp_path):
    """Market 83900: 83800 is a normal, placeable long stop.

    This is the everyday branch of #19597 -- the reduction has gone through
    (10 -> 5) and the remaining 5 gets its stop armed at the strategy price,
    83800, exactly as the Mia design's row for #19597 in its normal case
    describes. Nothing is closed.
    """

    session_factory = create_session_factory(tmp_path / "binding-385-correct-side.db")
    batch_id, component_id = _build_binding_385_component(session_factory)
    client = _Binding385Client(market_price="83900")

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "confirmed"
    assert client.close_calls == []
    assert [call["slTriggerPx"] for call in client.set_calls][0] == "83800"
    assert [call["sz"] for call in client.set_calls] == [REMAINING_SIZE, REMAINING_SIZE]
    assert client.cancel_sltp_calls == [{"ordId": "mia-stop-old"}] or (
        client.cancel_sltp_calls[0]["ordId"] == "mia-stop-old"
    )
    with session_factory() as session:
        rows = {
            row.order_id: row.status
            for row in session.query(PositionProtectionLedger)
        }
    assert rows["mia-stop-old"] == "cancelled"
    assert any(
        status == "verified" and order_id.startswith("mia-stop-new")
        for order_id, status in rows.items()
    )


def test_binding_385_break_even_target_on_the_wrong_side_closes_the_remainder(
    tmp_path,
):
    """Market 83700: the same 83800 target is now above the market.

    A long's stop must sit below the market to ever arm; at 83700 the
    strategy's break-even price for #19597 cannot be placed at all, and the
    KOL's own rule is "conflict means close at market". This is the exact
    open question M5 asked to have verified against the real numbers, and it
    answers with: the current code (unmodified by this task) already routes
    to `_begin_remainder_close` / the market-close fallback, not to a refusal
    and not to an order the exchange would trigger the instant it lands.

    Before-fix / after-fix note: this is a **pre-existing** behaviour, not a
    change made by this task. There is no "before" for this test to fail
    against; it is documented here as-is because the design explicitly asked
    to confirm it against binding 385's own numbers before deciding whether a
    fix was owed. It was not.
    """

    session_factory = create_session_factory(tmp_path / "binding-385-wrong-side.db")
    batch_id, component_id = _build_binding_385_component(session_factory)
    client = _Binding385Client(market_price="83700")

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "confirmed"
    assert result.reason_code is None or result.reason_code == ""
    # No stop was ever armed on the exchange for the remainder -- the whole
    # point is that 83800 could never be placed at 83700.
    assert client.set_calls == []
    assert [call["sz"] for call in client.close_calls] == [REMAINING_SIZE]
    assert client.close_calls[0]["posSide"] == "long"
    assert client.close_calls[0]["closePosId"] == POS_ID
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        evidence = json.loads(component.evidence_json)[-1]
    assert evidence["outcome"] == "remainder_closed_at_market"
    # The one flip side of this route: prove it by turning the condition off
    # in the same fixture. The original stop was never cancelled or replaced
    # -- it is left armed on the exchange until the position itself is gone
    # (docs/ARCHITECTURE.md 4.8: "先撤再平会制造裸仓窗口").
    with session_factory() as session:
        rows = {
            row.order_id: row.status
            for row in session.query(PositionProtectionLedger)
        }
    assert rows["mia-stop-old"] == "verified"


def test_the_wrong_side_result_is_specific_to_the_market_not_a_stuck_fixture(
    tmp_path,
):
    """Same fixture, same batch shape, only the price differs -- one flip.

    This is the paired negative case the repository's own testing lessons
    insist on (`docs/ARCHITECTURE.md` section 6: "同一个仓位、一处差异、两种
    结局"): rerun the identical #19597 fixture with the market moved back to
    the correct side and prove the outcome flips to the ordinary arm-the-stop
    branch, so the wrong-side result above is provably caused by the market
    price and not by some other property of this fixture.
    """

    correct_db = create_session_factory(tmp_path / "flip-correct.db")
    wrong_db = create_session_factory(tmp_path / "flip-wrong.db")

    correct_batch_id, correct_component_id = _build_binding_385_component(correct_db)
    wrong_batch_id, wrong_component_id = _build_binding_385_component(wrong_db)

    correct_result = _execute(
        correct_db, correct_batch_id, correct_component_id,
        _Binding385Client(market_price="83900"),
    )
    wrong_result = _execute(
        wrong_db, wrong_batch_id, wrong_component_id,
        _Binding385Client(market_price="83700"),
    )

    assert correct_result.status == "confirmed"
    assert wrong_result.status == "confirmed"
    with correct_db() as session:
        correct_evidence = json.loads(
            session.get(StrategyManagementComponent, correct_component_id)
            .evidence_json
        )[-1]
    with wrong_db() as session:
        wrong_evidence = json.loads(
            session.get(StrategyManagementComponent, wrong_component_id)
            .evidence_json
        )[-1]
    assert "new_stop_order_ids" in correct_evidence
    assert wrong_evidence["outcome"] == "remainder_closed_at_market"
