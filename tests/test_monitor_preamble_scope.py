"""Entry-preamble invariants must be scoped to auto-trade groups.

A ``notify_only`` group never places orders, so an unresolved preamble there has
no execution consequence.  Before this scoping existed a single such preamble
kept the production monitor permanently unhealthy, which hides real findings.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.production_safety_monitor import (
    _evaluate_entry_preamble_invariants,
    _projected_auto_trade_chat_ids,
    read_entry_preamble_invariants,
)

NOW = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
AUTO_TRADE_CHAT = -1003048800035
NOTIFY_ONLY_CHAT = -1003095914903


def _build_database(tmp_path, *, chats):
    """Seed one stale pending preamble per requested chat id."""

    database_path = tmp_path / "research.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE raw_messages (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            message_id INTEGER,
            posted_at TEXT
        );
        CREATE TABLE entry_preambles (
            id INTEGER PRIMARY KEY,
            raw_message_id INTEGER,
            chat_id INTEGER,
            symbol TEXT,
            side TEXT,
            status TEXT,
            created_at TEXT
        );
        CREATE TABLE signal_candidates (
            id INTEGER PRIMARY KEY,
            raw_message_id INTEGER,
            event_type TEXT,
            management_action TEXT
        );
        CREATE TABLE entry_strategy_assemblies (
            id INTEGER PRIMARY KEY,
            strategy_instance_id TEXT,
            fingerprint TEXT
        );
        CREATE TABLE execution_bindings (
            id INTEGER PRIMARY KEY,
            strategy_instance_id TEXT,
            payload_json TEXT
        );
        """
    )
    stale_at = (NOW - timedelta(hours=12)).replace(tzinfo=None)
    for index, chat_id in enumerate(chats, start=1):
        connection.execute(
            "INSERT INTO raw_messages (id, chat_id, message_id, posted_at)"
            " VALUES (?, ?, ?, ?)",
            (index, chat_id, 1000 + index, stale_at.isoformat(sep=" ")),
        )
        connection.execute(
            "INSERT INTO entry_preambles"
            " (id, raw_message_id, chat_id, symbol, side, status, created_at)"
            " VALUES (?, ?, ?, 'BTC', 'short', 'pending', ?)",
            (index, index, chat_id, stale_at.isoformat(sep=" ")),
        )
    connection.commit()
    connection.close()
    return database_path


def test_stale_preamble_in_auto_trade_group_still_reported(tmp_path):
    database_path = _build_database(tmp_path, chats=[AUTO_TRADE_CHAT])

    codes = read_entry_preamble_invariants(
        database_path, now=NOW, auto_trade_chat_ids=(AUTO_TRADE_CHAT,)
    )

    assert "stale_entry_preamble_unresolved" in codes
    assert "entry_preamble_scope_unavailable" not in codes


def test_stale_preamble_in_notify_only_group_does_not_turn_monitor_red(tmp_path):
    database_path = _build_database(tmp_path, chats=[NOTIFY_ONLY_CHAT])

    codes = read_entry_preamble_invariants(
        database_path, now=NOW, auto_trade_chat_ids=(AUTO_TRADE_CHAT,)
    )

    assert "stale_entry_preamble_unresolved" not in codes
    # The finding stays observable rather than being silently discarded.
    assert "entry_preamble_unresolved_notify_only" in codes


def test_notify_only_observation_is_not_a_reason_code():
    reasons: set[str] = set()
    details: dict[str, object] = {}

    _evaluate_entry_preamble_invariants(
        ("entry_preamble_unresolved_notify_only",), reasons, details
    )

    assert reasons == set()
    assert details["entry_preamble_observations"] == (
        "entry_preamble_unresolved_notify_only",
    )


def test_unavailable_scope_keeps_full_detection_and_flags_degradation(tmp_path):
    database_path = _build_database(
        tmp_path, chats=[AUTO_TRADE_CHAT, NOTIFY_ONLY_CHAT]
    )

    codes = read_entry_preamble_invariants(
        database_path, now=NOW, auto_trade_chat_ids=None
    )

    # Fail closed: without the scope we must not silently drop every finding.
    assert "stale_entry_preamble_unresolved" in codes
    assert "entry_preamble_scope_unavailable" in codes


def test_scope_degradation_is_a_reason_code():
    reasons: set[str] = set()
    details: dict[str, object] = {}

    _evaluate_entry_preamble_invariants(
        ("entry_preamble_scope_unavailable",), reasons, details
    )

    assert "entry_preamble_scope_unavailable" in reasons


def test_omitting_scope_keeps_legacy_behaviour_without_false_degradation(
    tmp_path,
):
    """Omitting the argument must differ from a failed scope lookup."""

    database_path = _build_database(tmp_path, chats=[NOTIFY_ONLY_CHAT])

    codes = read_entry_preamble_invariants(database_path, now=NOW)

    assert "stale_entry_preamble_unresolved" in codes
    assert "entry_preamble_scope_unavailable" not in codes


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not-a-list",
        b"bytes",
        ["-1003048800035"],
        [-1003048800035, None],
        [True],
    ],
)
def test_unusable_scope_payloads_project_to_none(value):
    """Anything we cannot fully trust must degrade, never read as 'empty'."""

    assert _projected_auto_trade_chat_ids(value) is None


def test_valid_scope_payload_is_normalised():
    assert _projected_auto_trade_chat_ids(
        [-1003048800035, -1002409877375, -1003048800035]
    ) == (-1003048800035, -1002409877375)


def test_empty_scope_payload_is_honoured_not_treated_as_missing():
    """A genuinely empty auto-trade set is a real answer, not a failure."""

    assert _projected_auto_trade_chat_ids([]) == ()
