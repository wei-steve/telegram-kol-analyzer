from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    DeepcoinWsEvent,
    ExecutionEvent,
    ExecutionOrderLeg,
)
from telegram_kol_research.protection_authority_shadow import (
    SHADOW_EVENT_ACTION,
    VERDICT_AGREED,
    VERDICT_CHAIN_FROZEN,
    VERDICT_CHAIN_RESOLVED_LEGACY_AMBIGUOUS,
    VERDICT_SET_MISMATCH,
    run_protection_authority_shadow_pass,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
INST = "ETH-USDT-SWAP"


class _Client:
    def __init__(self, pending_rows, *, pending_raises=False):
        self.pending_rows = pending_rows
        self.pending_raises = pending_raises
        self.write_calls = 0

    def list_positions(self, *, inst_id=None):
        return [
            {
                "instId": INST,
                "posId": "pos-1",
                "posSide": "long",
                "pos": "4",
                "avgPx": "2600",
            }
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        if self.pending_raises:
            raise RuntimeError("read failed")
        return [row for row in self.pending_rows if row["instId"] == inst_id]

    def set_position_sltp(self, payload):  # pragma: no cover - must never run
        self.write_calls += 1
        raise AssertionError("the shadow pass must not write to the exchange")

    def cancel_position_sltp(self, payload):  # pragma: no cover - must never run
        self.write_calls += 1
        raise AssertionError("the shadow pass must not write to the exchange")


def _seed(tmp_path, *, with_ledger_stop=True):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            strategy_instance_id="deepcoin:1:1:ETH:long",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
            attribution_status="verified",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        if with_ledger_stop:
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                strategy_instance_id=None,
                pos_id="pos-1",
                instrument_id=INST,
                side="long",
                order_id="stop-1",
                purpose="stop_loss",
                trigger_price="2500",
                size_text="4",
                status="verified",
                evidence_source="test",
                evidence={},
                seen_at=NOW,
            )
        session.commit()
    return session_factory


def _stop_row(order_id, price="2500"):
    return {
        "ordId": order_id,
        "instId": INST,
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "slTriggerPrice": price,
        "sz": "4",
    }


def _shadow_rows(session_factory):
    with session_factory() as session:
        return (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == SHADOW_EVENT_ACTION)
            .order_by(ExecutionEvent.id.asc())
            .all()
        )


def test_agreement_is_counted_and_recorded_not_merely_absent(tmp_path):
    session_factory = _seed(tmp_path)
    client = _Client([_stop_row("stop-1")])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["positions_seen"] == 1
    assert result["counts_by_verdict"][VERDICT_AGREED] == 1
    assert result["rows_recorded"] == 1
    assert client.write_calls == 0
    rows = _shadow_rows(session_factory)
    assert [row.reason for row in rows] == [VERDICT_AGREED]


def test_an_unchanged_comparison_is_counted_again_but_not_rerecorded(tmp_path):
    session_factory = _seed(tmp_path)
    client = _Client([_stop_row("stop-1")])

    run_protection_authority_shadow_pass(session_factory, deepcoin_client=client, now=NOW)
    second = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert second["counts_by_verdict"][VERDICT_AGREED] == 1
    assert second["rows_recorded"] == 0
    assert len(_shadow_rows(session_factory)) == 1


def test_chain_places_the_row_that_makes_the_legacy_matcher_ambiguous(tmp_path):
    session_factory = _seed(tmp_path)
    with session_factory() as session:
        session.add(
            DeepcoinWsEvent(
                venue="deepcoin",
                channel="TriggerOrder",
                action="push",
                order_sys_id="stop-2",
                trade_unit_id="pos-1",
                received_at=NOW,
                received_ms=1,
                raw_payload="{}",
                payload_hash="hash-stop-2",
            )
        )
        session.commit()
    client = _Client([_stop_row("stop-1"), _stop_row("stop-2", price="2535.06")])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["counts_by_verdict"][VERDICT_CHAIN_RESOLVED_LEGACY_AMBIGUOUS] == 1
    row = _shadow_rows(session_factory)[0]
    assert row.reason == VERDICT_CHAIN_RESOLVED_LEGACY_AMBIGUOUS
    assert "stop-2" in row.after_json


def test_a_failed_pending_read_freezes_instead_of_manufacturing_agreement(tmp_path):
    session_factory = _seed(tmp_path)
    client = _Client([], pending_raises=True)

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["read_failures"] == [INST]
    assert result["counts_by_verdict"][VERDICT_CHAIN_FROZEN] == 1
    assert result["counts_by_verdict"][VERDICT_AGREED] == 0


def test_an_unowned_row_freezes_the_chain_and_is_named(tmp_path):
    session_factory = _seed(tmp_path, with_ledger_stop=False)
    client = _Client([_stop_row("stop-unknown")])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["counts_by_verdict"][VERDICT_CHAIN_FROZEN] == 1
    row = _shadow_rows(session_factory)[0]
    assert "protection_order_unattributable" in row.after_json
    assert "stop-unknown" in row.after_json


def test_the_round_log_carries_the_paired_protection_counters(tmp_path):
    """Agreements and freezes ride the same line, so a window sums from the journal."""

    from telegram_kol_research.web_app import _build_deepcoin_reconcile_round_log

    session_factory = create_session_factory(tmp_path / "round.db")

    payload = _build_deepcoin_reconcile_round_log(
        session_factory,
        trigger="by_timer",
        round_started_at=NOW,
        round_finished_at=NOW,
        wake_requested_at=None,
        shadow_summary=None,
        protection_shadow_summary={
            "positions_seen": 2,
            "counts_by_verdict": {VERDICT_AGREED: 2, VERDICT_CHAIN_FROZEN: 0},
            "rows_recorded": 0,
            "read_failures": [],
        },
    )

    assert payload["protection_shadow"]["counts_by_verdict"][VERDICT_AGREED] == 2
    assert payload["protection_shadow"]["counts_by_verdict"][VERDICT_CHAIN_FROZEN] == 0
    # Ids only: no symbol, side, size or price may appear in an operational log.
    import json as _json

    assert "ETH" not in _json.dumps(payload)


def test_a_failing_protection_shadow_pass_never_disturbs_the_reconcile_loop():
    import asyncio

    from telegram_kol_research.web_app import _run_protection_authority_shadow_step

    def _explode():
        raise RuntimeError("client unavailable")

    result = asyncio.run(
        _run_protection_authority_shadow_step(
            session_factory=None,
            deepcoin_client_factory=_explode,
            now_provider=lambda: NOW,
        )
    )

    assert result is None


def test_two_positions_on_one_instrument_are_counted_exactly_twice(tmp_path):
    """The counter itself needs a test: a per-instrument read must not double-count.

    ARCHITECTURE section 6 -- a positive observation is only worth having if
    something asserts it counts correctly. Two positions sharing one
    ``trigger-orders-pending`` read is exactly the shape that would count each
    of them once per row rather than once per position.
    """

    session_factory = _seed(tmp_path)
    binding_id = 1
    with session_factory() as session:
        leg_id = upsert_execution_order_leg(
            session_factory,
            ExecutionOrderLegRecord(
                execution_binding_id=binding_id,
                leg_index=2,
                purpose="entry",
                order_kind="market",
                strategy_instance_id="deepcoin:1:1:ETH:long",
                venue="deepcoin",
                pos_id="pos-2",
                status="active",
                attribution_status="verified",
            ),
        )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            strategy_instance_id=None,
            pos_id="pos-2",
            instrument_id=INST,
            side="long",
            order_id="stop-2",
            purpose="stop_loss",
            trigger_price="2400",
            size_text="4",
            status="verified",
            evidence_source="test",
            evidence={},
            seen_at=NOW,
        )
        session.commit()

    class _TwoPositionClient(_Client):
        def list_positions(self, *, inst_id=None):
            return [
                {"instId": INST, "posId": "pos-1", "posSide": "long", "pos": "4"},
                {"instId": INST, "posId": "pos-2", "posSide": "long", "pos": "4"},
            ]

    client = _TwoPositionClient([_stop_row("stop-1"), _stop_row("stop-2", price="2400")])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["positions_seen"] == 2
    assert result["counts_by_verdict"][VERDICT_AGREED] == 2
    assert result["rows_recorded"] == 2
    assert len(_shadow_rows(session_factory)) == 2


def test_a_position_no_leg_owns_is_counted_but_never_written_down(tmp_path):
    """A row naming a manual position is the first step of claiming it."""

    from telegram_kol_research.protection_authority_shadow import (
        VERDICT_UNBOUND_POSITION,
    )

    session_factory = _seed(tmp_path)

    class _ManualPositionClient(_Client):
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": INST,
                    "posId": "manual-position-1",
                    "posSide": "short",
                    "pos": "3",
                }
            ]

    client = _ManualPositionClient([])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["counts_by_verdict"][VERDICT_UNBOUND_POSITION] == 1
    assert result["rows_recorded"] == 0
    assert _shadow_rows(session_factory) == []


def test_a_resting_entrys_stop_is_counted_as_stepped_over_not_as_a_freeze(tmp_path):
    """The positive observation paired with ``chain_frozen``.

    A freeze count of zero has two causes -- the exclusion worked, or no entry
    was resting -- and only this counter tells them apart.
    """

    from telegram_kol_research.execution_bindings import ExecutionOrderLegRecord

    session_factory = _seed(tmp_path)
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=1,
            leg_index=9,
            purpose="entry",
            order_kind="limit",
            strategy_instance_id="deepcoin:1:1:ETH:long",
            venue="deepcoin",
            status="pending",
            attribution_status="unassigned",
            order_id="entry-99",
            request={
                "instId": INST,
                "posSide": "long",
                "ordType": "limit",
                "px": "2600.0",
                "sz": "6.0",
                "slTriggerPx": "2400.0",
                "tdMode": "cross",
                "mrgPosition": "split",
                "side": "buy",
            },
        ),
    )
    with session_factory() as session:
        session.add(
            DeepcoinWsEvent(
                venue="deepcoin",
                channel="TriggerOrder",
                action="push",
                order_sys_id="entry-stop-98",
                trade_unit_id="default",
                received_at=NOW,
                received_ms=1,
                raw_payload="{}",
                payload_hash="hash-entry-stop-98",
            )
        )
        session.commit()
    resting = _stop_row("entry-stop-98", price="2400")
    resting["sz"] = "6"
    client = _Client([_stop_row("stop-1"), resting])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["excluded_pending_entry_stops"] == 1
    assert result["counts_by_verdict"][VERDICT_CHAIN_FROZEN] == 0
    # And this is where the new chain beats the old matcher: the same resting
    # entry's stop makes the legacy matcher call the whole instrument
    # ambiguous, while the chain steps over it and still names the position's
    # own stop.
    assert result["counts_by_verdict"][VERDICT_CHAIN_RESOLVED_LEGACY_AMBIGUOUS] == 1
    row = _shadow_rows(session_factory)[0]
    assert "entry-stop-98" in row.after_json


def test_the_cancel_precheck_is_evaluated_on_every_live_protection_order(tmp_path):
    """Its positive count exists because "no mismatches" alone proves nothing.

    A window with no protection orders and a window where every precheck passed
    both report zero mismatches. Only the ``match`` count separates them.
    """

    session_factory = _seed(tmp_path)
    client = _Client([_stop_row("stop-1")])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["cancel_precheck"] == {"match": 1}
    row = _shadow_rows(session_factory)[0]
    assert '"stop-1": "match"' in row.after_json.replace("'", '"')


def test_an_order_that_moved_between_the_two_reads_is_not_a_match(tmp_path):
    """The drift a cancel must catch happens *between* the two reads.

    Comparing the resolve-time read with itself can only ever answer "match",
    so the check is only worth anything if the second read is a real one. Here
    the exchange changes the trigger price between them, which is precisely the
    moment a cancel would otherwise remove an order it no longer recognises.
    """

    session_factory = _seed(tmp_path)

    class _ShiftingClient(_Client):
        def __init__(self, rows):
            super().__init__(rows)
            self.reads = 0

        def list_trigger_orders_pending(self, *, inst_id):
            self.reads += 1
            rows = super().list_trigger_orders_pending(inst_id=inst_id)
            if self.reads > 1:
                rows = [dict(row) for row in rows]
                for row in rows:
                    if row["ordId"] == "stop-1":
                        row["slTriggerPrice"] = "2400"
            return rows

    client = _ShiftingClient([_stop_row("stop-1")])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert client.reads == 2
    assert result["cancel_precheck"] == {
        "protection_cancel_target_trigger_changed": 1
    }


def test_ledger_drift_is_observed_without_blocking_anything(tmp_path):
    """A stale ledger row is a reason to look, not a reason to refuse a cancel.

    A staged take profit that partially fills leaves our recorded size behind
    the live one. Keying the cancel gate on that would break a management
    instruction that works today, so it is counted and not enforced.
    """

    session_factory = _seed(tmp_path)
    drifted = _stop_row("stop-1")
    drifted["sz"] = "2"  # ledger says 4

    client = _Client([drifted])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["ledger_drift"] == 1
    assert result["cancel_precheck"] == {"match": 1}


def test_legacy_absent_and_chain_resolved_is_an_improvement_not_a_mismatch(tmp_path):
    """The production shape of 2026-09-10 16:07, which `set_mismatch` misnamed.

    A limit entry's own stop is never written into the ledger -- its order id
    is not in the submission receipt -- so the legacy matcher has no ownership
    source and answers ``absent``: its verdict is that a live position has no
    protection at all, while the exchange holds a stop for it. The chain places
    that order by ``TU`` and names it. Filing that under "the two disagree"
    would frame the fix as a defect.
    """

    from telegram_kol_research.protection_authority_shadow import (
        VERDICT_CHAIN_RESOLVED_LEGACY_ABSENT,
    )

    session_factory = _seed(tmp_path, with_ledger_stop=False)
    with session_factory() as session:
        session.add(
            DeepcoinWsEvent(
                venue="deepcoin",
                channel="TriggerOrder",
                action="push",
                order_sys_id="entry-attached-stop",
                trade_unit_id="pos-1",
                received_at=NOW,
                received_ms=1,
                raw_payload="{}",
                payload_hash="hash-entry-attached",
            )
        )
        session.commit()
    client = _Client([_stop_row("entry-attached-stop")])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["counts_by_verdict"][VERDICT_CHAIN_RESOLVED_LEGACY_ABSENT] == 1
    assert result["counts_by_verdict"][VERDICT_SET_MISMATCH] == 0
    # Phase 6e: the same pass says how many ledger rows are missing, without
    # writing any of them.
    assert result["would_adopt"] == 1
    row = _shadow_rows(session_factory)[0]
    assert row.reason == VERDICT_CHAIN_RESOLVED_LEGACY_ABSENT
    assert "entry-attached-stop" in row.after_json


def test_nothing_is_adopted_while_the_ledger_already_knows_the_order(tmp_path):
    """``would_adopt`` counts what is missing, not what is present."""

    session_factory = _seed(tmp_path)
    client = _Client([_stop_row("stop-1")])

    result = run_protection_authority_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result["would_adopt"] == 0
    assert result["counts_by_verdict"][VERDICT_AGREED] == 1
