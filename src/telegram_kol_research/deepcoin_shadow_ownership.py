"""Does this exchange object belong to something this system created?

Phase 4 of the REST+WebSocket program, and a direct consequence of a phase 3
mistake worth keeping in the code rather than only in a status document: the
first version of the "orders that belong to no binding" scan consulted
``execution_bindings`` and ``execution_order_legs`` only, and reported four TPSL
stops this system had itself placed as somebody else's orders. Protection order
ids are written into the *protection* ledgers, not onto the binding row.

In a check whose whole point is "do not touch other people's orders", a false
"this is not ours" is the dangerous direction to be wrong in. So ownership here
is the union over every ledger that stores an exchange identifier, and the five
the phase 4 plan names are mandatory:

* ``execution_bindings``
* ``execution_order_legs``
* ``position_protection_ledger``
* ``trigger_protection_intents``
* ``position_take_profit_orders``

The remaining sources are additive; removing any of them can only shrink the
owned set and re-create the phase 3 error, which is why
:data:`REQUIRED_OWNERSHIP_TABLES` is asserted against
:data:`OWNERSHIP_SOURCES` by a test rather than trusted to review.

Every query here is a read. This module opens no exchange connection and writes
nothing at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# (table, order-id columns, position-id columns)
OWNERSHIP_SOURCES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("execution_bindings", ("order_id", "client_order_id"), ("pos_id",)),
    ("execution_order_legs", ("order_id", "client_order_id"), ("pos_id",)),
    ("position_protection_ledger", ("order_id",), ("pos_id",)),
    (
        "position_protection_legs",
        ("exchange_order_id", "parent_entry_order_id"),
        ("pos_id",),
    ),
    (
        "trigger_protection_intents",
        ("parent_trigger_order_id", "adopted_order_id"),
        (),
    ),
    ("position_take_profit_orders", ("order_id",), ("pos_id",)),
    ("position_mutation_intents", ("order_id",), ("pos_id",)),
    ("position_backup_stop_orders", ("order_id", "client_order_id"), ("pos_id",)),
    ("position_protection_revisions", (), ("pos_id",)),
)

# The five the phase 4 plan requires by name. A test holds these against
# ``OWNERSHIP_SOURCES`` so that shrinking the list is a test failure.
REQUIRED_OWNERSHIP_TABLES = frozenset(
    {
        "execution_bindings",
        "execution_order_legs",
        "position_protection_ledger",
        "trigger_protection_intents",
        "position_take_profit_orders",
    }
)


@dataclass
class SystemOwnedIds:
    """Every exchange identifier this system has recorded creating."""

    order_ids: set[str] = field(default_factory=set)
    position_ids: set[str] = field(default_factory=set)
    tables_read: tuple[str, ...] = ()
    tables_missing: tuple[str, ...] = ()

    def owns_order(self, *values: Any) -> bool:
        for value in values:
            text = "" if value is None else str(value).strip()
            if text and text in self.order_ids:
                return True
        return False

    def owns_position(self, value: Any) -> bool:
        text = "" if value is None else str(value).strip()
        return bool(text) and text in self.position_ids


def _split_ids(value: Any) -> list[str]:
    """Split one ledger cell into identifiers.

    A few columns hold comma-joined split posIds. Splitting is a storage detail,
    not an inference: each part is still a value this system wrote down itself.
    """

    text = "" if value is None else str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def load_system_owned_ids(
    session_factory: Callable[[], Any],
    *,
    sources: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
        OWNERSHIP_SOURCES
    ),
) -> SystemOwnedIds:
    """Read every recorded exchange identifier out of the ledgers.

    A table that does not exist in this database is reported in
    ``tables_missing`` rather than skipped silently: the caller needs to know
    that the owned set is narrower than the code intends, because a narrower set
    is exactly what produces a false "not ours".
    """

    from sqlalchemy import text as sql_text

    owned = SystemOwnedIds()
    read: list[str] = []
    missing: list[str] = []
    with session_factory() as session:
        available = {
            str(row[0])
            for row in session.execute(
                sql_text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).all()
        }
        for table, order_columns, position_columns in sources:
            if table not in available:
                missing.append(table)
                continue
            columns = {
                str(row[1])
                for row in session.execute(
                    sql_text(f"PRAGMA table_info({table})")
                ).all()
            }
            read.append(table)
            for column_names, sink in (
                (order_columns, owned.order_ids),
                (position_columns, owned.position_ids),
            ):
                for column in column_names:
                    if column not in columns:
                        continue
                    for row in session.execute(
                        sql_text(
                            f"SELECT DISTINCT {column} FROM {table} "
                            f"WHERE {column} IS NOT NULL"
                        )
                    ).all():
                        sink.update(_split_ids(row[0]))
    owned.tables_read = tuple(read)
    owned.tables_missing = tuple(missing)
    return owned
