#!/usr/bin/env python3
"""Fail-closed metadata and symlink validation for the monitor uv cache."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_cache(
    cache_root: Path,
    *,
    trust_anchor: Path,
    expected_owner_uid: int,
) -> None:
    anchor = Path(os.path.abspath(trust_anchor))
    root = Path(os.path.abspath(cache_root))
    if root == anchor:
        raise ValueError("cache root must be below the trusted ancestor")
    if not _is_within(root, anchor):
        raise ValueError("cache root escapes the trusted ancestor")

    components = [anchor]
    current = anchor
    for component_name in root.relative_to(anchor).parts:
        current /= component_name
        components.append(current)
    anchor_device: int | None = None
    for component in components:
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise ValueError("cache ancestor is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("cache ancestor must not be a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("cache ancestor must be a directory")
        if metadata.st_uid != expected_owner_uid:
            raise ValueError("cache ancestor owner is not trusted")
        if metadata.st_mode & 0o022:
            raise ValueError("cache ancestor is writable by another identity")
        if anchor_device is None:
            anchor_device = metadata.st_dev
        elif metadata.st_dev != anchor_device:
            raise ValueError("cache ancestor crosses a filesystem boundary")
    if anchor_device is None:
        raise ValueError("cache trusted ancestor is unavailable")
    root_device = anchor_device

    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            paths = [Path(entry.path) for entry in entries]
        for path in paths:
            metadata = path.lstat()
            if metadata.st_uid != expected_owner_uid:
                raise ValueError("cache entry owner is not trusted")
            if metadata.st_dev != root_device:
                raise ValueError("cache entry crosses a filesystem boundary")
            if stat.S_ISLNK(metadata.st_mode):
                raw_target = os.readlink(path)
                lexical_target = Path(
                    os.path.abspath(
                        raw_target if os.path.isabs(raw_target) else path.parent / raw_target
                    )
                )
                if not _is_within(lexical_target, root):
                    raise ValueError("cache symlink escapes the trusted root")
                try:
                    resolved_target = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ValueError("cache symlink target is unavailable") from exc
                if not _is_within(resolved_target, root):
                    raise ValueError("cache symlink resolves outside the trusted root")
                continue
            if metadata.st_mode & 0o022:
                raise ValueError("cache entry is writable by another identity")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif not stat.S_ISREG(metadata.st_mode):
                raise ValueError("cache entry type is not allowed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--trust-anchor", type=Path, required=True)
    parser.add_argument("--expected-owner-uid", type=int, required=True)
    args = parser.parse_args()
    try:
        validate_cache(
            args.cache_root,
            trust_anchor=args.trust_anchor,
            expected_owner_uid=args.expected_owner_uid,
        )
    except (OSError, ValueError) as exc:
        print(f"uv cache validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
