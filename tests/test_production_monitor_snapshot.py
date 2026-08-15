from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
import json
import os
import stat

import pytest

from telegram_kol_research.production_monitor_snapshot import (
    ProductionMonitorSnapshotStore,
    SnapshotCollectionEvidence,
    SnapshotGeneration,
)


NOW = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)
UID_HASH = "a" * 64
OTHER_UID_HASH = "b" * 64


def _collection(
    name: str,
    ordinal: int,
    *,
    complete: bool = True,
) -> SnapshotCollectionEvidence:
    identity_key = "posId" if name == "positions" else "ordId"
    rows = () if not complete else ({identity_key: f"{name}-{ordinal}"},)
    return SnapshotCollectionEvidence(
        name=name,
        available=True,
        schema_valid=True,
        complete=complete,
        page_count=1,
        row_count=1,
        rows=rows,
        reason_code=None if complete else "snapshot_pagination_incomplete",
    )


def _generation(
    ordinal: int,
    *,
    uid_scope_hash: str = UID_HASH,
    completed_at: datetime | None = None,
) -> SnapshotGeneration:
    completed = completed_at or NOW - timedelta(minutes=8 - ordinal)
    return SnapshotGeneration(
        generation=ordinal,
        outcome="SUCCESS",
        request_started_at=completed - timedelta(seconds=2),
        request_completed_at=completed,
        uid_scope_hash=uid_scope_hash,
        collections=tuple(
            _collection(name, ordinal)
            for name in ("positions", "open_orders", "pending_trigger_orders")
        ),
    )


def _failure(
    ordinal: int,
    reason: str,
    *,
    uid_scope_hash: str = UID_HASH,
) -> SnapshotGeneration:
    completed = NOW - timedelta(minutes=8 - ordinal)
    return SnapshotGeneration(
        generation=ordinal,
        outcome="FAILURE",
        request_started_at=completed - timedelta(seconds=2),
        request_completed_at=completed,
        uid_scope_hash=uid_scope_hash,
        collections=(),
        failure_code=reason,
    )


def _store(path):
    return ProductionMonitorSnapshotStore(path, now_factory=lambda: NOW)


def test_store_retains_only_three_distinct_complete_generations(tmp_path):
    store = _store(tmp_path / "manifest.json")

    for ordinal in range(4):
        store.seal_success(_generation(ordinal))

    loaded = store.load()
    assert [item.generation for item in loaded.generations] == [1, 2, 3]
    assert loaded.last_success is not None
    assert loaded.last_success.generation == 3
    assert loaded.latest_attempt is not None
    assert loaded.latest_attempt.generation == 3
    assert loaded.uid_scope_hash == UID_HASH
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    with pytest.raises(FrozenInstanceError):
        loaded.schema_version = 2


def test_failure_does_not_refresh_last_success(tmp_path):
    store = _store(tmp_path / "manifest.json")
    store.seal_success(_generation(1))
    first_success = store.load().last_success

    store.seal_failure(_failure(2, "exchange_timeout"))

    loaded = store.load()
    assert loaded.last_success == first_success
    assert loaded.last_success is not None
    assert loaded.last_success.generation == 1
    assert loaded.latest_attempt is not None
    assert loaded.latest_attempt.generation == 2
    assert loaded.latest_attempt.outcome == "FAILURE"
    assert loaded.latest_attempt.failure_code == "exchange_timeout"
    assert [item.generation for item in loaded.generations] == [1]


def test_success_requires_all_collections_complete_in_canonical_order(tmp_path):
    store = _store(tmp_path / "manifest.json")
    incomplete = replace(
        _generation(1),
        collections=(
            _collection("positions", 1),
            _collection("open_orders", 1, complete=False),
            _collection("pending_trigger_orders", 1),
        ),
    )
    out_of_order = replace(
        _generation(1),
        collections=tuple(reversed(_generation(1).collections)),
    )

    with pytest.raises(ValueError, match="complete collection"):
        store.seal_success(incomplete)
    with pytest.raises(ValueError, match="canonical collection order"):
        store.seal_success(out_of_order)
    assert not store.path.exists()


@pytest.mark.parametrize(
    ("collection_name", "identity_key"),
    [
        ("positions", "posId"),
        ("open_orders", "ordId"),
        ("pending_trigger_orders", "ordId"),
    ],
)
def test_success_rejects_duplicate_position_and_order_identities(
    tmp_path, collection_name, identity_key
):
    store = _store(tmp_path / "manifest.json")
    duplicate = SnapshotCollectionEvidence(
        name=collection_name,
        available=True,
        schema_valid=True,
        complete=True,
        page_count=1,
        row_count=2,
        rows=(
            {identity_key: "duplicate"},
            {identity_key: "duplicate"},
        ),
    )
    generation = _generation(1)
    collections = tuple(
        duplicate if item.name == collection_name else item
        for item in generation.collections
    )

    with pytest.raises(ValueError, match="duplicate collection identity"):
        store.seal_success(replace(generation, collections=collections))


def test_store_rejects_account_scope_change_and_non_hash_scope(tmp_path):
    store = _store(tmp_path / "manifest.json")
    store.seal_success(_generation(1))

    with pytest.raises(ValueError, match="account scope mismatch"):
        store.seal_success(_generation(2, uid_scope_hash=OTHER_UID_HASH))
    with pytest.raises(ValueError, match="uid_scope_hash"):
        _store(tmp_path / "other.json").seal_success(
            _generation(1, uid_scope_hash="raw-account-identifier")
        )
    assert store.load().latest_attempt.generation == 1


def test_store_rejects_future_time_time_reversal_and_out_of_order_generation(tmp_path):
    store = _store(tmp_path / "manifest.json")
    store.seal_success(_generation(1))

    with pytest.raises(ValueError, match="strictly increase"):
        store.seal_failure(_failure(1, "exchange_timeout"))
    with pytest.raises(ValueError, match="strictly increase"):
        store.seal_success(_generation(0))
    with pytest.raises(ValueError, match="request timestamps"):
        store.seal_success(
            replace(
                _generation(2),
                request_started_at=NOW - timedelta(minutes=1),
                request_completed_at=NOW - timedelta(minutes=2),
            )
        )
    with pytest.raises(ValueError, match="future"):
        store.seal_success(
            _generation(2, completed_at=NOW + timedelta(microseconds=1))
        )
    assert store.load().latest_attempt.generation == 1


def test_store_rejects_oversized_rows_before_replacing_previous_manifest(tmp_path):
    store = _store(tmp_path / "manifest.json")
    store.seal_success(_generation(1))
    before = store.path.read_bytes()
    generation = _generation(2)
    oversized = replace(
        generation.collections[0],
        rows=({"posId": "position-2", "note": "x" * 70_000},),
    )

    with pytest.raises(ValueError, match="safe size"):
        store.seal_success(
            replace(generation, collections=(oversized, *generation.collections[1:]))
        )

    assert store.path.read_bytes() == before
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []


def test_failure_envelope_is_closed_and_cannot_carry_partial_rows(tmp_path):
    store = _store(tmp_path / "manifest.json")
    partial = _collection("positions", 1, complete=False)

    with pytest.raises(ValueError, match="closed failure_code"):
        store.seal_failure(_failure(1, "Authorization: Bearer secret"))
    with pytest.raises(ValueError, match="partial rows"):
        store.seal_failure(
            replace(
                _failure(1, "snapshot_pagination_incomplete"),
                collections=(replace(partial, rows=({"posId": "partial"},)),),
            )
        )
    assert not store.path.exists()


def test_load_rejects_unknown_missing_duplicate_fields_and_oversized_file(tmp_path):
    path = tmp_path / "manifest.json"
    store = _store(path)
    store.seal_success(_generation(1))
    valid = json.loads(path.read_text(encoding="utf-8"))

    invalid_payloads = []
    unknown = dict(valid, surprise=True)
    invalid_payloads.append(json.dumps(unknown))
    missing = dict(valid)
    missing.pop("latest_attempt")
    invalid_payloads.append(json.dumps(missing))
    invalid_payloads.append('{"schema_version":1,"schema_version":1}')
    invalid_payloads.append("x" * (4 * 1024 * 1024 + 1))

    for raw in invalid_payloads:
        path.write_text(raw, encoding="utf-8")
        os.chmod(path, 0o600)
        with pytest.raises(ValueError, match="snapshot manifest"):
            store.load()


def test_load_and_seal_refuse_manifest_or_parent_symlink_without_touching_target(
    tmp_path,
):
    target = tmp_path / "target.json"
    target.write_text("do-not-touch", encoding="utf-8")
    linked = tmp_path / "manifest.json"
    linked.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        _store(linked).load()
    with pytest.raises(ValueError, match="symlink"):
        _store(linked).seal_success(_generation(1))
    assert target.read_text(encoding="utf-8") == "do-not-touch"

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _store(linked_parent / "manifest.json").seal_success(_generation(1))
    assert list(real_parent.iterdir()) == []


def test_atomic_persistence_fsyncs_file_then_replaces_then_fsyncs_directory(
    tmp_path, monkeypatch
):
    store = _store(tmp_path / "manifest.json")
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(descriptor):
        kind = "directory_fsync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file_fsync"
        events.append(kind)
        return real_fsync(descriptor)

    def recording_replace(*args, **kwargs):
        events.append("replace")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(
        "telegram_kol_research.production_monitor_snapshot.os.fsync",
        recording_fsync,
    )
    monkeypatch.setattr(
        "telegram_kol_research.production_monitor_snapshot.os.replace",
        recording_replace,
    )

    store.seal_success(_generation(1))

    assert events == ["file_fsync", "replace", "directory_fsync"]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_atomic_write_failure_preserves_last_sealed_manifest(tmp_path, monkeypatch):
    store = _store(tmp_path / "manifest.json")
    store.seal_success(_generation(1))
    before = store.path.read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(
        "telegram_kol_research.production_monitor_snapshot.os.replace",
        fail_replace,
    )
    with pytest.raises(OSError, match="injected replace failure"):
        store.seal_success(_generation(2))

    assert store.path.read_bytes() == before
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []
