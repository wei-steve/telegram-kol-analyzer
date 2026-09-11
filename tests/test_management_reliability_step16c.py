"""A-16c: an instruction whose action is known and whose target is not.

The 2026-09-11 chain, measured in A-16-0: the resolver refused three messages
for an ambiguous target, the payload was rewritten to 非策略, no signal
candidate and therefore no instruction item existed, and A-7's confirmation
channel had nothing to park -- so nothing was said and the user closed two live
positions by hand.

This step builds the item explicitly and parks it in the same transaction. The
tests below assert the parked state and, just as importantly, that the item is
never left executable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime


NOW = datetime(2026, 9, 11, 5, 3, 12, tzinfo=UTC)
INSTRUCTION = "先拿1000点利润50%，剩余做成本保护继续持有"
REASON = "无法唯一确定目标策略线程520或521"


def _permissive_incident_config():
    from telegram_kol_research.config import (
        ALWAYS_NOTIFIED_INCIDENT_TYPES,
        RuntimeIncidentConfig,
    )

    return RuntimeIncidentConfig(
        capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES)
    )


def _decision(**overrides):
    from telegram_kol_research.context_resolution import ContextResolutionDecision

    payload = {
        "decision": "unresolved",
        "target_thread_ids": (),
        "management_action": None,
        "confidence": 0.6,
        "supporting_message_ids": (),
        "opposing_message_ids": (),
        "conflict_types": ("target_ambiguous", "multiple_candidates"),
        "risk_reducing_fanout_allowed": False,
        "reanalysis_triggers": (),
        "reason": REASON,
    }
    payload.update(overrides)
    return ContextResolutionDecision(**payload)


class _Candidate:
    def __init__(self, thread_id, lifecycle_id):
        self.thread_id = thread_id
        self.lifecycle_id = lifecycle_id
        self.symbol = "BTC"
        self.side = "long"
        self.status = "entered"
        self.lifecycle_summary = {"entry": "77000", "symbol": "BTC", "side": "long"}
        self.binding_summary = None
        self.risk_state = "verified"


def _payload(*, instruction=INSTRUCTION):
    fields = {"symbol": {"value": "BTC", "confidence": 1.0, "source": "text"}}
    if instruction is not None:
        fields["management_instruction"] = {
            "value": instruction,
            "confidence": 1.0,
            "source": "text",
        }
    return {"evidence": {"text": {"fields": fields}}}


def _seed_raw_message(session_factory):
    from telegram_kol_research.models import RawMessage

    with session_factory() as session:
        row = RawMessage(
            chat_id=-1002337721508,
            message_id=10442,
            text="BTC市价78000附近，先拿1000点利润50%，剩余做成本保护继续持有。",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(row)
        session.commit()
        return int(row.id)


def _park(session_factory, raw_message_id, *, decision=None, payload=None,
          candidates=None):
    from telegram_kol_research.authoritative_recognition import (
        _park_unresolved_management_instruction,
    )

    return _park_unresolved_management_instruction(
        session_factory,
        raw_message_id=raw_message_id,
        payload=payload if payload is not None else _payload(),
        decision=decision if decision is not None else _decision(),
        candidates=candidates
        if candidates is not None
        else (_Candidate(520, 1151), _Candidate(521, 1152)),
    )


def _items(session_factory, raw_message_id):
    from telegram_kol_research.models import MessageInstructionItem

    with session_factory() as session:
        return [
            (item.id, item.instruction_kind, item.status,
             json.loads(item.result_json or "{}"))
            for item in session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == raw_message_id)
            .all()
        ]


def test_an_ambiguous_management_instruction_is_parked_not_dropped(tmp_path):
    """The item exists, it is a management item, and it is awaiting a human."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16c.db")
    raw_message_id = _seed_raw_message(session_factory)

    moved = _park(session_factory, raw_message_id)

    assert len(moved) == 1
    rows = _items(session_factory, raw_message_id)
    assert len(rows) == 1
    _, kind, status, result = rows[0]
    assert kind == "management"
    assert status == "awaiting_user_confirmation"
    assert result["management_unresolved"] is True
    assert [row["number"] for row in result["confirmation_candidates"]] == [1, 2]
    assert result["management_instruction"] == INSTRUCTION
    assert "520" in result["resolution_reason"]


def test_the_item_is_never_left_in_pending(tmp_path):
    """``pending`` is executable; this shape must never be executable."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16c.db")
    raw_message_id = _seed_raw_message(session_factory)

    _park(session_factory, raw_message_id)

    statuses = {status for _, _, status, _ in _items(session_factory, raw_message_id)}
    assert statuses == {"awaiting_user_confirmation"}
    assert "pending" not in statuses


def test_a_single_candidate_is_not_parked(tmp_path):
    """Nothing to choose between; A-16a's alert is the whole answer."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16c.db")
    raw_message_id = _seed_raw_message(session_factory)

    moved = _park(session_factory, raw_message_id,
                  candidates=(_Candidate(520, 1151),))

    assert moved == ()
    assert _items(session_factory, raw_message_id) == []


def test_a_non_ambiguous_conflict_is_not_parked(tmp_path):
    """The measured subset: target_ambiguous with two or more candidates."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16c.db")
    raw_message_id = _seed_raw_message(session_factory)

    moved = _park(session_factory, raw_message_id,
                  decision=_decision(conflict_types=("text_image_conflict",)))

    assert moved == ()
    assert _items(session_factory, raw_message_id) == []


def test_no_management_instruction_is_not_parked(tmp_path):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16c.db")
    raw_message_id = _seed_raw_message(session_factory)

    moved = _park(session_factory, raw_message_id, payload=_payload(instruction=None))

    assert moved == ()
    assert _items(session_factory, raw_message_id) == []


def test_parking_twice_produces_one_item(tmp_path):
    """A re-run of the same message must not ask the same question twice."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16c.db")
    raw_message_id = _seed_raw_message(session_factory)

    _park(session_factory, raw_message_id)
    _park(session_factory, raw_message_id)

    assert len(_items(session_factory, raw_message_id)) == 1


def test_a_claimed_unresolved_item_is_refused_and_reparked(tmp_path, monkeypatch):
    """The self-check refuses, it does not merely complain.

    An item of this shape reaching the claim path means the park did not hold.
    Executing it is the original shape of the incident, so the claim returns
    nothing and the item goes back to awaiting confirmation -- with an alert,
    because a refusal nobody hears is how the incident stayed invisible.
    """

    from telegram_kol_research import config as config_module
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.message_instruction_items import (
        claim_next_message_instruction_item,
    )
    from telegram_kol_research.models import MessageInstructionItem, RuntimeIncident

    session_factory = create_session_factory(tmp_path / "a16c.db")
    raw_message_id = _seed_raw_message(session_factory)
    _park(session_factory, raw_message_id)
    monkeypatch.setattr(
        config_module,
        "load_runtime_incident_config",
        lambda *a, **k: _permissive_incident_config(),
    )
    # Simulate the park not holding: put it back into the executable state.
    with session_factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == raw_message_id)
            .one()
        )
        item.status = "pending"
        session.commit()

    claimed = claim_next_message_instruction_item(
        session_factory, raw_message_id=raw_message_id, now=NOW
    )

    assert claimed is None, "an untargeted management item must not be executable"
    statuses = {status for _, _, status, _ in _items(session_factory, raw_message_id)}
    assert statuses == {"awaiting_user_confirmation"}
    with session_factory() as session:
        alerts = (
            session.query(RuntimeIncident)
            .filter(
                RuntimeIncident.incident_type == "unresolved_management_item_claimed"
            )
            .all()
        )
    assert len(alerts) == 1, "the refusal was silent"


# The offline replay the ruling asked for, at the width the data allows.
#
# Each block below is the `_context_resolution` and the management-instruction
# evidence as production stored them on 2026-09-11 for the three messages that
# went silent (raw 16078 / 16087 / 16131, KOL messages 10442 / 10443 / 10448).
#
# **What this replay cannot do, and why**: the first-pass `lifecycle_event` --
# which management action the model read -- is not persisted anywhere.
# ``recognition_decisions`` stores the payload *after* the downgrade rewrote it
# to ``{"event_type": "none"}``, and the run and attempt tables keep only
# fingerprints. So the replay asserts the routing decision (this message should
# have been parked and asked about) and not the action the item would carry.
# Persisting the first-pass payload is A-16e.
PRODUCTION_SILENCED = (
    (
        16078,
        ("target_ambiguous",),
        "先拿1000点利润50%，剩余做成本保护继续持有",
    ),
    (
        16087,
        ("target_ambiguous",),
        "仓位比较大的加仓的反弹记得减仓拿利润",
    ),
    (
        16131,
        ("target_ambiguous", "multiple_candidates"),
        "78200多单全部止盈出局，保留77000多单50%仓位冲击止盈位",
    ),
)


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("raw_id", "conflict_types", "instruction"), PRODUCTION_SILENCED
)
def test_the_three_silenced_messages_would_now_be_parked(
    tmp_path, raw_id, conflict_types, instruction
):
    """Every one of them reaches the confirmation channel instead of silence."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / f"replay-{raw_id}.db")
    raw_message_id = _seed_raw_message(session_factory)

    moved = _park(
        session_factory,
        raw_message_id,
        decision=_decision(conflict_types=conflict_types),
        payload=_payload(instruction=instruction),
    )

    assert len(moved) == 1
    rows = _items(session_factory, raw_message_id)
    assert [status for _, _, status, _ in rows] == ["awaiting_user_confirmation"]
    assert rows[0][3]["management_instruction"] == instruction
