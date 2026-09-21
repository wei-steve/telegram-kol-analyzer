"""F3: a position that is no longer open has no stop to verify.

``composite_position_without_verified_stop`` asks a confirmed
``replace_remaining_protection`` component for two verified stops in the
ledger.  That is right while the position is open, and permanently wrong once
it is not: the exchange voids a position's TPSL with the position, and
``retire_protection_for_closed_binding`` retires the ledger rows to match, so
the check reports a critical fault for every such batch forever.

The remainder-close marker (commit ``dde69b31``) already covers the one route
that closes the position on purpose.  It does not cover the position being
closed by anything else afterwards -- its own stop firing, a later full exit, a
manual close -- which is the ordinary end of every successful composite batch.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from telegram_kol_research.production_safety_monitor import (
    read_composite_management_invariants,
)


NOW = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)


def _database(tmp_path, name):
    database = tmp_path / name
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE strategy_management_batches (
          id INTEGER PRIMARY KEY, status TEXT, management_contract_json TEXT
        );
        CREATE TABLE strategy_management_legs (
          id INTEGER PRIMARY KEY, management_batch_id INTEGER,
          execution_order_leg_id INTEGER, pos_id TEXT
        );
        CREATE TABLE strategy_management_components (
          id INTEGER PRIMARY KEY, management_batch_id INTEGER,
          strategy_management_leg_id INTEGER, component_kind TEXT,
          status TEXT, desired_json TEXT, evidence_json TEXT,
          last_progress_at TEXT, updated_at TEXT
        );
        CREATE TABLE position_mutation_intents (
          id INTEGER PRIMARY KEY, idempotency_key TEXT, operation TEXT,
          status TEXT
        );
        CREATE TABLE position_protection_ledger (
          id INTEGER PRIMARY KEY, execution_order_leg_id INTEGER, pos_id TEXT,
          purpose TEXT, size_text TEXT, status TEXT
        );
        """
    )
    contract = json.dumps(
        {"required_components": ["replace_remaining_protection"]}
    )
    now_text = "2026-09-21 07:00:00"
    connection.execute(
        "INSERT INTO strategy_management_batches VALUES (1, 'succeeded', ?)",
        (contract,),
    )
    connection.execute(
        "INSERT INTO strategy_management_legs VALUES (1, 1, 11, 'pos-1')"
    )
    connection.execute(
        "INSERT INTO strategy_management_components "
        "VALUES (1, 1, 1, 'replace_remaining_protection', 'confirmed', '{}', "
        "?, ?, ?)",
        (json.dumps([{"new_stop_order_ids": ["stop-new-primary"]}]), now_text, now_text),
    )
    # The ledger rows were retired when the position closed, which is exactly
    # what the check reads as "no verified stop".
    connection.executemany(
        "INSERT INTO position_protection_ledger VALUES (?, 11, 'pos-1', ?, '3', 'retired')",
        [(1, "stop_loss"), (2, "backup_stop")],
    )
    connection.commit()
    connection.close()
    return database


@pytest.mark.parametrize(
    ("live_position_sizes", "expected"),
    [
        pytest.param(
            {"pos-1": Decimal("0.4")},
            ("composite_position_without_verified_stop",),
            id="an_open_position_still_needs_two_verified_stops",
        ),
        pytest.param(
            {"pos-1": Decimal("0")},
            (),
            id="a_flat_position_has_no_stop_to_verify",
        ),
        pytest.param(
            {"pos-2": Decimal("7")},
            (),
            id="a_position_the_exchange_no_longer_lists_is_gone",
        ),
    ],
)
def test_the_verified_stop_check_follows_the_live_position(
    tmp_path, live_position_sizes, expected
):
    database = _database(tmp_path, f"closed-{len(live_position_sizes)}-{expected}.db")
    before = database.read_bytes()

    codes = read_composite_management_invariants(
        database, now=NOW, live_position_sizes=live_position_sizes
    )

    assert codes == expected
    assert database.read_bytes() == before


def test_without_live_sizes_the_check_still_reports(tmp_path):
    """No live read is not "the position is gone"; unknown never excuses."""

    database = _database(tmp_path, "closed-unknown.db")

    codes = read_composite_management_invariants(database, now=NOW)

    assert codes == ("composite_position_without_verified_stop",)
