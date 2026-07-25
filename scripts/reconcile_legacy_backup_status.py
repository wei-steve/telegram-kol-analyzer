"""Reconcile stale legacy generic backup-stop records without exchange writes.

Default operation is a read-only plan.  ``--execute`` only changes local
database status after its exact reviewed fingerprint is supplied.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import build_deepcoin_client_from_env
from telegram_kol_research.legacy_backup_reconciliation import (
    apply_legacy_backup_reconciliation_plan,
    build_legacy_backup_reconciliation_plan,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-path", type=Path, default=Path("data/research.db"))
    parser.add_argument("--execute", action="store_true", help="Apply a reviewed database-only plan.")
    parser.add_argument("--expected-fingerprint", help="Fingerprint emitted by a prior dry run.")
    args = parser.parse_args(argv)
    if args.execute and not str(args.expected_fingerprint or "").strip():
        parser.error("--execute requires --expected-fingerprint from a reviewed dry run")
    return args


def main(
    argv: list[str] | None = None,
    *,
    client_builder=build_deepcoin_client_from_env,
) -> int:
    args = _parse_args(argv)
    session_factory = create_session_factory(args.database_path)
    client = client_builder()
    plan = build_legacy_backup_reconciliation_plan(
        session_factory, deepcoin_client=client, now=datetime.now(UTC),
    )
    print(json.dumps({
        "mode": "execute" if args.execute else "dry_run",
        "database_path": str(args.database_path),
        "plan": asdict(plan),
    }, ensure_ascii=False, indent=2, default=str))
    if not args.execute:
        return 0
    result = apply_legacy_backup_reconciliation_plan(
        session_factory, plan, deepcoin_client=client,
        expected_fingerprint=args.expected_fingerprint, now=datetime.now(UTC),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
