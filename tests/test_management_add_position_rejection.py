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
    assert result.batch.intent == "adjust_stop_loss"
    assert result.batch.effective_action == "break_even_by_market"
    assert result.batch.legs[0].planned_tpsl["break_even_reference_price"] == "83800"
    # The KOL's own worse number never becomes the armed target.
    assert result.batch.legs[0].planned_tpsl["break_even_reference_price"] != "83200"

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
