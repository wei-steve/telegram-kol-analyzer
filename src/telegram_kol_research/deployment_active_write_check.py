from __future__ import annotations

import sqlite3
from pathlib import Path


class ActiveWriteCheckError(ValueError):
    pass


_MAX_ACTIVE_WRITE_COUNT = 1_000_000

_COUNT_QUERIES = (
    """
    SELECT COUNT(*)
    FROM position_backup_stop_orders
    WHERE status = 'submitting'
    """,
    """
    SELECT COUNT(*)
    FROM execution_order_legs
    WHERE status IN ('submitting', 'cancel_submitting')
    """,
    """
    SELECT COUNT(*)
    FROM instruction_execution_contracts
    WHERE state = 'submitting'
    """,
    """
    SELECT COUNT(*)
    FROM strategy_management_components
    WHERE status IN ('submitting', 'cancel_submitting')
    """,
    """
    SELECT COUNT(*)
    FROM strategy_management_batches
    WHERE status = 'executing'
    """,
    """
    SELECT COUNT(*)
    FROM strategy_revision_batches
    WHERE status = 'submitting_replacements'
    """,
    """
    SELECT COUNT(*)
    FROM strategy_revision_legs AS child
    JOIN strategy_revision_batches AS b
      ON b.id = child.revision_batch_id
    WHERE child.status = 'cancel_submitting'
      AND typeof(b.advance_claim_token) = 'text'
      AND length(b.advance_claim_token) > 0
      AND b.advance_claimed_at IS NOT NULL
      AND length(CAST(b.advance_claimed_at AS text)) > 0
    """,
    """
    SELECT COUNT(*)
    FROM entry_revision_replacements AS child
    JOIN strategy_revision_batches AS b
      ON b.id = child.revision_batch_id
    WHERE child.status = 'submit_reserved'
      AND typeof(b.advance_claim_token) = 'text'
      AND length(b.advance_claim_token) > 0
      AND b.advance_claimed_at IS NOT NULL
      AND length(CAST(b.advance_claimed_at AS text)) > 0
    """,
    """
    SELECT COUNT(*)
    FROM trigger_protection_intents
    WHERE recovery_state IN ('submitting', 'cancel_submitting')
    """,
    """
    SELECT COUNT(*)
    FROM position_mutation_intents
    WHERE status IN ('submitting', 'cancel_submitting')
    """,
    """
    SELECT COUNT(*)
    FROM trade_signals
    WHERE status IN ('processing', 'submitting', 'cancel_submitting')
    """,
)


def count_active_exchange_writes(database_path: str | Path) -> int:
    connection: sqlite3.Connection | None = None
    total = 0
    failed = False
    cleanup_failed = False

    try:
        database = Path(database_path).resolve()
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
        )
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone() != (1,):
            raise ActiveWriteCheckError("active_write_check_failed")
        connection.execute("BEGIN")

        for query in _COUNT_QUERIES:
            row = connection.execute(query).fetchone()
            if row is None or len(row) != 1:
                raise ActiveWriteCheckError("active_write_check_failed")
            count = row[0]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ActiveWriteCheckError("active_write_check_failed")
            total = min(_MAX_ACTIVE_WRITE_COUNT, total + count)
    except Exception:
        failed = True
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                cleanup_failed = True
            try:
                connection.close()
            except Exception:
                cleanup_failed = True

    if failed or cleanup_failed:
        raise ActiveWriteCheckError("active_write_check_failed") from None
    return total
