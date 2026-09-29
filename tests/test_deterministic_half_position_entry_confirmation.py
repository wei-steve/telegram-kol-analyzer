"""M6/Q3: a bare "半仓入场" the model missed still halves the entry.

``docs/plans/2026-09-29-mia-management-verification-design.md`` section M6,
ruling Q3. Production reality (2026-09-25, 米娅群 -1003825498321):

- #19063 12:57:05 a BTC long strategy (entry 84400 附近, stop 82400, tp 87000).
- #19064 12:57:09, 4 seconds later, "半仓入场！\n@Tarderfengge QQ:158241758".
  The model's real authoritative payload judged this 闲聊 -- ``event_type``
  ``none``, confidence 0.6, ``target_lifecycle_id`` null -- and the strategy
  went on to market-fill at full size (10 contracts) instead of half.

Compare with #18890 (2026-09-24, same group), where the model *did* recognise
the identical bare phrase as ``entry_confirm`` / ``half_position_entry`` and
the strategy correctly opened at half size. The fix must produce the same
effect the model produced there, only for the case the model missed, and must
never re-apply when the model already got it right.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_revision_exchange_authority import (
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.message_recognition import apply_authoritative_mimo_payload
from telegram_kol_research.models import (
    EntryPreamble,
    MessageEvidenceVersion,
    RawMessage,
    RuntimeIncident,
    SignalCandidate,
    StrategyLifecycle,
)


CHAT_ID = -1003825498321  # 米娅群, matching the production evidence.


def _session_factory(path):
    session_factory = create_session_factory(path)
    seed_entry_revision_exchange_authority(
        session_factory,
        seeded_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    return session_factory


def _add_raw_message(session, *, message_id, posted_at, text, chat_id=CHAT_ID):
    raw = RawMessage(
        chat_id=chat_id,
        message_id=message_id,
        sender_id=7,
        sender_name="Mia",
        posted_at=posted_at,
        text=text,
        archived_target_group=True,
    )
    session.add(raw)
    session.flush()
    return raw


def _add_completed_evidence(session, raw, *, normalized="{}"):
    evidence = MessageEvidenceVersion(
        raw_message_id=raw.id,
        version=1,
        input_fingerprint=f"fingerprint-{raw.id}",
        model="mimo-v2.5",
        prompt_versions_json="{}",
        extraction_status="completed",
        confidence=0.95,
        text_evidence_json="{}",
        image_evidence_json='{"images":[]}',
        normalized_evidence_json=normalized,
    )
    session.add(evidence)
    session.flush()
    return evidence


def _add_pending_lifecycle(
    session,
    *,
    message_id,
    signal_at,
    symbol="BTC",
    side="long",
    entry_low=84300.0,
    entry_high=84500.0,
    stop_loss=82400.0,
    take_profit="87000",
    chat_id=CHAT_ID,
):
    lifecycle = StrategyLifecycle(
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        lifecycle_status="pending_entry",
        signal_at=signal_at,
        entry_range_low=entry_low,
        entry_range_high=entry_high,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    session.add(lifecycle)
    session.flush()
    return lifecycle


#: The model's real 2026-09-25 payload for raw #19064: judged 闲聊, no
#: lifecycle event, no target. Copied from the production snapshot verbatim
#: (only whitespace/formatting normalised) so the replay uses the actual
#: shape the model produced, not a stand-in.
_RAW_19064_MODEL_PAYLOAD = {
    "confidence": 0.6,
    "entry_context": None,
    "recognition_result": "非策略",
    "strategy": {
        "entry": None,
        "leverage": None,
        "order_type": None,
        "side": None,
        "stop_loss": None,
        "symbol": None,
        "take_profit": None,
    },
    "lifecycle_event": {
        "event_type": "none",
        "confidence": 0.6,
        "entry_price": None,
        "exit_price": None,
        "management_action": None,
        "side": None,
        "stop_loss": None,
        "symbol": None,
        "take_profit": None,
        "target_lifecycle_id": None,
        "reason": (
            "消息仅提及“半仓入场”，但未明确关联到任何具体的已有策略"
            "（如最新BTC做多策略1325）。缺乏足够的信息确认是对此前策略的入场"
            "确认（entry_confirm）或其他管理动作。"
        ),
    },
    "reason": "消息仅包含“半仓入场”和联系方式，判定为闲话。",
}

_CONFIRM_TEXT = "半仓入场！\n@Tarderfengge QQ:158241758"


def _entry_confirm_candidate(session, raw_message_id):
    return (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message_id)
        .filter(SignalCandidate.event_type == "entry_signal")
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# Positive case: the real #19063 + #19064 shape, 4 seconds apart.
# ---------------------------------------------------------------------------


def test_bare_half_position_entry_the_model_judged_chatter_still_halves_the_entry(
    tmp_path,
):
    session_factory = _session_factory(tmp_path / "half-position-fix.db")
    strategy_at = datetime(2026, 9, 25, 12, 57, 5, tzinfo=UTC)
    confirm_at = strategy_at + timedelta(seconds=4)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(session, message_id=700, signal_at=strategy_at)
        confirm_raw = _add_raw_message(
            session, message_id=701, posted_at=confirm_at, text=_CONFIRM_TEXT
        )
        _add_completed_evidence(session, confirm_raw)
        session.commit()
        confirm_raw_id, lifecycle_id = confirm_raw.id, lifecycle.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=confirm_raw_id,
        payload=_RAW_19064_MODEL_PAYLOAD,
        model="mimo-v2.5",
        authoritative_generation="generation-701",
    )

    # The model's own classification is untouched -- it is still "非策略" and
    # still carries its own low-confidence, no-op lifecycle_event. The fix
    # acts underneath it, not by relabelling the model's answer.
    assert result.status == "非策略"

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"

        preamble = session.query(EntryPreamble).one()
        assert preamble.raw_message_id == confirm_raw_id
        assert preamble.symbol == "BTC"
        assert preamble.side == "long"
        assert preamble.risk_multiplier == "0.5"
        assert preamble.status == "pending"

        candidate = _entry_confirm_candidate(session, confirm_raw_id)
        assert candidate is not None
        assert candidate.management_action == "entry_confirm"
        assert candidate.target_lifecycle_id is None

        incident = session.query(RuntimeIncident).one()
        assert incident.incident_type == "half_position_entry_confirmed_by_rule"
        assert incident.severity == "high"


def test_model_correctly_recognizing_the_confirmation_is_left_alone(tmp_path):
    """#18890's own shape: the model got it right, so the rule must not double it."""

    session_factory = _session_factory(tmp_path / "half-position-model-correct.db")
    strategy_at = datetime(2026, 9, 24, 14, 45, 33, tzinfo=UTC)
    confirm_at = strategy_at + timedelta(seconds=6)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session, message_id=693, signal_at=strategy_at, side="short",
            entry_low=84300.0, entry_high=84700.0, stop_loss=86200.0,
            take_profit="82800",
        )
        confirm_raw = _add_raw_message(
            session, message_id=694, posted_at=confirm_at, text=_CONFIRM_TEXT.replace("！", "")
        )
        _add_completed_evidence(session, confirm_raw)
        session.commit()
        confirm_raw_id, lifecycle_id = confirm_raw.id, lifecycle.id

    model_correct_payload = {
        "recognition_result": "非策略",
        "lifecycle_event": {
            "event_type": "entry_confirm",
            "confidence": 0.98,
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": None,
            "management_action": "half_position_entry",
            "reason": "半仓入场明确确认此前BTC空单待入场策略已部分进场。",
        },
        "strategy": {
            "entry": None, "leverage": None, "order_type": None, "side": None,
            "stop_loss": None, "symbol": None, "take_profit": None,
        },
    }

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=confirm_raw_id,
        payload=model_correct_payload,
        model="mimo-v2.5",
        authoritative_generation="generation-694",
    )

    with session_factory() as session:
        # Exactly one preamble, one candidate, no incident -- the model's own
        # entry_confirm path did all of the work; the deterministic fallback
        # never had a reason to run.
        assert session.query(EntryPreamble).count() == 1
        assert (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == confirm_raw_id)
            .count()
            == 1
        )
        assert session.query(RuntimeIncident).count() == 0
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"


# ---------------------------------------------------------------------------
# Counter-examples: the rule must stay quiet.
# ---------------------------------------------------------------------------


def test_bare_half_position_entry_more_than_60_seconds_later_is_not_confirmed(
    tmp_path,
):
    session_factory = _session_factory(tmp_path / "half-position-too-late.db")
    strategy_at = datetime(2026, 9, 25, 12, 57, 5, tzinfo=UTC)
    confirm_at = strategy_at + timedelta(seconds=70)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(session, message_id=700, signal_at=strategy_at)
        confirm_raw = _add_raw_message(
            session, message_id=701, posted_at=confirm_at, text=_CONFIRM_TEXT
        )
        _add_completed_evidence(session, confirm_raw)
        session.commit()
        confirm_raw_id, lifecycle_id = confirm_raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=confirm_raw_id,
        payload=_RAW_19064_MODEL_PAYLOAD,
        model="mimo-v2.5",
        authoritative_generation="generation-701",
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "pending_entry"
        assert session.query(EntryPreamble).count() == 0
        assert session.query(RuntimeIncident).count() == 0


def test_bare_half_position_entry_in_a_different_chat_is_not_confirmed(tmp_path):
    session_factory = _session_factory(tmp_path / "half-position-other-chat.db")
    strategy_at = datetime(2026, 9, 25, 12, 57, 5, tzinfo=UTC)
    confirm_at = strategy_at + timedelta(seconds=4)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session, message_id=700, signal_at=strategy_at, chat_id=CHAT_ID
        )
        confirm_raw = _add_raw_message(
            session,
            message_id=701,
            posted_at=confirm_at,
            text=_CONFIRM_TEXT,
            chat_id=CHAT_ID + 1,
        )
        _add_completed_evidence(session, confirm_raw)
        session.commit()
        confirm_raw_id, lifecycle_id = confirm_raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=confirm_raw_id,
        payload=_RAW_19064_MODEL_PAYLOAD,
        model="mimo-v2.5",
        authoritative_generation="generation-701",
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "pending_entry"
        assert session.query(EntryPreamble).count() == 0


def test_longer_half_position_entry_text_is_left_to_the_model(tmp_path):
    """"半仓入场，另一半等回调" carries its own qualification; not this rule's call."""

    session_factory = _session_factory(tmp_path / "half-position-longer-text.db")
    strategy_at = datetime(2026, 9, 25, 12, 57, 5, tzinfo=UTC)
    confirm_at = strategy_at + timedelta(seconds=4)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(session, message_id=700, signal_at=strategy_at)
        confirm_raw = _add_raw_message(
            session,
            message_id=701,
            posted_at=confirm_at,
            text="半仓入场，另一半等回调\n@Tarderfengge QQ:158241758",
        )
        _add_completed_evidence(session, confirm_raw)
        session.commit()
        confirm_raw_id, lifecycle_id = confirm_raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=confirm_raw_id,
        payload=_RAW_19064_MODEL_PAYLOAD,
        model="mimo-v2.5",
        authoritative_generation="generation-701",
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "pending_entry"
        assert session.query(EntryPreamble).count() == 0


def test_bare_half_position_entry_after_the_lifecycle_already_entered_does_not_resize(
    tmp_path,
):
    """An already-filled market leg (lifecycle no longer pending_entry) is untouched."""

    session_factory = _session_factory(tmp_path / "half-position-already-entered.db")
    strategy_at = datetime(2026, 9, 25, 12, 57, 5, tzinfo=UTC)
    confirm_at = strategy_at + timedelta(seconds=4)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(session, message_id=700, signal_at=strategy_at)
        lifecycle.lifecycle_status = "entered"
        lifecycle.entered_at = strategy_at + timedelta(seconds=1)
        confirm_raw = _add_raw_message(
            session, message_id=701, posted_at=confirm_at, text=_CONFIRM_TEXT
        )
        _add_completed_evidence(session, confirm_raw)
        session.commit()
        confirm_raw_id, lifecycle_id = confirm_raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=confirm_raw_id,
        payload=_RAW_19064_MODEL_PAYLOAD,
        model="mimo-v2.5",
        authoritative_generation="generation-701",
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.entered_at == (
            strategy_at + timedelta(seconds=1)
        ).replace(tzinfo=None)
        assert session.query(EntryPreamble).count() == 0
        assert session.query(RuntimeIncident).count() == 0
