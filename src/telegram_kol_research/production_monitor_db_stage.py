"""Create a coherent, closed-scope SQLite snapshot for monitor readers."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import tempfile
from urllib.parse import quote


PRODUCTION_DATABASE = Path("/opt/telegram-kol-analyzer/data/research.db")
MONITOR_DATABASE_DESTINATIONS = {
    "sentinel": Path(
        "/var/cache/telegram-kol-monitor-v2/sentinel/research-snapshot.db"
    ),
    "audit": Path("/var/cache/telegram-kol-monitor-v2/audit/research-snapshot.db"),
}
MONITOR_READER_IDENTITY = "telegram-kol-monitor-sentinel"


def _sqlite_read_only_uri(path: Path) -> str:
    return f"file:{quote(path.as_posix(), safe='/')}?mode=ro"


def stage_sqlite_snapshot(
    source: str | Path,
    destination: str | Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    """Back up one SQLite database into a sealed, atomically replaced file."""

    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.is_symlink():
        raise ValueError("database stage source must not be a symlink")
    source_metadata = source_path.stat()
    if not stat.S_ISREG(source_metadata.st_mode):
        raise ValueError("database stage source must be a regular file")
    parent = destination_path.parent
    if parent.is_symlink():
        raise ValueError("database stage destination parent is unsafe")
    parent_metadata = parent.stat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or parent_metadata.st_uid != owner_uid
        or parent_metadata.st_gid != owner_gid
    ):
        raise ValueError("database stage destination parent metadata is invalid")
    if destination_path.is_symlink():
        raise ValueError("database stage destination must not be a symlink")
    if destination_path.exists():
        destination_metadata = destination_path.stat()
        if (
            not stat.S_ISREG(destination_metadata.st_mode)
            or stat.S_IMODE(destination_metadata.st_mode) != 0o600
            or destination_metadata.st_uid != owner_uid
            or destination_metadata.st_gid != owner_gid
            or destination_metadata.st_nlink != 1
        ):
            raise ValueError("database stage destination metadata is invalid")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{destination_path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        with closing(
            sqlite3.connect(
                _sqlite_read_only_uri(source_path),
                uri=True,
                timeout=5.0,
            )
        ) as source_connection:
            source_connection.execute("PRAGMA query_only=ON")
            if source_connection.execute("PRAGMA query_only").fetchone() != (1,):
                raise ValueError("database stage source is not query-only")
            with closing(
                sqlite3.connect(temporary, timeout=5.0)
            ) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                journal_mode = destination_connection.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()
                if journal_mode is None or str(journal_mode[0]).lower() != "delete":
                    raise ValueError("database stage snapshot could not be sealed")
        with closing(
            sqlite3.connect(
                _sqlite_read_only_uri(temporary),
                uri=True,
                timeout=5.0,
            )
        ) as verification_connection:
            verification_connection.execute("PRAGMA query_only=ON")
            if verification_connection.execute("PRAGMA quick_check").fetchone() != (
                "ok",
            ):
                raise ValueError("database stage snapshot verification failed")
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination_path)
        temporary = Path()
        parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary != Path():
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def stage_production_monitor_database(consumer: str) -> Path:
    """Stage the fixed production database for one closed monitor consumer."""

    if os.geteuid() != 0:
        raise PermissionError("production monitor database staging requires root")
    try:
        destination = MONITOR_DATABASE_DESTINATIONS[consumer]
    except KeyError as exc:
        raise ValueError("database stage consumer is invalid") from exc
    identity = pwd.getpwnam(MONITOR_READER_IDENTITY)
    if identity.pw_uid == 0:
        raise ValueError("monitor reader identity must be unprivileged")
    stage_sqlite_snapshot(
        PRODUCTION_DATABASE,
        destination,
        owner_uid=identity.pw_uid,
        owner_gid=identity.pw_gid,
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage one coherent production-monitor SQLite snapshot."
    )
    parser.add_argument(
        "--consumer",
        required=True,
        choices=tuple(MONITOR_DATABASE_DESTINATIONS),
    )
    arguments = parser.parse_args()
    try:
        destination = stage_production_monitor_database(arguments.consumer)
    except (OSError, PermissionError, ValueError, sqlite3.Error):
        print('{"execution_status":"FAILED","staged":false}')
        return 1
    print(
        json.dumps(
            {
                "consumer": arguments.consumer,
                "destination": str(destination),
                "execution_status": "COMPLETED",
                "staged": True,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
