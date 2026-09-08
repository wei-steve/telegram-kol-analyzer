"""Remember which config files this process was not allowed to read.

A config file the process cannot read is skipped rather than raised
(:mod:`telegram_kol_research.llm_chat` and
:mod:`telegram_kol_research.telegram_client` both do this).  Skipping is what
keeps a permission mistake from failing messages, but "not raising" must not
become "not telling anyone": the earlier contract deliberately made an
unreadable non-secret config loud, and that visibility is now carried here
instead of by an exception in a per-message path.

Two carriers, both free:

* one ``WARNING`` per path per process -- these loaders run once per message,
  so warning every time would bury the fact it is trying to report;
* a process-local set the deployment-identity health endpoint reports as
  ``unreadable_config_files``.  That endpoint is contractually I/O-free, and
  reading this set is a memory read.

Only paths are ever recorded. File contents are never read, so they can never
be reported.
"""

from __future__ import annotations

import os
import threading
from typing import Any

_lock = threading.Lock()
_unreadable_paths: set[str] = set()


def note_unreadable_config_file(path: Any, logger) -> None:
    """Record ``path`` as unreadable, warning the first time only."""

    text = os.fspath(path)
    with _lock:
        first_time = text not in _unreadable_paths
        _unreadable_paths.add(text)
    if first_time:
        logger.warning(
            "skipping unreadable config file (this process cannot read it; "
            "values from the environment are used instead): %s",
            text,
        )


def unreadable_config_files() -> tuple[str, ...]:
    """Return the paths skipped so far, for the health endpoint."""

    with _lock:
        return tuple(sorted(_unreadable_paths))


def reset_unreadable_config_files() -> None:
    """Clear the record. For tests, which need each case to start clean."""

    with _lock:
        _unreadable_paths.clear()
