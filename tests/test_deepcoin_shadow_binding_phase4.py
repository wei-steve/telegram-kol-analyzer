"""Phase 4 guards: the shadow chain, its refusals, and the difference report.

The chain this file exercises is the one the handoff document specifies::

    REST main ordId -> Trade.OS -> REST-verified unique split posId
      -> TriggerOrder.TU -> TriggerOrder.OS

and the property that matters most is *conjunctive refusal*: five criteria, all
required, each of which must produce its own named refusal when it alone fails.
Five separate tests below hold that line, deliberately not one parametrised
case, so that a change collapsing two criteria into one cannot pass by making a
single assertion still true.

Frames come from the same recorded capture phase 2 and 3 used
(``tests/fixtures/deepcoin_ws_recorded_frames.jsonl``, 2026-09-05), which is why
the ids here look the way they do: the protection order's id really is the
entry's minus one and both were created in the same millisecond. That
coincidence is exactly the allocation pattern this phase forbids using, and the
tests are written against real data so that a future implementation cannot pass
them by leaning on it.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_shadow_binding import (
    BINDING_CRITERIA,
    CONFIDENCE_EXACT,
    CONFIDENCE_UNVERIFIED,
    REFUSAL_REASONS,
    SHADOW_STAGES,
    ShadowChainInputs,
    ShadowLedgerMutationError,
    evaluate_shadow_chain,
    guarded_ledger_counts,
    run_shadow_binding_pass,
    shadow_only_session,
    upsert_shadow_binding,
)
from telegram_kol_research.deepcoin_shadow_diff import (
    DIFF_LEDGER_ONLY,
    DIFF_POS_ID_MISMATCH,
    DIFF_PRICE_MISMATCH,
    DIFF_PROTECTION_ORD_ID_MISMATCH,
    DIFF_SHADOW_ONLY,
    DIFF_SIDE_MISMATCH,
    DIFF_SIZE_MISMATCH,
    DIFF_TIMING_ONLY,
    SHADOW_DIFF_KINDS,
    LedgerChainView,
    build_shadow_binding_report,
    compare_chain,
    load_ledger_chain_view,
    persist_diffs,
    run_shadow_diff_pass,
)
from telegram_kol_research.deepcoin_shadow_ownership import (
    OWNERSHIP_SOURCES,
    REQUIRED_OWNERSHIP_TABLES,
    SystemOwnedIds,
    load_system_owned_ids,
)
from telegram_kol_research.deepcoin_ws_resync import DeepcoinInstrumentIdMap
from telegram_kol_research.models import (
    DeepcoinShadowBinding,
    DeepcoinShadowDiff,
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
)

FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "deepcoin_ws_recorded_frames.jsonl"
)

MAIN_ORD_ID = "1001125145471184"
POS_ID = "1001125145471184"
PROTECTION_ORD_ID = "1001125145471183"
STREAM_INSTRUMENT = "ETHUSDT"
REST_INSTRUMENT = "ETH-USDT-SWAP"

BASE_TIME = datetime(2026, 9, 5, 19, 24, 0, tzinfo=UTC)


def _recorded_frames() -> list[dict]:
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _inbox_rows(channel: str) -> list[dict]:
    """Project the recorded capture into rows shaped like the phase 1 inbox."""

    rows: list[dict] = []
    for entry in _recorded_frames():
        payload = entry["payload"]
        raw_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        for item in payload.get("result", []):
            if item.get("table") != channel:
                continue
            data = item.get("data", {})
            rows.append(
                {
                    "channel": channel,
                    "order_sys_id": data.get("OS"),
                    "trade_unit_id": data.get("TU"),
                    "position_id": data.get("PI"),
                    "instrument_raw": data.get("I"),
                    "exchange_time_ms": data.get("UM"),
                    "received_at": datetime.fromisoformat(
                        entry["received_at"].replace("Z", "+00:00")
                    ),
                    "received_ms": entry["received_ms"],
                    "raw_payload": raw_payload,
                }
            )
    return rows


def _order_response(**overrides) -> dict:
    """The body ``POST /deepcoin/trade/order`` really returns.

    Copied field-for-field from a production submission on 2026-09-07: the
    ``posId`` sits at the top level, beside ``data`` rather than inside it, and
    it is the only place in the whole REST surface where an order id and a
    position id appear together.
    """

    row = {
        "code": "0",
        "msg": "",
        "data": {
            "ordId": MAIN_ORD_ID,
            "clOrdId": "TKFG9210E1",
            "sCode": "0",
            "sMsg": "",
            "tag": "",
        },
        "posId": POS_ID,
    }
    row.update(overrides)
    return row


def _rest_fill(**overrides) -> dict:
    """One ``list_trade_fills`` row, with the real field set.

    Verified against production: the fills endpoint carries **no** ``posId``.
    The fixture omits it deliberately -- a fixture richer than the exchange
    would let criterion 2 pass in a test and refuse in production.
    """

    row = {
        "instId": REST_INSTRUMENT,
        "ordId": MAIN_ORD_ID,
        "tradeId": "1000303910995812",
        "side": "buy",
        "posSide": "long",
        "fillSz": "0.1",
        "fillPx": "2478.78",
    }
    row.update(overrides)
    return row


def _rest_position(**overrides) -> dict:
    row = {
        "instId": REST_INSTRUMENT,
        "posId": POS_ID,
        "posSide": "long",
        "pos": "0.1",
    }
    row.update(overrides)
    return row


def _rest_trigger(**overrides) -> dict:
    """One REST protection row matching the recorded TriggerOrder frame."""

    row = {
        "triggerOrderType": "TPSL",
        "instId": REST_INSTRUMENT,
        "ordId": PROTECTION_ORD_ID,
        "posId": POS_ID,
        "posSide": "long",
        "side": "sell",
        "sz": "0.1",
        "slTriggerPx": "2488.78",
        "tpTriggerPx": "2468.78",
    }
    row.update(overrides)
    return row


def _instrument_map() -> DeepcoinInstrumentIdMap:
    instrument_map = DeepcoinInstrumentIdMap()
    instrument_map.build([{"instId": REST_INSTRUMENT}, {"instId": "BTC-USDT-SWAP"}])
    instrument_map.mark_built(now_ms=0)
    return instrument_map


def _complete_inputs(**overrides) -> ShadowChainInputs:
    inputs = ShadowChainInputs(
        main_ord_id=MAIN_ORD_ID,
        rest_order_response=_order_response(),
        instrument_stream=STREAM_INSTRUMENT,
        instrument_rest=REST_INSTRUMENT,
        instrument_resolved=True,
        trade_frames=_inbox_rows("Trade"),
        order_frames=_inbox_rows("Order"),
        trigger_frames=_inbox_rows("TriggerOrder"),
        position_frames=_inbox_rows("Position"),
        rest_fills=[_rest_fill()],
        rest_positions=[_rest_position()],
        rest_trigger_orders=[_rest_trigger()],
    )
    for key, value in overrides.items():
        setattr(inputs, key, value)
    return inputs


# ---------------------------------------------------------------- the chain


def test_the_recorded_chain_satisfies_all_five_criteria_at_once():
    result = evaluate_shadow_chain(_complete_inputs())

    assert result.binding_confidence == CONFIDENCE_EXACT
    assert result.refusal_reason is None
    assert result.pos_id == POS_ID
    assert result.protection_ord_ids == (PROTECTION_ORD_ID,)
    assert result.side == "long"
    assert result.instrument_rest == REST_INSTRUMENT
    assert all(result.criteria[name] for name in BINDING_CRITERIA)
    assert result.stage in SHADOW_STAGES


def test_criterion_one_alone_missing_refuses_with_its_own_reason():
    """No ``Trade`` frame carries the main ordId."""

    result = evaluate_shadow_chain(_complete_inputs(trade_frames=[]))

    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.refusal_reason == "no_trade_frame_for_main_ord_id"
    assert result.criteria["trade_os_equals_main_ord_id"] is False


def test_criterion_two_alone_missing_refuses_when_rest_posid_is_not_unique():
    """The order response names two different posIds for the one entry."""

    response = _order_response()
    response["data"] = {**response["data"], "posId": "1001125145471999"}
    result = evaluate_shadow_chain(_complete_inputs(rest_order_response=response))

    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.refusal_reason == "rest_pos_id_not_unique"
    assert result.criteria["trade_os_equals_main_ord_id"] is True
    assert result.criteria["rest_unique_directional_pos_id"] is False


def test_criterion_two_refuses_when_no_order_response_was_ever_recorded():
    """No response, no ordId -> posId link anywhere in the REST surface."""

    result = evaluate_shadow_chain(_complete_inputs(rest_order_response=None))

    assert result.refusal_reason == "no_rest_order_response_for_main_ord_id"
    assert result.pos_id is None


def test_criterion_two_refuses_when_the_exchange_does_not_confirm_the_position():
    """A recorded posId the exchange knows nothing about proves nothing."""

    result = evaluate_shadow_chain(
        _complete_inputs(rest_positions=[], rest_position_history=[])
    )

    assert result.refusal_reason == "rest_pos_id_not_confirmed_by_rest"


def test_the_pos_id_is_read_from_its_own_field_not_derived_from_the_ord_id():
    """``posId == ordId`` in production. The value must still come from ``posId``."""

    response = _order_response(posId="9009009009009009")
    result = evaluate_shadow_chain(
        _complete_inputs(
            rest_order_response=response,
            rest_positions=[_rest_position(posId="9009009009009009")],
        )
    )

    assert result.pos_id == "9009009009009009"
    assert result.pos_id != MAIN_ORD_ID


def test_criterion_three_alone_missing_refuses_when_tu_never_equals_posid():
    """``TU`` stays at ``default``: the protection order is not yet attributable."""

    frames = [
        {**row, "trade_unit_id": "default"} for row in _inbox_rows("TriggerOrder")
    ]
    result = evaluate_shadow_chain(_complete_inputs(trigger_frames=frames))

    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.refusal_reason == "no_trigger_frame_with_tu_equal_pos_id"
    assert result.criteria["rest_unique_directional_pos_id"] is True
    assert result.criteria["trigger_order_tu_equals_pos_id"] is False


def test_criterion_four_alone_missing_refuses_when_os_is_not_its_own_order():
    """A ``TU``-matching frame whose ``OS`` is the entry's id is not a protection order."""

    frames = [
        {**row, "trade_unit_id": POS_ID, "order_sys_id": MAIN_ORD_ID}
        for row in _inbox_rows("TriggerOrder")
    ]
    result = evaluate_shadow_chain(_complete_inputs(trigger_frames=frames))

    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.refusal_reason == "trigger_frame_without_own_ord_id"
    assert result.criteria["trigger_order_tu_equals_pos_id"] is True
    assert result.criteria["trigger_order_os_is_own_ord_id"] is False


def test_criterion_five_alone_missing_refuses_when_rest_and_stream_disagree():
    """The stream and REST report different stop-loss triggers for one order."""

    result = evaluate_shadow_chain(
        _complete_inputs(rest_trigger_orders=[_rest_trigger(slTriggerPx="2500.00")])
    )

    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.refusal_reason == "protection_tp_sl_mismatch"
    assert result.criteria["trigger_order_os_is_own_ord_id"] is True
    assert result.criteria["chain_fields_consistent"] is False


def test_every_refusal_reason_the_chain_can_emit_is_declared():
    for reason in (
        "no_trade_frame_for_main_ord_id",
        "rest_pos_id_not_unique",
        "no_trigger_frame_with_tu_equal_pos_id",
        "trigger_frame_without_own_ord_id",
        "protection_tp_sl_mismatch",
        "instrument_not_in_map",
        "rest_read_incomplete",
    ):
        assert reason in REFUSAL_REASONS


def test_an_unmapped_contract_fails_closed_rather_than_being_repaired():
    """A stream name phase 2's explicit map does not know is never guessed at."""

    result = evaluate_shadow_chain(
        _complete_inputs(
            instrument_stream="NEWCOINUSDT",
            instrument_rest=None,
            instrument_resolved=False,
        )
    )

    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.refusal_reason == "instrument_not_in_map"


def test_an_incomplete_rest_read_is_unknown_and_never_zero():
    result = evaluate_shadow_chain(
        _complete_inputs(
            rest_complete=False,
            rest_fills=[],
            rest_positions=[],
            rest_trigger_orders=[],
            rest_read_failures=("list_positions:DeepcoinClientError:http401",),
        )
    )

    assert result.refusal_reason == "rest_read_incomplete"
    assert result.pos_id is None
    assert "rest_read_failures" in result.evidence


def test_position_pi_is_recorded_as_support_and_never_satisfies_criterion_two():
    """``PI`` names the posId, and the chain still refuses without the REST link.

    The stream has already said which position this is. Criterion 2 does not
    care: without the exchange's own order response there is no REST-sourced
    link, and an undocumented push field is not allowed to become one.
    """

    response = _order_response()
    del response["posId"]
    result = evaluate_shadow_chain(_complete_inputs(rest_order_response=response))

    assert POS_ID in result.evidence["position_pi_seen_in_window"]
    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.refusal_reason == "no_rest_pos_id_for_main_ord_id"
    assert result.pos_id is None


def test_the_ord_id_minus_one_allocation_pattern_is_never_used_to_bind():
    """The recorded ids differ by one. Removing the real link must still refuse."""

    assert int(MAIN_ORD_ID) - int(PROTECTION_ORD_ID) == 1
    frames = [
        {**row, "trade_unit_id": "default"} for row in _inbox_rows("TriggerOrder")
    ]
    result = evaluate_shadow_chain(_complete_inputs(trigger_frames=frames))

    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.protection_ord_ids == ()


def _trigger_frame(ord_id: str, *, tp=None, sl=None) -> dict:
    """One TriggerOrder inbox row shaped exactly like the recorded capture."""

    template = _inbox_rows("TriggerOrder")[-1]
    data = json.loads(template["raw_payload"])["result"][0]["data"]
    data = {**data, "OS": ord_id, "TU": POS_ID}
    data.pop("TPT", None)
    data.pop("SLT", None)
    if tp is not None:
        data["TPT"] = tp
    if sl is not None:
        data["SLT"] = sl
    payload = {
        "action": "PushTriggerOrder",
        "result": [{"table": "TriggerOrder", "data": data}],
    }
    return {
        **template,
        "order_sys_id": ord_id,
        "trade_unit_id": POS_ID,
        "raw_payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def test_one_position_with_several_protection_orders_is_compared_as_a_set():
    """Supplementary check 8: ``set-position-sltp`` produces more than one order.

    One stop covering the whole position and two take-profits splitting it is
    the shape production actually creates. Comparing protection as a single
    value would report a difference here where there is none, so the chain
    carries the whole set and checks coverage per role.
    """

    stop_ord_id = PROTECTION_ORD_ID
    tp_one, tp_two = "1001125145471182", "1001125145471181"
    result = evaluate_shadow_chain(
        _complete_inputs(
            trigger_frames=[
                _trigger_frame(stop_ord_id, sl=2488.78),
                _trigger_frame(tp_one, tp=2465.0),
                _trigger_frame(tp_two, tp=2470.0),
            ],
            rest_trigger_orders=[
                _rest_trigger(sz="0.1", tpTriggerPx=None, slTriggerPx="2488.78"),
                _rest_trigger(
                    ordId=tp_one, sz="0.05", tpTriggerPx="2465.0", slTriggerPx=None
                ),
                _rest_trigger(
                    ordId=tp_two, sz="0.05", tpTriggerPx="2470.0", slTriggerPx=None
                ),
            ],
        )
    )

    assert result.binding_confidence == CONFIDENCE_EXACT
    assert result.protection_ord_ids == tuple(
        sorted([stop_ord_id, tp_one, tp_two])
    )
    assert result.evidence["protection_role_sizes"] == {
        "stop_loss": "0.1",
        "take_profit": "0.10",
    }


def test_protection_that_over_covers_the_position_is_a_size_mismatch():
    """Two full-size take-profits on a single position is not a chain we accept."""

    result = evaluate_shadow_chain(
        _complete_inputs(
            trigger_frames=[
                _trigger_frame(PROTECTION_ORD_ID, tp=2468.78),
                _trigger_frame("1001125145471182", tp=2465.0),
            ],
            rest_trigger_orders=[
                _rest_trigger(sz="0.1", slTriggerPx=None),
                _rest_trigger(
                    ordId="1001125145471182",
                    sz="0.1",
                    tpTriggerPx="2465.0",
                    slTriggerPx=None,
                ),
            ],
        )
    )

    assert result.binding_confidence == CONFIDENCE_UNVERIFIED
    assert result.refusal_reason == "protection_size_mismatch"


# ------------------------------------------------------- persistence and diff


def _seed_ledger(
    session_factory,
    *,
    pos_id: str = POS_ID,
    side: str = "long",
    protection: tuple[tuple[str, str, str], ...] = (
        (PROTECTION_ORD_ID, "2488.78", "0.1"),
    ),
    leg_verified_at: datetime | None = None,
    protection_seen_at: datetime | None = None,
) -> tuple[int, int]:
    """Create one entry binding, its entry leg and its protection ledger rows."""

    created = BASE_TIME
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="kol",
            chat_id=-1002282384698,
            message_id=15186,
            symbol="ETH",
            side=side,
            venue="deepcoin",
            order_id=MAIN_ORD_ID,
            pos_id=pos_id,
            created_at=created,
            updated_at=created,
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            order_id=MAIN_ORD_ID,
            pos_id=pos_id,
            venue="deepcoin",
            attribution_status="verified",
            last_verified_at=leg_verified_at or (BASE_TIME + timedelta(seconds=30)),
            # The verbatim exchange response, which is where the shadow chain
            # reads the posId from -- not from the ``pos_id`` column above.
            response_json=json.dumps(_order_response(), ensure_ascii=False),
            created_at=created,
            updated_at=created,
        )
        session.add(leg)
        session.flush()
        for order_id, trigger_price, size_text in protection:
            session.add(
                PositionProtectionLedger(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    pos_id=pos_id,
                    instrument_id=REST_INSTRUMENT,
                    side=side,
                    order_id=order_id,
                    purpose="stop_loss",
                    trigger_price=trigger_price,
                    size_text=size_text,
                    status="verified",
                    evidence_source="test",
                    evidence_json="{}",
                    first_seen_at=protection_seen_at
                    or (BASE_TIME + timedelta(seconds=45)),
                    last_seen_at=BASE_TIME + timedelta(seconds=45),
                    created_at=created,
                    updated_at=created,
                )
            )
        session.commit()
        return int(binding.id), int(leg.id)


def _store_chain(session_factory, result, *, observed_binding_id=None) -> int:
    return upsert_shadow_binding(
        session_factory,
        result,
        now=BASE_TIME,
        observed_execution_binding_id=observed_binding_id,
    )


def _shadow_row(session_factory, shadow_id: int) -> DeepcoinShadowBinding:
    with session_factory() as session:
        row = session.get(DeepcoinShadowBinding, shadow_id)
        session.expunge(row)
        return row


def test_a_shadow_write_never_reaches_a_ledger_table(tmp_path):
    """The runtime half of "the shadow only writes itself"."""

    session_factory = create_session_factory(tmp_path / "shadow.db")
    binding_id, _leg_id = _seed_ledger(session_factory)
    before = guarded_ledger_counts(session_factory)

    _store_chain(
        session_factory,
        evaluate_shadow_chain(_complete_inputs()),
        observed_binding_id=binding_id,
    )

    assert guarded_ledger_counts(session_factory) == before
    with session_factory() as session:
        assert session.query(DeepcoinShadowBinding).count() == 1


def test_the_shadow_session_refuses_a_ledger_write_before_it_flushes(tmp_path):
    """A leak is stopped at the session, not detected afterwards by counting."""

    session_factory = create_session_factory(tmp_path / "shadow.db")
    with pytest.raises(ShadowLedgerMutationError):
        with shadow_only_session(session_factory) as session:
            session.add(
                ExecutionBinding(
                    kol_id="kol",
                    chat_id=1,
                    message_id=1,
                    symbol="ETH",
                    side="long",
                )
            )
            session.commit()
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 0


def test_the_static_shadow_modules_never_import_a_ledger_writer():
    """The static half: no shadow module may reach a module that writes ledgers."""

    source_root = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"
    )
    forbidden = {
        "execution_bindings",
        "protection_ledger",
        "position_take_profit_orders",
        "trigger_protection_intents",
        "position_mutation_gateway",
        "native_tpsl_migration",
        "recovery_live_submit",
        "deepcoin_execution_actions",
    }
    for name in (
        "deepcoin_shadow_binding",
        "deepcoin_shadow_diff",
        "deepcoin_shadow_ownership",
    ):
        tree = ast.parse((source_root / f"{name}.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.rsplit(".", 1)[-1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.rsplit(".", 1)[-1])
        assert not (imported & forbidden), (name, sorted(imported & forbidden))


# ------------------------------------------------------------ the eight kinds


def _compare(shadow_row, ledger_view, *, owned=None):
    return compare_chain(
        shadow_row, ledger_view, owned=owned or SystemOwnedIds()
    )


def _kinds(records) -> set[str]:
    return {record.diff_kind for record in records}


def test_diff_kind_shadow_only_when_the_ledger_has_no_entry_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    shadow_id = _store_chain(session_factory, evaluate_shadow_chain(_complete_inputs()))
    records = _compare(
        _shadow_row(session_factory, shadow_id),
        LedgerChainView(main_ord_id=MAIN_ORD_ID),
    )

    assert _kinds(records) == {DIFF_SHADOW_ONLY}
    assert records[0].subject == "binding"
    assert records[0].shadow_value == MAIN_ORD_ID


def test_diff_kind_ledger_only_when_the_chain_could_not_be_verified(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    binding_id, _leg = _seed_ledger(session_factory)
    shadow_id = _store_chain(
        session_factory,
        evaluate_shadow_chain(_complete_inputs(trade_frames=[])),
        observed_binding_id=binding_id,
    )
    records = _compare(
        _shadow_row(session_factory, shadow_id),
        load_ledger_chain_view(session_factory, MAIN_ORD_ID),
    )

    assert _kinds(records) == {DIFF_LEDGER_ONLY}
    assert records[0].shadow_value == "no_trade_frame_for_main_ord_id"


def test_diff_kind_pos_id_mismatch(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(session_factory, pos_id="1001125145479999")
    shadow_id = _store_chain(session_factory, evaluate_shadow_chain(_complete_inputs()))
    records = _compare(
        _shadow_row(session_factory, shadow_id),
        load_ledger_chain_view(session_factory, MAIN_ORD_ID),
    )

    assert DIFF_POS_ID_MISMATCH in _kinds(records)


def test_diff_kind_side_mismatch(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(session_factory, side="short")
    shadow_id = _store_chain(session_factory, evaluate_shadow_chain(_complete_inputs()))
    records = _compare(
        _shadow_row(session_factory, shadow_id),
        load_ledger_chain_view(session_factory, MAIN_ORD_ID),
    )

    assert DIFF_SIDE_MISMATCH in _kinds(records)


def test_diff_kind_protection_ord_id_mismatch(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(
        session_factory, protection=(("1001125145470000", "2488.78", "0.1"),)
    )
    shadow_id = _store_chain(session_factory, evaluate_shadow_chain(_complete_inputs()))
    records = _compare(
        _shadow_row(session_factory, shadow_id),
        load_ledger_chain_view(session_factory, MAIN_ORD_ID),
    )

    assert DIFF_PROTECTION_ORD_ID_MISMATCH in _kinds(records)
    record = next(
        item for item in records if item.diff_kind == DIFF_PROTECTION_ORD_ID_MISMATCH
    )
    assert record.evidence["only_in_shadow"] == [PROTECTION_ORD_ID]
    assert record.evidence["only_in_ledger"] == ["1001125145470000"]


def test_diff_kind_price_mismatch(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(
        session_factory, protection=((PROTECTION_ORD_ID, "2499.00", "0.1"),)
    )
    shadow_id = _store_chain(
        session_factory,
        evaluate_shadow_chain(
            _complete_inputs(
                trigger_frames=[_trigger_frame(PROTECTION_ORD_ID, sl=2488.78)],
                rest_trigger_orders=[_rest_trigger(tpTriggerPx=None)],
            )
        ),
    )
    records = _compare(
        _shadow_row(session_factory, shadow_id),
        load_ledger_chain_view(session_factory, MAIN_ORD_ID),
    )

    assert DIFF_PRICE_MISMATCH in _kinds(records)


def test_diff_kind_size_mismatch(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(
        session_factory, protection=((PROTECTION_ORD_ID, "2488.78", "0.9"),)
    )
    shadow_id = _store_chain(
        session_factory,
        evaluate_shadow_chain(
            _complete_inputs(
                trigger_frames=[_trigger_frame(PROTECTION_ORD_ID, sl=2488.78)],
                rest_trigger_orders=[_rest_trigger(tpTriggerPx=None)],
            )
        ),
    )
    records = _compare(
        _shadow_row(session_factory, shadow_id),
        load_ledger_chain_view(session_factory, MAIN_ORD_ID),
    )

    assert DIFF_SIZE_MISMATCH in _kinds(records)


def test_diff_kind_timing_only_is_a_benefit_measure_not_a_defect(tmp_path):
    """Same conclusion, different discovery time: the number phase 5 rests on."""

    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(
        session_factory,
        leg_verified_at=BASE_TIME + timedelta(seconds=41),
        protection_seen_at=BASE_TIME + timedelta(seconds=41),
    )
    shadow_id = _store_chain(session_factory, evaluate_shadow_chain(_complete_inputs()))
    records = _compare(
        _shadow_row(session_factory, shadow_id),
        load_ledger_chain_view(session_factory, MAIN_ORD_ID),
    )

    assert _kinds(records) == {DIFF_TIMING_ONLY}
    leads = sorted(record.lead_seconds for record in records)
    assert all(lead > 0 for lead in leads)


def test_every_declared_diff_kind_has_a_case_above():
    """Eight kinds, eight cases. The list and the tests cannot drift apart."""

    covered = {
        DIFF_SHADOW_ONLY,
        DIFF_LEDGER_ONLY,
        DIFF_POS_ID_MISMATCH,
        DIFF_PROTECTION_ORD_ID_MISMATCH,
        DIFF_SIDE_MISMATCH,
        DIFF_SIZE_MISMATCH,
        DIFF_PRICE_MISMATCH,
        DIFF_TIMING_ONLY,
    }
    assert covered == set(SHADOW_DIFF_KINDS)


# ------------------------------------------------------------------ ownership


def test_ownership_covers_every_ledger_the_plan_names():
    """The phase 3 lesson, made mechanical.

    Answering "is this ours?" from the binding tables alone reported four of
    this system's own TPSL stops as somebody else's. Shrinking this list can
    only re-create that, so the required set is asserted rather than reviewed.
    """

    assert REQUIRED_OWNERSHIP_TABLES <= {table for table, _o, _p in OWNERSHIP_SOURCES}


def test_a_protection_order_id_written_only_to_a_protection_ledger_is_owned(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(session_factory)

    owned = load_system_owned_ids(session_factory)

    assert owned.owns_order(PROTECTION_ORD_ID)
    assert owned.owns_order(MAIN_ORD_ID)
    assert owned.owns_position(POS_ID)
    assert not owned.owns_order("9999999999999999")
    assert REQUIRED_OWNERSHIP_TABLES <= set(owned.tables_read)


# ----------------------------------------------------- persistence and report


def test_the_report_returns_counts_only_and_no_identifier(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    binding_id, _leg = _seed_ledger(
        session_factory,
        leg_verified_at=BASE_TIME + timedelta(seconds=41),
        protection_seen_at=BASE_TIME + timedelta(seconds=41),
    )
    _store_chain(
        session_factory,
        evaluate_shadow_chain(_complete_inputs()),
        observed_binding_id=binding_id,
    )
    run_shadow_diff_pass(session_factory, now=BASE_TIME)

    report = build_shadow_binding_report(session_factory, now=BASE_TIME)

    assert report["chain_count"] == 1
    assert report["exact_count"] == 1
    assert report["unverified_count"] == 0
    assert set(report["counts_by_diff_kind"]) == set(SHADOW_DIFF_KINDS)
    assert report["timing_only_count"] >= 1
    assert report["timing_only_median_lead_seconds"] > 0
    serialized = json.dumps(report, default=str)
    for secret in (MAIN_ORD_ID, POS_ID, PROTECTION_ORD_ID, REST_INSTRUMENT):
        assert secret not in serialized


def test_re_running_the_diff_pass_is_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(session_factory, pos_id="1001125145479999")
    _store_chain(session_factory, evaluate_shadow_chain(_complete_inputs()))

    first = run_shadow_diff_pass(session_factory, now=BASE_TIME)
    second = run_shadow_diff_pass(session_factory, now=BASE_TIME + timedelta(minutes=1))

    assert first["diffs_written"] >= 1
    assert second["diffs_written"] == 0
    with session_factory() as session:
        assert session.query(DeepcoinShadowDiff).count() == first["diffs_written"]


class _StubClient:
    """A read-only Deepcoin stand-in. It has no write method at all, on purpose."""

    def __init__(self, *, fills=None, positions=None, triggers=None, fail=()):
        self._fills = fills if fills is not None else [_rest_fill()]
        self._positions = positions if positions is not None else [_rest_position()]
        self._triggers = triggers if triggers is not None else [_rest_trigger()]
        self._fail = set(fail)
        self.calls: list[str] = []

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name in self._fail:
            raise RuntimeError(f"{name} unavailable")

    def list_trade_fills_by_order_id(self, *, inst_id, order_id):
        self._maybe_fail("list_trade_fills_by_order_id")
        return [row for row in self._fills if row.get("ordId") == order_id]

    def list_positions(self, *, inst_id=None):
        self._maybe_fail("list_positions")
        return list(self._positions)

    def list_position_history(self, *, inst_id, pos_id=None):
        self._maybe_fail("list_position_history")
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        self._maybe_fail("list_trigger_orders_pending")
        return list(self._triggers)


def _seed_inbox(session_factory) -> None:
    from telegram_kol_research.deepcoin_private_ws import persist_ws_frame_rows

    for entry in _recorded_frames():
        persist_ws_frame_rows(
            session_factory,
            json.dumps(entry["payload"], ensure_ascii=False, separators=(",", ":")),
            received_at=datetime.fromisoformat(
                entry["received_at"].replace("Z", "+00:00")
            ),
            received_ms=entry["received_ms"],
        )


def test_a_full_pass_builds_the_chain_and_leaves_every_ledger_untouched(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    binding_id, _leg = _seed_ledger(session_factory)
    _seed_inbox(session_factory)
    before = guarded_ledger_counts(session_factory)
    now = datetime.fromtimestamp(1788636240089 / 1000, tz=UTC) + timedelta(minutes=1)

    result = run_shadow_binding_pass(
        session_factory,
        client=_StubClient(),
        instrument_map=_instrument_map(),
        now=now,
    )

    assert result.evaluated == 1
    assert result.exact == 1
    assert guarded_ledger_counts(session_factory) == before
    with session_factory() as session:
        row = session.query(DeepcoinShadowBinding).one()
        assert row.binding_confidence == CONFIDENCE_EXACT
        assert row.pos_id == POS_ID
        assert row.protection_ord_id == PROTECTION_ORD_ID
        assert row.observed_execution_binding_id == binding_id


def test_a_failed_rest_read_during_a_pass_produces_unverified_not_empty(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_inbox(session_factory)
    now = datetime.fromtimestamp(1788636240089 / 1000, tz=UTC) + timedelta(minutes=1)

    result = run_shadow_binding_pass(
        session_factory,
        client=_StubClient(fail={"list_trigger_orders_pending"}),
        instrument_map=_instrument_map(),
        now=now,
    )

    assert result.exact == 0
    assert result.unverified == 1
    assert result.rest_read_failures
    with session_factory() as session:
        row = session.query(DeepcoinShadowBinding).one()
        assert row.binding_confidence == CONFIDENCE_UNVERIFIED
        assert row.refusal_reason == "rest_read_incomplete"
        assert row.pos_id is None


def test_a_pass_with_no_instrument_map_entry_refuses_rather_than_guessing(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_inbox(session_factory)
    empty_map = DeepcoinInstrumentIdMap()
    empty_map.build([{"instId": "BTC-USDT-SWAP"}])
    empty_map.mark_built(now_ms=0)
    now = datetime.fromtimestamp(1788636240089 / 1000, tz=UTC) + timedelta(minutes=1)

    client = _StubClient()
    result = run_shadow_binding_pass(
        session_factory, client=client, instrument_map=empty_map, now=now
    )

    assert result.unverified == 1
    assert client.calls == []
    with session_factory() as session:
        assert (
            session.query(DeepcoinShadowBinding).one().refusal_reason
            == "instrument_not_in_map"
        )


# ------------------------------------------ the per-round reconcile round log


def test_the_round_log_names_its_trigger_times_and_touched_bindings(tmp_path):
    """Phase 3 could only estimate the wake lead. This line makes it measurable."""

    from telegram_kol_research.web_app import _build_deepcoin_reconcile_round_log

    session_factory = create_session_factory(tmp_path / "round.db")
    binding_id, _leg = _seed_ledger(session_factory)
    started = BASE_TIME - timedelta(seconds=1)

    payload = _build_deepcoin_reconcile_round_log(
        session_factory,
        trigger="by_wake",
        round_started_at=started,
        round_finished_at=started + timedelta(seconds=13),
        wake_requested_at=started - timedelta(seconds=4),
        shadow_summary={"evaluated": 1},
    )

    assert payload["trigger"] == "by_wake"
    assert payload["wake_to_round_start_seconds"] == 4.0
    assert payload["duration_seconds"] == 13.0
    assert payload["touched_binding_ids"] == [binding_id]
    assert payload["shadow"] == {"evaluated": 1}
    # Ids only. No symbol, side, size or price may appear in an operational log.
    serialized = json.dumps(payload)
    for forbidden in ("ETH", "long", "2478.78", MAIN_ORD_ID):
        assert forbidden not in serialized


def test_the_round_log_reports_a_timer_round_with_no_wake_time(tmp_path):
    from telegram_kol_research.web_app import _build_deepcoin_reconcile_round_log

    session_factory = create_session_factory(tmp_path / "round.db")

    payload = _build_deepcoin_reconcile_round_log(
        session_factory,
        trigger="by_timer",
        round_started_at=BASE_TIME,
        round_finished_at=BASE_TIME + timedelta(seconds=2),
        wake_requested_at=None,
        shadow_summary=None,
    )

    assert payload["trigger"] == "by_timer"
    assert payload["wake_frame_received_at"] is None
    assert payload["wake_to_round_start_seconds"] is None
    assert payload["touched_binding_ids"] == []
    assert "shadow" not in payload


def test_a_failing_shadow_observation_never_disturbs_the_reconcile_loop():
    """Shadow work is additive. Its failure must not skip or repeat a round."""

    import asyncio

    from telegram_kol_research.web_app import _run_deepcoin_shadow_observation_step

    class _Boom:
        def rest_id_for_stream_name(self, name):
            raise RuntimeError("map exploded")

    async def _run():
        return await _run_deepcoin_shadow_observation_step(
            session_factory=None,
            deepcoin_client_factory=lambda: None,
            instrument_map=_Boom(),
            now_provider=lambda: BASE_TIME,
        )

    assert asyncio.run(_run()) is None


def test_the_shadow_step_is_skipped_entirely_without_an_instrument_map():
    import asyncio

    from telegram_kol_research.web_app import _run_deepcoin_shadow_observation_step

    calls: list[str] = []

    async def _run():
        return await _run_deepcoin_shadow_observation_step(
            session_factory=None,
            deepcoin_client_factory=lambda: calls.append("client"),
            instrument_map=None,
            now_provider=lambda: BASE_TIME,
        )

    assert asyncio.run(_run()) is None
    assert calls == []


def test_the_report_endpoint_is_localhost_only_and_returns_counts_only(tmp_path):
    from fastapi.testclient import TestClient

    from telegram_kol_research.web_app import create_web_app

    app = create_web_app(database_path=tmp_path / "api.db", runtime_role="web")
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        allowed = client.get("/api/runtime/deepcoin-shadow-binding-report")
        assert allowed.status_code == 200
        body = allowed.json()
        assert set(body["counts_by_diff_kind"]) == set(SHADOW_DIFF_KINDS)
        assert body["chain_count"] == 0
        assert "main_ord_id" not in json.dumps(body)

        refused = client.get(
            "/api/runtime/deepcoin-shadow-binding-report",
            headers={"x-forwarded-for": "203.0.113.9"},
        )
        assert refused.status_code == 404


def test_a_verified_chain_costs_no_further_rest_reads_without_a_new_frame(tmp_path):
    """Re-reading a settled chain every thirty seconds would be pure load."""

    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(session_factory)
    _seed_inbox(session_factory)
    now = datetime.fromtimestamp(1788636240089 / 1000, tz=UTC) + timedelta(minutes=1)
    client = _StubClient()

    first = run_shadow_binding_pass(
        session_factory, client=client, instrument_map=_instrument_map(), now=now
    )
    calls_after_first = len(client.calls)
    second = run_shadow_binding_pass(
        session_factory,
        client=client,
        instrument_map=_instrument_map(),
        now=now + timedelta(seconds=43),
    )

    assert first.exact == 1
    assert second.candidates_seen == 1
    assert second.candidates_due == 0
    assert second.evaluated == 0
    assert len(client.calls) == calls_after_first


def test_an_unverified_chain_is_retried_on_a_slow_timer(tmp_path):
    """A refusal can resolve later, so it is retried -- but not every round."""

    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_inbox(session_factory)
    now = datetime.fromtimestamp(1788636240089 / 1000, tz=UTC) + timedelta(minutes=1)
    client = _StubClient(fail={"list_trigger_orders_pending"})

    run_shadow_binding_pass(
        session_factory, client=client, instrument_map=_instrument_map(), now=now
    )
    soon = run_shadow_binding_pass(
        session_factory,
        client=client,
        instrument_map=_instrument_map(),
        now=now + timedelta(seconds=43),
    )
    later = run_shadow_binding_pass(
        session_factory,
        client=client,
        instrument_map=_instrument_map(),
        now=now + timedelta(minutes=10),
    )

    assert soon.candidates_due == 0
    assert later.candidates_due == 1


def test_supplementary_check_nine_records_both_sides_over_time():
    """Observation, not a verdict: what each protection side did, and when.

    The recorded capture contains the real ``TS`` "0" -> "1" arming transition
    and the position going from 0 to 0.1, which is the shape check 9 asks for.
    """

    result = evaluate_shadow_chain(_complete_inputs())

    protection_timeline = result.evidence["protection_status_timeline"]
    assert [value for _at, value in protection_timeline[PROTECTION_ORD_ID]] == [
        "0",
        "1",
    ]
    position_timeline = result.evidence["position_qty_timeline"]
    assert [value for _at, value in position_timeline[POS_ID]] == ["0", "0.1"]
    assert result.evidence["rest_position_present"] is True


def test_a_ledger_entry_the_stream_never_saw_costs_no_rest_read(tmp_path):
    """An entry from before the stream existed refuses without touching REST.

    It still gets a row -- ``ledger_only`` needs it to exist to be reported --
    but a contract name that was never observed cannot be normalised, so there
    is nothing to ask the exchange about and nothing is asked.
    """

    session_factory = create_session_factory(tmp_path / "shadow.db")
    _seed_ledger(session_factory)
    client = _StubClient()

    result = run_shadow_binding_pass(
        session_factory,
        client=client,
        instrument_map=_instrument_map(),
        now=BASE_TIME + timedelta(hours=1),
    )

    assert result.evaluated == 1
    assert result.unverified == 1
    assert client.calls == []
    with session_factory() as session:
        assert (
            session.query(DeepcoinShadowBinding).one().refusal_reason
            == "instrument_unknown"
        )


def test_no_rest_read_endpoint_can_supply_the_pos_id():
    """The finding that shaped criterion 2, kept as an executable statement.

    Verified read-only against production on 2026-09-07: fills, order history,
    positions, position history and both trigger-order endpoints. Not one of
    them returns an order id and a position id together. The link exists only in
    the body of ``POST /deepcoin/trade/order``.

    These fixtures carry the exact field sets those endpoints returned, so if a
    future Deepcoin release starts publishing ``posId`` on a read, this test is
    where that shows up.
    """

    assert "posId" not in _rest_fill()
    # The real pending-TPSL row, verbatim from production.
    real_pending_tpsl = {
        "instType": "SWAP",
        "instId": REST_INSTRUMENT,
        "ordId": "1001125157891310",
        "triggerPx": "0",
        "sz": "0",
        "side": "sell",
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "slTriggerPrice": "2430",
        "closeSLTriggerPrice": "2430",
        "tpTriggerPrice": "0",
        "closeTPTriggerPrice": "0",
    }
    assert "posId" not in real_pending_tpsl

    # And with every read available, no response means no posId.
    result = evaluate_shadow_chain(_complete_inputs(rest_order_response=None))
    assert result.pos_id is None
    assert result.refusal_reason == "no_rest_order_response_for_main_ord_id"
