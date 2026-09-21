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
):
    """One batch with one management leg per entry in ``legs``.

    ``legs`` is a list of dicts: ``pos_id``, ``start_size``, ``target_size``
    and ``ledger`` (a list of ``(order_id, purpose, price, size, status)``).
    """

    contract = _contract()
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
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        binding = ExecutionBinding(
            strategy_instance_id="strategy-eth-long",
            kol_id="miya",
            chat_id=701,
            message_id=1,
            symbol="ETH",
            side="long",
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
                        side="long",
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
                pos_side="long",
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


def _batch_172_ledger():
    return [
        ("stop-primary", "stop_loss", "2600", "1.5", "verified"),
        ("stop-backup", "backup_stop", "2595", "1.5", "verified"),
        # TP1 filled by itself before the instruction arrived. Production
        # recorded it as `protection_missing`; the fill predicate is what
        # decides, not the status.
        ("tp-1", "take_profit", "2690", "0.7", "protection_missing"),
        ("tp-2", "take_profit", "2720", "0.4", "verified"),
        ("tp-3", "take_profit", "2750", "0.4", "verified"),
    ]


def _batch_172_pending():
    return [
        pending_stop_row(
            ord_id="stop-primary",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2600",
            size="1.5",
        ),
        pending_stop_row(
            ord_id="stop-backup",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2595",
            size="1.5",
        ),
        pending_take_profit_row(
            ord_id="tp-2",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2720",
            size="0.4",
        ),
        pending_take_profit_row(
            ord_id="tp-3",
            inst_id=INSTRUMENT,
            pos_side="long",
            trigger_price="2750",
            size="0.4",
        ),
    ]


def _batch_172(tmp_path, name="batch-172.db"):
    session_factory = create_session_factory(tmp_path / name)
    batch_id, components = _build_batch(
        session_factory,
        legs=[
            {
                "pos_id": BATCH_172_POS,
                "start_size": "0.8",
                "target_size": "0.4",
                "ledger": _batch_172_ledger(),
            }
        ],
    )
    client = _ProductionShapeClient(
        positions=[
            {
                **position_row(
                    pos_id=BATCH_172_POS,
                    inst_id=INSTRUMENT,
                    pos_side="long",
                    size="0.8",
                    avg_price="2650",
                ),
                # The market is above the long's entry, so a break-even stop at
                # 2650 is a legal stop and the remainder-close fallback must
                # stay out of the way.
                "lastPx": "2700",
            }
        ],
        pending=_batch_172_pending(),
        trigger_history=[
            trigger_history_row(
                ord_id="tp-1",
                inst_id=INSTRUMENT,
                pos_side="long",
                trigger_price="2690",
                size="0.7",
            )
        ],
    )
    return session_factory, batch_id, components, client


def test_batch_172_shape_runs_all_three_components_to_succeeded(tmp_path):
    session_factory, batch_id, _components, client = _batch_172(tmp_path)

    batch = _run(session_factory, batch_id, client)

    assert batch.status == "succeeded", batch.reason_code
    assert [row.status for row in batch.components] == ["confirmed"] * 3

    # Component one: TP1 is gone and proven filled, so nothing cancels it. TP2
    # and TP3 together are 0.8 against a 0.4 target, so exactly one stage is
    # released -- the earlier one.
    assert "tp-1" not in client.cancel_calls
    assert "tp-2" in client.cancel_calls
    assert "tp-3" not in client.cancel_calls

    # Component two: one reduction to the immutable target.
    assert [call["sz"] for call in client.close_calls] == ["0.4"]
    assert client._positions[BATCH_172_POS]["pos"] == "0.4"

    # Component three: two new stops written and read back, then -- and only
    # then -- the two old ones cancelled.
    assert len(client.set_calls) == 2
    assert client.cancel_calls[-2:] == ["stop-backup", "stop-primary"] or set(
        client.cancel_calls[-2:]
    ) == {"stop-primary", "stop-backup"}
    first_set = client.cancel_calls.index("stop-primary")
    assert first_set > client.cancel_calls.index("tp-2")


def test_batch_172_component_one_proves_the_fill_without_cancelling_it(tmp_path):
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
    assert client.cancel_calls == ["tp-2"]


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
