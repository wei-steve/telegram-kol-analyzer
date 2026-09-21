"""`partial_then_break_even`: market-close the remainder when break-even cannot arm.

Design: `docs/plans/2026-09-21-composite-remainder-market-close-design.md`.

The shape this covers: the reduction half of a composite instruction succeeds,
and then the break-even stop turns out to sit on the wrong side of the market,
so it can never be armed. Before this change the component stopped for a person
and the remaining position kept its original, far-away stop. Now, and only when
all six of design 3.1's conditions hold, the remaining position is closed at
market instead -- the same disposition `break_even_by_market` already reaches
for a single position.

Nothing on this route cancels or moves a protection order: the original stop
stays armed on the exchange until the position is flat, and the exchange
invalidates the position's TPSL along with the position.
"""

from __future__ import annotations

import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection
from telegram_kol_research.models import (
    PositionMutationIntent,
    PositionProtectionLedger,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
)

from test_strategy_management_executor import (
    NOW,
    _CompositeRemainderCloseClient as _RemainderCloseClient,
    _prepare_composite_protection_component,
    _stamp_composite_break_even_reference,
)


# The long fixture's remaining position is 5 at a mark of 65000. A break-even
# reference of 65500 is above the market, so a long's stop can never be armed
# there -- this is exactly `requested_stop_market_side_invalid`.
PASSED_REFERENCE = "65500"


def _prepare_passed_reference_component(session_factory, *, reference=PASSED_REFERENCE):
    batch_id, component_id = _prepare_composite_protection_component(session_factory)
    _stamp_composite_break_even_reference(session_factory, batch_id, price=reference)
    return batch_id, component_id


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


def _persist_partial_close_intent(session_factory, *, batch_id, status):
    """The reduction's own close intent, exactly as the gateway leaves it."""

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        leg = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch_id)
            .one()
        )
        partial = (
            session.query(StrategyManagementComponent)
            .filter(
                StrategyManagementComponent.management_batch_id == batch_id,
                StrategyManagementComponent.component_kind
                == "converge_partial_close",
            )
            .one()
        )
        intent = PositionMutationIntent(
            idempotency_key=f"{partial.id}:close:attempt:1",
            venue="deepcoin",
            operation="close_position",
            strategy_instance_id=batch.strategy_instance_id,
            execution_binding_id=batch.execution_binding_id,
            execution_order_leg_id=leg.execution_order_leg_id,
            pos_id="pos-composite",
            authority_fingerprint="a" * 64,
            request_fingerprint="b" * 64,
            status=status,
            request_json="{}",
            reserved_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(intent)
        session.commit()
        return intent.id


def test_remainder_close_waits_for_the_reduction_intent_before_submitting(tmp_path):
    """Design 9.3: the reduction's own close intent blocks the remainder close.

    The partial-close component confirms on position size, not on its intent, so
    on the inline path its `close_position` intent is still `submitted` when the
    protection component runs. `PositionMutationGateway._has_other_unresolved_close`
    matches on (pos_id, execution_order_leg_id) and would `blocked` the remainder
    close outright. The fallback therefore reconciles first and waits, rather
    than burning an attempt on a block.
    """

    session_factory = create_session_factory(tmp_path / "remainder-waits.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    intent_id = _persist_partial_close_intent(
        session_factory, batch_id=batch_id, status="submitted"
    )
    client = _RemainderCloseClient()

    first = _execute(session_factory, batch_id, component_id, client)

    assert first.status == "recovery_required"
    assert first.reason_code == "remainder_close_waiting_partial_close_confirmation"
    assert client.close_calls == []
    # Nothing on the protection side was touched while waiting.
    assert not any(
        event.startswith("set_") or event.startswith("cancel_")
        for event in client.events
    )

    with session_factory() as session:
        session.get(PositionMutationIntent, intent_id).status = "confirmed"
        session.commit()

    second = _execute(session_factory, batch_id, component_id, client)

    assert second.status == "confirmed"
    assert [call["sz"] for call in client.close_calls] == ["5"]
    assert client.close_calls[0]["closePosId"] == "pos-composite"
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        evidence = json.loads(component.evidence_json)
    assert evidence[-1]["outcome"] == "remainder_closed_at_market"
    assert evidence[-1]["remaining_size"] == "0"


# ---------------------------------------------------------------------------
# Design 3.1: every condition, negated. Each negation keeps today's behaviour.
# ---------------------------------------------------------------------------


def _make_contract_explicit_price(session_factory, batch_id, *, stop_price):
    from telegram_kol_research.strategy_management_contracts import (
        load_management_contract,
        management_contract_fingerprint,
    )

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        payload = json.loads(batch.management_contract_json)
        payload.update(
            stop_mode="explicit_price",
            stop_price=stop_price,
            stop_price_source="current_message_text",
        )
        batch.management_contract_json = json.dumps(payload)
        fingerprint = management_contract_fingerprint(
            load_management_contract(batch.management_contract_json)
        )
        batch.management_contract_fingerprint = fingerprint
        for component in session.query(StrategyManagementComponent).filter(
            StrategyManagementComponent.management_batch_id == batch_id
        ):
            desired = json.loads(component.desired_json)
            desired["contract_fingerprint"] = fingerprint
            component.desired_json = json.dumps(desired, sort_keys=True)
        session.commit()


def test_an_explicit_message_price_that_cannot_arm_still_stops_for_a_person(tmp_path):
    """Design 3.1 condition 2, and commander's ruling 3.

    A price a person wrote in a message is not a break-even instruction, so
    "this stop cannot be armed" is not evidence that they wanted out. It keeps
    today's `operator_required`, and nothing is closed.
    """

    session_factory = create_session_factory(tmp_path / "explicit-price.db")
    batch_id, component_id = _prepare_composite_protection_component(session_factory)
    _make_contract_explicit_price(session_factory, batch_id, stop_price="65500")
    client = _RemainderCloseClient()

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "operator_required"
    assert result.reason_code == "requested_stop_market_side_invalid"
    assert client.close_calls == []
    assert client.events == []


def test_a_component_that_already_started_placing_stops_never_closes(tmp_path):
    """Design 3.1 condition 3: the two routes never cross over mid-attempt."""

    session_factory = create_session_factory(tmp_path / "already-placing.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        desired = json.loads(component.desired_json)
        desired["protection_replacement_execution"] = {
            "primary_stop": "64000",
            "backup_stop": "63872",
            "old_stop_order_ids": ["stop-old-primary"],
            "retained_take_profit_total": "0",
        }
        component.desired_json = json.dumps(desired, sort_keys=True)
        session.commit()
    client = _RemainderCloseClient()

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "operator_required"
    assert result.reason_code == "requested_stop_market_side_invalid"
    assert client.close_calls == []
    assert client.events == []


def test_a_closed_live_gate_closes_nothing_and_reads_no_quote(tmp_path):
    """Design 3.1 condition 5: the same gate the reduction half writes under."""

    session_factory = create_session_factory(tmp_path / "gate-closed.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient()

    from telegram_kol_research.strategy_management_composite_executor import (
        execute_protection_replacement_component,
    )

    result = execute_protection_replacement_component(
        session_factory,
        batch_id=batch_id,
        component_id=component_id,
        deepcoin_client=client,
        live_execution_gate=lambda: False,
        now_provider=lambda: NOW,
        price_tick="0.1",
        backup_buffer_bps="20",
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "live_execution_disabled"
    assert client.close_calls == []
    assert "ticker" not in client.events


def test_a_fresh_quote_that_disagrees_with_the_position_row_closes_nothing(tmp_path):
    """Design 3.1 condition 6.

    The position row's mark can be a whole reconcile round old. When the
    uncached `last` says the stop is placeable after all, the next attempt goes
    back through the normal preflight and arms it -- one tick later, nothing
    closed.
    """

    session_factory = create_session_factory(tmp_path / "quote-disagrees.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient()
    client.quote_price = "66000"  # above the 65500 reference: placeable again

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "recovery_required"
    assert result.reason_code == "break_even_market_side_disagreement"
    assert client.close_calls == []
    assert [event for event in client.events if event.startswith("set_")] == []
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        assert "remainder_close_execution" not in json.loads(component.desired_json)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda client: setattr(client, "get_ticker_quote", _raise_quote),
            id="raises",
        ),
        pytest.param(
            lambda client: setattr(
                client, "get_ticker_quote", lambda *, inst_id: {"price": "65000"}
            ),
            id="no_instrument_or_field",
        ),
        pytest.param(
            lambda client: setattr(
                client,
                "get_ticker_quote",
                lambda *, inst_id: {
                    "instrument_id": "BTC-USDT-SWAP",
                    "price": "65000",
                    "price_field": "markPx",
                },
            ),
            id="wrong_price_field",
        ),
        pytest.param(
            lambda client: setattr(
                client,
                "get_ticker_quote",
                lambda *, inst_id: {
                    "instrument_id": "ETH-USDT-SWAP",
                    "price": "65000",
                    "price_field": "last",
                },
            ),
            id="other_instrument",
        ),
    ],
)
def test_an_unusable_quote_closes_nothing(tmp_path, mutate):
    """Design 3.1 condition 6: not knowing the price is never a reason to act."""

    session_factory = create_session_factory(tmp_path / "quote-unusable.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient()
    mutate(client)

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "recovery_required"
    assert result.reason_code == "break_even_market_quote_unavailable"
    assert client.close_calls == []


def test_an_existing_tighter_stop_is_kept_and_the_fallback_is_unreachable():
    """Design 1.2: `keep_tighter_stop` and the fallback are mutually exclusive.

    If any existing stop is both tighter than the target and still on the live
    side of the market, then the target is on the live side too, so
    `plan_composite_stop_replacement` returns instead of raising. There is no
    input on which an armed tighter stop and a market-close fallback can both
    be reached.
    """

    from telegram_kol_research.strategy_management_market_policy import (
        plan_composite_stop_replacement,
    )

    decision = plan_composite_stop_replacement(
        side="short",
        requested_stop="80500",
        market_price="80400",
        price_tick="0.1",
        backup_buffer_bps="20",
        existing_stop_prices=["80450"],
    )

    assert decision.action == "keep_tighter_stop"
    assert decision.primary_stop == "80450"


def _raise_quote(*, inst_id):
    raise RuntimeError("ticker unavailable")


# ---------------------------------------------------------------------------
# Design 3.2 step 5: what each gateway outcome means.
# ---------------------------------------------------------------------------


def test_an_unknown_close_outcome_is_never_resent(tmp_path):
    session_factory = create_session_factory(tmp_path / "close-unknown.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="unknown")

    first = _execute(session_factory, batch_id, component_id, client)
    second = _execute(session_factory, batch_id, component_id, client)

    assert first.status == second.status == "awaiting_exchange"
    assert first.reason_code == "remainder_close_outcome_unknown"
    assert len(client.close_calls) == 1


def test_a_definitely_rejected_close_retries_with_a_fresh_attempt_key(tmp_path):
    session_factory = create_session_factory(tmp_path / "close-rejected.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="rejected")

    first = _execute(session_factory, batch_id, component_id, client)
    client.remainder_close_outcome = "confirmed"
    second = _execute(session_factory, batch_id, component_id, client)

    assert first.status == "recovery_required"
    assert first.reason_code == "remainder_close_definitely_rejected"
    assert second.status == "confirmed"
    with session_factory() as session:
        keys = sorted(
            row.idempotency_key
            for row in session.query(PositionMutationIntent)
        )
    assert keys == [
        f"{component_id}:close:remainder:attempt:1",
        f"{component_id}:close:remainder:attempt:2",
    ]
    assert client.close_calls[0]["clOrdId"] != client.close_calls[1]["clOrdId"]


def test_a_blocked_close_keeps_the_gateways_own_reason(tmp_path):
    """The gateway's pre-write refusals stay the gateway's, verbatim.

    Here the live gate flips between the fallback's own check and the
    gateway's, which is a real race; the point is that the refusal reason is
    reported as itself rather than folded into a fallback-specific code, and
    that nothing was submitted.
    """

    from telegram_kol_research.strategy_management_composite_executor import (
        execute_protection_replacement_component,
    )

    session_factory = create_session_factory(tmp_path / "close-blocked.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient()
    gate_calls = []

    def flipping_gate():
        gate_calls.append(1)
        return len(gate_calls) == 1

    result = execute_protection_replacement_component(
        session_factory,
        batch_id=batch_id,
        component_id=component_id,
        deepcoin_client=client,
        live_execution_gate=flipping_gate,
        now_provider=lambda: NOW,
        price_tick="0.1",
        backup_buffer_bps="20",
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "live_execution_disabled"
    assert client.close_calls == []
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
    assert intent.status == "blocked"


def test_a_submitted_close_that_has_not_landed_yet_waits(tmp_path):
    session_factory = create_session_factory(tmp_path / "close-not-flat.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="accepted")

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "awaiting_exchange"
    assert result.reason_code == "remainder_close_not_yet_flat"
    assert len(client.close_calls) == 1


# ---------------------------------------------------------------------------
# Design 3.2 step 2: the batch's own unfilled entry legs.
# ---------------------------------------------------------------------------


def _add_deferred_entry_leg(session_factory, batch_id, *, pos_id=None, status="pending"):
    from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        deferred = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="limit",
            client_order_id="entry-leg-2",
            pos_id=pos_id,
            venue="deepcoin",
            attribution_status="verified" if pos_id else "unassigned",
            status=status,
        )
        session.add(deferred)
        session.flush()
        snapshot = json.loads(batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = [deferred.id]
        batch.target_snapshot_json = json.dumps(snapshot)
        session.commit()
        return deferred.id


def test_an_unfilled_entry_leg_is_cancelled_before_the_remainder_close(tmp_path):
    """Design 3.2 step 2, and the reason it exists.

    A KOL who says "reduce and move the stop to cost" must not have a pending
    second entry leg fill afterwards and re-grow the position that was just
    closed.
    """

    session_factory = create_session_factory(tmp_path / "cancel-entry.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    deferred_id = _add_deferred_entry_leg(session_factory, batch_id)
    client = _RemainderCloseClient()
    client.open_orders = [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "entry-regular-2",
            "clOrdId": "entry-leg-2",
            "posSide": "long",
        }
    ]

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "confirmed"
    assert client.cancel_order_calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "clOrdId": "entry-leg-2",
            "mrgPosition": "split",
        }
    ]
    assert client.events.index("cancel_entry") < client.events.index("close")
    from telegram_kol_research.models import ExecutionOrderLeg

    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, deferred_id).status == "cancelled"
        component = session.get(StrategyManagementComponent, component_id)
        evidence = json.loads(component.evidence_json)[-1]
    assert evidence["cancelled_deferred_entry_leg_ids"] == [deferred_id]


def test_no_unfilled_entry_leg_means_no_read_and_no_cancel(tmp_path):
    session_factory = create_session_factory(tmp_path / "no-entry-legs.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient()

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "confirmed"
    assert client.cancel_order_calls == []
    assert "cancel_entry" not in client.events


def test_an_entry_leg_that_just_filled_stops_for_a_person_without_closing(tmp_path):
    """Commander's ruling 4: v1 does not guess across an entry-fill race."""

    session_factory = create_session_factory(tmp_path / "entry-filled.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    _add_deferred_entry_leg(
        session_factory, batch_id, pos_id="pos-composite-2", status="active"
    )
    client = _RemainderCloseClient()

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "operator_required"
    assert result.reason_code == "remainder_close_deferred_entry_cancel_failed"
    assert client.close_calls == []
    assert client.cancel_order_calls == []


def test_a_refused_entry_cancel_stops_for_a_person_without_closing(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-cancel-fails.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    _add_deferred_entry_leg(session_factory, batch_id)
    client = _RemainderCloseClient()
    client.open_orders = [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "entry-regular-2",
            "clOrdId": "entry-leg-2",
            "posSide": "long",
        }
    ]

    def refuse(payload):
        raise DeepcoinDefiniteRejection("cancel refused")

    client.cancel_order = refuse

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "operator_required"
    assert result.reason_code == "remainder_close_deferred_entry_cancel_failed"
    assert client.close_calls == []


# ---------------------------------------------------------------------------
# Design 3.3 / 4: restart. The reconciler reads the exchange and writes only
# local state; it never resubmits a close.
# ---------------------------------------------------------------------------


def _reconcile(session_factory, client):
    from telegram_kol_research.strategy_management_composite_reconciliation import (
        reconcile_composite_management_components,
    )

    return reconcile_composite_management_components(
        session_factory,
        deepcoin_client=client,
        reconciled_at=NOW,
        allow_new_writes=False,
    )


def _stage_interrupted_before_close(session_factory, component_id):
    """The crash window of design 4 row 3: decided, nothing submitted yet."""

    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        desired = json.loads(component.desired_json)
        desired["remainder_close_execution"] = {
            "reason": "requested_stop_market_side_invalid",
            "requested_stop": PASSED_REFERENCE,
            "primary_stop": PASSED_REFERENCE,
            "position_market_price": "65000",
            "ticker_last": "65000",
            "ticker_field": "last",
            "phase": "cancel_deferred_entries",
            "deferred_entries_cancelled": False,
            "intent_ids": [],
        }
        component.desired_json = json.dumps(desired, sort_keys=True)
        component.status = "submitting"
        session.commit()


def test_restart_during_the_entry_cancel_stops_for_a_person(tmp_path):
    """Design 4 row 3. The original stop is still armed the whole time."""

    session_factory = create_session_factory(tmp_path / "restart-cancel.db")
    _batch_id, component_id = _prepare_passed_reference_component(session_factory)
    _stage_interrupted_before_close(session_factory, component_id)
    client = _RemainderCloseClient()

    outcome = _reconcile(session_factory, client)

    assert outcome.recoverable == 1
    assert client.close_calls == []
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
    assert component.status == "operator_required"
    assert component.reason_code == "remainder_close_interrupted_before_close"


def test_restart_with_a_reserved_intent_blocks_it_and_allows_a_new_attempt(tmp_path):
    """Design 4 row 4: reserved is durable proof nothing was sent."""

    session_factory = create_session_factory(tmp_path / "restart-reserved.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="unknown")
    _execute(session_factory, batch_id, component_id, client)
    with session_factory() as session:
        intent = session.query(PositionMutationIntent).one()
        intent.status = "reserved"
        intent_id = intent.id
        session.get(StrategyManagementComponent, component_id).status = "submitting"
        session.commit()

    outcome = _reconcile(session_factory, client)

    assert outcome.recoverable == 1
    with session_factory() as session:
        assert session.get(PositionMutationIntent, intent_id).status == "blocked"
        component = session.get(StrategyManagementComponent, component_id)
    assert component.status == "recovery_required"
    assert component.reason_code == "remainder_close_reserved_before_write"

    client.remainder_close_outcome = "confirmed"
    second = _execute(session_factory, batch_id, component_id, client)

    assert second.status == "confirmed"
    with session_factory() as session:
        keys = sorted(
            row.idempotency_key for row in session.query(PositionMutationIntent)
        )
    assert keys == [
        f"{component_id}:close:remainder:attempt:1",
        f"{component_id}:close:remainder:attempt:2",
    ]


def test_restart_with_an_unknown_intent_stays_awaiting_and_writes_nothing(tmp_path):
    """Design 4 row 5: an unknown outcome is read, never resent."""

    session_factory = create_session_factory(tmp_path / "restart-unknown.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="unknown")
    _execute(session_factory, batch_id, component_id, client)
    submitted_before = len(client.close_calls)

    outcome = _reconcile(session_factory, client)

    assert outcome.awaiting == 1
    assert len(client.close_calls) == submitted_before
    with session_factory() as session:
        assert (
            session.get(StrategyManagementComponent, component_id).status
            == "awaiting_exchange"
        )


def test_restart_confirms_from_a_confirmed_intent_and_a_flat_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "restart-confirm.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="unknown")
    _execute(session_factory, batch_id, component_id, client)
    with session_factory() as session:
        session.query(PositionMutationIntent).one().status = "confirmed"
        session.commit()
    client.position_size = "0"

    outcome = _reconcile(session_factory, client)

    assert outcome.reconciled == 1
    assert len(client.close_calls) == 1
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        evidence = json.loads(component.evidence_json)[-1]
    assert component.status == "confirmed"
    assert evidence["outcome"] == "remainder_closed_at_market"
    assert evidence["evidence_tier"] == "exact_close_intent_confirmed"


def test_restart_with_a_confirmed_intent_and_a_residual_asks_for_another_pass(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "restart-residual.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="unknown")
    _execute(session_factory, batch_id, component_id, client)
    with session_factory() as session:
        session.query(PositionMutationIntent).one().status = "confirmed"
        session.commit()
    client.position_size = "2"

    outcome = _reconcile(session_factory, client)

    assert outcome.recoverable == 1
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
    assert component.status == "recovery_required"
    assert component.reason_code == "remainder_close_confirmed_with_residual"


@pytest.mark.parametrize("intent_status", ["rejected", "blocked"])
def test_restart_with_a_terminal_intent_and_a_live_position_retries(
    tmp_path, intent_status
):
    session_factory = create_session_factory(
        tmp_path / f"restart-terminal-{intent_status}.db"
    )
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="unknown")
    _execute(session_factory, batch_id, component_id, client)
    with session_factory() as session:
        session.query(PositionMutationIntent).one().status = intent_status
        session.commit()

    outcome = _reconcile(session_factory, client)

    assert outcome.recoverable == 1
    assert len(client.close_calls) == 1
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
    assert component.status == "recovery_required"
    assert component.reason_code == "remainder_close_terminal_without_fill"


@pytest.mark.parametrize("intent_status", ["rejected", "blocked"])
def test_a_position_that_vanished_without_our_confirmed_close_stops_for_a_person(
    tmp_path, intent_status
):
    """Commander's ruling 5. Someone else's close is not ours to claim."""

    session_factory = create_session_factory(
        tmp_path / f"restart-vanished-{intent_status}.db"
    )
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="unknown")
    _execute(session_factory, batch_id, component_id, client)
    with session_factory() as session:
        session.query(PositionMutationIntent).one().status = intent_status
        session.commit()
    client.position_size = "0"

    outcome = _reconcile(session_factory, client)

    assert outcome.recoverable == 1
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
    assert component.status == "operator_required"
    assert component.reason_code == (
        "remainder_position_absent_without_confirmed_close"
    )


def test_the_executor_resume_reads_a_vanished_position_the_same_way(tmp_path):
    """Design 3.2 step 6, the executor's own half of the same rule."""

    session_factory = create_session_factory(tmp_path / "resume-vanished.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="rejected")
    first = _execute(session_factory, batch_id, component_id, client)
    assert first.status == "recovery_required"
    client.position_size = "0"

    second = _execute(session_factory, batch_id, component_id, client)

    assert second.status == "operator_required"
    assert second.reason_code == (
        "remainder_position_absent_without_confirmed_close"
    )
    assert len(client.close_calls) == 1


def test_the_executor_resume_confirms_a_vanished_position_after_a_confirmed_close(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "resume-confirmed.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient(close_outcome="unknown")
    _execute(session_factory, batch_id, component_id, client)
    with session_factory() as session:
        session.query(PositionMutationIntent).one().status = "confirmed"
        session.get(StrategyManagementComponent, component_id).status = (
            "recovery_required"
        )
        session.commit()
    client.position_size = "0"

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "confirmed"
    assert len(client.close_calls) == 1


def test_a_resume_that_never_cancelled_the_entry_legs_stops_for_a_person(tmp_path):
    session_factory = create_session_factory(tmp_path / "resume-interrupted.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    _stage_interrupted_before_close(session_factory, component_id)
    with session_factory() as session:
        session.get(StrategyManagementComponent, component_id).status = (
            "recovery_required"
        )
        session.commit()
    client = _RemainderCloseClient()

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "operator_required"
    assert result.reason_code == "remainder_close_interrupted_before_close"
    assert client.close_calls == []


def test_the_retry_cap_is_shared_with_the_protection_route_unchanged(tmp_path):
    """Design 4: the three-attempt cap is the existing one, untouched."""

    session_factory = create_session_factory(tmp_path / "retry-cap.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        component.status = "recovery_required"
        component.attempt_count = 3
        session.commit()
    client = _RemainderCloseClient()

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "operator_required"
    assert result.reason_code == "protection_replacement_retry_exhausted"
    assert client.close_calls == []
    assert client.events == []


# ---------------------------------------------------------------------------
# Design 8: the production shape, end to end through the batch executor.
# ---------------------------------------------------------------------------


SHORT_SHAPE = {
    "side": "short",
    "pos_id": "pos-short",
    "entry_range": ("80500", "81600"),
    "reference": "80500",
    "avg_entry_price": "80436",
    "deferred_price": "81510",
    "mark_price": "80600",
    "take_profit": "79000",
    "old_primary": "82300",
    "old_backup": "82346",
}
LONG_SHAPE = {
    "side": "long",
    "pos_id": "pos-long",
    "entry_range": ("64000", "65000"),
    "reference": "65000",
    "avg_entry_price": "64780",
    "deferred_price": "64050",
    "mark_price": "64900",
    "take_profit": "66000",
    "old_primary": "63000",
    "old_backup": "62970",
}


class _ProductionCompositeClient:
    """One instrument, one or two positions, and every read the batch makes."""

    def __init__(self, shapes, *, start_size="6"):
        self.shapes = [shapes] if isinstance(shapes, dict) else list(shapes)
        self.instrument = "BTC-USDT-SWAP"
        self.sizes = {shape["pos_id"]: start_size for shape in self.shapes}
        self.calls: list[tuple[str, dict]] = []
        self.history: list[dict] = []
        self.pending = []
        for shape in self.shapes:
            pos_id = shape["pos_id"]
            self.pending.extend(
                [
                    {
                        "ordId": f"tp-first-{pos_id}", "posId": pos_id,
                        "instId": self.instrument, "posSide": shape["side"],
                        "triggerOrderType": "TPSL",
                        "tpTriggerPx": shape["take_profit"], "sz": "3",
                    },
                    {
                        "ordId": f"stop-old-primary-{pos_id}", "posId": pos_id,
                        "instId": self.instrument, "posSide": shape["side"],
                        "triggerOrderType": "TPSL",
                        "slTriggerPx": shape["old_primary"], "sz": "6",
                    },
                    {
                        "ordId": f"stop-old-backup-{pos_id}", "posId": pos_id,
                        "instId": self.instrument, "posSide": shape["side"],
                        "triggerOrderType": "TPSL",
                        "slTriggerPx": shape["old_backup"], "sz": "6",
                    },
                ]
            )
        self.open_orders = [
            {
                "instId": self.instrument,
                "ordId": "entry-order-2",
                "clOrdId": "entry-leg-2",
                "posSide": self.shapes[0]["side"],
                "px": self.shapes[0]["deferred_price"],
            }
        ]

    # -- reads ---------------------------------------------------------
    def list_positions(self, *, inst_id=None):
        return [
            {
                "posId": shape["pos_id"],
                "instId": self.instrument,
                "posSide": shape["side"],
                "pos": self.sizes[shape["pos_id"]],
                "avgPx": shape["avg_entry_price"],
                "markPx": shape["mark_price"],
                "mgnMode": "cross",
                "mrgPosition": "split",
            }
            for shape in self.shapes
            if self.sizes[shape["pos_id"]] != "0"
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

    def list_trigger_order_history(self, *, inst_id):
        return []

    def list_order_history(self, *, inst_id):
        return list(self.history)

    def list_trade_fills(self, *, inst_id):
        return []

    def list_open_orders(self, *, inst_id=None):
        return list(self.open_orders)

    def get_ticker_quote(self, *, inst_id):
        self.calls.append(("get_ticker_quote", {"inst_id": inst_id}))
        return {
            "instrument_id": self.instrument,
            "price": self.shapes[0]["mark_price"],
            "price_field": "last",
        }

    # -- writes --------------------------------------------------------
    def set_position_sltp(self, payload):
        self.calls.append(("set_position_sltp", dict(payload)))
        raise AssertionError("the fallback route must never arm a new stop")

    def cancel_position_sltp(self, payload):
        self.calls.append(("cancel_position_sltp", dict(payload)))
        self.pending = [
            row for row in self.pending if row["ordId"] != payload["ordId"]
        ]
        return {"code": "0", "data": {"ordId": payload["ordId"]}}

    def cancel_order(self, payload):
        self.calls.append(("cancel_order", dict(payload)))
        self.open_orders = [
            row
            for row in self.open_orders
            if row.get("clOrdId") != payload.get("clOrdId")
        ]
        return {"code": "0", "data": {"ordId": "entry-order-2"}}

    def place_order(self, payload):
        self.calls.append(("place_order", dict(payload)))
        from decimal import Decimal

        pos_id = str(payload["closePosId"])
        self.sizes[pos_id] = str(
            Decimal(self.sizes[pos_id]) - Decimal(str(payload["sz"]))
        )
        order_id = f"close-{len(self.close_payloads)}"
        # The exchange reports the fill; that is how a close intent leaves the
        # unresolved set and stops blocking the next one (design 9.3).
        self.history.append(
            {
                "ordId": order_id,
                "clOrdId": payload.get("clOrdId"),
                "state": "filled",
                "instId": payload["instId"],
                "posSide": payload["posSide"],
                "closePosId": payload["closePosId"],
                "sz": payload["sz"],
            }
        )
        return {"code": "0", "data": {"ordId": order_id}}

    def arm_stops_instead_of_refusing(self):
        """Let a placeable break-even actually create its two stops."""

        armed: list[dict] = []

        def set_position_sltp(payload):
            self.calls.append(("set_position_sltp", dict(payload)))
            armed.append(dict(payload))
            order_id = f"stop-new-{len(armed)}"
            self.pending.append(
                {
                    "ordId": order_id,
                    "posId": payload["posId"],
                    "instId": self.instrument,
                    "posSide": payload["posSide"],
                    "triggerOrderType": "TPSL",
                    "slTriggerPx": payload["slTriggerPx"],
                    "sz": payload["sz"],
                }
            )
            return {"code": "0", "data": {"ordId": order_id}}

        self.set_position_sltp = set_position_sltp
        return armed

    @property
    def close_payloads(self):
        return [payload for name, payload in self.calls if name == "place_order"]

    @property
    def cancel_sltp_ids(self):
        return [
            payload["ordId"]
            for name, payload in self.calls
            if name == "cancel_position_sltp"
        ]


def _persist_production_composite_batch(session_factory, shapes):
    """One binding, one instrument, one composite batch over N positions.

    ``shapes[0]`` owns the deferred (unfilled) entry leg and supplies the
    lifecycle's entry range; every shape gets its own filled entry leg,
    management leg, three components and protection ledger rows.
    """

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        RawMessage,
        RecognitionDecision,
        StrategyLifecycle,
    )
    from telegram_kol_research.strategy_management_contracts import (
        ManagementInstructionContract,
        management_contract_fingerprint,
        serialize_management_contract,
    )

    shapes = [shapes] if isinstance(shapes, dict) else list(shapes)
    shape = shapes[0]
    low, high = shape["entry_range"]
    contract = ManagementInstructionContract(
        version=2,
        target_lifecycle_id=1,
        strategy_instance_id="strategy-production",
        symbol="BTC",
        side=shape["side"],
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
        current_message_text="止盈50%，止损移动至开仓价",
    )
    contract_json = serialize_management_contract(contract)
    fingerprint = management_contract_fingerprint(contract)
    with session_factory() as session:
        raw = RawMessage(
            chat_id=901, message_id=17813,
            text="止盈50%，止损移动至开仓价", posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id, input_kind="text",
            authoritative_model="mimo", authoritative_status="策略管理",
            authoritative_payload_json="{}", agreement_status="authoritative_only",
            differences_json="[]",
        )
        lifecycle = StrategyLifecycle(
            id=1, chat_id=901, message_id=17800, symbol="BTC",
            side=shape["side"], lifecycle_status="entered", signal_at=NOW,
            entry_range_low=float(low), entry_range_high=float(high),
        )
        binding = ExecutionBinding(
            strategy_instance_id="strategy-production", kol_id="fengge",
            chat_id=901, message_id=17800, symbol="BTC", side=shape["side"],
            venue="deepcoin", margin_mode="cross", position_mode="split",
            pos_id=",".join(item["pos_id"] for item in shapes), status="active",
        )
        session.add_all([decision, lifecycle, binding])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        filled_legs = []
        for index, item in enumerate(shapes):
            filled = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=index + 1, purpose="entry", order_kind="market",
                pos_id=item["pos_id"], venue="deepcoin",
                attribution_status="verified",
                response_json=json.dumps({"data": {"posId": item["pos_id"]}}),
                status="active",
            )
            session.add(filled)
            filled_legs.append(filled)
        deferred = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=len(shapes) + 1, purpose="entry", order_kind="limit",
            client_order_id="entry-leg-2", pos_id=None, venue="deepcoin",
            attribution_status="unassigned", status="pending",
        )
        session.add(deferred)
        session.flush()
        batch = StrategyManagementBatch(
            idempotency_fingerprint="production-composite",
            raw_message_id=raw.id,
            recognition_decision_id=decision.id,
            recognition_generation="production-generation",
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
            target_fingerprint="target-production",
            target_snapshot_json=json.dumps(
                {
                    "identity": {
                        "target_lifecycle_id": lifecycle.id,
                        "execution_binding_id": binding.id,
                        "strategy_instance_id": binding.strategy_instance_id,
                        "manageable_entry_leg_ids": [
                            leg.id for leg in filled_legs
                        ],
                        "deferred_entry_leg_ids": [deferred.id],
                        "capability_deferred_entry_leg_ids": [],
                    },
                    "break_even_reference": {
                        "price": shape["reference"],
                        "source": "strategy_first_leg",
                    },
                    "positions": [
                        {
                            "pos_id": item["pos_id"],
                            "trusted_start_size": "6",
                            "target_remaining_size": "3",
                            "avg_entry_price": item["avg_entry_price"],
                            "quantity_step": "1",
                            "min_quantity": "1",
                        }
                        for item in shapes
                    ],
                }
            ),
            planned_at=NOW, created_at=NOW, updated_at=NOW,
        )
        session.add(batch)
        session.flush()
        management_leg_ids = []
        for index, (item, filled) in enumerate(zip(shapes, filled_legs)):
            management_leg = StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=filled.id,
                pos_id=item["pos_id"], leg_index=index + 1, status="planned",
                preflight_size="6", planned_close_size="3",
                avg_entry_price=item["avg_entry_price"], quantity_step="1",
                planned_tpsl_json=json.dumps(
                    {
                        "intent": "partial_then_break_even",
                        "stop_loss_text": None,
                        "break_even_reference_price": item["reference"],
                        "break_even_reference_source": "strategy_first_leg",
                    }
                ),
                created_at=NOW, updated_at=NOW,
            )
            session.add(management_leg)
            session.flush()
            management_leg_ids.append(management_leg.id)
            for sequence, kind in enumerate(contract.required_components):
                session.add(
                    StrategyManagementComponent(
                        management_batch_id=batch.id,
                        strategy_management_leg_id=management_leg.id,
                        strategy_management_leg_scope=management_leg.id,
                        component_kind=kind,
                        sequence=sequence,
                        status="pending",
                        idempotency_key=f"component:{kind}:{management_leg.id}",
                        desired_json=json.dumps(
                            {
                                "contract_fingerprint": fingerprint,
                                "pos_id": item["pos_id"],
                                "execution_order_leg_id": filled.id,
                                "trusted_start_size": "6",
                                "target_remaining_size": "3",
                                "avg_entry_price": item["avg_entry_price"],
                                "quantity_step": "1",
                                "min_quantity": "1",
                                "component_kind": kind,
                            },
                            sort_keys=True,
                        ),
                        evidence_json="[]",
                        created_at=NOW, updated_at=NOW,
                    )
                )
            owner = {
                "venue": "deepcoin",
                "execution_binding_id": binding.id,
                "execution_order_leg_id": filled.id,
                "strategy_instance_id": binding.strategy_instance_id,
                "pos_id": item["pos_id"],
                "instrument_id": "BTC-USDT-SWAP",
                "side": item["side"],
                "evidence_source": "native_tpsl_pending_readback",
                "evidence_json": "{}",
                "first_seen_at": NOW,
                "last_seen_at": NOW,
                "created_at": NOW,
                "updated_at": NOW,
            }
            pos_id = item["pos_id"]
            session.add_all(
                [
                    PositionProtectionLedger(
                        **owner, order_id=f"tp-first-{pos_id}",
                        purpose="take_profit",
                        trigger_price=item["take_profit"], size_text="3",
                        status="verified",
                    ),
                    PositionProtectionLedger(
                        **owner, order_id=f"stop-old-primary-{pos_id}",
                        purpose="stop_loss",
                        trigger_price=item["old_primary"], size_text="6",
                        status="verified",
                    ),
                    PositionProtectionLedger(
                        **owner, order_id=f"stop-old-backup-{pos_id}",
                        purpose="backup_stop",
                        trigger_price=item["old_backup"], size_text="6",
                        status="verified",
                    ),
                ]
            )
        session.commit()
        return (
            batch.id,
            management_leg_ids,
            [leg.id for leg in filled_legs],
            deferred.id,
        )


def _settle_reductions(session_factory, batch_id, client):
    """Record the take-profit consumption and the reduction as already done.

    Used only by the two-position case. `plan_take_profit_consumption` compares
    every pending take-profit on the instrument against *one* leg's ledger, so
    two positions of the same instrument make each other's consumption
    component refuse with `take_profit_order_identity_conflict`. That is a
    pre-existing property of that component; this route neither causes it nor
    fixes it, so the two-leg case starts after the reduction instead.
    """

    with session_factory() as session:
        for component in session.query(StrategyManagementComponent).filter(
            StrategyManagementComponent.management_batch_id == batch_id,
            StrategyManagementComponent.sequence < 2,
        ):
            component.status = "confirmed"
            component.completed_at = NOW
            component.evidence_json = json.dumps(
                [{"remaining_size": "3", "evidence_tier": "exact_position_target"}]
            )
        for row in session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.purpose == "take_profit"
        ):
            row.status = "cancelled"
        session.commit()
    for pos_id in list(client.sizes):
        client.sizes[pos_id] = "3"
    client.pending = [
        row for row in client.pending if not row["ordId"].startswith("tp-first")
    ]


def _run_production_batch(session_factory, batch_id, client):
    from types import SimpleNamespace

    from telegram_kol_research.strategy_management_composite_executor import (
        execute_composite_management_batch,
    )

    provider = SimpleNamespace(
        get_contract_spec=lambda instrument_id: SimpleNamespace(price_tick="0.1")
    )
    result = None
    for _ in range(6):
        result = execute_composite_management_batch(
            session_factory,
            batch_id=batch_id,
            deepcoin_client=client,
            contract_spec_provider=provider,
            live_execution_gate=lambda: True,
            now_provider=lambda: NOW,
        )
        if result.status in {"succeeded", "recovery_required", "blocked"}:
            break
    return result


@pytest.mark.parametrize(
    "shape", [SHORT_SHAPE, LONG_SHAPE], ids=["short", "long"]
)
def test_the_production_shape_reduces_cancels_and_leaves_at_market(tmp_path, shape):
    """Design 8. The whole instruction, one batch, one exchange.

    Short: range 80500-81600, only leg 1 filled at 80436, reference 80500, size
    6, reduce 50%, leg 2 resting at 81510, market 80600. Long mirror: range
    64000-65000, reference 65000, market 64900.
    """

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        RuntimeIncident,
        StrategyLifecycle,
        StrategyManagementNotification,
    )

    session_factory = create_session_factory(tmp_path / f"prod-{shape['side']}.db")
    batch_id, management_leg_ids, filled_ids, deferred_id = (
        _persist_production_composite_batch(session_factory, shape)
    )
    client = _ProductionCompositeClient(shape)

    result = _run_production_batch(session_factory, batch_id, client)

    assert result.status == "succeeded"
    assert result.reason_code == "composite_remainder_market_closed"
    # The reduction, then the remainder -- and nothing else was written.
    assert [payload["sz"] for payload in client.close_payloads] == ["3", "3"]
    assert client.close_payloads[1]["clOrdId"] == (
        f"CM{batch_id}L{management_leg_ids[0]}R1"
    )
    assert client.close_payloads[1]["closePosId"] == shape["pos_id"]
    # Only the consumed first take profit was cancelled. The original stop was
    # never touched; it stayed armed until the position was flat.
    assert client.cancel_sltp_ids == [f"tp-first-{shape['pos_id']}"]
    assert [
        row["slTriggerPx"] for row in client.pending if "slTriggerPx" in row
    ] == [shape["old_primary"], shape["old_backup"]]
    assert [payload["clOrdId"] for name, payload in client.calls
            if name == "cancel_order"] == ["entry-leg-2"]

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        filled = session.get(ExecutionOrderLeg, filled_ids[0])
        deferred = session.get(ExecutionOrderLeg, deferred_id)
        ledger_statuses = {
            row.order_id: row.status
            for row in session.query(PositionProtectionLedger)
        }
        notification = (
            session.query(StrategyManagementNotification)
            .filter(StrategyManagementNotification.management_batch_id == batch_id)
            .one()
        )
        incidents = [
            row.severity
            for row in session.query(RuntimeIncident).filter(
                RuntimeIncident.incident_type
                == "composite_break_even_remainder_closed"
            )
        ]

    assert filled.status == "closed"
    assert filled.terminal_reason == "management_full_close_confirmed"
    assert deferred.status == "cancelled"
    assert binding.status == "closed"
    assert binding.pos_id is None
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.management_action == "full_close_confirmed"
    assert set(ledger_statuses.values()) == {"retired", "cancelled"} or set(
        ledger_statuses.values()
    ) == {"retired"}
    assert all(
        status != "verified" for status in ledger_statuses.values()
    )
    payload = json.loads(notification.payload_json)
    assert payload["partial_close"] == (
        f"剩余 0（保本价 {shape['reference']} 已被市价 "
        f"{shape['mark_price']} 越过，剩余仓位已市价全平）"
    )
    assert payload["protection"] == "未挂保本止损：仓位已全平，原保护单随仓位失效"
    assert incidents == ["low"]


def test_a_placeable_break_even_still_arms_the_stop_and_closes_nothing_extra(
    tmp_path,
):
    """Design 8 variant: market 80450, so 80500 is armable on a short."""

    session_factory = create_session_factory(tmp_path / "prod-placeable.db")
    shape = {**SHORT_SHAPE, "mark_price": "80450"}
    batch_id, _leg_ids, filled_ids, _deferred_id = (
        _persist_production_composite_batch(session_factory, shape)
    )
    client = _ProductionCompositeClient(shape)
    armed = client.arm_stops_instead_of_refusing()

    result = _run_production_batch(session_factory, batch_id, client)

    from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg

    assert result.status == "succeeded"
    assert result.reason_code == "composite_management_exchange_confirmed"
    assert [payload["sz"] for payload in client.close_payloads] == ["3"]
    # Primary at the strategy's price, backup 20bps further out for a short.
    assert [payload["slTriggerPx"] for payload in armed] == ["80500", "80661"]
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        assert session.get(ExecutionOrderLeg, filled_ids[0]).status == "active"
        assert (
            session.get(ExecutionBinding, batch.execution_binding_id).status
            == "active"
        )


def test_a_two_leg_batch_that_closes_one_leg_keeps_the_binding_active(tmp_path):
    """Design 8 variant. One leg arms its stop, the other leaves at market.

    The books must record exactly that: the closed leg terminalized, the other
    still open, the binding still `active` and the lifecycle still `entered`.
    A full exit here would be a lie about a position that is still on the
    exchange.
    """

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        StrategyLifecycle,
    )

    session_factory = create_session_factory(tmp_path / "prod-two-legs.db")
    # Leg A's reference has been passed (80500 against a market of 80600);
    # leg B's has not (80800 is still above the market for a short).
    leg_a = SHORT_SHAPE
    leg_b = {
        **SHORT_SHAPE,
        "pos_id": "pos-short-2",
        "reference": "80800",
        "avg_entry_price": "80700",
        "take_profit": "78900",
        "old_primary": "82400",
        "old_backup": "82446",
    }
    batch_id, management_leg_ids, filled_ids, deferred_id = (
        _persist_production_composite_batch(session_factory, [leg_a, leg_b])
    )
    client = _ProductionCompositeClient([leg_a, leg_b])
    # The reduction half of both legs has already happened (see the module's
    # note on `_settle_reductions`: two positions of one instrument cannot run
    # `consume_take_profit_stage` today, which is a pre-existing limitation of
    # that component and not something this route changes).
    _settle_reductions(session_factory, batch_id, client)
    armed = client.arm_stops_instead_of_refusing()

    result = _run_production_batch(session_factory, batch_id, client)

    assert result.status == "succeeded"
    assert result.reason_code == "composite_remainder_market_closed"
    # Exactly one remainder close -- leg A's.
    assert [payload["closePosId"] for payload in client.close_payloads] == [
        "pos-short"
    ]
    assert [payload["sz"] for payload in client.close_payloads] == ["3"]
    # Leg B armed its own two stops; leg A armed none.
    assert [payload["posId"] for payload in armed] == [
        "pos-short-2", "pos-short-2",
    ]
    assert [payload["slTriggerPx"] for payload in armed] == ["80800", "80961.6"]

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        closed_leg = session.get(ExecutionOrderLeg, filled_ids[0])
        open_leg = session.get(ExecutionOrderLeg, filled_ids[1])
        deferred = session.get(ExecutionOrderLeg, deferred_id)
        remaining_stops = {
            row.order_id: row.status
            for row in session.query(PositionProtectionLedger).filter(
                PositionProtectionLedger.pos_id == "pos-short-2"
            )
        }
    assert closed_leg.status == "closed"
    assert open_leg.status == "active"
    assert deferred.status == "cancelled"
    assert binding.status == "active"
    assert binding.pos_id == "pos-short-2"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    # Leg B's protection is untouched by leg A's exit.
    assert remaining_stops["stop-new-1"] == "verified"
    assert remaining_stops["stop-new-2"] == "verified"


def test_the_books_are_terminalized_in_the_same_transaction_as_the_success(
    tmp_path,
):
    """Design 4 row 6 / design 1.5.

    The component confirms, the process dies before completion, and the next
    tick finishes the batch. The batch may only become `succeeded` together
    with the books: the manual-close sweep stops skipping this position the
    moment the batch leaves a managed state, and would otherwise record the
    exit as manual within about ninety seconds.
    """

    from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle

    session_factory = create_session_factory(tmp_path / "prod-resume-complete.db")
    batch_id, _leg_ids, filled_ids, _deferred = (
        _persist_production_composite_batch(session_factory, SHORT_SHAPE)
    )
    client = _ProductionCompositeClient(SHORT_SHAPE)
    _settle_reductions(session_factory, batch_id, client)
    from telegram_kol_research.strategy_management_composite_executor import (
        execute_protection_replacement_component,
    )

    with session_factory() as session:
        protection = (
            session.query(StrategyManagementComponent)
            .filter(
                StrategyManagementComponent.management_batch_id == batch_id,
                StrategyManagementComponent.sequence == 2,
            )
            .one()
        )
        protection_id = protection.id
        session.get(StrategyManagementBatch, batch_id).status = "executing"
        session.commit()
    component_result = execute_protection_replacement_component(
        session_factory,
        batch_id=batch_id,
        component_id=protection_id,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        price_tick="0.1",
        backup_buffer_bps="20",
    )
    assert component_result.status == "confirmed"
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        binding_id = batch.execution_binding_id
        lifecycle_id = batch.target_lifecycle_id
        # Nothing terminal has been written yet: the batch is still executing
        # and therefore still shields the position from the sweep.
        assert batch.status == "executing"
        assert session.get(ExecutionBinding, binding_id).status == "active"
        assert (
            session.get(StrategyLifecycle, lifecycle_id).lifecycle_status
            == "entered"
        )

    result = _run_production_batch(session_factory, batch_id, client)

    assert result.status == "succeeded"
    assert result.reason_code == "composite_remainder_market_closed"
    assert len(client.close_payloads) == 1
    with session_factory() as session:
        assert session.get(ExecutionBinding, binding_id).status == "closed"
        assert (
            session.get(StrategyLifecycle, lifecycle_id).exit_reason
            == "kol_signal"
        )


def test_the_watchdog_and_the_adapter_see_one_ordinary_composite_success(tmp_path):
    """Design 8: the terminal shape the watchdog clears on is unchanged.

    `oncall_detector` keys its case on `intent`, not on the exchange verb --
    keying on the verb split raw 17813 into two cases on 2026-09-20 -- and it
    clears on batch status alone. The instruction adapter filters the same way
    and treats `confirmed` as the only component success. The fallback must
    therefore leave all three of those untouched: same intent, a clearing
    status, every component confirmed.
    """

    from telegram_kol_research.oncall_detector import (
        MANAGEMENT_BATCH_FAULT_STATUSES,
    )

    session_factory = create_session_factory(tmp_path / "prod-watchdog.db")
    batch_id, _leg_ids, _filled, _deferred = (
        _persist_production_composite_batch(session_factory, SHORT_SHAPE)
    )
    client = _ProductionCompositeClient(SHORT_SHAPE)

    result = _run_production_batch(session_factory, batch_id, client)

    assert result.status == "succeeded"
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        statuses = {
            row.component_kind: row.status
            for row in session.query(StrategyManagementComponent).filter(
                StrategyManagementComponent.management_batch_id == batch_id
            )
        }
    assert batch.intent == "partial_then_break_even"
    assert batch.effective_action == "partial_then_break_even"
    assert batch.status not in MANAGEMENT_BATCH_FAULT_STATUSES
    assert set(statuses.values()) == {"confirmed"}


def test_a_lifecycle_that_already_exited_refuses_to_be_terminalized_again(
    tmp_path,
):
    """The identity guard of design 3.4: the batch freezes, nothing is rewritten."""

    from telegram_kol_research.models import StrategyLifecycle

    session_factory = create_session_factory(tmp_path / "prod-identity-drift.db")
    batch_id, _leg_ids, _filled, _deferred = (
        _persist_production_composite_batch(session_factory, SHORT_SHAPE)
    )
    client = _ProductionCompositeClient(SHORT_SHAPE)
    _settle_reductions(session_factory, batch_id, client)
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        lifecycle.lifecycle_status = "exited"
        lifecycle.exit_reason = "manual"
        session.commit()

    result = _run_production_batch(session_factory, batch_id, client)

    assert result.status == "recovery_required"
    assert result.reason_code == (
        "composite_remainder_terminalization_identity_mismatch"
    )
    with session_factory() as session:
        lifecycle = session.get(
            StrategyLifecycle,
            session.get(StrategyManagementBatch, batch_id).target_lifecycle_id,
        )
    assert lifecycle.exit_reason == "manual"


# ---------------------------------------------------------------------------
# Design 5: everything else that reaches the same `except` branch is unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("position_size", "status", "reason"),
    [
        ("11", "operator_required", "position_size_increased_after_snapshot"),
        ("4", "operator_required", "position_below_target_remaining"),
        ("6", "recovery_required", "partial_close_component_not_converged"),
    ],
)
def test_a_drifted_position_keeps_its_old_disposition_even_when_passed(
    tmp_path, position_size, status, reason
):
    """Design 5. The fallback fires on one reason only, under one condition.

    The break-even reference is passed here too, so this is exactly the input
    that would close a position if the guard were any looser.
    """

    session_factory = create_session_factory(tmp_path / f"drift-{position_size}.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient()
    client.position_size = position_size

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == status
    assert result.reason_code == reason
    assert client.close_calls == []
    assert client.events == []


def test_a_retained_take_profit_larger_than_the_position_still_stops_for_a_person(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "oversized-tp.db")
    batch_id, component_id = _prepare_composite_protection_component(
        session_factory, retained_size="6"
    )
    _stamp_composite_break_even_reference(
        session_factory, batch_id, price=PASSED_REFERENCE
    )
    client = _RemainderCloseClient()

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "operator_required"
    assert result.reason_code == "retained_take_profit_exceeds_position"
    assert client.close_calls == []
    assert client.events == []


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        pytest.param(
            lambda client: setattr(client, "list_positions", lambda *, inst_id=None: None),
            "positions_snapshot_incomplete",
            id="snapshot_incomplete",
        ),
        pytest.param(
            lambda client: setattr(client, "list_positions", lambda *, inst_id=None: []),
            "target_live_position_not_unique",
            id="position_missing",
        ),
    ],
)
def test_an_unreadable_position_never_reaches_the_fallback(tmp_path, mutate, reason):
    session_factory = create_session_factory(tmp_path / f"unreadable-{reason}.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient()
    mutate(client)

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "recovery_required"
    assert result.reason_code == reason
    assert client.close_calls == []


def test_a_position_row_without_a_price_never_reaches_the_fallback(tmp_path):
    session_factory = create_session_factory(tmp_path / "no-mark-price.db")
    batch_id, component_id = _prepare_passed_reference_component(session_factory)
    client = _RemainderCloseClient()
    base = client.list_positions

    def without_price(*, inst_id=None):
        rows = base(inst_id=inst_id)
        for row in rows:
            row.pop("markPx", None)
            row.pop("last", None)
            row.pop("lastPx", None)
        return rows

    client.list_positions = without_price

    result = _execute(session_factory, batch_id, component_id, client)

    assert result.status == "recovery_required"
    assert result.reason_code == "break_even_market_price_invalid"
    assert client.close_calls == []
    assert "ticker" not in client.events
