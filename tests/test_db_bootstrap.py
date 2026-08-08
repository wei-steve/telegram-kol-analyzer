import sqlite3

from sqlalchemy import text

from telegram_kol_research import db as db_module
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import Base


def test_database_bootstrap_creates_tables(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    engine = session_factory.kw["bind"]
    tables = set(Base.metadata.tables)
    assert "raw_messages" in tables
    assert "signal_candidates" in tables
    assert "trade_ideas" in tables
    assert "recognition_experiments" in tables
    assert "runtime_incidents" in tables
    assert "runtime_incident_observations" in tables
    assert "runtime_agent_recovery_attempts" in tables
    assert "message_operation_contracts" in tables
    assert "message_operation_items" in tables
    assert "entry_preambles" in tables
    assert "entry_strategy_assemblies" in tables
    assert "entry_strategy_fragments" in tables
    assert "entry_assembly_fragments" in tables
    assert engine is not None


def test_entry_preamble_tables_are_added_to_existing_database(tmp_path):
    database_path = tmp_path / "research.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE legacy_rows (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO legacy_rows (id, value) VALUES (1, 'keep')")

    create_session_factory(database_path)
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        legacy_value = connection.execute(
            "SELECT value FROM legacy_rows WHERE id = 1"
        ).fetchone()[0]

    assert {
        "entry_preambles",
        "entry_strategy_assemblies",
        "entry_strategy_fragments",
        "entry_assembly_fragments",
    } <= tables
    assert legacy_value == "keep"


def test_entry_fragment_schema_enforces_kind_status_and_unique_association(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        fragment_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='entry_strategy_fragments'"
        ).fetchone()[0]
        link_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='entry_assembly_fragments'"
        ).fetchone()[0]

    assert "risk_multiplier" in fragment_sql
    assert "leg_allocation" in fragment_sql
    assert "supplemental_entry" in fragment_sql
    assert "pending" in fragment_sql
    assert "blocked" in fragment_sql
    assert "UNIQUE (entry_strategy_assembly_id, entry_strategy_fragment_id)" in link_sql


def test_cleanup_notification_columns_are_added_to_existing_execution_events(tmp_path):
    database_path = tmp_path / "research.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE execution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_instance_id VARCHAR(255),
            execution_binding_id INTEGER,
            venue VARCHAR(64) NOT NULL,
            action VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            order_id VARCHAR(255),
            pos_id VARCHAR(255),
            created_at DATETIME NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_events)").fetchall()
    }
    conn.close()

    assert {
        "notification_status",
        "notification_fingerprint",
        "notification_message_id",
        "notification_error",
        "notification_attempts",
        "notification_next_attempt_at",
        "notification_claim_token",
        "notification_claimed_at",
        "notified_at",
    } <= columns


def test_existing_session_factory_skips_bootstrap_and_preserves_writes(tmp_path):
    database_path = tmp_path / "research.db"
    writable_session_factory = create_session_factory(database_path)
    with writable_session_factory() as session:
        session.execute(
            text(
                "INSERT INTO strategy_lifecycles ("
                "chat_id, message_id, symbol, side, lifecycle_status, "
                "signal_at, created_at, updated_at, management_action, "
                "filled_tp_index"
                ") VALUES (1, 2, 'BTC', 'long', 'pending', "
                "'2026-07-29 00:00:00', '2026-07-29 00:00:00', "
                "'2026-07-29 00:00:00', 'expiry_review_requested', -1)"
            )
        )
        session.commit()

    existing_session_factory = db_module.create_existing_session_factory(
        database_path
    )
    with existing_session_factory() as session:
        notified_at = session.execute(
            text(
                "SELECT expiry_review_notified_at "
                "FROM strategy_lifecycles WHERE chat_id = 1 AND message_id = 2"
            )
        ).scalar_one()
        assert notified_at is None
        session.execute(
            text(
                "UPDATE strategy_lifecycles "
                "SET expiry_review_notified_at = '2026-07-29 01:00:00'"
            )
        )
        session.commit()

    with existing_session_factory() as session:
        assert session.execute(
            text(
                "SELECT expiry_review_notified_at "
                "FROM strategy_lifecycles WHERE chat_id = 1 AND message_id = 2"
            )
        ).scalar_one() == "2026-07-29 01:00:00"


def test_message_instruction_items_have_visibility_retry_columns(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(message_instruction_items)"
        ).fetchall()
    }
    conn.close()

    assert {
        "visibility_first_failed_at",
        "visibility_retry_attempts",
        "visibility_next_attempt_at",
    } <= columns


def test_position_mutation_and_management_sla_schema(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        mutation_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(position_mutation_intents)"
            )
        }
        batch_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(strategy_management_batches)"
            )
        }
        item_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(message_instruction_items)"
            )
        }

    assert "position_mutation_intents" in table_names
    assert {
        "idempotency_key",
        "operation",
        "strategy_instance_id",
        "execution_binding_id",
        "execution_order_leg_id",
        "pos_id",
        "order_id",
        "authority_fingerprint",
        "request_fingerprint",
        "status",
        "request_json",
        "response_json",
        "error_json",
        "reserved_at",
        "submitted_at",
        "confirmed_at",
    } <= mutation_columns
    sla_columns = {
        "execution_deadline_at",
        "operator_escalation_at",
        "last_progress_at",
        "escalation_state",
        "escalation_notified_at",
    }
    assert sla_columns <= batch_columns
    assert sla_columns <= item_columns


def test_database_bootstrap_creates_break_even_convergence_schema(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        observation_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(position_reconciliation_observations)"
            )
        }
        convergence_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(strategy_break_even_convergences)"
            )
        }
        leg_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(strategy_break_even_convergence_legs)"
            )
        }
        index_names = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(strategy_break_even_convergences)"
            )
        } | {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(strategy_break_even_convergence_legs)"
            )
        }

    assert {
        "position_reconciliation_observations",
        "strategy_break_even_convergences",
        "strategy_break_even_convergence_legs",
    } <= table_names
    assert {
        "venue",
        "execution_binding_id",
        "execution_order_leg_id",
        "strategy_instance_id",
        "pos_id",
        "instrument_id",
        "side",
        "size_text",
        "avg_entry_price",
        "pending_tpsl_json",
        "snapshot_complete",
        "snapshot_fingerprint",
        "observed_at",
    } <= observation_columns
    assert {
        "venue",
        "strategy_instance_id",
        "execution_binding_id",
        "target_lifecycle_id",
        "trigger_type",
        "trigger_identity",
        "trigger_evidence_json",
        "target_snapshot_json",
        "execution_mode",
        "status",
        "reason_code",
        "planned_at",
        "started_at",
        "completed_at",
        "updated_at",
    } <= convergence_columns
    assert {
        "convergence_id",
        "execution_order_leg_id",
        "pos_id",
        "preflight_size",
        "avg_entry_price",
        "old_protection_json",
        "decision_json",
        "mutation_intent_id",
        "exchange_order_id",
        "status",
        "reason_code",
    } <= leg_columns
    assert {
        "uq_strategy_break_even_convergence_trigger",
        "uq_strategy_break_even_convergence_leg_position",
    } <= index_names


def test_database_bootstrap_creates_prompt_registry_tables(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert {
        "ai_prompt_definitions",
        "ai_prompt_versions",
        "ai_prompt_test_runs",
        "ai_prompt_invocations",
    } <= names


def test_database_bootstrap_creates_recognition_decisions_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(recognition_decisions)").fetchall()
    }
    conn.close()

    assert {
        "raw_message_id",
        "input_kind",
        "authoritative_model",
        "authoritative_status",
        "authoritative_payload_json",
        "auxiliary_model",
        "auxiliary_status",
        "auxiliary_payload_json",
        "agreement_status",
        "differences_json",
        "automation_status",
        "automation_reason",
        "notification_status",
        "notification_error",
        "prompt_versions_json",
        "comparison_status",
        "disagreement_severity",
        "comparison_model",
        "comparison_payload_json",
        "comparison_error",
        "comparison_attempts",
        "comparison_next_attempt_at",
        "comparison_started_at",
        "comparison_claim_token",
        "compared_at",
        "notification_fingerprint",
        "notification_payload_json",
        "created_at",
        "updated_at",
    }.issubset(columns)


def test_database_bootstrap_backfills_recognition_decisions_semantic_review_as_completed(
    tmp_path,
):
    database_path = tmp_path / "research.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE recognition_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_message_id INTEGER NOT NULL UNIQUE,
            input_kind VARCHAR(32) NOT NULL,
            authoritative_model VARCHAR(128) NOT NULL,
            authoritative_status VARCHAR(32) NOT NULL,
            authoritative_payload_json TEXT NOT NULL,
            auxiliary_model VARCHAR(128),
            auxiliary_status VARCHAR(32),
            auxiliary_payload_json TEXT,
            agreement_status VARCHAR(32) NOT NULL,
            differences_json TEXT NOT NULL DEFAULT '[]',
            automation_status VARCHAR(32),
            automation_reason TEXT,
            notification_status VARCHAR(32),
            notification_error TEXT,
            prompt_versions_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO recognition_decisions (
            raw_message_id, input_kind, authoritative_model, authoritative_status,
            authoritative_payload_json, agreement_status, created_at, updated_at
        ) VALUES (
            1, 'text', 'mimo', '非策略', '{}', 'unknown',
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(recognition_decisions)").fetchall()
    }
    status = conn.execute(
        "SELECT comparison_status FROM recognition_decisions WHERE raw_message_id = 1"
    ).fetchone()[0]
    conn.close()

    assert {
        "comparison_status",
        "disagreement_severity",
        "comparison_model",
        "comparison_payload_json",
        "comparison_error",
        "comparison_attempts",
        "comparison_next_attempt_at",
        "comparison_started_at",
        "comparison_claim_token",
        "compared_at",
        "notification_fingerprint",
        "notification_payload_json",
    } <= columns
    assert status == "completed"


def test_database_bootstrap_creates_trading_settings_table(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    engine = session_factory.kw["bind"]

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(trading_settings)")).fetchall()
        }

    assert {"key", "value_json", "updated_at"}.issubset(columns)


def test_database_bootstrap_backfills_missing_sqlite_columns(tmp_path):
    database_path = tmp_path / "research.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE signal_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            entry_text VARCHAR(255),
            stop_loss_text VARCHAR(255),
            take_profit_text TEXT,
            leverage_text VARCHAR(64),
            parse_source VARCHAR(32),
            confidence FLOAT,
            review_status VARCHAR(32),
            created_at DATETIME
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(signal_candidates)").fetchall()
    }
    conn.close()

    assert "source_id" in columns
    assert "event_type" in columns
    assert "review_note" in columns


def test_database_bootstrap_backfills_signal_candidate_management_columns_without_changing_rows(
    tmp_path,
):
    database_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE signal_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            event_type VARCHAR(64) NOT NULL DEFAULT 'entry_signal',
            parse_source VARCHAR(32),
            confidence FLOAT,
            review_status VARCHAR(32),
            created_at DATETIME
        )
        """
    )
    conn.execute(
        """
        INSERT INTO signal_candidates (
            raw_message_id, symbol, side, event_type, parse_source,
            confidence, review_status, created_at
        ) VALUES (7, 'BTC', 'short', 'position_update', 'legacy', 0.9, 'pending', CURRENT_TIMESTAMP)
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(signal_candidates)").fetchall()
    }
    row = conn.execute(
        "SELECT raw_message_id, symbol, side, event_type, parse_source, confidence, review_status "
        "FROM signal_candidates"
    ).fetchone()
    conn.close()

    assert {
        "target_lifecycle_id",
        "management_action",
        "management_fraction",
        "recognition_generation",
    } <= columns
    assert row == (7, "BTC", "short", "position_update", "legacy", 0.9, "pending")


def test_database_bootstrap_backfills_missing_execution_binding_columns(tmp_path):
    database_path = tmp_path / "research.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE execution_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kol_id VARCHAR(255),
            chat_id INTEGER,
            message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            venue VARCHAR(64),
            order_id VARCHAR(255),
            created_at DATETIME
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_bindings)").fetchall()
    }
    conn.close()

    assert "pos_id" in columns
    assert "status" in columns
    assert "updated_at" in columns


def test_database_bootstrap_backfills_missing_recovery_decision_columns(tmp_path):
    database_path = tmp_path / "research.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE recovery_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kol_id VARCHAR(255),
            chat_id INTEGER,
            message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            action VARCHAR(64),
            created_at DATETIME
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(recovery_decisions)").fetchall()
    }
    conn.close()

    assert "reason_codes_json" in columns
    assert "entry_range_text" in columns
    assert "max_loss_usdt" in columns


def test_database_bootstrap_creates_trade_signals_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(trade_signals)").fetchall()
    }
    conn.close()

    assert "signal_uid" in columns
    assert "strategy_instance_id" in columns
    assert "payload_json" in columns
    assert "result_json" in columns
    assert "processed_at" in columns
    assert "updated_at" in columns


def test_database_bootstrap_creates_execution_events_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_events)").fetchall()
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(execution_events)").fetchall()
    }
    conn.close()

    assert {
        "strategy_instance_id",
        "execution_binding_id",
        "trade_signal_id",
        "action",
        "status",
        "order_id",
        "pos_id",
        "before_json",
        "after_json",
        "request_json",
        "response_json",
        "exchange_event_time",
    }.issubset(columns)
    assert "ix_execution_events_strategy_created" in indexes
    assert "ix_execution_events_order" in indexes


def test_database_bootstrap_creates_unique_bound_position_close_reservations(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(bound_position_close_reservations)").fetchall()
    }
    unique_indexes = [
        row[1]
        for row in conn.execute("PRAGMA index_list(bound_position_close_reservations)").fetchall()
        if row[2]
    ]
    conn.close()

    assert {"pos_id", "execution_binding_id", "status", "last_error"}.issubset(columns)
    assert unique_indexes


def test_database_bootstrap_creates_execution_order_legs_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_order_legs)").fetchall()
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(execution_order_legs)").fetchall()
    }
    conn.close()

    assert {
        "execution_binding_id",
        "strategy_instance_id",
        "leg_index",
        "purpose",
        "order_kind",
        "order_id",
        "client_order_id",
        "pos_id",
        "status",
        "request_json",
        "response_json",
        "venue",
        "attribution_status",
        "attribution_evidence_json",
        "terminal_reason",
        "last_verified_at",
    }.issubset(columns)
    assert "ix_execution_order_legs_binding" in indexes
    assert "ix_execution_order_legs_pos" in indexes
    assert "uq_execution_order_legs_venue_pos" in indexes


def test_database_bootstrap_creates_management_batch_and_management_leg_schema(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    batch_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(strategy_management_batches)").fetchall()
    }
    leg_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(strategy_management_legs)").fetchall()
    }
    notification_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(strategy_management_notifications)"
        ).fetchall()
    }
    batch_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_management_batches)").fetchall()
    }
    leg_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_management_legs)").fetchall()
    }
    conn.close()

    assert {
        "idempotency_fingerprint",
        "raw_message_id",
        "recognition_decision_id",
        "recognition_generation",
        "target_lifecycle_id",
        "strategy_instance_id",
        "execution_binding_id",
        "intent",
        "effective_action",
        "execution_mode",
        "requested_fraction",
        "effective_fraction",
        "partial_round_before",
        "status",
        "reason_code",
        "target_fingerprint",
        "target_snapshot_json",
        "planned_at",
        "started_at",
        "reconciled_at",
        "completed_at",
        "notification_state",
        "notification_fingerprint",
        "created_at",
        "updated_at",
    } <= batch_columns
    assert {
        "management_batch_id",
        "execution_order_leg_id",
        "pos_id",
        "leg_index",
        "status",
        "preflight_size",
        "planned_close_size",
        "avg_entry_price",
        "quantity_step",
        "old_tpsl_json",
        "planned_tpsl_json",
        "client_order_id",
        "exchange_order_id",
        "request_json",
        "response_json",
        "last_error",
        "last_exchange_snapshot_json",
        "created_at",
        "updated_at",
    } <= leg_columns
    assert {"claimed_at", "lease_expires_at"} <= notification_columns
    assert "uq_strategy_management_batches_idempotency" in batch_indexes
    assert "uq_strategy_management_batches_active_strategy" in batch_indexes
    assert "uq_strategy_management_legs_batch_pos" in leg_indexes


def test_database_bootstrap_adds_management_batch_and_management_leg_indexes_to_legacy_tables(
    tmp_path,
):
    database_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE strategy_management_batches (
            id INTEGER PRIMARY KEY,
            idempotency_fingerprint VARCHAR(64) NOT NULL,
            strategy_instance_id VARCHAR(255) NOT NULL,
            status VARCHAR(32) NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE strategy_management_legs (
            id INTEGER PRIMARY KEY,
            management_batch_id INTEGER NOT NULL,
            pos_id VARCHAR(255) NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    batch_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_management_batches)").fetchall()
    }
    batch_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(strategy_management_batches)").fetchall()
    }
    leg_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_management_legs)").fetchall()
    }
    conn.close()

    assert "uq_strategy_management_batches_idempotency" in batch_indexes
    assert "execution_mode" in batch_columns
    assert "uq_strategy_management_batches_active_strategy" in batch_indexes
    assert "uq_strategy_management_legs_batch_pos" in leg_indexes


def test_database_bootstrap_skips_management_batch_lock_index_for_legacy_duplicates(
    tmp_path,
):
    database_path = tmp_path / "legacy-duplicates.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE strategy_management_batches (
            id INTEGER PRIMARY KEY,
            idempotency_fingerprint VARCHAR(64) NOT NULL,
            strategy_instance_id VARCHAR(255) NOT NULL,
            status VARCHAR(32) NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO strategy_management_batches VALUES (?, ?, ?, ?)",
        [
            (1, "one", "strategy-1", "ready"),
            (2, "two", "strategy-1", "recovery_required"),
        ],
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_management_batches)").fetchall()
    }
    rows = conn.execute(
        "SELECT id, status FROM strategy_management_batches ORDER BY id"
    ).fetchall()
    conn.close()

    assert "uq_strategy_management_batches_active_strategy" not in indexes
    assert rows == [(1, "ready"), (2, "recovery_required")]


def test_database_bootstrap_skips_management_batch_idempotency_index_for_legacy_duplicates(
    tmp_path,
):
    database_path = tmp_path / "legacy-idempotency-duplicates.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE strategy_management_batches (
            id INTEGER PRIMARY KEY,
            idempotency_fingerprint VARCHAR(64) NOT NULL,
            strategy_instance_id VARCHAR(255) NOT NULL,
            status VARCHAR(32) NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO strategy_management_batches VALUES (?, ?, ?, ?)",
        [
            (1, "duplicate", "strategy-1", "succeeded"),
            (2, "duplicate", "strategy-2", "blocked"),
        ],
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_management_batches)").fetchall()
    }
    rows = conn.execute(
        "SELECT id, idempotency_fingerprint FROM strategy_management_batches ORDER BY id"
    ).fetchall()
    conn.close()

    assert "uq_strategy_management_batches_idempotency" not in indexes
    assert rows == [(1, "duplicate"), (2, "duplicate")]


def test_database_bootstrap_skips_management_leg_index_for_legacy_duplicates(tmp_path):
    database_path = tmp_path / "legacy-leg-duplicates.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE strategy_management_legs (
            id INTEGER PRIMARY KEY,
            management_batch_id INTEGER NOT NULL,
            pos_id VARCHAR(255) NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO strategy_management_legs VALUES (?, ?, ?)",
        [(1, 10, "position-1"), (2, 10, "position-1")],
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_management_legs)").fetchall()
    }
    rows = conn.execute(
        "SELECT id, management_batch_id, pos_id FROM strategy_management_legs ORDER BY id"
    ).fetchall()
    conn.close()

    assert "uq_strategy_management_legs_batch_pos" not in indexes
    assert rows == [(1, 10, "position-1"), (2, 10, "position-1")]


def test_database_bootstrap_backfills_position_attribution_schema(tmp_path):
    database_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE execution_order_legs (
            id INTEGER PRIMARY KEY,
            execution_binding_id INTEGER NOT NULL,
            strategy_instance_id VARCHAR(255),
            leg_index INTEGER NOT NULL,
            purpose VARCHAR(64) NOT NULL,
            order_kind VARCHAR(64) NOT NULL DEFAULT 'unknown',
            order_id VARCHAR(255),
            client_order_id VARCHAR(255),
            pos_id VARCHAR(255),
            status VARCHAR(32) NOT NULL DEFAULT 'submitted',
            request_json TEXT,
            response_json TEXT,
            created_at DATETIME,
            updated_at DATETIME
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    leg_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_order_legs)").fetchall()
    }
    audit_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(position_attribution_audits)").fetchall()
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(execution_order_legs)").fetchall()
    }
    conn.close()

    assert {
        "venue",
        "attribution_status",
        "attribution_evidence_json",
        "terminal_reason",
        "last_verified_at",
    } <= leg_columns
    assert {
        "execution_binding_id",
        "execution_order_leg_id",
        "venue",
        "pos_id",
        "event_type",
        "prior_state",
        "new_state",
        "fingerprint",
        "evidence_json",
        "notification_status",
        "notification_error",
        "notified_at",
        "created_at",
    } <= audit_columns
    assert "uq_execution_order_legs_venue_pos" in indexes


def test_database_bootstrap_enables_sqlite_busy_timeout(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        busy_timeout = session.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert busy_timeout >= 30000


def test_database_bootstrap_backfills_expiry_review_notification_state(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(strategy_lifecycles)").fetchall()
    }
    for column_name in ("expiry_review_notified_at", "expiry_review_next_at"):
        if column_name in existing_columns:
            conn.execute(f"ALTER TABLE strategy_lifecycles DROP COLUMN {column_name}")
    conn.execute(
        """
        INSERT INTO strategy_lifecycles (
            chat_id, message_id, symbol, side, lifecycle_status, signal_at,
            filled_tp_index, management_action, last_checked_at, created_at, updated_at
        ) VALUES (
            88, 7001, 'BTC', 'long', 'pending_entry',
            '2026-07-27 00:00:00', -1, 'expiry_review_requested',
            '2026-07-27 03:05:00', '2026-07-27 00:00:00',
            '2026-07-27 03:05:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_lifecycles (
            chat_id, message_id, symbol, side, lifecycle_status, signal_at,
            filled_tp_index, management_action, last_checked_at, created_at, updated_at
        ) VALUES (
            88, 7002, 'ETH', 'short', 'pending_entry',
            '2026-07-27 00:00:00', -1, 'expiry_review_continued',
            '2026-07-27 04:10:00', '2026-07-27 00:00:00',
            '2026-07-27 04:10:00'
        )
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(strategy_lifecycles)").fetchall()
    }
    rows = conn.execute(
        """
        SELECT message_id, expiry_review_notified_at, expiry_review_next_at
        FROM strategy_lifecycles
        ORDER BY message_id
        """
    ).fetchall()
    conn.close()

    assert {"expiry_review_notified_at", "expiry_review_next_at"} <= columns
    assert rows == [
        (7001, "2026-07-27 03:05:00", None),
        (7002, None, "2026-07-27 07:10:00"),
    ]


def test_database_bootstrap_backfills_web_performance_indexes(tmp_path):
    database_path = tmp_path / "research.db"

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    raw_message_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(raw_messages)").fetchall()
    }
    lifecycle_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(strategy_lifecycles)").fetchall()
    }
    conn.close()

    assert "ix_raw_messages_chat_posted_message" in raw_message_indexes
    assert "ix_strategy_lifecycles_chat_status_signal" in lifecycle_indexes
    assert "ix_strategy_lifecycles_chat_status_entered" in lifecycle_indexes
    assert "ix_strategy_lifecycles_chat_status_exited" in lifecycle_indexes


def test_database_bootstrap_makes_legacy_assembly_preamble_nullable(tmp_path):
    database_path = tmp_path / "legacy-assembly.db"
    conn = sqlite3.connect(database_path)
    conn.execute(
        """
        CREATE TABLE entry_strategy_assemblies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_preamble_id INTEGER NOT NULL UNIQUE,
            strategy_raw_message_id INTEGER NOT NULL,
            signal_candidate_id INTEGER NOT NULL UNIQUE,
            strategy_instance_id VARCHAR(255) NOT NULL UNIQUE,
            risk_multiplier VARCHAR(32) NOT NULL,
            evidence_json TEXT NOT NULL,
            fingerprint VARCHAR(64) NOT NULL UNIQUE,
            created_at DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO entry_strategy_assemblies (
            entry_preamble_id, strategy_raw_message_id, signal_candidate_id,
            strategy_instance_id, risk_multiplier, evidence_json, fingerprint,
            created_at
        ) VALUES (1, 2, 3, 'legacy', '0.5', '{}', ?, '2026-08-08 00:00:00')
        """,
        ("f" * 64,),
    )
    conn.execute(
        "CREATE TABLE entry_strategy_assemblies_nullable (stale INTEGER)"
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]: row for row in conn.execute(
            "PRAGMA table_info(entry_strategy_assemblies)"
        ).fetchall()
    }
    rows = conn.execute(
        "SELECT entry_preamble_id, strategy_instance_id FROM entry_strategy_assemblies"
    ).fetchall()
    conn.close()

    assert columns["entry_preamble_id"][3] == 0
    assert rows == [(1, "legacy")]
