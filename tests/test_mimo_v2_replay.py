from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from telegram_kol_research.mimo_v2_replay import (
    MimoV2ReplayInputError,
    create_read_only_replay_snapshot,
    load_replay_message_ids,
    validate_replay_inputs,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage


def _write_ids(tmp_path: Path, content: str = "7\n") -> Path:
    path = tmp_path / "approved-ids.txt"
    path.write_text(content, encoding="utf-8")
    return path


def _valid_boundaries(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source_database = tmp_path / "source.db"
    source_database.write_bytes(b"SQLite fixture placeholder")
    message_id_file = _write_ids(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    artifact_dir = tmp_path / "artifacts"
    return source_database, message_id_file, media_root, artifact_dir


def _seed_source_database(tmp_path: Path) -> tuple[Path, int]:
    source = tmp_path / "source.db"
    session_factory = create_session_factory(source)
    with session_factory() as session:
        message = RawMessage(chat_id=77, message_id=91, text="private replay text")
        session.add(message)
        session.commit()
        message_id = int(message.id)
    session_factory.kw["bind"].dispose()
    return source, message_id


def _source_component_signatures(source: Path) -> dict[str, tuple[bytes, int, int, int]]:
    signatures: dict[str, tuple[bytes, int, int, int]] = {}
    for suffix in ("", "-wal", "-shm"):
        path = source.with_name(source.name + suffix)
        if not path.exists():
            continue
        stat = path.stat()
        signatures[suffix or "main"] = (
            path.read_bytes(),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_mode,
        )
    return signatures


def test_load_replay_message_ids_is_bounded_and_stable(tmp_path):
    source = _write_ids(tmp_path, "7\n# incident\n9\n7\n")

    assert load_replay_message_ids(source, max_messages=2) == (7, 9)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1 2"])
def test_load_replay_message_ids_rejects_malformed_values(tmp_path, value):
    source = _write_ids(tmp_path, value)

    with pytest.raises(MimoV2ReplayInputError, match="message_id_invalid"):
        load_replay_message_ids(source, max_messages=200)


def test_load_replay_message_ids_rejects_empty_file(tmp_path):
    source = _write_ids(tmp_path, "\n# none\n")

    with pytest.raises(MimoV2ReplayInputError, match="message_id_list_empty"):
        load_replay_message_ids(source, max_messages=200)


def test_load_replay_message_ids_rejects_more_than_bound(tmp_path):
    source = _write_ids(tmp_path, "1\n2\n3\n")

    with pytest.raises(MimoV2ReplayInputError, match="message_id_limit_exceeded"):
        load_replay_message_ids(source, max_messages=2)


@pytest.mark.parametrize("max_messages", [0, 201, True])
def test_load_replay_message_ids_rejects_invalid_bound(tmp_path, max_messages):
    source = _write_ids(tmp_path)

    with pytest.raises(MimoV2ReplayInputError, match="max_messages_invalid"):
        load_replay_message_ids(source, max_messages=max_messages)


def test_validate_replay_path_boundary_creates_private_artifact_dir(tmp_path):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )

    inputs = validate_replay_inputs(
        source_database=source_database,
        message_id_file=message_id_file,
        media_root=media_root,
        artifact_dir=artifact_dir,
        max_messages=200,
    )

    assert inputs.raw_message_ids == (7,)
    assert inputs.artifact_dir == artifact_dir.resolve()
    assert artifact_dir.is_dir()
    assert artifact_dir.stat().st_mode & 0o777 == 0o700


def test_validate_replay_path_boundary_accepts_existing_empty_directory(tmp_path):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )
    artifact_dir.mkdir()

    inputs = validate_replay_inputs(
        source_database=source_database,
        message_id_file=message_id_file,
        media_root=media_root,
        artifact_dir=artifact_dir,
        max_messages=200,
    )

    assert inputs.artifact_dir == artifact_dir.resolve()
    assert list(artifact_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        ("source_database", "source_database_invalid"),
        ("message_id_file", "message_id_file_invalid"),
        ("media_root", "media_root_invalid"),
    ],
)
def test_validate_replay_path_boundary_rejects_missing_input(
    tmp_path,
    boundary,
    reason,
):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )
    values = {
        "source_database": source_database,
        "message_id_file": message_id_file,
        "media_root": media_root,
    }
    values[boundary].unlink() if values[boundary].is_file() else values[boundary].rmdir()

    with pytest.raises(MimoV2ReplayInputError, match=reason):
        validate_replay_inputs(
            **values,
            artifact_dir=artifact_dir,
            max_messages=200,
        )


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        ("source_database", "source_database_invalid"),
        ("message_id_file", "message_id_file_invalid"),
        ("media_root", "media_root_invalid"),
        ("artifact_dir", "artifact_dir_invalid"),
    ],
)
def test_validate_replay_path_boundary_rejects_symlink(
    tmp_path,
    boundary,
    reason,
):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )
    targets = {
        "source_database": source_database,
        "message_id_file": message_id_file,
        "media_root": media_root,
        "artifact_dir": tmp_path / "real-artifacts",
    }
    if boundary == "artifact_dir":
        targets[boundary].mkdir()
    links = {
        key: tmp_path / f"{key}-link"
        for key in targets
    }
    links[boundary].symlink_to(
        targets[boundary],
        target_is_directory=targets[boundary].is_dir(),
    )
    values = {
        "source_database": source_database,
        "message_id_file": message_id_file,
        "media_root": media_root,
        "artifact_dir": artifact_dir,
    }
    values[boundary] = links[boundary]

    with pytest.raises(MimoV2ReplayInputError, match=reason):
        validate_replay_inputs(**values, max_messages=200)


def test_validate_replay_path_boundary_rejects_nonempty_artifact_dir(tmp_path):
    source_database, message_id_file, media_root, artifact_dir = (
        _valid_boundaries(tmp_path)
    )
    artifact_dir.mkdir()
    (artifact_dir / "existing.json").write_text("{}", encoding="utf-8")

    with pytest.raises(MimoV2ReplayInputError, match="artifact_dir_not_empty"):
        validate_replay_inputs(
            source_database=source_database,
            message_id_file=message_id_file,
            media_root=media_root,
            artifact_dir=artifact_dir,
            max_messages=200,
        )


def test_online_snapshot_reads_source_without_modifying_it(tmp_path):
    source, message_id = _seed_source_database(tmp_path)
    before = _source_component_signatures(source)
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    working = private_root / "working.db"

    create_read_only_replay_snapshot(source, working)

    assert _source_component_signatures(source) == before
    with sqlite3.connect(working) as connection:
        assert connection.execute(
            "SELECT id FROM raw_messages WHERE id = ?",
            (message_id,),
        ).fetchone() == (message_id,)


def test_online_snapshot_rejects_existing_destination(tmp_path):
    source, _ = _seed_source_database(tmp_path)
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    working = private_root / "working.db"
    working.write_bytes(b"do not overwrite")

    with pytest.raises(MimoV2ReplayInputError, match="snapshot_destination_invalid"):
        create_read_only_replay_snapshot(source, working)

    assert working.read_bytes() == b"do not overwrite"


def test_online_snapshot_requires_private_real_parent(tmp_path):
    source, _ = _seed_source_database(tmp_path)
    public_root = tmp_path / "public"
    public_root.mkdir(mode=0o755)

    with pytest.raises(MimoV2ReplayInputError, match="snapshot_destination_invalid"):
        create_read_only_replay_snapshot(source, public_root / "working.db")
