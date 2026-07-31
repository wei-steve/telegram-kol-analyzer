from __future__ import annotations

from datetime import UTC, datetime
import json

from telegram_kol_research.live_position_snapshot import (
    LivePositionSnapshotStore,
)


CAPTURED_AT = datetime(2026, 7, 31, 8, 15, tzinfo=UTC)


def test_snapshot_store_persists_nested_datetimes_and_version(tmp_path):
    path = tmp_path / "positions.json"
    store = LivePositionSnapshotStore(path)

    saved = store.finish_success(
        {
            "positions": [
                {
                    "pos_id": "pos-1",
                    "checked_at": CAPTURED_AT,
                    "protection": [{"created_at": CAPTURED_AT}],
                }
            ],
            "error": None,
        },
        captured_at=CAPTURED_AT,
    )

    loaded = LivePositionSnapshotStore(path).read()

    assert loaded is not None
    assert loaded.version == saved.version
    assert loaded.captured_at == CAPTURED_AT
    assert loaded.payload["positions"][0]["checked_at"] == CAPTURED_AT
    assert (
        loaded.payload["positions"][0]["protection"][0]["created_at"]
        == CAPTURED_AT
    )


def test_snapshot_store_ignores_corrupt_persisted_file(tmp_path):
    path = tmp_path / "positions.json"
    path.write_text("{not-json", encoding="utf-8")

    store = LivePositionSnapshotStore(path)

    assert store.read() is None
    assert store.last_load_error is not None


def test_failed_refresh_preserves_last_success_and_releases_single_flight(tmp_path):
    store = LivePositionSnapshotStore(tmp_path / "positions.json")
    saved = store.finish_success(
        {"positions": [{"pos_id": "pos-1"}], "error": None},
        captured_at=CAPTURED_AT,
    )

    assert store.begin_refresh() is True
    assert store.begin_refresh() is False

    store.finish_failure("upstream unavailable")
    current = store.read()

    assert current is not None
    assert current.version == saved.version
    assert current.payload == saved.payload
    assert current.last_error == "upstream unavailable"
    assert current.refreshing is False
    assert store.begin_refresh() is True


def test_snapshot_store_returns_deep_copies(tmp_path):
    store = LivePositionSnapshotStore(tmp_path / "positions.json")
    store.finish_success(
        {"positions": [{"pos_id": "pos-1", "attribution": {"state": "bound"}}]},
        captured_at=CAPTURED_AT,
    )

    first = store.read()
    assert first is not None
    first.payload["positions"][0]["attribution"]["state"] = "changed"

    second = store.read()

    assert second is not None
    assert second.payload["positions"][0]["attribution"]["state"] == "bound"


def test_finish_success_atomically_replaces_existing_file(tmp_path):
    path = tmp_path / "positions.json"
    store = LivePositionSnapshotStore(path)
    first = store.finish_success(
        {"positions": [{"pos_id": "old"}]},
        captured_at=CAPTURED_AT,
    )
    second = store.finish_success(
        {"positions": []},
        captured_at=datetime(2026, 7, 31, 8, 16, tzinfo=UTC),
    )

    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert second.version != first.version
    assert persisted["version"] == second.version
    assert persisted["payload"]["positions"] == []
    assert not list(tmp_path.glob("*.tmp"))
