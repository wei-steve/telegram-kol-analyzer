"""Execution-layer actionability gate (first-pass phase 3 plan section 5.2).

Pure rule tests first, then the production replays R5 / R6 / R15 through the
three apply paths and the planner backstop. Sample texts are production raw
messages (contact footers stripped); the raw ids are in the test ids.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.management_actionability import (
    ActionabilityRefusal,
    assess_management_actionability,
)
from telegram_kol_research.management_directives import (
    directive_is_not_actionable,
    resolve_management_directive,
)
from telegram_kol_research.message_recognition import apply_authoritative_mimo_payload
from telegram_kol_research.models import (
    ExecutionBinding,
    MessageRecognition,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.recognition_failure_attribution import (
    ALERTED_REASONS,
    reason_code_from_recognition_reason,
)


def rule_of(text, event, intent, **kwargs):
    refusal = assess_management_actionability(text, event, intent, **kwargs)
    return None if refusal is None else refusal.rule


EXIT = {"event_type": "exit_position"}

# ---------------------------------------------------------------------------
# pure rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "今晚比特涨到86左右我们多单应该就要准备平仓了",  # 19011
        "预计今天等比特冲86左右时会分批止盈掉",  # 19115 clause
        "打算明天离场",
        "先考虑清仓",
        "可能会平掉一半",
        "看情况止盈",
        "其它币种多单继续持有做好加一次仓的预期",  # 19448
    ],
)
def test_rule_1_intent_markers_with_an_action_verb_are_refused(text):
    assert rule_of(text, EXIT, "full_exit") == "intent_marker"


@pytest.mark.parametrize(
    "text",
    [
        "如果突破84800就全部平仓",
        "若跌破83000清仓",
        "假如拉不动就离场",
        "万一失败就平掉",
        "一旦走弱立刻出局",
        "走弱的话就平仓",
    ],
)
def test_rule_2_hypotheticals_with_an_action_verb_are_refused(text):
    assert rule_of(text, EXIT, "full_exit") == "hypothetical_condition"


def test_markers_only_count_in_the_same_clause_as_the_action_verb():
    # 预计 is in a clause about something else; the order is in its own clause.
    assert rule_of("预计今晚有波动，现在全部平仓", EXIT, "full_exit") is None
    assert rule_of("如果你还在场内，全部出局", EXIT, "full_exit") is None
    # ASCII comma and newline split clauses too.
    assert rule_of("预计今晚有波动,全部平仓", EXIT, "full_exit") is None
    assert rule_of("应该会涨\n全部平仓", EXIT, "full_exit") is None


def test_a_marker_without_any_action_verb_is_not_this_gates_business():
    assert rule_of("应该会涨", {"event_type": "position_update"}, "adjust_stop_loss") in (
        "price_required",
        None,
    )
    assert rule_of("预计今天涨", EXIT, "full_exit") == "exit_verb_required"


@pytest.mark.parametrize("intent", ["full_exit", "partial_take_profit", "cancel_entry"])
def test_rule_3_price_trigger_with_an_immediate_intent_is_refused(intent):
    assert rule_of("涨到86000时全部平仓", {}, intent) == "price_trigger_immediate"
    assert rule_of("突破86000再平仓", {}, intent) == "price_trigger_immediate"
    assert rule_of("跌破84000就走", {}, intent) == "price_trigger_immediate"


def test_rule_3_price_trigger_with_an_order_placing_intent_stands():
    stop = {"event_type": "position_update", "stop_loss": "84000"}
    assert rule_of("跌破84000就止损", stop, "adjust_stop_loss") is None
    assert rule_of("涨到86000时止损上移到85000", {"stop_loss": "85000"}, "adjust_stop_loss") is None
    assert rule_of("站稳85000再把止损移到成本", {}, "move_stop_to_break_even") is None
    assert (
        rule_of("突破86000时止盈50%", {"take_profit": "86000"}, "adjust_take_profit")
        is None
    )


def test_a_trigger_word_without_a_number_or_timing_is_not_rule_3():
    assert rule_of("现价全部平仓，到位了", {}, "full_exit") is None
    assert rule_of("全部平仓 突破84000", {}, "full_exit") is None  # no 时/再/就


@pytest.mark.parametrize(
    ("intent", "event"),
    [
        ("adjust_stop_loss", {"event_type": "position_update"}),
        ("adjust_take_profit", {"event_type": "position_update"}),
    ],
)
def test_rule_4_a_price_requiring_intent_without_a_price_is_refused(intent, event):
    assert rule_of("设个止损吧", event, intent) == "price_required"
    assert rule_of("设个止损吧", event, intent, check_price=False) is None


def test_rule_4_accepts_a_price_from_the_event_or_the_text():
    assert rule_of("设个止损吧", {"stop_loss": "0.51"}, "adjust_stop_loss") is None
    assert rule_of("JTO设个止损0.51", {}, "adjust_stop_loss") is None
    # a stop to cost names its price in words
    assert rule_of("修改止损到成本保护", {}, "adjust_stop_loss") is None
    # 9+ digit contact numbers are not prices
    assert (
        rule_of("设止损\n@Tarderfengge QQ:158241758", {}, "adjust_stop_loss")
        == "price_required"
    )


@pytest.mark.parametrize(
    "text",
    [
        "中长线币种QNT目标位1200美元；TAO目标位3000美元；继续持有即可；",  # 19445
        "多单继续持有，看好走势",  # 走势 is not 走
        "平均成本不变",  # 平均 is not 平
        "#Pump looks good\nAdd more",
    ],
)
def test_rule_5_a_full_exit_from_the_event_type_alone_needs_an_exit_verb(text):
    assert rule_of(text, EXIT, "full_exit") == "exit_verb_required"


@pytest.mark.parametrize(
    "text",
    [
        "保本出局",
        "全部平仓",
        "先临时离场，等下一步通知",
        "清仓走人",
        "止盈掉剩余",
        "落袋为安",
        "全部止盈",
        "平掉多单",
    ],
)
def test_rule_5_explicit_exit_verbs_pass(text):
    assert rule_of(text, EXIT, "full_exit") is None


def test_rule_5_only_applies_when_the_exit_comes_from_the_event_type_alone():
    # An explicit exit action, or another event type, is not rule 5's case.
    assert (
        rule_of("继续持有", {"event_type": "exit_position", "management_action": "exit_full"}, "full_exit")
        is None
    )
    assert rule_of("继续持有", {"event_type": "position_update"}, "full_exit") is None


def test_rule_5_needs_the_verb_in_a_clause_that_passed_rules_1_and_2():
    # 平 only appears inside a rule-1 clause -> refused by rule 1 first.
    assert rule_of("应该准备平了", EXIT, "full_exit") == "intent_marker"


@pytest.mark.parametrize(
    "intent", ["none", "hold_update", "add_position", "risk_increasing", "", None]
)
def test_only_exchange_writing_intents_are_judged(intent):
    assert (
        assess_management_actionability("如果突破可继续持有，准备加仓", EXIT, intent)
        is None
    )


def test_refusal_shape():
    refusal = assess_management_actionability("涨到86000时全部平仓", {}, "full_exit")
    assert isinstance(refusal, ActionabilityRefusal)
    assert refusal.rule == "price_trigger_immediate"
    assert refusal.reason_code == "management_not_actionable:price_trigger_immediate"
    assert "86000" in refusal.detail


def test_the_gate_is_a_pure_function_of_its_arguments():
    event = {"event_type": "exit_position", "stop_loss": None}
    before = dict(event)
    assert assess_management_actionability(None, EXIT, "full_exit") is not None
    assess_management_actionability("准备平仓", event, "full_exit")
    assert event == before


# ---------------------------------------------------------------------------
# resolve_management_directive
# ---------------------------------------------------------------------------


def test_directive_refusal_is_intent_none_with_the_recorded_reason():
    directive = resolve_management_directive(
        text="今晚比特涨到86左右我们多单应该就要准备平仓了",
        lifecycle_event={"event_type": "exit_position", "symbol": "BTC", "side": "long"},
    )
    assert directive.intent == "none"
    assert directive.reason_code == "management_not_actionable:intent_marker"
    assert directive.risk_reducing is False and directive.fanout_allowed is False
    assert directive_is_not_actionable(directive)


def test_directive_for_a_plain_exit_is_unchanged():
    directive = resolve_management_directive(
        text="保本出局", lifecycle_event={"event_type": "exit_position"}
    )
    assert (directive.intent, directive.reason_code) == ("full_exit", "explicit_full_exit")
    assert not directive_is_not_actionable(directive)


def test_recovery_path_can_bypass_the_gate():
    """``position_management_remediation`` re-drives already admitted candidates
    (rebuilt without the model's event_type) and must not re-litigate them."""

    directive = resolve_management_directive(
        text="取消这个计划",
        lifecycle_event={"event_type": "exit_position"},
        actionability_gate=False,
    )
    assert directive.intent == "full_exit"
    gated = resolve_management_directive(
        text="取消这个计划", lifecycle_event={"event_type": "exit_position"}
    )
    assert gated.reason_code == "management_not_actionable:exit_verb_required"


def test_directive_stop_with_price_trigger_stands():
    directive = resolve_management_directive(
        text="跌破84000就止损",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "adjust_stop_loss",
            "stop_loss": "84000",
        },
    )
    assert directive.intent == "adjust_stop_loss"
    assert directive.stop_loss == "84000"


def test_directive_full_exit_with_price_trigger_is_refused():
    directive = resolve_management_directive(
        text="涨到86000时全部平仓",
        lifecycle_event={"event_type": "exit_position"},
    )
    assert directive.reason_code == "management_not_actionable:price_trigger_immediate"


# ---------------------------------------------------------------------------
# R15: what must keep working
# ---------------------------------------------------------------------------

_TP_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "take_profit_adjustment_replay.json").read_text(
        encoding="utf-8"
    )
)
_TP_MESSAGES = {int(r["raw_message_id"]): r for r in _TP_FIXTURE["messages"]}


def test_r15_mia_m1_is_still_partial_then_break_even():
    directive = resolve_management_directive(
        text="BTC现价83400附近，止盈50%，剩余仓位止损位下移至84500，做无风险持仓！",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "partial_take_profit",
            "symbol": "BTC",
            "side": "short",
        },
    )
    assert directive.intent == "partial_then_break_even"
    assert directive.fraction == 0.5
    assert str(directive.stop_loss) == "84500.0" or directive.stop_loss == 84500.0


@pytest.mark.parametrize("raw_id", [18199, 19670])
def test_r15_take_profit_adjustment_is_still_adjust_take_profit(raw_id):
    row = _TP_MESSAGES[raw_id]
    event = {k: v for k, v in row["lifecycle_event"].items() if k != "targets"}
    directive = resolve_management_directive(text=row["text"], lifecycle_event=event)
    assert directive.intent == "adjust_take_profit"


def test_r15_p_nearby_take_profit_is_not_refused_as_a_price_trigger():
    assert (
        rule_of(
            "82500附近可以止盈30%先",
            {"event_type": "position_update", "take_profit": "82500"},
            "adjust_take_profit",
        )
        is None
    )


@pytest.mark.parametrize("text", ["保本出局", "先保本出局"])
def test_r15_m8_break_even_exit_is_still_a_full_exit(text):
    directive = resolve_management_directive(
        text=text, lifecycle_event={"event_type": "position_update"}
    )
    assert directive.intent == "full_exit"


# ---------------------------------------------------------------------------
# R5 / R6: production replays through the authoritative apply path
# ---------------------------------------------------------------------------

POSTED = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _replay(
    tmp_path,
    *,
    text,
    event,
    symbol="BTC",
    side="long",
    with_binding=True,
    prices=(83000, 81000, "86000"),
):
    """One entered lifecycle (optionally with a live binding) and one message."""

    factory = create_session_factory(tmp_path / "gate.db")
    with factory() as session:
        binding = None
        if with_binding:
            binding = ExecutionBinding(
                kol_id="k",
                chat_id=500,
                message_id=1,
                symbol=symbol,
                side=side,
                venue="deepcoin",
                status="active",
                pos_id="pos-1",
            )
            session.add(binding)
            session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=500,
            message_id=1,
            symbol=symbol,
            side=side,
            lifecycle_status="entered",
            signal_at=POSTED - timedelta(hours=2),
            entered_at=POSTED - timedelta(hours=1),
            entry_price_actual=prices[0],
            stop_loss=prices[1],
            take_profit=prices[2],
            execution_binding_id=binding.id if binding is not None else None,
        )
        raw = RawMessage(chat_id=500, message_id=2, posted_at=POSTED, text=text)
        session.add_all([lifecycle, raw])
        session.commit()
        lifecycle_id, raw_id = lifecycle.id, raw.id
    lifecycle_event = dict(event)
    lifecycle_event.update(
        {
            "target_lifecycle_id": lifecycle_id,
            "symbol": symbol,
            "side": side,
            "confidence": 0.93,
        }
    )
    apply_authoritative_mimo_payload(
        factory,
        raw_message_id=raw_id,
        model="mimo",
        payload={
            "recognition_result": "非策略",
            "confidence": 0.95,
            "lifecycle_event": lifecycle_event,
            "input_reading": {"observed_text": text},
        },
    )
    with factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.parse_source == "mimo_authoritative")
            .all()
        )
        recognition = session.query(MessageRecognition).one()
        return {
            "status": lifecycle.lifecycle_status,
            "management_action": lifecycle.management_action,
            "exit_signal_message_id": lifecycle.exit_signal_message_id,
            "candidates": [
                (c.event_type, c.management_action, c.stop_loss_text) for c in candidates
            ],
            "reason": recognition.reason,
            "recognition_status": recognition.status,
        }


_R5 = [
    (19011, "今晚比特涨到86左右我们多单应该就要准备平仓了", "intent_marker"),
    (19115, "目前还持有几个小币种的多单，预计今天等比特冲86左右时会分批止盈掉；", "intent_marker"),
    # "加仓" makes the directive ``risk_increasing`` before the gate is asked:
    # refused by the existing add-position path (Q1), which the gate leaves alone.
    (19360, "VIRTUAL未跌破启动点前，只考虑加仓", None),
    (19445, "中长线币种QNT目标位1200美元；TAO目标位3000美元；继续持有即可；", "exit_verb_required"),
    (19448, "其它币种多单继续持有做好加一次仓的预期；", "intent_marker"),
    (
        19653,
        "所以多单继续持有；万一拉不上去5-2中的c浪继续延长的话做好81左右加一次仓的预期；不过那样心态就不好了",
        "intent_marker",
    ),
    (19439, "#Pump looks good\nAdd more\n\n______________________\n#泵看起来不错\n添加更多", "exit_verb_required"),
    (
        18843,
        "比特币多单小级别关注84400-84800这个位子，如果突破可继续持有，这笔交易在关注三小时，有变动我会在会员群通知。",
        "exit_verb_required",
    ),
]


@pytest.mark.parametrize(("raw_id", "text", "rule"), _R5, ids=[str(r[0]) for r in _R5])
@pytest.mark.parametrize("with_binding", [True, False], ids=["live_binding", "no_binding"])
def test_r5_commentary_never_writes_even_if_context_says_exit_thread(
    tmp_path, raw_id, text, rule, with_binding
):
    """The event is exactly what an ``exit_thread`` context answer would build:
    ``exit_position`` on an exact, entered, live target. No exit is recorded."""

    result = _replay(tmp_path, text=text, event=EXIT, with_binding=with_binding)

    assert result["candidates"] == []
    assert result["status"] == "entered"
    assert result["management_action"] is None
    assert result["exit_signal_message_id"] is None
    code = reason_code_from_recognition_reason(result["reason"])
    if rule is None:
        assert "management_not_actionable" not in (result["reason"] or "")
        return
    assert code == f"management_not_actionable:{rule}"
    assert code not in ALERTED_REASONS  # recorded, visible, not alerted
    assert result["recognition_status"] != "识别失败"


def test_r5_the_same_exit_with_an_explicit_verb_still_writes(tmp_path):
    """Control: the fixture setup does produce a close signal for a real exit."""

    result = _replay(tmp_path, text="保本出局", event=EXIT)

    assert [c[0] for c in result["candidates"]] == ["close_signal"]
    assert result["exit_signal_message_id"] == 2


@pytest.mark.parametrize(
    ("raw_id", "text", "symbol", "price", "prices"),
    [
        (19692, "JTO设个止损0.51", "JTO", "0.51", (0.6, 0.5, "0.8")),
        (19694, "TAO设止损281", "TAO", "281", (300, 270, "350")),
    ],
)
def test_r6_real_stop_instructions_are_not_refused(
    tmp_path, raw_id, text, symbol, price, prices
):
    result = _replay(
        tmp_path,
        text=text,
        symbol=symbol,
        prices=prices,
        event={
            "event_type": "position_update",
            "management_action": "adjust_stop_loss",
            "stop_loss": price,
        },
    )

    assert [c[1] for c in result["candidates"]] == ["adjust_stop_loss"]
    assert [c[2] for c in result["candidates"]] == [price]
    assert "management_not_actionable" not in (result["reason"] or "")


def test_r6_19454_add_more_is_still_rejected_by_the_add_position_path(tmp_path):
    result = _replay(
        tmp_path,
        text="#QNT add more\nTP 1000 -6000$",
        symbol="QNT",
        event={
            "event_type": "position_update",
            "management_action": "add_position",
            "take_profit": "1000-6000",
        },
    )

    assert result["candidates"] == []
    directive = resolve_management_directive(
        text="#QNT add more\nTP 1000 -6000$",
        lifecycle_event={
            "event_type": "position_update",
            "management_action": "add_position",
            "symbol": "QNT",
        },
    )
    # The existing Q1 path, not the new gate.
    assert directive.reason_code == "risk_increasing_fanout_forbidden"
    assert not directive_is_not_actionable(directive)
    assert "management_not_actionable" not in (result["reason"] or "")


def test_r15_mia_m1_through_the_apply_path(tmp_path):
    result = _replay(
        tmp_path,
        text="BTC现价83400附近，止盈50%，剩余仓位止损位下移至84500，做无风险持仓！",
        side="short",
        event={"event_type": "position_update", "management_action": "partial_take_profit"},
    )

    assert [c[1] for c in result["candidates"]] == ["partial_then_break_even"]


def test_r15_m8_through_the_apply_path(tmp_path):
    result = _replay(tmp_path, text="保本出局", event={"event_type": "position_update"})

    assert [c[1] for c in result["candidates"]] == ["full_exit"]


# ---------------------------------------------------------------------------
# the other hook points
# ---------------------------------------------------------------------------


def test_explicit_target_admission_refuses_with_the_gates_reason(tmp_path):
    from telegram_kol_research.message_recognition import (
        _admit_one_explicit_management_target_in_session,
    )

    factory = create_session_factory(tmp_path / "admit.db")
    with factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=500,
            message_id=1,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=POSTED - timedelta(hours=2),
        )
        raw = RawMessage(chat_id=500, message_id=2, posted_at=POSTED, text="x")
        session.add_all([lifecycle, raw])
        session.commit()
        with pytest.raises(ValueError) as raised:
            _admit_one_explicit_management_target_in_session(
                session,
                raw_message=raw,
                target_decision={
                    "event_type": "exit_position",
                    "management_action": "exit_full",
                    "target_lifecycle_id": lifecycle.id,
                    "symbol": "BTC",
                    "side": "long",
                },
                instruction_text="涨到86000时全部平仓",
            )
    assert str(raised.value) == "management_not_actionable:price_trigger_immediate"


def test_planner_backstop_blocks_a_candidate_that_reached_it_anyway(tmp_path):
    from types import SimpleNamespace

    from telegram_kol_research.strategy_management_planner import (
        _actionability_backstop_refusal,
    )

    def identity(text, parse_source="mimo_authoritative"):
        return SimpleNamespace(
            raw_message=SimpleNamespace(text=text),
            candidate=SimpleNamespace(parse_source=parse_source, stop_loss_text=None),
        )

    refusal = _actionability_backstop_refusal(
        identity=identity("涨到86000时全部平仓"), intent="full_exit"
    )
    assert refusal is not None and refusal.rule == "price_trigger_immediate"
    refusal = _actionability_backstop_refusal(
        identity=identity("今晚应该会平仓"), intent="full_exit"
    )
    assert refusal is not None and refusal.rule == "intent_marker"
    # rule 4 is not evaluated here (the stop gate sees the resolved price)
    assert (
        _actionability_backstop_refusal(identity=identity("设个止损"), intent="adjust_stop_loss")
        is None
    )
    # deterministic exits are not text instructions
    assert (
        _actionability_backstop_refusal(
            identity=identity("涨到86000时全部平仓", parse_source="source_deletion"),
            intent="full_exit",
        )
        is None
    )
    assert (
        _actionability_backstop_refusal(identity=identity("保本出局"), intent="full_exit")
        is None
    )
