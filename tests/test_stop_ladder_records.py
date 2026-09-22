"""Order-level fill evidence, and the level derived back out of it.

The fixtures are the venue's own shapes (``tests/deepcoin_production_rows``):
a pending TPSL row has 23 keys and **no ``posId``**, a trigger-history row has
no ``state``.  A fixture that invented either would let the defect this phase
exists to fix pass unnoticed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from deepcoin_production_rows import (
    pending_take_profit_row,
    position_row,
    trigger_history_row,
)

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    PositionMutationIntent,
    PositionProtectionLedger,
    PositionReconciliationObservation,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.stop_ladder_records import (
    derive_filled_tp_level,
    reconcile_take_profit_fill_levels,
)


NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
POS = "1001125216121996"
TPS = (("tp-79800", "79800"), ("tp-79100", "79100"), ("tp-78400", "78400"))


def _seed(tmp_path, *, tps=TPS, statuses=None):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
            pos_id=POS,
            strategy_instance_id="deepcoin:1:1:BTC:short",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            purpose="entry",
            order_kind="limit",
            strategy_instance_id="deepcoin:1:1:BTC:short",
            venue="deepcoin",
            pos_id=POS,
            status="active",
            attribution_status="verified",
        ),
    )
    with session_factory() as session:
        for index, (order_id, price) in enumerate(tps):
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                strategy_instance_id="deepcoin:1:1:BTC:short",
                pos_id=POS,
                instrument_id=INST,
                side="short",
                order_id=order_id,
                purpose="take_profit",
                trigger_price=price,
                size_text="3",
                status=(statuses or {}).get(order_id, "verified"),
                evidence_source="test",
                evidence={},
                seen_at=NOW - timedelta(minutes=10),
            )
        session.commit()
    return session_factory, binding_id, leg_id


def _observe(session_factory, binding_id, leg_id, sizes, *, complete=True):
    with session_factory() as session:
        for index, size in enumerate(sizes):
            session.add(
                PositionReconciliationObservation(
                    venue="deepcoin",
                    execution_binding_id=binding_id,
                    execution_order_leg_id=leg_id,
                    strategy_instance_id="deepcoin:1:1:BTC:short",
                    pos_id=POS,
                    instrument_id=INST,
                    side="short",
                    size_text=str(size),
                    avg_entry_price="80436",
                    pending_tpsl_json="[]",
                    snapshot_complete=complete,
                    snapshot_fingerprint=f"fp-{index}",
                    observed_at=NOW - timedelta(minutes=5 - index),
                )
            )
        session.commit()


def _reconcile(
    session_factory,
    *,
    pending=(),
    history=(),
    complete=True,
    errors=None,
    size="7",
):
    with session_factory() as session:
        result = reconcile_take_profit_fill_levels(
            session,
            positions=[
                position_row(
                    pos_id=POS,
                    inst_id=INST,
                    pos_side="short",
                    size=size,
                    avg_price="80436",
                )
            ],
            pending_orders=[
                pending_take_profit_row(
                    ord_id=order_id,
                    inst_id=INST,
                    pos_side="short",
                    trigger_price=price,
                    size="3",
                )
                for order_id, price in pending
            ],
            trigger_history=list(history),
            pending_snapshot_complete_by_instrument={INST: complete},
            snapshot_errors=errors or {},
            observed_at=NOW,
        )
        session.commit()
        return result


def _clean_history(order_id, price):
    return trigger_history_row(
        ord_id=order_id,
        inst_id=INST,
        pos_side="short",
        trigger_price=price,
        size="3",
    )


def _ledger(session_factory, order_id):
    with session_factory() as session:
        return (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=order_id)
            .one()
        )


def _evidence(session_factory, order_id):
    import json

    row = _ledger(session_factory, order_id)
    return json.loads(row.evidence_json or "{}").get("take_profit_fill")


# --------------------------------------------------------------- form A / B


def test_form_a_a_clean_trigger_history_row_fills_the_ledger_row(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)

    result = _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        history=[_clean_history("tp-79800", "79800")],
    )

    assert result.filled == 1
    assert _ledger(session_factory, "tp-79800").status == "filled"
    evidence = _evidence(session_factory, "tp-79800")
    assert evidence["level"] == 1
    assert evidence["evidence_form"] == "trigger_history"
    assert evidence["decided_at"] == NOW.isoformat()


def test_form_b_a_silent_history_plus_a_smaller_position_fills_the_row(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "7"])

    result = _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        history=[],
    )

    assert result.filled == 1
    evidence = _evidence(session_factory, "tp-79800")
    assert evidence["evidence_form"] == "position_decrease"
    assert evidence["observation_ids"]


def test_form_b_takes_any_decrease_and_never_compares_quantity(tmp_path):
    """The stage is 3 contracts; the position fell by 1.  Still reached."""

    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "9"])

    result = _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        size="9",
    )

    assert result.filled == 1


def test_a_position_that_did_not_shrink_is_not_a_fill(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "10"])

    result = _reconcile(
        session_factory, pending=(("tp-79100", "79100"), ("tp-78400", "78400"))
    )

    assert result.filled == 0
    assert result.unproven == 1
    assert _ledger(session_factory, "tp-79800").status == "verified"


def test_an_order_we_cancelled_is_never_a_fill(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "7"])
    with session_factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="cancel-tp-79800",
                venue="deepcoin",
                operation="cancel_position_sltp",
                strategy_instance_id="deepcoin:1:1:BTC:short",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                pos_id=POS,
                order_id="tp-79800",
                authority_fingerprint="fp",
                request_fingerprint="fp",
                status="confirmed",
                reserved_at=NOW - timedelta(minutes=6),
            )
        )
        session.commit()

    result = _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        history=[_clean_history("tp-79800", "79800")],
    )

    assert result.filled == 0
    assert _ledger(session_factory, "tp-79800").status == "verified"
    assert result.reasons.get("stop_ladder_cancel_intended") == 1


def test_an_order_still_resting_is_not_a_fill_and_is_not_counted(tmp_path):
    session_factory, _binding_id, _leg_id = _seed(tmp_path)

    result = _reconcile(
        session_factory,
        pending=TPS,
        history=[_clean_history("tp-79800", "79800")],
    )

    assert (result.filled, result.unproven) == (0, 0)


def test_an_incomplete_snapshot_changes_nothing_and_only_counts(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "7"])

    result = _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        history=[_clean_history("tp-79800", "79800")],
        complete=False,
    )

    assert result.filled == 0
    # Only the absent rung is "unprovable"; the reason is recorded for all
    # three, because an incomplete read cannot even say the other two are
    # still resting.
    assert result.unproven == 1
    assert result.reasons.get("stop_ladder_pending_snapshot_incomplete") == 3
    assert _ledger(session_factory, "tp-79800").status == "verified"


def test_a_snapshot_error_is_unknown_not_a_fill(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "7"])

    result = _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        errors={"trigger_orders": "read failed"},
    )

    assert result.filled == 0


def test_a_failed_trigger_is_never_a_fill(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "7"])

    result = _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        history=[
            trigger_history_row(
                ord_id="tp-79800",
                inst_id=INST,
                pos_side="short",
                trigger_price="79800",
                size="3",
                error_code="51004",
            )
        ],
    )

    assert result.filled == 0
    assert result.reasons.get("take_profit_trigger_failed") == 1


def test_one_decrease_settles_every_rung_that_vanished_with_it(tmp_path):
    """Form B is per order, and the spec's conjunction is per order too.

    Two rungs gone from one complete snapshot, one decrease, nothing in the
    history: both are reached, so the level is 2.  Recorded here because it is
    the aggressive edge of form B -- if only one of them really traded, the
    would-be stop is one rung tighter than the truth.  In phase 1 that is a
    shadow line and nothing else; it is on the first-sample checklist.
    """

    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "4"])

    result = _reconcile(session_factory, pending=(("tp-78400", "78400"),), size="4")

    assert result.filled == 2
    with session_factory() as session:
        assert derive_filled_tp_level(
            session, execution_binding_id=binding_id, pos_ids=[POS]
        ).filled_level == 2


def test_the_reconcile_creates_no_position_mutation_intent(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "7"])

    _reconcile(session_factory, pending=(("tp-79100", "79100"),))

    with session_factory() as session:
        assert session.query(PositionMutationIntent).count() == 0


def test_a_row_already_filled_is_not_rewritten(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _observe(session_factory, binding_id, leg_id, ["10", "7"])
    _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        history=[_clean_history("tp-79800", "79800")],
    )
    first = _evidence(session_factory, "tp-79800")

    again = _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        history=[_clean_history("tp-79800", "79800")],
    )

    assert again.filled == 0
    assert _evidence(session_factory, "tp-79800") == first


# ------------------------------------------------------------- derivation


def test_the_level_comes_back_out_of_the_evidence(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _reconcile(
        session_factory,
        pending=(("tp-78400", "78400"),),
        history=[
            _clean_history("tp-79800", "79800"),
            _clean_history("tp-79100", "79100"),
        ],
    )

    with session_factory() as session:
        ladder = derive_filled_tp_level(
            session, execution_binding_id=binding_id, pos_ids=[POS]
        )

    assert ladder.filled_level == 2
    assert ladder.levels_by_position == {POS: 2}
    assert [rung.level for rung in ladder.rungs_by_position[POS]] == [1, 2, 3]


def test_the_derivation_is_identical_when_it_runs_again(tmp_path):
    """Nothing is cached, so a restart re-reads the same answer."""

    session_factory, binding_id, leg_id = _seed(tmp_path)
    _reconcile(
        session_factory,
        pending=(("tp-79100", "79100"), ("tp-78400", "78400")),
        history=[_clean_history("tp-79800", "79800")],
    )

    with session_factory() as session:
        first = derive_filled_tp_level(
            session, execution_binding_id=binding_id, pos_ids=[POS]
        ).as_evidence()
    with session_factory() as session:
        second = derive_filled_tp_level(
            session, execution_binding_id=binding_id, pos_ids=[POS]
        ).as_evidence()

    assert first == second
    assert first["filled_level"] == 1


def test_a_retired_rung_renumbers_the_ones_below_it(tmp_path):
    """A KOL moving a take profit cancels the old order; the ladder follows."""

    session_factory, binding_id, _leg_id = _seed(
        tmp_path, statuses={"tp-79800": "retired"}
    )
    _reconcile(
        session_factory,
        pending=(("tp-78400", "78400"),),
        history=[_clean_history("tp-79100", "79100")],
    )

    with session_factory() as session:
        ladder = derive_filled_tp_level(
            session, execution_binding_id=binding_id, pos_ids=[POS]
        )

    assert ladder.filled_level == 1
    assert [rung.order_id for rung in ladder.rungs_by_position[POS]] == [
        "tp-79100",
        "tp-78400",
    ]


def test_a_ledger_row_filled_by_protection_health_still_counts(tmp_path):
    """The other writer's row carries no level; the sequence supplies it."""

    session_factory, binding_id, _leg_id = _seed(
        tmp_path, statuses={"tp-79800": "filled"}
    )

    with session_factory() as session:
        ladder = derive_filled_tp_level(
            session, execution_binding_id=binding_id, pos_ids=[POS]
        )

    assert ladder.filled_level == 1


def test_no_evidence_means_level_zero(tmp_path):
    session_factory, binding_id, _leg_id = _seed(tmp_path)

    with session_factory() as session:
        ladder = derive_filled_tp_level(
            session, execution_binding_id=binding_id, pos_ids=[POS]
        )

    assert ladder.filled_level == 0
