from __future__ import annotations

import json
from pathlib import Path
import resource
import sys

from telegram_kol_research.manual_pending_entry_reconciliation import (
    _create_verified_backup,
)


def main() -> int:
    source = Path(sys.argv[1])
    backup = Path(sys.argv[2])
    limit_bytes = int(sys.argv[3])
    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    _create_verified_backup(source, backup)
    print(json.dumps({"status": "complete", "size": backup.stat().st_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
