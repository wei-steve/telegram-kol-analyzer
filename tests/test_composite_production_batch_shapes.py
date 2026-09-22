"""The three production batch shapes, end to end, on the venue's real rows.

Batches 146, 150, 153, 159, 166 and 172 are every composite instruction the
system planned since 2026-09-04, and not one of them reached the exchange.  The
shapes below are those batches', reconstructed from the point queries recorded
in ``docs/plans/2026-09-21-composite-zero-success-root-cause.md``:

* **172** -- ETH long: an adopted stop for 1.5 and a backup stop, TP1 (2690
  x 0.7) already filled by itself, TP2 (2720 x 0.4) and TP3 (2750 x 0.4) still
  resting, live position 0.8.
* **159** -- two full-size stops and **no take profit at all**.  Component one
  could not even start: the instrument name was read from this leg's
  take-profit ledger rows, and there were none.
* **150** -- binding 320's shape: two legs on one instrument, each with its own
  take profits, which used to make each leg refuse because of the other's.

Every exchange row comes from ``tests/deepcoin_production_rows``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from deepcoin_production_rows import (
    pending_stop_row,
    pending_take_profit_row,
    position_row,
    trigger_history_row,
)

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection
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
from telegram_kol_research.strategy_management_composite_executor import (
    execute_composite_management_batch,
    execute_take_profit_consumption_component,
)
from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
    management_contract_fingerprint,
    serialize_management_contract,
)


NOW = datetime(2026, 9, 21, 7, 0, tzinfo=UTC).replace(tzinfo=None)
INSTRUMENT = "ETH-USDT-SWAP"
MESSAGE = "止盈50%，止损移动至开仓价"


class _ContractSpecs:
    def get_contract_spec(self, instrument_id):
        from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec

        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=1.0,
            quantity_step=0.1,
            min_quantity=0.1,
            price_tick=0.01,
        )


# --- Building one composite batch in the shape the planner writes ------------


def _contract(*, side="long"):
    return ManagementInstructionContract(
        version=2,
        target_lifecycle_id=1,
        strategy_instance_id="strategy-eth-long",
        symbol="ETH",
        side=side,
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
        current_message_text=MESSAGE,
    )


def _build_batch(
    session_factory,
    *,
    legs,
    avg_entry_price="2650",
    side="long",
):
    """One batch with one management leg per entry in ``legs``.

    ``legs`` is a list of dicts: ``pos_id``, ``start_size``, ``target_size``
    and ``ledger`` (a list of ``(order_id, purpose, price, size, status)``).
    """

    contract = _contract(side=side)
    contract_json = serialize_management_contract(contract)
    fingerprint = management_contract_fingerprint(contract)
    with session_factory() as session:
        raw = RawMessage(chat_id=701, message_id=2, text=MESSAGE, posted_at=NOW)
        session.add(raw)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="管理",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        lifecycle = StrategyLifecycle(
            id=1,
            chat_id=701,
            message_id=1,
            symbol="ETH",
            side=side,
            lifecycle_status="entered",
            signal_at=NOW,
        )
        binding = ExecutionBinding(
            strategy_instance_id="strategy-eth-long",
            kol_id="miya",
            chat_id=701,
            message_id=1,
            symbol="ETH",
            side=side,
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id=legs[0]["pos_id"],
            status="active",
        )
        session.add_all([decision, lifecycle, binding])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        batch = StrategyManagementBatch(
            idempotency_fingerprint="composite-production-shape",
            raw_message_id=raw.id,
            recognition_decision_id=decision.id,
            recognition_generation="gen-1",
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
            target_fingerprint="target-production-shape",
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.flush()
        positions = []
        component_ids: dict[str, list[int]] = {}
        for index, leg in enumerate(legs):
            entry_leg = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=index,
                purpose="entry",
                order_kind="market",
                pos_id=leg["pos_id"],
                venue="deepcoin",
                attribution_status="verified",
                response_json=json.dumps({"data": {"posId": leg["pos_id"]}}),
                status="active",
            )
            session.add(entry_leg)
            session.flush()
            positions.append(
                {
                    "pos_id": leg["pos_id"],
                    "trusted_start_size": leg["start_size"],
                    "target_remaining_size": leg["target_size"],
                    "avg_entry_price": avg_entry_price,
                    "quantity_step": "0.1",
                    "min_quantity": "0.1",
                }
            )
            management_leg = StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=entry_leg.id,
                pos_id=leg["pos_id"],
                leg_index=index,
                status="planned",
                preflight_size=leg["start_size"],
                planned_close_size=str(
                    float(leg["start_size"]) - float(leg["target_size"])
                ),
                avg_entry_price=avg_entry_price,
                quantity_step="0.1",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(management_leg)
            session.flush()
            for sequence, kind in enumerate(contract.required_components):
                component = StrategyManagementComponent(
                    management_batch_id=batch.id,
                    strategy_management_leg_id=management_leg.id,
                    strategy_management_leg_scope=management_leg.id,
                    component_kind=kind,
                    sequence=sequence,
                    status="pending",
                    idempotency_key=f"component:{leg['pos_id']}:{kind}",
                    desired_json=json.dumps(
                        {
                            "contract_fingerprint": fingerprint,
                            "pos_id": leg["pos_id"],
                            "execution_order_leg_id": entry_leg.id,
                            "trusted_start_size": leg["start_size"],
                            "target_remaining_size": leg["target_size"],
                            "avg_entry_price": avg_entry_price,
                            "quantity_step": "0.1",
                            "min_quantity": "0.1",
                            "component_kind": kind,
                        },
                        sort_keys=True,
                    ),
                    evidence_json="[]",
                    created_at=NOW,
                    updated_at=NOW,
                )
                session.add(component)
                session.flush()
                component_ids.setdefault(kind, []).append(component.id)
            for order_id, purpose, price, size, status in leg["ledger"]:
                session.add(
                    PositionProtectionLedger(
                        venue="deepcoin",
                        execution_binding_id=binding.id,
                        execution_order_leg_id=entry_leg.id,
                        strategy_instance_id=binding.strategy_instance_id,
                        pos_id=leg["pos_id"],
                        instrument_id=INSTRUMENT,
                        side=side,
                        order_id=order_id,
                        purpose=purpose,
                        trigger_price=price,
                        size_text=size,
                        status=status,
                        evidence_source="exchange_adopted_by_tu",
                        evidence_json="{}",
                        first_seen_at=NOW,
                        last_seen_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
        batch.target_snapshot_json = json.dumps(
            {
                "identity": {
                    "target_lifecycle_id": 1,
                    "deferred_entry_leg_ids": [],
                    "capability_deferred_entry_leg_ids": [],
                },
                "positions": positions,
            }
        )
        session.commit()
        return batch.id, component_ids


class _ProductionShapeClient:
    """A Deepcoin stub whose every row is the venue's own shape."""

    def __init__(self, *, positions, pending, trigger_history=()):
        self._positions = {row["posId"]: dict(row) for row in positions}
        self.pending = [dict(row) for row in pending]
        self.history = [dict(row) for row in trigger_history]
        self.cancel_calls: list[str] = []
        self.close_calls: list[dict] = []
        self.set_calls: list[dict] = []
        self._set_count = 0
        self.quote_price = "2700"

    def list_positions(self, *, inst_id=None):
        return [dict(row) for row in self._positions.values()]

    def list_trigger_orders_pending(self, *, inst_id):
        return [dict(row) for row in self.pending]

    def list_trigger_order_history(self, *, inst_id):
        return [dict(row) for row in self.history]

    def list_order_history(self, *, inst_id):
        return []

    def list_trade_fills(self, *, inst_id):
        return []

    def get_ticker_quote(self, *, inst_id):
        return {
            "instrument_id": INSTRUMENT,
            "price": self.quote_price,
            "price_field": "last",
        }

    def cancel_position_sltp(self, payload):
        order_id = str(payload["ordId"])
        self.cancel_calls.append(order_id)
        self.pending = [row for row in self.pending if row["ordId"] != order_id]
        return {"code": "0", "data": {"ordId": order_id}}

    def place_order(self, payload):
        """The close goes through ``POST /trade/order`` with ``closePosId``."""

        self.close_calls.append(dict(payload))
        row = self._positions[str(payload["closePosId"])]
        row["pos"] = str(round(float(row["pos"]) - float(payload["sz"]), 8))
        return {"code": "0", "data": {"ordId": f"close-{len(self.close_calls)}"}}

    def set_position_sltp(self, payload):
        self._set_count += 1
        self.set_calls.append(dict(payload))
        role = "primary" if self._set_count % 2 == 1 else "backup"
        order_id = f"stop-new-{role}-{self._set_count}"
        self.pending.append(
            pending_stop_row(
                ord_id=order_id,
                inst_id=INSTRUMENT,
                pos_side=str(payload["posSide"]),
                trigger_price=str(payload["slTriggerPx"]),
                size=str(payload["sz"]),
            )
        )
        return {"code": "0", "data": {"ordId": order_id}}


def _run(session_factory, batch_id, client):
    return execute_composite_management_batch(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )


# --- Batch 172 ---------------------------------------------------------------


BATCH_172_POS = "1001125231241310"

#: The ladder the strategy staged on the original 1.5, and the market price at
#: the moment the instruction arrived.  The short row is the long row mirrored
#: across the 2650 entry, so a break-even stop at 2650 is legal on both sides.
_BATCH_172_LADDER = {
    "long": {
        "stops": (("stop-primary", "2600"), ("stop-backup", "2595")),
        "take_profit_prices": ("2690", "2720", "2750"),
        "market": "2700",
    },
    "short": {
        "stops": (("stop-primary", "2700"), ("stop-backup", "2705")),
        "take_profit_prices": ("2610", "2580", "2550"),
        "market": "2600",
    },
}
_BATCH_172_TP_IDS = ("tp-1", "tp-2", "tp-3")


def _batch_172_ledger(side, *, tp1_pending, tp_sizes):
    ladder = _BATCH_172_LADDER[side]
    rows = [
        (order_id, purpose, price, "1.5", "verified")
        for (order_id, price), purpose in zip(
            ladder["stops"], ("stop_loss", "backup_stop"), strict=True
        )
    ]
    for index, order_id in enumerate(_BATCH_172_TP_IDS):
        # TP1 filled by itself before the instruction arrived. Production
        # recorded it as `protection_missing`; the fill predicate is what
        # decides, not the status.
        status = "verified" if index or tp1_pending else "protection_missing"
        rows.append(
            (
                order_id,
                "take_profit",
                ladder["take_profit_prices"][index],
                tp_sizes[index],
                status,
            )
        )
    return rows


def _batch_172_pending(side, *, tp1_pending, tp_sizes):
    ladder = _BATCH_172_LADDER[side]
    rows = [
        pending_stop_row(
            ord_id=order_id,
            inst_id=INSTRUMENT,
            pos_side=side,
            trigger_price=price,
            size="1.5",
        )
        for order_id, price in ladder["stops"]
    ]
    for index, order_id in enumerate(_BATCH_172_TP_IDS):
        if index == 0 and not tp1_pending:
            continue
        rows.append(
            pending_take_profit_row(
                ord_id=order_id,
                inst_id=INSTRUMENT,
                pos_side=side,
                trigger_price=ladder["take_profit_prices"][index],
                size=tp_sizes[index],
            )
        )
    return rows


def _batch_172(
    tmp_path,
    name="batch-172.db",
    *,
    side="long",
    tp1_pending=False,
    tp_sizes=("0.7", "0.4", "0.4"),
):
    """Batch 172's shape, with or without TP1 already filled.

    ``target_size`` is what the planner would have written for this live size:
    ``allocate_close_sizes`` floors the aggregate to the 0.1 quantity step, so
    half of 1.5 is 0.7 (not 0.75), leaving 0.8, and half of 0.8 is 0.4.
    """

    ladder = _BATCH_172_LADDER[side]
    live_size = "1.5" if tp1_pending else "0.8"
    target_size = "0.8" if tp1_pending else "0.4"
    session_factory = create_session_factory(tmp_path / name)
    batch_id, components = _build_batch(
        session_factory,
        side=side,
        legs=[
            {
                "pos_id": BATCH_172_POS,
                "start_size": live_size,
                "target_size": target_size,
                "ledger": _batch_172_ledger(
                    side, tp1_pending=tp1_pending, tp_sizes=tp_sizes
                ),
            }
        ],
    )
    client = _ProductionShapeClient(
        positions=[
            {
                **position_row(
                    pos_id=BATCH_172_POS,
                    inst_id=INSTRUMENT,
                    pos_side=side,
                    size=live_size,
                    avg_price="2650",
                ),
                # The market is on the profitable side of the entry, so a
                # break-even stop at 2650 is a legal stop and the
                # remainder-close fallback must stay out of the way.
                "lastPx": ladder["market"],
            }
        ],
        pending=_batch_172_pending(
            side, tp1_pending=tp1_pending, tp_sizes=tp_sizes
        ),
        trigger_history=(
            []
            if tp1_pending
            else [
                trigger_history_row(
                    ord_id="tp-1",
                    inst_id=INSTRUMENT,
                    pos_side=side,
                    trigger_price=ladder["take_profit_prices"][0],
                    size=tp_sizes[0],
                )
            ]
        ),
    )
    return session_factory, batch_id, components, client


def _component(batch, kind):
    return next(row for row in batch.components if row.component_kind == kind)


def _evidence(component):
    return [item for item in (component.evidence or []) if isinstance(item, dict)]


def test_batch_172_shape_runs_all_three_components_to_succeeded(tmp_path):
    """The user's policy of 2026-09-22: a filled TP1 is the reduction."""

    session_factory, batch_id, _components, client = _batch_172(tmp_path)

    batch = _run(session_factory, batch_id, client)

    assert batch.status == "succeeded", batch.reason_code
    assert [row.status for row in batch.components] == ["confirmed"] * 3

    # Component one: TP1 is gone and proven filled, so nothing cancels it --
    # and because no second reduction follows, the target remaining is the live
    # 0.8, which TP2 + TP3 (0.4 + 0.4) exactly meet. Neither is excess.
    assert "tp-1" not in client.cancel_calls
    assert "tp-2" not in client.cancel_calls
    assert "tp-3" not in client.cancel_calls

    # Component two: the first take profit already took the profit. Nothing is
    # closed a second time, and no exchange write happens at all.
    assert client.close_calls == []
    assert client._positions[BATCH_172_POS]["pos"] == "0.8"
    partial = _component(batch, "converge_partial_close")
    assert partial.reason_code == "partial_close_skipped_first_take_profit_filled"
    assert any(
        item.get("planned_close_size") == "0" and item.get("remaining_size") == "0.8"
        for item in _evidence(partial)
    )

    # Component three: the stops still move to the break-even target, sized to
    # the whole remaining position, and only then are the old ones cancelled.
    assert len(client.set_calls) == 2
    assert [call["sz"] for call in client.set_calls] == ["0.8", "0.8"]
    assert client.set_calls[0]["slTriggerPx"] == "2650"
    assert len({call["slTriggerPx"] for call in client.set_calls}) == 2
    assert sorted(client.cancel_calls) == ["stop-backup", "stop-primary"]


def test_batch_172_component_one_proves_the_fill_without_cancelling_anything(
    tmp_path,
):
    session_factory, batch_id, components, client = _batch_172(
        tmp_path, "batch-172-one.db"
    )

    result = execute_take_profit_consumption_component(
        session_factory,
        batch_id=batch_id,
        component_id=components["consume_take_profit_stage"][0],
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    assert result.status == "confirmed", result.reason_code
    assert result.proven_filled_quantity == "0.7"
    assert client.cancel_calls == []
    with session_factory() as session:
        component = session.get(
            StrategyManagementComponent,
            components["consume_take_profit_stage"][0],
        )
        history = json.loads(component.evidence_json)
    assert any(
        item.get("first_stage_consumed_by_fill") is True
        and item.get("evidence_tier") == "trigger_history_clean_trigger"
        and item.get("effective_target_remaining_size") == "0.8"
        for item in history
        if isinstance(item, dict)
    )


def test_batch_172_with_the_first_take_profit_still_pending_reduces(tmp_path):
    """The other half of the policy: not filled yet -> cancel TP1 and reduce."""

    session_factory, batch_id, _components, client = _batch_172(
        tmp_path, "batch-172-pending.db", tp1_pending=True
    )

    batch = _run(session_factory, batch_id, client)

    assert batch.status == "succeeded", batch.reason_code
    # Component one cancels the first stage and nothing else: TP2 + TP3 = 0.8
    # is exactly the 0.8 target remaining.
    assert client.cancel_calls[0] == "tp-1"
    assert "tp-2" not in client.cancel_calls
    assert "tp-3" not in client.cancel_calls
    # Component two reduces by the contract fraction, floored to the step.
    assert [call["sz"] for call in client.close_calls] == ["0.7"]
    assert client._positions[BATCH_172_POS]["pos"] == "0.8"
    partial = _component(batch, "converge_partial_close")
    assert partial.reason_code is None
    assert [call["sz"] for call in client.set_calls] == ["0.8", "0.8"]


def test_batch_172_restart_between_components_keeps_the_same_decision(tmp_path):
    """Component two reads the decision from the record, not from memory."""

    session_factory, batch_id, components, client = _batch_172(
        tmp_path, "batch-172-restart.db"
    )
    result = execute_take_profit_consumption_component(
        session_factory,
        batch_id=batch_id,
        component_id=components["consume_take_profit_stage"][0],
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )
    assert result.status == "confirmed", result.reason_code

    # A restart between component one and component two: nothing but the rows
    # survives.
    restarted = create_session_factory(tmp_path / "batch-172-restart.db")
    batch = _run(restarted, batch_id, client)

    assert batch.status == "succeeded", batch.reason_code
    assert client.close_calls == []
    assert client._positions[BATCH_172_POS]["pos"] == "0.8"


def test_batch_172_short_mirror_does_not_reduce_again(tmp_path):
    session_factory, batch_id, _components, client = _batch_172(
        tmp_path, "batch-172-short.db", side="short"
    )

    batch = _run(session_factory, batch_id, client)

    assert batch.status == "succeeded", batch.reason_code
    # Only the two old stops were cancelled: the short's take-profit ladder is
    # 2610 / 2580 / 2550, TP1 filled, and TP2 + TP3 are exactly the live 0.8.
    assert client.cancel_calls == ["stop-backup", "stop-primary"]
    assert client.close_calls == []
    assert client._positions[BATCH_172_POS]["pos"] == "0.8"
    assert [call["sz"] for call in client.set_calls] == ["0.8", "0.8"]
    assert client.set_calls[0]["slTriggerPx"] == "2650"


def test_batch_172_first_take_profit_filling_mid_cancel_does_not_reduce(tmp_path):
    """The race: TP1 was resting when planned and filled before the cancel."""

    session_factory, batch_id, _components, client = _batch_172(
        tmp_path, "batch-172-race.db", tp1_pending=True
    )
    resting_cancel = client.cancel_position_sltp

    def cancel_position_sltp(payload):
        if str(payload["ordId"]) != "tp-1":
            return resting_cancel(payload)
        # The stage filled between the plan and the write, so the venue refuses
        # the cancel outright and the position is already 0.8.
        client.cancel_calls.append("tp-1")
        client.pending = [row for row in client.pending if row["ordId"] != "tp-1"]
        client._positions[BATCH_172_POS]["pos"] = "0.8"
        client.history = [
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="2690",
                size="0.7",
            )
        ]
        raise DeepcoinDefiniteRejection("order does not exist")

    client.cancel_position_sltp = cancel_position_sltp

    batch = _run(session_factory, batch_id, client)

    # The fill is still a fill, so the second reduction is still skipped even
    # though ``trusted_start_size`` is the pre-fill 1.5 -- the effective target
    # is what is live, not what the snapshot said.
    assert client.close_calls == []
    assert client._positions[BATCH_172_POS]["pos"] == "0.8"
    assert "tp-2" not in client.cancel_calls
    assert "tp-3" not in client.cancel_calls
    consume = _component(batch, "consume_take_profit_stage")
    assert consume.status == "confirmed"
    assert any(
        item.get("fill_race") is True
        and item.get("first_stage_consumed_by_fill") is True
        for item in _evidence(consume)
    )
    partial = _component(batch, "converge_partial_close")
    assert partial.status == "confirmed"
    assert partial.reason_code == "partial_close_skipped_first_take_profit_filled"

    # Component three then stops, and for a reason that has nothing to do with
    # this policy and predates it: only ``protection_health``'s reconciliation
    # round marks a filled take profit's ledger row, so within the race window
    # TP1's row is still ``verified`` and the three stages add up to more than
    # the position. Before this change the same shape reached the same place by
    # the already-at-target path. Writing no stop is the safe half of it: the
    # original 1.5 stop is still armed.
    assert batch.status == "recovery_required"
    assert batch.reason_code == "retained_take_profit_exceeds_position"
    assert client.set_calls == []


def test_batch_172_over_staged_ladder_still_releases_the_excess(tmp_path):
    """Not reducing never means leaving more take profit than position."""

    session_factory, batch_id, _components, client = _batch_172(
        tmp_path,
        "batch-172-overstaged.db",
        tp_sizes=("0.7", "0.4", "0.7"),
    )

    batch = _run(session_factory, batch_id, client)

    assert batch.status == "succeeded", batch.reason_code
    # TP2 + TP3 would be 1.1 against a live 0.8, so the earlier stage is
    # released and TP3 (0.7) is retained.
    assert client.cancel_calls[0] == "tp-2"
    assert "tp-3" not in client.cancel_calls
    assert client.close_calls == []
    assert client._positions[BATCH_172_POS]["pos"] == "0.8"


def test_batch_172_without_fill_evidence_refuses_instead_of_guessing(tmp_path):
    """The position is 0.8 against a 1.5 stop, and that is not proof."""

    session_factory, batch_id, components, client = _batch_172(
        tmp_path, "batch-172-noevidence.db"
    )
    client.history = []

    result = execute_take_profit_consumption_component(
        session_factory,
        batch_id=batch_id,
        component_id=components["consume_take_profit_stage"][0],
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "take_profit_terminal_state_unknown"
    assert client.cancel_calls == []


def test_batch_172_with_a_failed_trigger_refuses(tmp_path):
    session_factory, batch_id, components, client = _batch_172(
        tmp_path, "batch-172-failed.db"
    )
    client.history = [
        trigger_history_row(
            ord_id="tp-1",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2690",
            size="0.7",
            error_code="51004",
            error_message="rejected",
        )
    ]

    result = execute_take_profit_consumption_component(
        session_factory,
        batch_id=batch_id,
        component_id=components["consume_take_profit_stage"][0],
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    assert result.status == "recovery_required"
    assert client.cancel_calls == []


def test_batch_172_with_an_unattributable_take_profit_freezes(tmp_path):
    """A same-side take profit nothing can place stops the whole component."""

    session_factory, batch_id, components, client = _batch_172(
        tmp_path, "batch-172-unowned.db"
    )
    client.pending.append(
        pending_take_profit_row(
            ord_id="someone-elses-tp",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2800",
            size="3",
        )
    )

    result = execute_take_profit_consumption_component(
        session_factory,
        batch_id=batch_id,
        component_id=components["consume_take_profit_stage"][0],
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "take_profit_unattributable_pending_order"
    assert client.cancel_calls == []


def test_batch_172_ledger_price_drift_refuses(tmp_path):
    session_factory, batch_id, components, client = _batch_172(
        tmp_path, "batch-172-drift.db"
    )
    for row in client.pending:
        if row["ordId"] == "tp-2":
            row["tpTriggerPrice"] = "2721"
            row["closeTPTriggerPrice"] = "2721"

    result = execute_take_profit_consumption_component(
        session_factory,
        batch_id=batch_id,
        component_id=components["consume_take_profit_stage"][0],
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "take_profit_order_identity_conflict"
    assert client.cancel_calls == []


# --- Batch 159 ---------------------------------------------------------------


BATCH_159_POS = "1001125123045253"


def test_batch_159_shape_two_stops_and_no_take_profit_still_runs(tmp_path):
    """Component one has nothing to consume, and that is a confirmation."""

    session_factory = create_session_factory(tmp_path / "batch-159.db")
    batch_id, components = _build_batch(
        session_factory,
        legs=[
            {
                "pos_id": BATCH_159_POS,
                "start_size": "6",
                "target_size": "3",
                "ledger": [
                    ("1001125123045252", "stop_loss", "2600", "6", "verified"),
                    ("1001125123048630", "backup_stop", "2595", "0", "verified"),
                ],
            }
        ],
    )
    client = _ProductionShapeClient(
        positions=[
            {
                **position_row(
                    pos_id=BATCH_159_POS,
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    size="6",
                    avg_price="2650",
                ),
                "lastPx": "2700",
            }
        ],
        pending=[
            pending_stop_row(
                ord_id="1001125123045252",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="2600",
                size="6",
                close_trigger_price="",
            ),
            pending_stop_row(
                ord_id="1001125123048630",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="2595",
                size="0",
            ),
        ],
    )

    batch = _run(session_factory, batch_id, client)

    assert batch.status == "succeeded", batch.reason_code
    consume = next(
        row
        for row in batch.components
        if row.component_kind == "consume_take_profit_stage"
    )
    assert consume.status == "confirmed"
    assert any(
        item.get("evidence_tier") == "no_take_profit_ledger_row"
        for item in (consume.evidence or [])
        if isinstance(item, dict)
    )
    assert [call["sz"] for call in client.close_calls] == ["3"]
    # The stops were replaced, and nothing was cancelled before the new pair.
    assert len(client.set_calls) == 2
    assert sorted(client.cancel_calls) == [
        "1001125123045252",
        "1001125123048630",
    ]


# --- Batch 150 ---------------------------------------------------------------


BATCH_150_POS_A = "1001125140000001"
BATCH_150_POS_B = "1001125140000002"


def test_batch_150_shape_two_legs_on_one_instrument_do_not_conflict(tmp_path):
    """Binding 320: each leg sees the other's take profits and must not refuse."""

    session_factory = create_session_factory(tmp_path / "batch-150.db")
    batch_id, components = _build_batch(
        session_factory,
        legs=[
            {
                "pos_id": BATCH_150_POS_A,
                "start_size": "11",
                "target_size": "5.5",
                "ledger": [
                    ("a-stop", "stop_loss", "2600", "11", "verified"),
                    ("a-backup", "backup_stop", "2595", "11", "verified"),
                    ("a-tp-1", "take_profit", "2700", "5.5", "verified"),
                ],
            },
            {
                "pos_id": BATCH_150_POS_B,
                "start_size": "11",
                "target_size": "5.5",
                "ledger": [
                    ("b-stop", "stop_loss", "2600", "11", "verified"),
                    ("b-backup", "backup_stop", "2595", "11", "verified"),
                    ("b-tp-1", "take_profit", "2700", "5.5", "verified"),
                ],
            },
        ],
    )
    pending = []
    for prefix, _pos in (("a", BATCH_150_POS_A), ("b", BATCH_150_POS_B)):
        pending.extend(
            [
                pending_stop_row(
                    ord_id=f"{prefix}-stop",
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    trigger_price="2600",
                    size="11",
                ),
                pending_stop_row(
                    ord_id=f"{prefix}-backup",
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    trigger_price="2595",
                    size="11",
                ),
                pending_take_profit_row(
                    ord_id=f"{prefix}-tp-1",
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    trigger_price="2700",
                    size="5.5",
                ),
            ]
        )
    client = _ProductionShapeClient(
        positions=[
            position_row(
                pos_id=pos_id,
                inst_id=INSTRUMENT,
                pos_side="long",
                size="11",
                avg_price="2650",
            )
            for pos_id in (BATCH_150_POS_A, BATCH_150_POS_B)
        ],
        pending=pending,
    )

    for component_id in components["consume_take_profit_stage"]:
        result = execute_take_profit_consumption_component(
            session_factory,
            batch_id=batch_id,
            component_id=component_id,
            deepcoin_client=client,
            live_execution_gate=lambda: True,
            now_provider=lambda: NOW,
        )
        assert result.status == "confirmed", result.reason_code

    # Each leg released exactly its own first stage and touched nothing else.
    assert sorted(client.cancel_calls) == ["a-tp-1", "b-tp-1"]
