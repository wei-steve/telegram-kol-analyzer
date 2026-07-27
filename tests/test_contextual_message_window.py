import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from telegram_kol_research.contextual_message_window import (
    build_contextual_message_window,
    fetch_missing_reply_target,
    render_authoritative_context,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_evidence import save_message_evidence_version
from telegram_kol_research.models import (
    RawMessage,
    StrategyLifecycle,
    StrategyMessageLink,
    StrategyThread,
)


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 27, hour, tzinfo=UTC)


def test_reply_chain_keeps_exact_ancestors_and_evidence_versions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        root = RawMessage(
            chat_id=7,
            message_id=100,
            posted_at=_at(1),
            text="BTC 做多",
        )
        update = RawMessage(
            chat_id=7,
            message_id=101,
            posted_at=_at(2),
            text="更新止损",
            reply_to_message_id=100,
        )
        current = RawMessage(
            chat_id=7,
            message_id=102,
            posted_at=_at(3),
            text="先取消",
            reply_to_message_id=101,
        )
        session.add_all([root, update, current])
        session.commit()
        root_id = root.id
        current_id = current.id
    evidence = save_message_evidence_version(
        session_factory,
        raw_message_id=root_id,
        input_fingerprint="sha256:root",
        model="mimo-v2.5",
        prompt_versions={"evidence": "v1"},
        extraction_status="completed",
        confidence=0.9,
        text_evidence={"observed_text": "BTC 做多", "fields": {}},
        image_evidence={"images": []},
        normalized_evidence={"symbol": "BTC", "side": "long"},
    )

    with session_factory() as session:
        window = build_contextual_message_window(
            session,
            raw_message_id=current_id,
        )

    assert [item.message_id for item in window.reply_chain] == [101, 100]
    assert window.reply_chain[1].evidence_version_id == evidence.id
    assert window.reply_chain[0].posted_at is not None
    rendered = render_authoritative_context(window)
    assert "Reply context" in rendered
    assert '"message_id": 100' in rendered
    assert "2026-07-27T01:00:00" in rendered


def test_recent_window_is_bounded_to_72_hours_and_50_messages(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=8,
                message_id=1,
                posted_at=now - timedelta(hours=73),
                text="too old",
            )
        )
        for message_id in range(2, 62):
            session.add(
                RawMessage(
                    chat_id=8,
                    message_id=message_id,
                    posted_at=now - timedelta(minutes=62 - message_id),
                    text=f"message {message_id}",
                )
            )
        current = RawMessage(
            chat_id=8,
            message_id=62,
            posted_at=now,
            text="更新",
        )
        session.add(current)
        session.commit()
        current_id = current.id

    with session_factory() as session:
        window = build_contextual_message_window(
            session,
            raw_message_id=current_id,
            max_age_hours=72,
            max_messages=50,
        )

    assert len(window.messages) == 50
    assert window.messages[0].message_id == 12
    assert all(item.text != "too old" for item in window.messages)
    assert [item.message_id for item in window.messages] == sorted(
        item.message_id for item in window.messages
    )


def test_reply_chain_keeps_five_ancestors_even_outside_recent_window(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    current_at = datetime(2026, 7, 27, 12, tzinfo=UTC)
    with session_factory() as session:
        for offset, message_id in enumerate(range(500, 506)):
            session.add(
                RawMessage(
                    chat_id=81,
                    message_id=message_id,
                    posted_at=current_at - timedelta(hours=120 - offset),
                    text=f"ancestor {message_id}",
                    reply_to_message_id=(message_id - 1 if message_id > 500 else None),
                )
            )
        current = RawMessage(
            chat_id=81,
            message_id=506,
            posted_at=current_at,
            text="取消引用策略",
            reply_to_message_id=505,
        )
        session.add(current)
        session.commit()
        current_id = current.id

    with session_factory() as session:
        window = build_contextual_message_window(
            session,
            raw_message_id=current_id,
            max_age_hours=72,
            max_reply_depth=5,
        )

    assert window.messages == ()
    assert [item.message_id for item in window.reply_chain] == [
        505,
        504,
        503,
        502,
        501,
    ]
    assert "reply_depth_exceeded" in window.errors


def test_context_messages_include_existing_strategy_thread_links(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        root = RawMessage(
            chat_id=82,
            message_id=600,
            posted_at=_at(1),
            text="BTC 做多",
        )
        current = RawMessage(
            chat_id=82,
            message_id=601,
            posted_at=_at(2),
            text="更新止损",
            reply_to_message_id=600,
        )
        session.add_all([root, current])
        session.flush()
        thread = StrategyThread(
            chat_id=82,
            root_message_id=600,
            symbol="BTC",
            side="long",
        )
        session.add(thread)
        session.flush()
        session.add(
            StrategyMessageLink(
                strategy_thread_id=thread.id,
                raw_message_id=root.id,
                relation_kind="root",
                resolver="test",
                confidence=1.0,
                evidence_json="{}",
                decision_version="v1",
            )
        )
        session.commit()
        current_id = current.id
        thread_id = thread.id

    with session_factory() as session:
        window = build_contextual_message_window(
            session,
            raw_message_id=current_id,
        )

    root_context = window.reply_chain[0]
    assert len(root_context.strategy_links) == 1
    assert root_context.strategy_links[0].strategy_thread_id == thread_id
    assert root_context.strategy_links[0].relation_kind == "root"


def test_reply_cycle_and_missing_target_are_reported_without_guessing(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        first = RawMessage(
            chat_id=9,
            message_id=200,
            posted_at=_at(1),
            text="first",
            reply_to_message_id=201,
        )
        second = RawMessage(
            chat_id=9,
            message_id=201,
            posted_at=_at(2),
            text="second",
            reply_to_message_id=200,
        )
        missing = RawMessage(
            chat_id=9,
            message_id=202,
            posted_at=_at(3),
            text="missing reply",
            reply_to_message_id=999,
        )
        session.add_all([first, second, missing])
        session.commit()
        second_id = second.id
        missing_id = missing.id

    with session_factory() as session:
        cycle = build_contextual_message_window(
            session,
            raw_message_id=second_id,
        )
        unresolved = build_contextual_message_window(
            session,
            raw_message_id=missing_id,
        )

    assert [item.message_id for item in cycle.reply_chain] == [200]
    assert "reply_cycle_detected" in cycle.errors
    assert unresolved.reply_chain[0].message_id == 999
    assert unresolved.reply_chain[0].resolution_status == "missing"
    assert "reply_target_unavailable" in unresolved.errors


def test_active_strategies_include_timestamped_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        current = RawMessage(
            chat_id=10,
            message_id=300,
            posted_at=_at(3),
            text="有入场的保护成本",
        )
        session.add(current)
        session.flush()
        session.add(
            StrategyLifecycle(
                chat_id=10,
                message_id=299,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=_at(1),
                entered_at=_at(2),
            )
        )
        session.commit()
        current_id = current.id

    with session_factory() as session:
        window = build_contextual_message_window(
            session,
            raw_message_id=current_id,
        )

    assert len(window.active_strategies) == 1
    strategy = window.active_strategies[0]
    assert strategy.source_message_id == 299
    assert strategy.signal_at.startswith("2026-07-27T01:00:00")


def test_missing_reply_target_is_fetched_through_raw_ingest(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    class Client:
        async def get_messages(self, chat_id, ids):
            assert chat_id == 11
            assert ids == 4004
            return SimpleNamespace(
                id=4004,
                sender_id=99,
                message="BTC 限价多单",
                reply_to_msg_id=None,
                date=_at(1),
                edit_date=None,
                media=None,
                get_sender=lambda: SimpleNamespace(
                    first_name="Trader",
                    last_name=None,
                    username="trader",
                ),
            )

    fetched = asyncio.run(
        fetch_missing_reply_target(
            Client(),
            session_factory=session_factory,
            chat_id=11,
            message_id=4004,
            media_root=tmp_path / "media",
        )
    )

    assert fetched is True
    with session_factory() as session:
        row = (
            session.query(RawMessage)
            .filter(
                RawMessage.chat_id == 11,
                RawMessage.message_id == 4004,
            )
            .one()
        )
    assert row.text == "BTC 限价多单"
