"""Q1 patch (2026-09-29 Mia design, section 7.1, ruling C-plus-patch).

Scope: a strategy where an add-position instruction was already refused
(``risk_increasing_fanout_forbidden`` / ``lifecycle_apply_failed``), and a
later ``adjust_stop_loss`` names an explicit price worse than our own average
fill. The KOL's later messages keep referring to the position as if the
refused add had gone through -- naming a stop derived from a higher (for a
long) average price than the one we actually hold -- and following that
number verbatim would realize a loss we never took the risk for. The patch:
supersede the message's number with the strategy's own break-even price
(reusing ``move_stop_to_break_even``'s existing machinery verbatim, including
its already-correct market-side-invalid -> market-close fallback), and raise
one ``high`` incident every time this happens. Without the rejection history,
or with a price that is not worse than the fill, ``adjust_stop_loss`` behaves
exactly as it does today -- unchanged.

Binding 385 / lifecycle 1343's real 2026-09-28 sequence is the shape this
guards: raw 19514 ("可加仓同等仓位，入场均价83200", refused, target lifecycle
1343), and a later "剩余仓位止损上移至83200" while our own fill is 83800.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    RuntimeIncident,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row

from test_strategy_management_planner import (
    PLANNED_AT,
    _ContractSpecs,
    _disable_reconciliation,
    _persist_exact_management_target,
    _planner,
    _position,
    _ReadOnlyDeepcoin,
)

from telegram_kol_research.management_add_position_rejection import (
    explicit_stop_worse_than_fill,
    find_rejected_add_position_before,
)


@pytest.fixture(autouse=True)
def _fixed_stop_gate_check_clock(monkeypatch):
    """Same fixture ``test_strategy_management_planner.py`` uses.

    ``management_stop_price_gate.stop_gate_clock`` ignores the ``now`` it is
    given and reads the real wall clock instead, so any test whose fixture
    timestamps are not "now" needs this pin -- otherwise the quote this file
    stamps at ``PLANNED_AT`` reads as stale by however old ``PLANNED_AT`` is,
    and every explicit-price ``adjust_stop_loss`` refuses with
    ``management_stop_reference_unavailable`` before this guard is ever
    reached.
    """

    from telegram_kol_research import management_stop_price_gate as gate

    monkeypatch.setattr(gate, "_stop_check_now", lambda: PLANNED_AT)


# ---------------------------------------------------------------------------
# Focused unit tests: the two independent judgements, in isolation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "stop_price", "avg_entry_price", "expected"),
    [
        ("long", "83200", "83800", True),
        ("long", "84000", "83800", False),
        ("long", "83800", "83800", False),
        ("short", "84500", "83800", True),
        ("short", "83000", "83800", False),
        ("weird_side", "83200", "83800", False),
        ("long", "not-a-number", "83800", False),
    ],
)
def test_explicit_stop_worse_than_fill(side, stop_price, avg_entry_price, expected):
    assert (
        explicit_stop_worse_than_fill(
            side=side, stop_price=stop_price, avg_entry_price=avg_entry_price,
        )
        == expected
    )


def _add_recognition_decision(
    session_factory,
    *,
    chat_id,
    message_id,
    posted_at,
    automation_reason,
    lifecycle_event,
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id, message_id=message_id, posted_at=posted_at,
            text="raw text irrelevant to this guard",
        )
        session.add(raw)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(
                    {"lifecycle_event": lifecycle_event}, ensure_ascii=False,
                ),
                agreement_status="authoritative_only",
                differences_json="[]",
                automation_reason=automation_reason,
            )
        )
        session.commit()
        return raw.id


def test_find_rejected_add_position_before_matches_action_and_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "guard-query.db")
    signal_at = PLANNED_AT

    rejected_id = _add_recognition_decision(
        session_factory,
        chat_id=100, message_id=40, posted_at=signal_at + timedelta(minutes=5),
        automation_reason="lifecycle_apply_failed",
        lifecycle_event={
            "management_action": "add_position",
            "target_lifecycle_id": 1343,
        },
    )
    # Distractors that must not match: wrong automation_reason, wrong action,
    # wrong target, and one before the signal window.
    _add_recognition_decision(
        session_factory,
        chat_id=100, message_id=41, posted_at=signal_at + timedelta(minutes=6),
        automation_reason="target_not_verifiable",
        lifecycle_event={
            "management_action": "add_position", "target_lifecycle_id": 1343,
        },
    )
    _add_recognition_decision(
        session_factory,
        chat_id=100, message_id=42, posted_at=signal_at + timedelta(minutes=7),
        automation_reason="lifecycle_apply_failed",
        lifecycle_event={
            "management_action": "partial_take_profit", "target_lifecycle_id": 1343,
        },
    )
    _add_recognition_decision(
        session_factory,
        chat_id=100, message_id=43, posted_at=signal_at + timedelta(minutes=8),
        automation_reason="lifecycle_apply_failed",
        lifecycle_event={
            "management_action": "add_position", "target_lifecycle_id": 9999,
        },
    )
    _add_recognition_decision(
        session_factory,
        chat_id=100, message_id=39, posted_at=signal_at - timedelta(minutes=1),
        automation_reason="lifecycle_apply_failed",
        lifecycle_event={
            "management_action": "add_position", "target_lifecycle_id": 1343,
        },
    )

    with session_factory() as session:
        evidence = find_rejected_add_position_before(
            session, chat_id=100, target_lifecycle_id=1343, signal_at=signal_at,
        )

    assert evidence is not None
    assert evidence.raw_message_id == rejected_id
    assert evidence.management_action == "add_position"


def test_find_rejected_add_position_before_returns_none_without_a_match(tmp_path):
    session_factory = create_session_factory(tmp_path / "guard-query-none.db")
    with session_factory() as session:
        assert (
            find_rejected_add_position_before(
                session, chat_id=100, target_lifecycle_id=1343, signal_at=PLANNED_AT,
            )
            is None
        )


# ---------------------------------------------------------------------------
# Planner-level replay: the real ``plan_strategy_management_batch`` entry
# point, binding-385-shaped numbers (BTC long, fill 83800, KOL price 83200).
# ---------------------------------------------------------------------------


def _seed_rejected_add_position(session_factory, *, chat_id, target_lifecycle_id, signal_at):
    _add_recognition_decision(
        session_factory,
        chat_id=chat_id, message_id=25, posted_at=signal_at + timedelta(minutes=1),
        automation_reason="lifecycle_apply_failed",
        lifecycle_event={
            "management_action": "add_position",
            "target_lifecycle_id": target_lifecycle_id,
            "entry_price": "83200",
            "side": "long",
            "symbol": "BTC",
        },
    )


def _matching_tpsl_order(*, pos_id, trigger_price):
    return {
        "ordId": "binding-385-old-stop",
        "posId": pos_id,
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "slTriggerPx": trigger_price,
        "sz": "0",
    }


def _seed_verified_stop(session_factory, *, binding_id, pos_id, trigger_price):
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding_id, pos_id=pos_id,
        ).one()
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=leg.strategy_instance_id,
            pos_id=pos_id,
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="binding-385-old-stop",
            purpose="stop_loss",
            trigger_price=trigger_price,
            size_text="0",
            status="verified",
            evidence_source="entry_protection_response",
            evidence={"match": "exact_written_order"},
            seen_at=PLANNED_AT,
        )
        session.commit()


def test_worse_stop_after_rejected_add_is_superseded_by_strategy_price(tmp_path):
    """Before this patch: the plan would have carried 83200 straight through.

    This is the replay the design asked for: raw 19514 (add_position,
    refused, target lifecycle) followed by an explicit ``adjust_stop_loss`` of
    83200 while our own fill is 83800. Confirm the fix by turning the history
    off in the paired negative test below.
    """

    planner = _planner()
    session_factory = create_session_factory(tmp_path / "worse-stop-superseded.db")
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory,
        intent="adjust_stop_loss",
        side="long",
        current_stop_loss=81800,
        requested_stop_loss="83200",
        stop_price_source="current_message_text",
        management_text="剩余仓位止损上移至83200",
        entry_range=(83800, 83800),
    )
    monkeypatch = pytest.MonkeyPatch()
    _disable_reconciliation(monkeypatch, planner)
    _seed_rejected_add_position(
        session_factory, chat_id=100, target_lifecycle_id=lifecycle_id,
        signal_at=PLANNED_AT,
    )
    _seed_verified_stop(
        session_factory, binding_id=binding_id, pos_id="pos-b", trigger_price="81800",
    )

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=_ReadOnlyDeepcoin(
            [_position(avg_px="83800", side="long")],
            tpsl_orders=[_matching_tpsl_order(pos_id="pos-b", trigger_price="81800")],
        ),
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )
    monkeypatch.undo()

    assert result.status == "ready"
    # The persisted intent itself is rewritten, not just the effective
    # action: the executor's ``reserve_break_even_market_actions`` refuses any
    # batch whose ``intent`` is not literally ``move_stop_to_break_even``.
    assert result.batch.intent == "move_stop_to_break_even"
    assert result.batch.effective_action == "break_even_by_market"
    assert result.batch.legs[0].planned_tpsl["break_even_reference_price"] == "83800"
    # The KOL's own worse number never becomes the armed target, and is
    # removed from the candidate entirely -- not merely overridden by the
    # reference -- so ``_planned_stop_price`` cannot prefer it back.
    assert result.batch.legs[0].planned_tpsl["break_even_reference_price"] != "83200"
    assert result.batch.legs[0].planned_tpsl.get("stop_loss_text") is None

    with session_factory() as session:
        incidents = session.query(RuntimeIncident).filter(
            RuntimeIncident.incident_type
            == "management_add_position_rejected_stop_superseded"
        ).all()
    assert len(incidents) == 1
    assert incidents[0].severity == "high"


def test_the_same_stop_is_unchanged_without_a_rejected_add_history(tmp_path):
    """Paired negative: identical fixture, no rejection row -- one flip.

    Same explicit 83200, same 83800 fill, same everything else. Without the
    rejected-add history this is an ordinary tightening
    ``adjust_stop_loss`` (81800 -> 83200 is tighter for a long) and the
    message's own number is armed, exactly as it behaves today.
    """

    planner = _planner()
    session_factory = create_session_factory(tmp_path / "worse-stop-unchanged.db")
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory,
        intent="adjust_stop_loss",
        side="long",
        current_stop_loss=81800,
        requested_stop_loss="83200",
        stop_price_source="current_message_text",
        management_text="剩余仓位止损上移至83200",
        entry_range=(83800, 83800),
    )
    monkeypatch = pytest.MonkeyPatch()
    _disable_reconciliation(monkeypatch, planner)
    # No rejected-add-position row this time.
    _seed_verified_stop(
        session_factory, binding_id=binding_id, pos_id="pos-b", trigger_price="81800",
    )

    client = _ReadOnlyDeepcoin(
        [_position(avg_px="83800", side="long")],
        tpsl_orders=[_matching_tpsl_order(pos_id="pos-b", trigger_price="81800")],
    )
    monkeypatch.setattr(client, "get_ticker_quote", lambda **kwargs: {
        "instrument_id": "BTC-USDT-SWAP", "price": "83900", "price_field": "last",
        "observed_at": PLANNED_AT.isoformat(),
    })

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )
    monkeypatch.undo()

    assert result.status == "ready"
    assert result.batch.effective_action == "adjust_stop_loss"
    assert result.batch.legs[0].planned_tpsl["stop_loss_text"] == "83200"

    with session_factory() as session:
        incidents = session.query(RuntimeIncident).filter(
            RuntimeIncident.incident_type
            == "management_add_position_rejected_stop_superseded"
        ).all()
    assert incidents == []


def test_a_stop_that_is_not_worse_than_the_fill_is_also_unchanged(tmp_path):
    """Both conditions are independent: rejection history alone is not enough.

    Same rejected-add history as the first test, but the explicit stop
    (84000) is *better* than our 83800 fill, so the guard must not fire.
    """

    planner = _planner()
    session_factory = create_session_factory(tmp_path / "not-worse-unchanged.db")
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory,
        intent="adjust_stop_loss",
        side="long",
        current_stop_loss=81800,
        requested_stop_loss="84000",
        stop_price_source="current_message_text",
        management_text="剩余仓位止损上移至84000",
        entry_range=(83800, 83800),
    )
    monkeypatch = pytest.MonkeyPatch()
    _disable_reconciliation(monkeypatch, planner)
    _seed_rejected_add_position(
        session_factory, chat_id=100, target_lifecycle_id=lifecycle_id,
        signal_at=PLANNED_AT,
    )
    _seed_verified_stop(
        session_factory, binding_id=binding_id, pos_id="pos-b", trigger_price="81800",
    )

    client = _ReadOnlyDeepcoin(
        [_position(avg_px="83800", side="long")],
        tpsl_orders=[_matching_tpsl_order(pos_id="pos-b", trigger_price="81800")],
    )
    monkeypatch.setattr(client, "get_ticker_quote", lambda **kwargs: {
        "instrument_id": "BTC-USDT-SWAP", "price": "84200", "price_field": "last",
        "observed_at": PLANNED_AT.isoformat(),
    })

    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )
    monkeypatch.undo()

    assert result.status == "ready"
    assert result.batch.effective_action == "adjust_stop_loss"
    assert result.batch.legs[0].planned_tpsl["stop_loss_text"] == "84000"

    with session_factory() as session:
        incidents = session.query(RuntimeIncident).filter(
            RuntimeIncident.incident_type
            == "management_add_position_rejected_stop_superseded"
        ).all()
    assert incidents == []


# ---------------------------------------------------------------------------
# Executor-level replay: the real batch this guard plans, handed to the
# worker's own execution entry point (``execute_management_batch``), not a
# hand-built batch shaped to look like one. Two market prices, and one
# mutation check that the redirect is actually load-bearing.
# ---------------------------------------------------------------------------


def _plan_worse_stop_batch(
    session_factory, *, with_rejection_history, redirect_active=True,
):
    """The exact fixture the first three tests use: 83200 KOL price, 83800 fill.

    Returns the real ``ManagementPlanningResult`` from
    ``plan_strategy_management_batch`` -- the same entry point production
    calls -- so the executor tests below hand the executor a batch this
    module actually produced, not one built by hand to resemble it.

    ``redirect_active=False`` is only for the mutation test: with the
    redirect turned off, planning takes the ordinary ``adjust_stop_loss``
    explicit-price stop-gate path, which needs a plan-time quote the
    redirected path never reads at all -- so this only patches
    ``get_ticker_quote`` in that one case, to keep the other tests proving
    the redirected path never needs a plan-time quote either.
    """

    planner = _planner()
    raw_id, lifecycle_id, binding_id = _persist_exact_management_target(
        session_factory,
        intent="adjust_stop_loss",
        side="long",
        current_stop_loss=81800,
        requested_stop_loss="83200",
        stop_price_source="current_message_text",
        management_text="剩余仓位止损上移至83200",
        entry_range=(83800, 83800),
    )
    monkeypatch = pytest.MonkeyPatch()
    _disable_reconciliation(monkeypatch, planner)
    if with_rejection_history:
        _seed_rejected_add_position(
            session_factory, chat_id=100, target_lifecycle_id=lifecycle_id,
            signal_at=PLANNED_AT,
        )
    _seed_verified_stop(
        session_factory, binding_id=binding_id, pos_id="pos-b", trigger_price="81800",
    )
    client = _ReadOnlyDeepcoin(
        [_position(avg_px="83800", side="long")],
        tpsl_orders=[_matching_tpsl_order(pos_id="pos-b", trigger_price="81800")],
    )
    if not redirect_active:
        monkeypatch.setattr(client, "get_ticker_quote", lambda **kwargs: {
            "instrument_id": "BTC-USDT-SWAP", "price": "83900", "price_field": "last",
            "observed_at": PLANNED_AT.isoformat(),
        })
    result = planner.plan_strategy_management_batch(
        session_factory,
        raw_message_id=raw_id,
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        planned_at=PLANNED_AT,
    )
    monkeypatch.undo()
    return result


class _BreakEvenExecutorClient:
    """Everything ``execute_management_batch``'s break-even-by-market route
    reads or writes, for one BTC long position ``pos-b`` at 83800/10.

    ``market_price`` backs both the position row's ``markPx`` (what
    ``reserve_break_even_market_actions`` reads first) and
    ``get_ticker_quote`` (what ``validate_batch_stops`` and the market
    decision itself read) -- the two are never made to disagree here, the
    same simplification ``test_mia_m5_composite_remainder_replay.py`` makes
    for the composite route.
    """

    def __init__(self, *, market_price: str, position_size: str = "10"):
        self.market_price = market_price
        self.position_size = position_size
        self.pending = [
            {
                "ordId": "binding-385-old-stop", "posId": "pos-b",
                "instId": "BTC-USDT-SWAP", "posSide": "long",
                "triggerOrderType": "TPSL", "slTriggerPx": "81800", "sz": "0",
            },
        ]
        self.open_orders: list[dict] = []
        self.set_calls: list[dict] = []
        self.cancel_sltp_calls: list[dict] = []
        self.close_calls: list[dict] = []

    def list_positions(self, *, inst_id=None):
        if self.position_size in (None, "0"):
            return []
        return [
            {
                "posId": "pos-b", "instId": "BTC-USDT-SWAP", "posSide": "long",
                "pos": self.position_size, "avgPx": "83800",
                "markPx": self.market_price, "mgnMode": "cross",
                "mrgPosition": "split", "posMode": "split",
            }
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

    def list_trigger_order_history(self, *, inst_id):
        return []

    def list_order_history(self, *, inst_id):
        return []

    def list_trade_fills(self, *, inst_id):
        return []

    def list_open_orders(self, *, inst_id=None):
        return list(self.open_orders)

    def get_ticker_quote(self, *, inst_id):
        return {
            "instrument_id": "BTC-USDT-SWAP", "price": self.market_price,
            "price_field": "last", "observed_at": PLANNED_AT.isoformat(),
        }

    def set_position_sltp(self, payload):
        self.set_calls.append(dict(payload))
        order_id = f"new-stop-{len(self.set_calls)}"
        # A full-position (non-partial) protection write omits ``sz``
        # entirely (``PositionMutationGateway.set_exact_position_sltp``); the
        # exchange's own convention for "whole position" is ``sz="0"``.
        self.pending.append(
            {
                "ordId": order_id, "posId": "pos-b", "instId": "BTC-USDT-SWAP",
                "posSide": "long", "triggerOrderType": "TPSL",
                "slTriggerPx": payload["slTriggerPx"],
                "sz": payload.get("sz", "0"),
            }
        )
        return {"code": "0", "data": {"ordId": order_id}}

    def cancel_position_sltp(self, payload):
        order_id = payload["ordId"]
        self.cancel_sltp_calls.append(dict(payload))
        self.pending = [row for row in self.pending if row["ordId"] != order_id]
        return {"code": "0", "data": {"ordId": order_id}}

    def cancel_trigger_order(self, payload):  # pragma: no cover - unused route
        raise AssertionError("this replay never cancels via cancel_trigger_order")

    def place_order(self, payload):
        self.close_calls.append(dict(payload))
        self.position_size = "0"
        return {"code": "0", "data": {"ordId": f"close-{len(self.close_calls)}"}}

    def cancel_order(self, payload):  # pragma: no cover - no deferred entries
        raise AssertionError("this replay has no deferred entry legs to cancel")


def _execute(session_factory, batch_id, *, market_price):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    client = _BreakEvenExecutorClient(market_price=market_price)
    result = execute_management_batch(
        session_factory, batch_id=batch_id, deepcoin_client=client,
        executed_at=PLANNED_AT,
    )
    return result, client


def test_executor_arms_the_strategy_price_not_83200_when_the_market_allows_it(
    tmp_path,
):
    """Market 83900: the redirected batch behaves exactly like a native
    ``move_stop_to_break_even`` batch reaching the executor -- the strategy
    price (83800) is armed, and 83200 is written nowhere at all.
    """

    session_factory = create_session_factory(tmp_path / "exec-armed.db")
    plan = _plan_worse_stop_batch(session_factory, with_rejection_history=True)
    assert plan.status == "ready"
    assert plan.batch.intent == "move_stop_to_break_even"

    result, client = _execute(session_factory, plan.batch.id, market_price="83900")

    assert result["status"] == "succeeded"
    assert client.close_calls == []
    assert [call["slTriggerPx"] for call in client.set_calls] == ["83800"]
    assert all(call["slTriggerPx"] != "83200" for call in client.set_calls)
    assert client.cancel_sltp_calls and (
        client.cancel_sltp_calls[0]["ordId"] == "binding-385-old-stop"
    )

    from telegram_kol_research.management_stop_price_gate import (
        validate_batch_stops,
    )
    from telegram_kol_research.strategy_management_batches import (
        load_management_batch,
    )

    batch = load_management_batch(session_factory, plan.batch.id)
    gate = validate_batch_stops(
        session_factory, batch=batch, client=client, now=PLANNED_AT,
    )
    assert gate is None


def test_executor_market_closes_the_remainder_when_83800_is_on_the_wrong_side(
    tmp_path,
):
    """Market 83700: the strategy price 83800 is above the market for a long,
    so it can never be armed. The executor closes at market instead of
    writing either 83800 or 83200 as a stop.
    """

    session_factory = create_session_factory(tmp_path / "exec-market-close.db")
    plan = _plan_worse_stop_batch(session_factory, with_rejection_history=True)
    assert plan.status == "ready"

    result, client = _execute(session_factory, plan.batch.id, market_price="83700")

    assert result["status"] == "reconciling"
    assert client.set_calls == []
    assert [(call["closePosId"], call["sz"]) for call in client.close_calls] == [
        ("pos-b", "10")
    ]

    from telegram_kol_research.management_stop_price_gate import (
        validate_batch_stops,
    )
    from telegram_kol_research.strategy_management_batches import (
        load_management_batch,
    )

    batch = load_management_batch(session_factory, plan.batch.id)
    gate = validate_batch_stops(
        session_factory, batch=batch, client=client, now=PLANNED_AT,
    )
    assert gate is None


def test_removing_the_redirect_writes_83200_straight_through(tmp_path, monkeypatch):
    """Mutation check: turn the redirect off, keep everything else identical.

    ``strategy_management_planner._redirect_stop_worse_than_fill_after_rejected_add``
    is monkeypatched to its own no-op default (``default_intent`` unchanged,
    ``identity`` unchanged, no evidence) -- exactly what it returns today when
    either of its two conditions is false. With the same rejection history and
    the same worse price, the batch now keeps ``intent == "adjust_stop_loss"``
    and its explicit 83200, and the executor arms 83200 verbatim. This is the
    positive control for the two tests above: without the redirect, the same
    fixture produces the bug the redirect exists to prevent.
    """

    planner = _planner()

    def _no_redirect(session_factory, *, identity, lifecycle, economics, candidate,
                      default_intent, now):
        return default_intent, identity, None

    monkeypatch.setattr(
        planner, "_redirect_stop_worse_than_fill_after_rejected_add", _no_redirect,
    )

    session_factory = create_session_factory(tmp_path / "exec-mutation.db")
    plan = _plan_worse_stop_batch(
        session_factory, with_rejection_history=True, redirect_active=False,
    )

    assert plan.status == "ready"
    assert plan.batch.intent == "adjust_stop_loss"
    assert plan.batch.legs[0].planned_tpsl["stop_loss_text"] == "83200"

    result, client = _execute(session_factory, plan.batch.id, market_price="83900")

    assert result["status"] == "succeeded"
    assert [call["slTriggerPx"] for call in client.set_calls] == ["83200"]
