import os
from pathlib import Path
import sqlite3
import stat

import pytest

from telegram_kol_research.production_monitor_db_stage import stage_sqlite_snapshot


def test_stage_sqlite_snapshot_includes_committed_wal_and_is_atomic_mode_0600(tmp_path):
    source = tmp_path / "source" / "research.db"
    destination = tmp_path / "private" / "research-snapshot.db"
    source.parent.mkdir()
    destination.parent.mkdir(mode=0o700)
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
    writer.execute("INSERT INTO evidence VALUES ('committed-in-wal')")
    writer.commit()
    assert source.with_name("research.db-wal").exists()

    stage_sqlite_snapshot(
        source,
        destination,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as reader:
        assert reader.execute("PRAGMA query_only").fetchone() == (0,)
        assert reader.execute("SELECT value FROM evidence").fetchall() == [
            ("committed-in-wal",)
        ]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.stat().st_nlink == 1
    assert not destination.with_name("research-snapshot.db-wal").exists()
    writer.close()


def test_stage_sqlite_snapshot_rejects_symlink_source_or_destination(tmp_path):
    source = tmp_path / "research.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE evidence(value TEXT)")
    source_link = tmp_path / "source-link.db"
    source_link.symlink_to(source)
    private = tmp_path / "private"
    private.mkdir()
    destination = private / "snapshot.db"

    with pytest.raises(ValueError, match="source"):
        stage_sqlite_snapshot(
            source_link,
            destination,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    destination.symlink_to(source)
    with pytest.raises(ValueError, match="destination"):
        stage_sqlite_snapshot(
            source,
            destination,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
