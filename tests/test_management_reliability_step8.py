"""A-8: the label split, its alarm, and the ghost-target hand-off.

The inventory these tests encode: 181 auto_trade messages recorded as
``识别失败`` and not one of them a recognition failure. Four different things
were wearing that one label, and only some of them are worth waking a person
for.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from telegram_kol_research.recognition_failure_attribution import (
    APPLY_FAILED,
    ALERTED_REASONS,
    CONTRACT_INVALID,
    NO_ACTIONABLE_INTENT,
    NO_TARGET_NAMED,
    TARGET_NOT_VERIFIABLE,
    classify_unapplied_lifecycle_event,
    reason_code_from_recognition_reason,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# The four outcomes the one label was hiding
# --------------------------------------------------------------------------


def test_a_message_asking_for_nothing_is_not_a_failure():
    """29 of the 181: "继续拿着不变", "带好止盈止损"."""

    verdict = classify_unapplied_lifecycle_event(
        intent="none", target_lifecycle_id=1096, target_verified=False
    )
    assert verdict.reason_code == NO_ACTIONABLE_INTENT


def test_asking_for_nothing_beats_a_ghost_target():
    """Ordering, not an accident.

    raw 15170 and 15316 are both: hold what you have, aimed at a lifecycle
    with no execution binding. Nothing was lost, so nothing should page. If
    the target were asked about first these would alert, and that noise is
    what buried the ten real losses.
    """

    verdict = classify_unapplied_lifecycle_event(
        intent="none",
        target_lifecycle_id=1096,
        target_verified=False,
        target_detail="no_execution_binding",
    )
    assert verdict.reason_code not in ALERTED_REASONS


def test_a_real_instruction_with_no_target_named():
    """55 of the 181, all of them before 2026-07-27."""

    verdict = classify_unapplied_lifecycle_event(
        intent="partial_take_profit", target_lifecycle_id=None, target_verified=None
    )
    assert verdict.reason_code == NO_TARGET_NAMED


def test_a_real_instruction_aimed_at_a_ghost_is_alerted():
    """raw 14500's shape: 将加仓的部分止盈出局, at a lifecycle with no binding."""

    verdict = classify_unapplied_lifecycle_event(
        intent="partial_take_profit",
        target_lifecycle_id=1043,
        target_verified=False,
        target_detail="no_execution_binding",
    )
    assert verdict.reason_code == TARGET_NOT_VERIFIABLE
    assert verdict.reason_code in ALERTED_REASONS


def test_a_verified_target_that_still_did_not_apply_is_the_real_failure():
    """The inventory found zero of these, which is exactly why it needs an alarm."""

    verdict = classify_unapplied_lifecycle_event(
        intent="adjust_stop_loss", target_lifecycle_id=1097, target_verified=True
    )
    assert verdict.reason_code == APPLY_FAILED
    assert verdict.reason_code in ALERTED_REASONS


def test_the_benign_two_are_never_alerted():
    assert NO_ACTIONABLE_INTENT not in ALERTED_REASONS
    assert NO_TARGET_NAMED not in ALERTED_REASONS


# --------------------------------------------------------------------------
# Carrying the verdict on the recognition row
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("authoritative_lifecycle_not_applied:no_actionable_intent", NO_ACTIONABLE_INTENT),
        ("authoritative_lifecycle_not_applied:target_not_verifiable", TARGET_NOT_VERIFIABLE),
        (
            "authoritative_instruction_contract_invalid:instruction_0_strategy_incomplete",
            CONTRACT_INVALID,
        ),
        ("MiMo lifecycle event could not be applied safely", None),
        (None, None),
        ("", None),
    ],
)
def test_the_verdict_reads_back_off_the_recognition_row(reason, expected):
    assert reason_code_from_recognition_reason(reason) == expected


def test_rows_written_before_a8_keep_their_old_meaning():
    """A historical row is not retro-labelled; it maps to the old reason.

    Retro-labelling would put a confident new verdict on evidence that never
    supported it -- the old rows do not record which of the four they were.
    """

    from telegram_kol_research.authoritative_recognition import (
        _lifecycle_not_applied_reason,
    )

    class Row:
        status = "识别失败"
        reason = "MiMo lifecycle event could not be applied safely"

    assert _lifecycle_not_applied_reason(Row()) == "mimo_authoritative_not_safely_applied"


def test_a_recognition_that_applied_is_left_alone():
    from telegram_kol_research.authoritative_recognition import (
        _lifecycle_not_applied_reason,
    )

    class Row:
        status = "是策略"
        reason = None

    assert _lifecycle_not_applied_reason(Row()) is None


# --------------------------------------------------------------------------
# The alarm: who gets told
# --------------------------------------------------------------------------


def _alert(reason, mode, captured, awaiting=False):
    from telegram_kol_research.authoritative_recognition import (
        _alert_recognition_not_applied,
    )

    class Raw:
        chat_id = -1002337721508

    class Query:
        def filter(self, *args):
            return self

        def first(self):
            return object() if awaiting else None

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, model, ident):
            return Raw()

        def query(self, model):
            return Query()

    return _alert_recognition_not_applied(
        lambda: Session(),
        raw_message_id=15578,
        automation={"status": "skipped", "reason": reason},
        group_trading_mode_provider=lambda chat_id: mode,
        capture=lambda **kwargs: captured.append(kwargs),
    )


def test_a_contract_rejection_in_an_auto_trade_group_alerts():
    captured: list[dict] = []
    assert _alert(CONTRACT_INVALID, "auto_trade", captured) == CONTRACT_INVALID
    assert captured[0]["reason_code"] == CONTRACT_INVALID
    assert captured[0]["chat_id"] == -1002337721508


def test_the_same_rejection_in_a_notify_only_group_is_recorded_but_silent():
    """A group that executes nothing loses nothing when an instruction drops."""

    captured: list[dict] = []
    assert _alert(CONTRACT_INVALID, "notify_only", captured) is None
    assert captured == []


def test_benign_outcomes_do_not_alert_even_in_auto_trade_groups():
    for reason in (NO_ACTIONABLE_INTENT, NO_TARGET_NAMED):
        captured: list[dict] = []
        assert _alert(reason, "auto_trade", captured) is None
        assert captured == []


def test_an_unreadable_group_mode_errs_towards_telling_somebody():
    def explode(chat_id):
        raise RuntimeError("groups.yaml unreadable")

    from telegram_kol_research.authoritative_recognition import (
        _alert_recognition_not_applied,
    )

    class Raw:
        chat_id = -1002337721508

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, model, ident):
            return Raw()

    captured: list[dict] = []
    result = _alert_recognition_not_applied(
        lambda: Session(),
        raw_message_id=15578,
        automation={"status": "skipped", "reason": CONTRACT_INVALID},
        group_trading_mode_provider=explode,
        capture=lambda **kwargs: captured.append(kwargs),
    )
    assert result == CONTRACT_INVALID
    assert len(captured) == 1


def test_the_incident_type_is_always_notified():
    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert "authoritative_recognition_failed" in ALWAYS_NOTIFIED_INCIDENT_TYPES


def test_one_message_asks_one_question_when_both_gates_fire():
    """A-7 parks and notifies during assessment; A-8 sees the same message
    afterwards. Two notifications for one instruction would be worse than one.
    """

    captured: list[dict] = []
    result = _alert(TARGET_NOT_VERIFIABLE, "auto_trade", captured, awaiting=True)
    assert result is None
    assert captured == []


# --------------------------------------------------------------------------
# Task 1: the contract, relaxed as the user approved on 2026-09-09
# --------------------------------------------------------------------------


def _entry_payload(*, stop_loss, take_profit):
    return {
        "recognition_result": "是策略",
        "confidence": 0.8,
        "strategy": {
            "symbol": "ETH",
            "side": "long",
            "entry": "2370附近",
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        },
    }


def test_an_entry_without_a_take_profit_is_accepted():
    """raw 14492's shape: ETH long 2370附近, stop 2335, no target named.

    Fifteen of the twenty-two dropped signals were missing only this field.
    """

    from telegram_kol_research.authoritative_instructions import (
        normalize_authoritative_instructions,
    )

    built = normalize_authoritative_instructions(
        _entry_payload(stop_loss="2335", take_profit=None)
    )
    assert len(built) == 1
    assert built[0].strategy["stop_loss"] == "2335"
    # Absent, not empty: "" would read downstream as a take profit that was
    # given and is blank, which is a different claim about the message.
    assert built[0].strategy["take_profit"] is None


def test_an_entry_without_a_stop_loss_is_still_refused():
    """raw 15578: ETH long 2464, take profit 3200, no stop.

    Position size is derived from the stop, so this one stays refused -- and
    now it is refused loudly, as contract_invalid.
    """

    from telegram_kol_research.authoritative_instructions import (
        AuthoritativeInstructionError,
        normalize_authoritative_instructions,
    )

    with pytest.raises(AuthoritativeInstructionError) as excinfo:
        normalize_authoritative_instructions(
            _entry_payload(stop_loss=None, take_profit="3200")
        )
    assert "strategy_incomplete" in str(excinfo.value)
    assert reason_code_from_recognition_reason(
        f"authoritative_instruction_contract_invalid:{excinfo.value}"
    ) == CONTRACT_INVALID


def test_the_v2_contract_relaxes_the_same_field():
    from telegram_kol_research.mimo_v2_contract import _parse_complete_strategy

    parsed = _parse_complete_strategy(
        {"symbol": "eth", "side": "long", "entry": "2370", "stop_loss": "2335"},
        ordinal=0,
    )
    assert parsed["symbol"] == "ETH"
    assert parsed["take_profit"] is None


def test_the_v2_contract_still_requires_a_stop_loss():
    from telegram_kol_research.mimo_v2_contract import (
        MimoV2ContractError,
        _parse_complete_strategy,
    )

    with pytest.raises(MimoV2ContractError):
        _parse_complete_strategy(
            {"symbol": "ETH", "side": "long", "entry": "2464", "take_profit": "3200"},
            ordinal=0,
        )


def test_a_relaxed_entry_still_faces_every_risk_gate():
    """Relaxing the contract admits messages to the pipeline, not to trading.

    Twelve of the fifteen name a symbol outside the whitelist and stay in
    manual review; the one without a stop would be refused twice over.
    """

    from telegram_kol_research.trading_decision import (
        TradingDecisionInput,
        evaluate_trading_decision,
    )

    def decide(symbol, stop_loss, take_profit):
        return evaluate_trading_decision(
            TradingDecisionInput(
                kol_id=1,
                chat_id=-1002337721508,
                message_id=14492,
                symbol=symbol,
                side="long",
                entry_text="2370附近",
                stop_loss_text=stop_loss,
                take_profit_text=take_profit,
                parse_source="mimo_authoritative",
                confidence=0.8,
                trading_mode="auto_trade",
                symbol_whitelist=["BTC", "ETH"],
                max_loss_usdt=100.0,
            ),
            active_positions=[],
        )

    assert decide("ETH", "2335", None).action == "eligible_for_auto_trade"
    assert "symbol_not_whitelisted" in decide("NEIRO", "0.0000675", None).reason_codes
    assert "missing_stop_loss" in decide("ETH", None, "3200").reason_codes


# --------------------------------------------------------------------------
# A-8b: the refusals A-8 left wearing the old label
# --------------------------------------------------------------------------


def test_every_other_refusal_now_has_its_own_code():
    """raw 15702 and 15703 are why this exists.

    They landed hours after A-8 shipped, still carrying
    ``mimo_authoritative_not_safely_applied`` -- because A-8 split one branch
    and everything else fell through the fallback that keeps pre-A-8 rows
    honest.
    """

    from telegram_kol_research.recognition_failure_attribution import (
        MANAGEMENT_FRACTION_INVALID,
        MEDIA_UNREADABLE,
        SYMBOL_PRICE_SCALE_CONFLICT,
    )

    cases = {
        "management_fraction_invalid": MANAGEMENT_FRACTION_INVALID,
        "symbol_price_scale_conflict: MiMo 输出 BTC，但入场价处于 ETH 区间": (
            SYMBOL_PRICE_SCALE_CONFLICT
        ),
        "图片识别失败：GLM-OCR 未能提取到文字内容": MEDIA_UNREADABLE,
        "图片文件未下载到本地，请重新同步该消息后再识别": MEDIA_UNREADABLE,
    }
    for reason, expected in cases.items():
        assert reason_code_from_recognition_reason(reason) == expected


def test_a_genuine_recognition_failure_keeps_the_legacy_reason():
    """A MiMo call that failed is a recognition failure, and says so.

    The fallback still protects it, and every pre-A-8 row with it.
    """

    from telegram_kol_research.authoritative_recognition import (
        _lifecycle_not_applied_reason,
    )

    class Row:
        status = "识别失败"
        reason = "MiMo authoritative recognition failed"

    assert reason_code_from_recognition_reason(Row.reason) is None
    assert _lifecycle_not_applied_reason(Row()) == (
        "mimo_authoritative_not_safely_applied"
    )


def test_each_new_code_alerts_in_an_auto_trade_group():
    from telegram_kol_research.recognition_failure_attribution import (
        MANAGEMENT_FRACTION_INVALID,
        MEDIA_UNREADABLE,
        SYMBOL_PRICE_SCALE_CONFLICT,
    )

    for reason in (
        MANAGEMENT_FRACTION_INVALID,
        SYMBOL_PRICE_SCALE_CONFLICT,
        MEDIA_UNREADABLE,
    ):
        captured: list[dict] = []
        assert _alert(reason, "auto_trade", captured) == reason
        assert captured[0]["reason_code"] == reason


def test_each_new_code_is_recorded_but_silent_in_a_notify_only_group():
    """raw 15702 and 15703 were both notify_only: nothing there executes."""

    from telegram_kol_research.recognition_failure_attribution import (
        MANAGEMENT_FRACTION_INVALID,
        MEDIA_UNREADABLE,
        SYMBOL_PRICE_SCALE_CONFLICT,
    )

    for reason in (
        MANAGEMENT_FRACTION_INVALID,
        SYMBOL_PRICE_SCALE_CONFLICT,
        MEDIA_UNREADABLE,
    ):
        captured: list[dict] = []
        assert _alert(reason, "notify_only", captured) is None
        assert captured == []


def test_a_refused_fraction_keeps_the_recognition_result(tmp_path):
    """The refusal is unchanged; only the label stops lying about it."""

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.message_recognition import (
        apply_authoritative_mimo_payload,
    )
    from telegram_kol_research.models import (
        MessageInstructionItem,
        RawMessage,
        SignalCandidate,
        StrategyLifecycle,
    )

    factory = create_session_factory(tmp_path / "fraction.db")
    with factory() as session:
        raw = RawMessage(chat_id=9821, message_id=7, text="减仓")
        target = StrategyLifecycle(
            chat_id=9821,
            message_id=6,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 9, 5, tzinfo=UTC),
        )
        session.add_all([raw, target])
        session.commit()
        raw_id, target_id = raw.id, target.id

    result = apply_authoritative_mimo_payload(
        factory,
        raw_message_id=raw_id,
        model="mimo",
        payload={
            "recognition_result": "非策略",
            "confidence": 0.95,
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "partial_take_profit",
                "management_fraction": "150%",
                "target_lifecycle_id": target_id,
                "symbol": "BTC",
                "side": "long",
                "confidence": 0.95,
            },
        },
    )

    assert result.status == "非策略"
    assert reason_code_from_recognition_reason(result.reason) == (
        "management_fraction_invalid"
    )
    with factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(MessageInstructionItem).count() == 0
        assert session.get(StrategyLifecycle, target_id).lifecycle_status == "entered"


# --------------------------------------------------------------------------
# A-8c: the fraction record is written either way, delivered only where it matters
# --------------------------------------------------------------------------


def _fraction_incident(tmp_path, *, chat_id, provider, name, row_chat_id=None):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.management_fraction_gate import (
        record_fraction_rejection,
    )
    from telegram_kol_research.models import RawMessage, RuntimeIncident

    factory = create_session_factory(tmp_path / name)
    with factory() as session:
        raw = RawMessage(
            chat_id=row_chat_id if row_chat_id is not None else chat_id,
            message_id=7,
            text="减仓",
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id

    record_fraction_rejection(
        factory,
        raw_message_id=raw_id,
        chat_id=chat_id,
        group_trading_mode_provider=provider,
    )
    with factory() as session:
        return session.query(RuntimeIncident).one()


def test_a_refused_fraction_in_an_auto_trade_group_stays_deliverable(tmp_path):
    row = _fraction_incident(
        tmp_path,
        chat_id=-1002337721508,
        provider=lambda chat_id: "auto_trade",
        name="auto.db",
    )

    assert row.incident_type == "management_fraction_rejected"
    assert row.notification_status == "pending"
    assert "auto_trade_deliverable" in row.redacted_summary


def test_a_refused_fraction_in_a_notify_only_group_is_recorded_but_silent(tmp_path):
    """raw 15702 and 15703's group. Nothing there executes, so nothing was lost.

    The row is written ``suppressed`` rather than left in ``pending`` for a
    claimer that is never coming -- a queue of things nobody will send is how
    the real ones get lost.
    """

    row = _fraction_incident(
        tmp_path,
        chat_id=-1002960443256,
        provider=lambda chat_id: "notify_only",
        name="notify.db",
    )

    assert row.notification_status == "suppressed"
    assert "records_only_group_mode_notify_only" in row.redacted_summary


def test_an_unknown_group_mode_is_silent_rather_than_wrong(tmp_path):
    """No provider, no chat, or a provider that raises: all answer the same.

    Silence about a message that could not have traded is cheap; paging
    somebody about one is not.
    """

    def explode(chat_id):
        raise RuntimeError("groups.yaml unreadable")

    for provider, chat_id, name in (
        (None, -1002960443256, "no-provider.db"),
        (lambda chat_id: "auto_trade", None, "no-chat.db"),  # chat unavailable
        (explode, -1002960443256, "provider-raises.db"),
    ):
        row = _fraction_incident(
            tmp_path,
            chat_id=chat_id,
            provider=provider,
            name=name,
            row_chat_id=-1002960443256,
        )
        assert row.notification_status == "suppressed"
        assert "records_only_group_mode_unknown" in row.redacted_summary
