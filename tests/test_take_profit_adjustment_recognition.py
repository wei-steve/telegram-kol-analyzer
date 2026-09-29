"""Real messages through the authoritative recognition path to a candidate.

What recognition writes is what the planner reads: the candidate's
``management_action``, and, for the 欧阳 template, the stop and its provenance.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_recognition import apply_authoritative_mimo_payload
from telegram_kol_research.models import RawMessage, SignalCandidate, StrategyLifecycle

FIXTURE = Path(__file__).parent / "fixtures" / "take_profit_adjustment_replay.json"
MESSAGES = {
    int(row["raw_message_id"]): row
    for row in json.loads(FIXTURE.read_text(encoding="utf-8"))["messages"]
}
POSTED = datetime(2026, 9, 21, 16, 15, tzinfo=UTC)


def _apply(tmp_path, raw_id, *, side, symbol="BTC", stop_loss, take_profit, event=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    factory = create_session_factory(tmp_path / f"{raw_id}.db")
    row = MESSAGES[raw_id]
    with factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=500,
            message_id=1,
            symbol=symbol,
            side=side,
            lifecycle_status="entered",
            signal_at=POSTED - timedelta(hours=2),
            entered_at=POSTED - timedelta(hours=1),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        raw = RawMessage(chat_id=500, message_id=2, posted_at=POSTED, text=row["text"])
        session.add_all([lifecycle, raw])
        session.commit()
        lifecycle_id, raw_message_id = lifecycle.id, raw.id
    lifecycle_event = dict(event or row["lifecycle_event"])
    lifecycle_event.pop("targets", None)
    lifecycle_event.update(
        {
            "target_lifecycle_id": lifecycle_id,
            "symbol": symbol,
            "side": side,
            "confidence": max(float(lifecycle_event.get("confidence") or 0), 0.9),
        }
    )
    apply_authoritative_mimo_payload(
        factory,
        raw_message_id=raw_message_id,
        model="mimo",
        payload={
            "recognition_result": "非策略",
            "confidence": 0.95,
            "lifecycle_event": lifecycle_event,
            "input_reading": {"observed_text": row["observed_text"] or row["text"]},
        },
    )
    with factory() as session:
        return [
            (
                candidate.management_action,
                candidate.management_fraction,
                candidate.stop_loss_text,
                candidate.stop_price_source,
            )
            for candidate in session.query(SignalCandidate)
            .filter(SignalCandidate.parse_source == "mimo_authoritative")
            .all()
        ]


def test_18199_becomes_adjust_take_profit_and_never_partial(tmp_path):
    candidates = _apply(tmp_path, 18199, side="short", stop_loss=86500, take_profit="84000/82000")

    assert [row[:2] for row in candidates] == [("adjust_take_profit", None)]


def test_13848_ouyang_template_carries_the_labelled_stop(tmp_path):
    candidates = _apply(tmp_path, 13848, side="short", stop_loss=80000, take_profit="74000")

    assert candidates == [("adjust_take_profit", None, "78700", "current_message_text")]


def test_17745_ouyang_eth_template(tmp_path):
    candidates = _apply(
        tmp_path, 17745, side="long", symbol="ETH", stop_loss=2580, take_profit="2690"
    )

    assert candidates == [("adjust_take_profit", None, "2624", "current_message_text")]


@pytest.mark.parametrize("raw_id", [14306, 19670, 18294])
def test_allocation_messages_never_become_market_reductions(tmp_path, raw_id):
    side = "long" if raw_id == 14306 else "short"
    candidates = _apply(tmp_path, raw_id, side=side, stop_loss=None, take_profit="80000")

    assert [row[:2] for row in candidates] == [("adjust_take_profit", None)]


def test_18532_conditional_sentence_is_not_a_take_profit_adjustment(tmp_path):
    candidates = _apply(tmp_path, 18532, side="short", stop_loss=86500, take_profit="84200")

    assert all(row[0] != "adjust_take_profit" for row in candidates)


@pytest.mark.parametrize("raw_id", [17900, 17901, 18154, 19597, 16873, 18532])
def test_mia_and_other_regressions_are_exactly_what_they_were(tmp_path, monkeypatch, raw_id):
    """Whatever these produced before the hook, they still produce.

    (#19597 is ``adjust_stop_loss`` today -- the lost 50% is the Mia
    management-verification line's to fix, not this one's.)
    """

    from telegram_kol_research import management_directives

    with_hook = _apply(tmp_path / "a", raw_id, side="long", stop_loss=60000, take_profit="90000")
    monkeypatch.setattr(
        management_directives, "_take_profit_adjustment_directive", lambda **_: None
    )
    without_hook = _apply(
        tmp_path / "b", raw_id, side="long", stop_loss=60000, take_profit="90000"
    )

    assert with_hook == without_hook
    assert all(row[0] != "adjust_take_profit" for row in with_hook)
