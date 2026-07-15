"""Database bootstrap helpers for the local research app."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE
from telegram_kol_research.models import Base


POSITION_OWNERSHIP_UNIQUE_INDEX_NAME = "uq_execution_order_legs_venue_pos"
POSITION_OWNERSHIP_UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_order_legs_venue_pos "
    "ON execution_order_legs (venue, pos_id) "
    "WHERE pos_id IS NOT NULL AND pos_id != ''"
)
MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME = (
    "uq_strategy_management_batches_idempotency"
)
MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME = (
    "uq_strategy_management_batches_active_strategy"
)
MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    "uq_strategy_management_batches_active_strategy "
    "ON strategy_management_batches (strategy_instance_id) "
    f"WHERE {ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE}"
)
MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME = (
    "uq_strategy_management_legs_batch_pos"
)
REQUIRED_MANAGEMENT_UNIQUE_INDEX_NAMES = frozenset(
    {
        MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME,
        MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME,
        MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME,
    }
)


SQLITE_COMPAT_COLUMNS: dict[str, dict[str, str]] = {
    "raw_messages": {
        "sender_name": "ALTER TABLE raw_messages ADD COLUMN sender_name VARCHAR(255)",
        "archived_target_group": "ALTER TABLE raw_messages ADD COLUMN archived_target_group BOOLEAN NOT NULL DEFAULT 0",
        "edit_date": "ALTER TABLE raw_messages ADD COLUMN edit_date DATETIME",
    },
    "media_assets": {
        "ocr_text": "ALTER TABLE media_assets ADD COLUMN ocr_text TEXT",
    },
    "signal_candidates": {
        "source_id": "ALTER TABLE signal_candidates ADD COLUMN source_id INTEGER",
        "event_type": "ALTER TABLE signal_candidates ADD COLUMN event_type VARCHAR(64) NOT NULL DEFAULT 'entry_signal'",
        "target_lifecycle_id": "ALTER TABLE signal_candidates ADD COLUMN target_lifecycle_id INTEGER",
        "management_action": "ALTER TABLE signal_candidates ADD COLUMN management_action VARCHAR(64)",
        "management_fraction": "ALTER TABLE signal_candidates ADD COLUMN management_fraction FLOAT",
        "recognition_generation": "ALTER TABLE signal_candidates ADD COLUMN recognition_generation VARCHAR(64)",
        "review_note": "ALTER TABLE signal_candidates ADD COLUMN review_note TEXT",
    },
    "trade_ideas": {
        "source_id": "ALTER TABLE trade_ideas ADD COLUMN source_id INTEGER",
    },
    "strategy_alerts": {
        "raw_message_id": "ALTER TABLE strategy_alerts ADD COLUMN raw_message_id INTEGER",
        "sender_name": "ALTER TABLE strategy_alerts ADD COLUMN sender_name VARCHAR(255)",
        "original_text": "ALTER TABLE strategy_alerts ADD COLUMN original_text TEXT",
        "is_strategy": "ALTER TABLE strategy_alerts ADD COLUMN is_strategy BOOLEAN",
        "strategy_kind": "ALTER TABLE strategy_alerts ADD COLUMN strategy_kind VARCHAR(32)",
        "ai_confidence": "ALTER TABLE strategy_alerts ADD COLUMN ai_confidence FLOAT",
        "kol_label": "ALTER TABLE strategy_alerts ADD COLUMN kol_label VARCHAR(255)",
        "reason_short": "ALTER TABLE strategy_alerts ADD COLUMN reason_short TEXT",
        "error_message": "ALTER TABLE strategy_alerts ADD COLUMN error_message TEXT",
        "forwarded_at": "ALTER TABLE strategy_alerts ADD COLUMN forwarded_at DATETIME",
        "updated_at": "ALTER TABLE strategy_alerts ADD COLUMN updated_at DATETIME",
    },
    "execution_bindings": {
        "strategy_instance_id": "ALTER TABLE execution_bindings ADD COLUMN strategy_instance_id VARCHAR(255)",
        "pos_id": "ALTER TABLE execution_bindings ADD COLUMN pos_id VARCHAR(255)",
        "client_order_id": "ALTER TABLE execution_bindings ADD COLUMN client_order_id VARCHAR(255)",
        "margin_mode": "ALTER TABLE execution_bindings ADD COLUMN margin_mode VARCHAR(32) NOT NULL DEFAULT 'cross'",
        "position_mode": "ALTER TABLE execution_bindings ADD COLUMN position_mode VARCHAR(32) NOT NULL DEFAULT 'split'",
        "payload_json": "ALTER TABLE execution_bindings ADD COLUMN payload_json TEXT",
        "last_exchange_status": "ALTER TABLE execution_bindings ADD COLUMN last_exchange_status VARCHAR(64)",
        "recovered_at": "ALTER TABLE execution_bindings ADD COLUMN recovered_at DATETIME",
        "status": "ALTER TABLE execution_bindings ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'open'",
        "updated_at": "ALTER TABLE execution_bindings ADD COLUMN updated_at DATETIME",
    },
    "execution_order_legs": {
        "venue": (
            "ALTER TABLE execution_order_legs "
            "ADD COLUMN venue VARCHAR(64) NOT NULL DEFAULT 'deepcoin'"
        ),
        "attribution_status": (
            "ALTER TABLE execution_order_legs "
            "ADD COLUMN attribution_status VARCHAR(32) NOT NULL DEFAULT 'unassigned'"
        ),
        "attribution_evidence_json": (
            "ALTER TABLE execution_order_legs ADD COLUMN attribution_evidence_json TEXT"
        ),
        "terminal_reason": (
            "ALTER TABLE execution_order_legs ADD COLUMN terminal_reason VARCHAR(64)"
        ),
        "last_verified_at": (
            "ALTER TABLE execution_order_legs ADD COLUMN last_verified_at DATETIME"
        ),
    },
    "position_attribution_audits": {
        "notification_status": (
            "ALTER TABLE position_attribution_audits "
            "ADD COLUMN notification_status VARCHAR(32)"
        ),
        "notification_error": (
            "ALTER TABLE position_attribution_audits ADD COLUMN notification_error TEXT"
        ),
        "notified_at": (
            "ALTER TABLE position_attribution_audits ADD COLUMN notified_at DATETIME"
        ),
    },
    "recovery_decisions": {
        "reason_codes_json": "ALTER TABLE recovery_decisions ADD COLUMN reason_codes_json TEXT NOT NULL DEFAULT '[]'",
        "entry_range_text": "ALTER TABLE recovery_decisions ADD COLUMN entry_range_text VARCHAR(255)",
        "stop_loss_text": "ALTER TABLE recovery_decisions ADD COLUMN stop_loss_text VARCHAR(255)",
        "max_loss_usdt": "ALTER TABLE recovery_decisions ADD COLUMN max_loss_usdt FLOAT NOT NULL DEFAULT 20.0",
        "review_status": "ALTER TABLE recovery_decisions ADD COLUMN review_status VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "reviewed_at": "ALTER TABLE recovery_decisions ADD COLUMN reviewed_at DATETIME",
        "review_note": "ALTER TABLE recovery_decisions ADD COLUMN review_note TEXT",
        "run_at": "ALTER TABLE recovery_decisions ADD COLUMN run_at DATETIME",
        "updated_at": "ALTER TABLE recovery_decisions ADD COLUMN updated_at DATETIME",
    },
    "strategy_lifecycles": {
        "entry_signal_message_id": "ALTER TABLE strategy_lifecycles ADD COLUMN entry_signal_message_id INTEGER",
        "management_signal_message_id": "ALTER TABLE strategy_lifecycles ADD COLUMN management_signal_message_id INTEGER",
        "management_action": "ALTER TABLE strategy_lifecycles ADD COLUMN management_action VARCHAR(64)",
        "management_note": "ALTER TABLE strategy_lifecycles ADD COLUMN management_note TEXT",
    },
    "recognition_experiments": {
        "updated_at": "ALTER TABLE recognition_experiments ADD COLUMN updated_at DATETIME",
    },
    "recognition_decisions": {
        "prompt_versions_json": (
            "ALTER TABLE recognition_decisions "
            "ADD COLUMN prompt_versions_json TEXT NOT NULL DEFAULT '{}'"
        ),
        "comparison_status": (
            "ALTER TABLE recognition_decisions "
            "ADD COLUMN comparison_status VARCHAR(32) NOT NULL DEFAULT 'completed'"
        ),
        "disagreement_severity": (
            "ALTER TABLE recognition_decisions ADD COLUMN disagreement_severity VARCHAR(32)"
        ),
        "comparison_model": (
            "ALTER TABLE recognition_decisions ADD COLUMN comparison_model VARCHAR(128)"
        ),
        "comparison_payload_json": (
            "ALTER TABLE recognition_decisions ADD COLUMN comparison_payload_json TEXT"
        ),
        "comparison_error": (
            "ALTER TABLE recognition_decisions ADD COLUMN comparison_error TEXT"
        ),
        "comparison_attempts": (
            "ALTER TABLE recognition_decisions "
            "ADD COLUMN comparison_attempts INTEGER NOT NULL DEFAULT 0"
        ),
        "comparison_next_attempt_at": (
            "ALTER TABLE recognition_decisions ADD COLUMN comparison_next_attempt_at DATETIME"
        ),
        "comparison_started_at": (
            "ALTER TABLE recognition_decisions ADD COLUMN comparison_started_at DATETIME"
        ),
        "comparison_claim_token": (
            "ALTER TABLE recognition_decisions ADD COLUMN comparison_claim_token VARCHAR(64)"
        ),
        "compared_at": (
            "ALTER TABLE recognition_decisions ADD COLUMN compared_at DATETIME"
        ),
        "notification_fingerprint": (
            "ALTER TABLE recognition_decisions ADD COLUMN notification_fingerprint VARCHAR(64)"
        ),
        "notification_payload_json": (
            "ALTER TABLE recognition_decisions ADD COLUMN notification_payload_json TEXT"
        ),
    },
    "ai_prompt_versions": {
        "validated_at": "ALTER TABLE ai_prompt_versions ADD COLUMN validated_at DATETIME",
        "validation_result_json": "ALTER TABLE ai_prompt_versions ADD COLUMN validation_result_json TEXT",
    },
    "ai_prompt_test_runs": {
        "model_kind": (
            "ALTER TABLE ai_prompt_test_runs "
            "ADD COLUMN model_kind VARCHAR(32) NOT NULL DEFAULT 'unknown'"
        ),
        "active_prompt_versions_json": (
            "ALTER TABLE ai_prompt_test_runs "
            "ADD COLUMN active_prompt_versions_json TEXT NOT NULL DEFAULT '{}'"
        ),
    },
    "trade_signals": {
        "strategy_instance_id": "ALTER TABLE trade_signals ADD COLUMN strategy_instance_id VARCHAR(255)",
        "result_json": "ALTER TABLE trade_signals ADD COLUMN result_json TEXT",
        "last_error": "ALTER TABLE trade_signals ADD COLUMN last_error TEXT",
        "attempts": "ALTER TABLE trade_signals ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "processed_at": "ALTER TABLE trade_signals ADD COLUMN processed_at DATETIME",
    },
}

SQLITE_COMPAT_INDEXES: dict[str, str] = {
    "ix_raw_messages_chat_posted_message": (
        "CREATE INDEX IF NOT EXISTS ix_raw_messages_chat_posted_message "
        "ON raw_messages (chat_id, posted_at, message_id)"
    ),
    "ix_strategy_lifecycles_chat_status_signal": (
        "CREATE INDEX IF NOT EXISTS ix_strategy_lifecycles_chat_status_signal "
        "ON strategy_lifecycles (chat_id, lifecycle_status, signal_at)"
    ),
    "ix_strategy_lifecycles_chat_status_entered": (
        "CREATE INDEX IF NOT EXISTS ix_strategy_lifecycles_chat_status_entered "
        "ON strategy_lifecycles (chat_id, lifecycle_status, entered_at)"
    ),
    "ix_strategy_lifecycles_chat_status_exited": (
        "CREATE INDEX IF NOT EXISTS ix_strategy_lifecycles_chat_status_exited "
        "ON strategy_lifecycles (chat_id, lifecycle_status, exited_at)"
    ),
    "ix_trading_settings_key": (
        "CREATE INDEX IF NOT EXISTS ix_trading_settings_key "
        "ON trading_settings (key)"
    ),
    "ix_execution_bindings_strategy_instance": (
        "CREATE INDEX IF NOT EXISTS ix_execution_bindings_strategy_instance "
        "ON execution_bindings (strategy_instance_id)"
    ),
    "ix_execution_bindings_client_order": (
        "CREATE INDEX IF NOT EXISTS ix_execution_bindings_client_order "
        "ON execution_bindings (client_order_id)"
    ),
    "ix_execution_order_legs_binding": (
        "CREATE INDEX IF NOT EXISTS ix_execution_order_legs_binding "
        "ON execution_order_legs (execution_binding_id)"
    ),
    "ix_execution_order_legs_strategy": (
        "CREATE INDEX IF NOT EXISTS ix_execution_order_legs_strategy "
        "ON execution_order_legs (strategy_instance_id)"
    ),
    "ix_execution_order_legs_order": (
        "CREATE INDEX IF NOT EXISTS ix_execution_order_legs_order "
        "ON execution_order_legs (order_id)"
    ),
    "ix_execution_order_legs_client_order": (
        "CREATE INDEX IF NOT EXISTS ix_execution_order_legs_client_order "
        "ON execution_order_legs (client_order_id)"
    ),
    "ix_execution_order_legs_pos": (
        "CREATE INDEX IF NOT EXISTS ix_execution_order_legs_pos "
        "ON execution_order_legs (pos_id)"
    ),
    POSITION_OWNERSHIP_UNIQUE_INDEX_NAME: POSITION_OWNERSHIP_UNIQUE_INDEX_SQL,
    MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME: (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_strategy_management_batches_idempotency "
        "ON strategy_management_batches (idempotency_fingerprint)"
    ),
    MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME: (
        MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_SQL
    ),
    MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME: (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_strategy_management_legs_batch_pos "
        "ON strategy_management_legs (management_batch_id, pos_id)"
    ),
    "ix_execution_events_strategy_created": (
        "CREATE INDEX IF NOT EXISTS ix_execution_events_strategy_created "
        "ON execution_events (strategy_instance_id, created_at)"
    ),
    "ix_execution_events_binding_created": (
        "CREATE INDEX IF NOT EXISTS ix_execution_events_binding_created "
        "ON execution_events (execution_binding_id, created_at)"
    ),
    "ix_execution_events_action_created": (
        "CREATE INDEX IF NOT EXISTS ix_execution_events_action_created "
        "ON execution_events (action, created_at)"
    ),
    "ix_execution_events_order": (
        "CREATE INDEX IF NOT EXISTS ix_execution_events_order "
        "ON execution_events (order_id)"
    ),
    "ix_execution_events_pos": (
        "CREATE INDEX IF NOT EXISTS ix_execution_events_pos "
        "ON execution_events (pos_id)"
    ),
    "ix_trade_signals_status_created": (
        "CREATE INDEX IF NOT EXISTS ix_trade_signals_status_created "
        "ON trade_signals (status, created_at)"
    ),
    "ix_trade_signals_strategy_instance": (
        "CREATE INDEX IF NOT EXISTS ix_trade_signals_strategy_instance "
        "ON trade_signals (strategy_instance_id)"
    ),
}


def init_db(engine: Engine) -> None:
    """Create all database tables if they do not already exist."""

    _configure_sqlite(engine)
    Base.metadata.create_all(engine)
    _backfill_sqlite_columns(engine)
    _backfill_sqlite_indexes(engine)


def _configure_sqlite(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
        connection.execute(text("PRAGMA busy_timeout=30000"))


def _backfill_sqlite_columns(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        for table_name, required_columns in SQLITE_COMPAT_COLUMNS.items():
            existing_tables = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }
            if table_name not in existing_tables:
                continue

            existing_columns = {
                row[1]
                for row in connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for column_name, alter_sql in required_columns.items():
                if column_name not in existing_columns:
                    connection.execute(text(alter_sql))


def _backfill_sqlite_indexes(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        existing_tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        for index_name, create_index_sql in SQLITE_COMPAT_INDEXES.items():
            table_name = create_index_sql.rsplit(" ON ", 1)[1].split(" ", 1)[0]
            if table_name in existing_tables:
                if (
                    index_name == "uq_execution_order_legs_venue_pos"
                    and connection.execute(
                        text(
                            "SELECT 1 FROM execution_order_legs "
                            "WHERE pos_id IS NOT NULL AND pos_id != '' "
                            "GROUP BY venue, pos_id HAVING COUNT(*) > 1 LIMIT 1"
                        )
                    ).first()
                    is not None
                ):
                    # Keep the database readable so the audited repair command can
                    # resolve legacy duplicates. Runtime ownership gates fail closed.
                    continue
                if _management_unique_index_has_duplicates(connection, index_name):
                    # A partially deployed legacy schema must remain readable. The
                    # unsafe duplicate rows continue to fail closed until an audited
                    # repair can make the matching unique index installable.
                    continue
                connection.execute(text(create_index_sql))


def _management_unique_index_has_duplicates(connection, index_name: str) -> bool:
    duplicate_queries = {
        MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME: (
            "SELECT 1 FROM strategy_management_batches "
            "GROUP BY idempotency_fingerprint HAVING COUNT(*) > 1 LIMIT 1"
        ),
        MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME: (
            "SELECT 1 FROM strategy_management_batches "
            f"WHERE {ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE} "
            "GROUP BY strategy_instance_id HAVING COUNT(*) > 1 LIMIT 1"
        ),
        MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME: (
            "SELECT 1 FROM strategy_management_legs "
            "GROUP BY management_batch_id, pos_id HAVING COUNT(*) > 1 LIMIT 1"
        ),
    }
    query = duplicate_queries.get(index_name)
    return query is not None and connection.execute(text(query)).first() is not None


def ensure_position_ownership_unique_index(connection) -> None:
    """Install the ownership index only after a fresh duplicate check."""

    duplicate = connection.execute(
        text(
            "SELECT 1 FROM execution_order_legs "
            "WHERE pos_id IS NOT NULL AND pos_id != '' "
            "GROUP BY venue, pos_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError("duplicate position ownership remains")
    connection.execute(text(POSITION_OWNERSHIP_UNIQUE_INDEX_SQL))


def create_session_factory(database_path: str | Path) -> sessionmaker:
    """Create a SQLite session factory and initialize core tables."""

    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30},
        future=True,
    )
    init_db(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
