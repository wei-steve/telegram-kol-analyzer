"""A-16b: a second entry at a price we already hold waits for a person.

2026-09-10: message 10434 was the entry card (BTC, around 77000, long) and
10435, three minutes later, read like a second one. The system opened both, so
that price carried 30 contracts instead of 15 -- and 陈哥's own inventories the
next day both say two BTC longs, 78200 and 77000, never two at 77000. The
ambiguity in the second message is a matter of reading natural language; what
the system can do is notice it is about to double an exposure and ask.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta


NOW = datetime(2026, 9, 10, 15, 11, 0, tzinfo=UTC)
CHAT_ID = -1002337721508


def _seed(session_factory, *, existing_px="77000.0", existing_kind="limit",
          existing_symbol="BTC", existing_side="long",
          existing_created_at=None, existing_message_id=10434):
    """One existing entry (10434) and one fresh candidate (10435)."""

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        RawMessage,
        SignalCandidate,
    )

    created = existing_created_at or (NOW - timedelta(minutes=3))
    with session_factory() as session:
        session.add(RawMessage(
            chat_id=CHAT_ID, message_id=existing_message_id, text="entry card",
            created_at=created.replace(tzinfo=None),
        ))
        new_message = RawMessage(
            chat_id=CHAT_ID, message_id=10435, text="BTC再挂一笔限价中长线多单",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(new_message)
        binding = ExecutionBinding(
            kol_id="kol", chat_id=CHAT_ID, message_id=existing_message_id,
            symbol=existing_symbol, side=existing_side, venue="deepcoin",
            margin_mode="cross", position_mode="split", status="active",
        )
        session.add(binding)
        session.flush()
        session.add(ExecutionOrderLeg(
            execution_binding_id=binding.id, leg_index=1, purpose="entry",
            venue="deepcoin", order_kind=existing_kind, status="active",
            strategy_instance_id="deepcoin:x",
            request_json=json.dumps({"px": existing_px, "sz": "15.0"}),
            created_at=created.replace(tzinfo=None),
        ))
        candidate = SignalCandidate(
            raw_message_id=new_message.id, event_type="entry_signal",
            parse_source="mimo_authoritative", symbol="BTC", side="long",
            entry_text="77000",
        )
        session.add(candidate)
        session.commit()
        return int(new_message.id), int(candidate.id), int(binding.id)


def _find(session_factory, raw_message_id, candidate_id, *, now=NOW):
    from telegram_kol_research.duplicate_entry_confirmation import (
        find_duplicate_entry_binding,
    )
    from telegram_kol_research.models import SignalCandidate

    with session_factory() as session:
        candidate = session.get(SignalCandidate, candidate_id)
        found = find_duplicate_entry_binding(
            session, raw_message_id=raw_message_id, candidate=candidate, now=now
        )
        return int(found.id) if found is not None else None


def test_the_second_entry_at_the_same_price_is_detected(tmp_path):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, binding_id = _seed(session_factory)

    assert _find(session_factory, raw_id, cand_id) == binding_id


def test_the_price_is_compared_as_a_number_not_as_text(tmp_path):
    """6f-1 spent three months on ``"75700.0" != "75700"``."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, binding_id = _seed(session_factory, existing_px="77000.000")

    assert _find(session_factory, raw_id, cand_id) == binding_id


def test_a_different_price_is_not_a_duplicate(tmp_path):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, _ = _seed(session_factory, existing_px="78200.0")

    assert _find(session_factory, raw_id, cand_id) is None


def test_a_different_side_is_not_a_duplicate(tmp_path):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, _ = _seed(session_factory, existing_side="short")

    assert _find(session_factory, raw_id, cand_id) is None


def test_a_different_symbol_is_not_a_duplicate(tmp_path):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, _ = _seed(session_factory, existing_symbol="ETH")

    assert _find(session_factory, raw_id, cand_id) is None


def test_an_entry_older_than_two_hours_is_not_a_duplicate(tmp_path):
    """The window is the rule's whole scope; without it every re-entry is one."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, _ = _seed(
        session_factory, existing_created_at=NOW - timedelta(hours=2, minutes=1)
    )

    assert _find(session_factory, raw_id, cand_id) is None


def test_an_entry_just_inside_two_hours_is_a_duplicate(tmp_path):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, binding_id = _seed(
        session_factory, existing_created_at=NOW - timedelta(hours=1, minutes=59)
    )

    assert _find(session_factory, raw_id, cand_id) == binding_id


def test_the_message_s_own_leg_is_not_a_duplicate_of_itself(tmp_path):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, _ = _seed(session_factory, existing_message_id=10435)

    assert _find(session_factory, raw_id, cand_id) is None


def test_parking_moves_the_item_out_of_every_executable_state(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.duplicate_entry_confirmation import (
        park_duplicate_entry,
    )
    from telegram_kol_research.models import MessageInstructionItem

    session_factory = create_session_factory(tmp_path / "a16b.db")
    raw_id, cand_id, binding_id = _seed(session_factory)
    with session_factory() as session:
        item = MessageInstructionItem(
            raw_message_id=raw_id, signal_candidate_id=cand_id, sequence=0,
            instruction_kind="entry", idempotency_key="k", status="executing",
        )
        session.add(item)
        session.commit()
        item_id = int(item.id)
    parked = park_duplicate_entry(
        session_factory,
        message_instruction_item_id=item_id,
        duplicate_of_execution_binding_id=binding_id,
        duplicate_of_message_id=10434,
        duplicate_symbol="BTC",
        duplicate_side="long",
        now=NOW,
    )

    assert parked is True
    with session_factory() as session:
        row = session.get(MessageInstructionItem, item_id)
        result = json.loads(row.result_json)
        assert row.status == "awaiting_user_confirmation"
        assert result["duplicate_entry_unconfirmed"] is True
        assert result["duplicate_of_execution_binding_id"] == binding_id
        assert result["duplicate_of_message_id"] == 10434


# The offline replay the ruling asked for: 10434 unaffected, 10435 parked.
def test_the_replay_of_10434_and_10435(tmp_path):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "replay.db")
    raw_id, cand_id, binding_id = _seed(session_factory)

    # 10435 (the second message) is recognised as a duplicate of 10434's binding.
    assert _find(session_factory, raw_id, cand_id) == binding_id

    # 10434 itself: its own candidate finds nothing, because the only entry at
    # this price in the window is its own.
    from telegram_kol_research.models import RawMessage, SignalCandidate

    with session_factory() as session:
        first_id = int(
            session.query(RawMessage.id)
            .filter(RawMessage.message_id == 10434)
            .scalar()
        )
        own = SignalCandidate(
            raw_message_id=first_id, event_type="entry_signal",
            parse_source="mimo_authoritative", symbol="BTC", side="long",
            entry_text="77000",
        )
        session.add(own)
        session.commit()
        own_id = int(own.id)

    assert _find(session_factory, first_id, own_id) is None
