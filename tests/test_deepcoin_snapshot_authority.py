from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_execution_operations import (
    advance_account_write_generation,
)
from telegram_kol_research.deepcoin_snapshot_authority import (
    build_exchange_collection_evidence,
    capture_account_snapshot,
)


UID_HASH = "c" * 64


def _rows(count: int):
    return [{"ordId": f"order-{index:03d}", "state": "live"} for index in range(count)]


def test_collection_exception_and_invalid_schema_are_unavailable():
    failed = build_exchange_collection_evidence(
        endpoint="trigger_orders_pending",
        response=None,
        read_error=RuntimeError("Authorization: Bearer secret"),
    )
    invalid = build_exchange_collection_evidence(
        endpoint="trigger_orders_pending",
        response={"data": [{"ordId": "ok"}, "not-a-row"]},
    )

    assert failed.available is False
    assert failed.schema_valid is False
    assert failed.complete is False
    assert failed.rows == ()
    assert failed.reason_code == "snapshot_read_unavailable"
    assert "secret" not in repr(failed).lower()
    assert invalid.available is True
    assert invalid.schema_valid is False
    assert invalid.complete is False
    assert invalid.rows == ()
    assert invalid.reason_code == "snapshot_schema_invalid"


def test_row_limit_requires_affirmative_completion_and_visibility_requires_complete():
    ninety_nine = build_exchange_collection_evidence(
        endpoint="trigger_orders_pending",
        response={"data": _rows(99)},
        expected_order_ids={"order-098"},
    )
    one_hundred = build_exchange_collection_evidence(
        endpoint="trigger_orders_pending",
        response={"data": _rows(100)},
        expected_order_ids={"order-099"},
    )
    affirmed = build_exchange_collection_evidence(
        endpoint="trigger_orders_pending",
        response={"data": _rows(100), "hasMore": False},
        expected_order_ids={"order-099"},
    )

    assert ninety_nine.complete is True
    assert ninety_nine.expected_order_ids_visible is True
    assert one_hundred.complete is False
    assert one_hundred.reason_code == "snapshot_page_limit_ambiguous"
    assert one_hundred.expected_order_ids_visible is False
    assert affirmed.complete is True
    assert affirmed.expected_order_ids_visible is True


@pytest.mark.parametrize(
    "metadata",
    [
        {"nextCursor": "cursor-2"},
        {"cursor": "cursor-1"},
        {"page": 1, "total": 200},
        {"hasMore": "unknown"},
        {"nextPageCursor": "cursor-2"},
        {"has_more": True},
    ],
)
def test_unsupported_or_unfinished_pagination_is_incomplete(metadata):
    evidence = build_exchange_collection_evidence(
        endpoint="trigger_orders_pending",
        response={"data": [{"ordId": "order-1"}], **metadata},
    )

    assert evidence.complete is False
    assert evidence.reason_code == "snapshot_pagination_incomplete"


def test_collection_fingerprint_is_order_independent_and_content_sensitive():
    first = build_exchange_collection_evidence(
        endpoint="open_orders",
        response={"data": [{"ordId": "a", "sz": "1"}, {"ordId": "b", "sz": "2"}]},
    )
    reordered = build_exchange_collection_evidence(
        endpoint="open_orders",
        response={"data": [{"sz": "2", "ordId": "b"}, {"sz": "1", "ordId": "a"}]},
    )
    changed = build_exchange_collection_evidence(
        endpoint="open_orders",
        response={"data": [{"ordId": "a", "sz": "1"}, {"ordId": "b", "sz": "3"}]},
    )

    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint != changed.fingerprint
    assert 'ordId' not in repr(first)
    assert '"a"' not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.complete = False


def test_deep_or_nonfinite_rows_are_schema_invalid_without_traceback():
    deep: object = "leaf"
    for _ in range(20):
        deep = {"nested": deep}

    for row in ({"nested": deep}, {"price": float("nan")}):
        evidence = build_exchange_collection_evidence(
            endpoint="positions",
            response={"data": [row]},
        )
        assert evidence.schema_valid is False
        assert evidence.complete is False
        assert evidence.rows == ()


def test_generation_drift_invalidates_whole_snapshot_even_if_writer_finishes(tmp_path):
    session_factory = create_session_factory(tmp_path / "snapshot-generation.db")

    def writer_during_capture():
        advance_account_write_generation(session_factory, uid_scope_hash=UID_HASH)
        advance_account_write_generation(session_factory, uid_scope_hash=UID_HASH)
        return {"data": [{"posId": "position-1"}]}

    snapshot = capture_account_snapshot(
        session_factory,
        uid_scope_hash=UID_HASH,
        readers={
            "positions": writer_during_capture,
            "open_orders": lambda: {"data": []},
        },
    )

    assert snapshot.start_write_generation == 0
    assert snapshot.end_write_generation == 2
    assert snapshot.complete is False
    assert snapshot.reason_code == "snapshot_write_generation_changed"
    assert all(collection.complete is False for collection in snapshot.collections)


def test_writer_already_in_progress_invalidates_equal_odd_generations(tmp_path):
    session_factory = create_session_factory(tmp_path / "snapshot-inflight-writer.db")
    pre_writer = advance_account_write_generation(
        session_factory, uid_scope_hash=UID_HASH
    )
    assert pre_writer.generation == 1

    snapshot = capture_account_snapshot(
        session_factory,
        uid_scope_hash=UID_HASH,
        readers={"positions": lambda: {"data": []}},
    )

    assert snapshot.start_write_generation == 1
    assert snapshot.end_write_generation == 1
    assert snapshot.complete is False
    assert snapshot.reason_code == "snapshot_write_in_progress"
    assert snapshot.collections[0].complete is False


def test_loader_retains_each_endpoint_failure_and_never_converts_it_to_absence(tmp_path):
    session_factory = create_session_factory(tmp_path / "snapshot-errors.db")

    def fail():
        raise RuntimeError("DC-ACCESS-KEY: sensitive")

    snapshot = capture_account_snapshot(
        session_factory,
        uid_scope_hash=UID_HASH,
        readers={
            "positions": lambda: {"data": []},
            "trigger_orders_pending": fail,
            "order_history": lambda: {"data": "not-a-list"},
        },
    )

    by_endpoint = {item.endpoint: item for item in snapshot.collections}
    assert snapshot.complete is False
    assert by_endpoint["positions"].complete is True
    assert by_endpoint["trigger_orders_pending"].available is False
    assert by_endpoint["order_history"].schema_valid is False
    assert "sensitive" not in repr(snapshot).lower()
