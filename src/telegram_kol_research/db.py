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
MESSAGE_PROCESSING_JOB_RAW_MESSAGE_UNIQUE_INDEX_NAME = (
    "uq_message_processing_jobs_raw_message_id"
)
MESSAGE_PROCESSING_JOB_RAW_MESSAGE_UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    "uq_message_processing_jobs_raw_message_id "
    "ON message_processing_jobs (raw_message_id)"
)
WORKER_COMMAND_JOB_COMMAND_ID_INDEX_NAME = "uq_worker_command_jobs_command_id"
WORKER_COMMAND_JOB_COMMAND_ID_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_command_jobs_command_id "
    "ON worker_command_jobs (command_id)"
)
WORKER_COMMAND_JOB_IDEMPOTENCY_INDEX_NAME = (
    "uq_worker_command_jobs_type_idempotency"
)
WORKER_COMMAND_JOB_IDEMPOTENCY_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    "uq_worker_command_jobs_type_idempotency "
    "ON worker_command_jobs (command_type, idempotency_key)"
)
WORKER_COMMAND_JOB_CLAIM_SCAN_INDEX_NAME = "ix_worker_command_jobs_claim_scan"
WORKER_COMMAND_JOB_CLAIM_SCAN_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_worker_command_jobs_claim_scan "
    "ON worker_command_jobs (status, lease_expires_at, created_at)"
)
WORKER_COMMAND_JOB_FINGERPRINT_INDEX_NAME = (
    "ix_worker_command_jobs_request_fingerprint"
)
WORKER_COMMAND_JOB_FINGERPRINT_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_worker_command_jobs_request_fingerprint "
    "ON worker_command_jobs (request_fingerprint)"
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
MANAGEMENT_MARKET_DECISION_BATCH_INDEX_NAME = (
    "uq_strategy_management_market_decisions_batch"
)
MANAGEMENT_COMPONENT_IDEMPOTENCY_INDEX_NAME = (
    "uq_strategy_management_components_idempotency"
)
MANAGEMENT_COMPONENT_BATCH_SCOPE_KIND_INDEX_NAME = (
    "uq_strategy_management_components_batch_scope_kind"
)
MANAGEMENT_COMPONENT_BATCH_SCOPE_SEQUENCE_INDEX_NAME = (
    "uq_strategy_management_components_batch_scope_sequence"
)
REQUIRED_MANAGEMENT_UNIQUE_INDEX_NAMES = frozenset(
    {
        MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME,
        MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME,
        MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME,
        MANAGEMENT_MARKET_DECISION_BATCH_INDEX_NAME,
        MANAGEMENT_COMPONENT_IDEMPOTENCY_INDEX_NAME,
        MANAGEMENT_COMPONENT_BATCH_SCOPE_KIND_INDEX_NAME,
        MANAGEMENT_COMPONENT_BATCH_SCOPE_SEQUENCE_INDEX_NAME,
    }
)


SQLITE_COMPAT_COLUMNS: dict[str, dict[str, str]] = {
    "trigger_protection_intents": {
        "recovery_disposition": (
            "ALTER TABLE trigger_protection_intents "
            "ADD COLUMN recovery_disposition VARCHAR(32)"
        ),
        "last_reason_code": (
            "ALTER TABLE trigger_protection_intents "
            "ADD COLUMN last_reason_code VARCHAR(128)"
        ),
        "last_evidence_json": (
            "ALTER TABLE trigger_protection_intents ADD COLUMN last_evidence_json TEXT"
        ),
    },
    "runtime_incidents": {
        "agent_attempt_count": (
            "ALTER TABLE runtime_incidents "
            "ADD COLUMN agent_attempt_count INTEGER NOT NULL DEFAULT 0"
        ),
        "agent_next_attempt_at": (
            "ALTER TABLE runtime_incidents "
            "ADD COLUMN agent_next_attempt_at DATETIME"
        ),
    },
    "trigger_protection_stop_rescues": {
        "error_json": (
            "ALTER TABLE trigger_protection_stop_rescues ADD COLUMN error_json TEXT"
        ),
    },
    "raw_messages": {
        "sender_name": "ALTER TABLE raw_messages ADD COLUMN sender_name VARCHAR(255)",
        "archived_target_group": "ALTER TABLE raw_messages ADD COLUMN archived_target_group BOOLEAN NOT NULL DEFAULT 0",
        "edit_date": "ALTER TABLE raw_messages ADD COLUMN edit_date DATETIME",
        "source_status": "ALTER TABLE raw_messages ADD COLUMN source_status VARCHAR(32) NOT NULL DEFAULT 'active'",
        "deleted_at": "ALTER TABLE raw_messages ADD COLUMN deleted_at DATETIME",
        "deletion_event_fingerprint": "ALTER TABLE raw_messages ADD COLUMN deletion_event_fingerprint VARCHAR(64)",
    },
    "telegram_source_message_events": {
        "processing_status": "ALTER TABLE telegram_source_message_events ADD COLUMN processing_status VARCHAR(32) NOT NULL DEFAULT 'recorded'",
        "reason_code": "ALTER TABLE telegram_source_message_events ADD COLUMN reason_code VARCHAR(128)",
        "completed_at": "ALTER TABLE telegram_source_message_events ADD COLUMN completed_at DATETIME",
    },
    "source_message_deletion_exits": {
        "management_batch_id": "ALTER TABLE source_message_deletion_exits ADD COLUMN management_batch_id INTEGER",
        "claim_token": "ALTER TABLE source_message_deletion_exits ADD COLUMN claim_token VARCHAR(64)",
        "claimed_at": "ALTER TABLE source_message_deletion_exits ADD COLUMN claimed_at DATETIME",
        "cancellation_signal_ids_json": "ALTER TABLE source_message_deletion_exits ADD COLUMN cancellation_signal_ids_json TEXT NOT NULL DEFAULT '[]'",
        "last_reason": "ALTER TABLE source_message_deletion_exits ADD COLUMN last_reason VARCHAR(128)",
        "last_reconciled_at": "ALTER TABLE source_message_deletion_exits ADD COLUMN last_reconciled_at DATETIME",
    },
    "media_assets": {
        "ocr_text": "ALTER TABLE media_assets ADD COLUMN ocr_text TEXT",
    },
    "message_evidence_versions": {
        "mimo_recognition_run_id": (
            "ALTER TABLE message_evidence_versions ADD COLUMN "
            "mimo_recognition_run_id INTEGER REFERENCES mimo_recognition_runs(id)"
        ),
    },
    "signal_candidates": {
        "source_id": "ALTER TABLE signal_candidates ADD COLUMN source_id INTEGER",
        "event_type": "ALTER TABLE signal_candidates ADD COLUMN event_type VARCHAR(64) NOT NULL DEFAULT 'entry_signal'",
        "target_lifecycle_id": "ALTER TABLE signal_candidates ADD COLUMN target_lifecycle_id INTEGER",
        "management_action": "ALTER TABLE signal_candidates ADD COLUMN management_action VARCHAR(64)",
        "management_fraction": "ALTER TABLE signal_candidates ADD COLUMN management_fraction FLOAT",
        "recognition_generation": "ALTER TABLE signal_candidates ADD COLUMN recognition_generation VARCHAR(64)",
        "stop_price_source": "ALTER TABLE signal_candidates ADD COLUMN stop_price_source VARCHAR(32)",
        "review_note": "ALTER TABLE signal_candidates ADD COLUMN review_note TEXT",
        "management_contract_json": (
            "ALTER TABLE signal_candidates ADD COLUMN management_contract_json TEXT"
        ),
        "management_contract_fingerprint": (
            "ALTER TABLE signal_candidates "
            "ADD COLUMN management_contract_fingerprint VARCHAR(64)"
        ),
    },
    "strategy_revision_batches": {
        "revision_kind": (
            "ALTER TABLE strategy_revision_batches ADD COLUMN revision_kind "
            "VARCHAR(32) NOT NULL DEFAULT 'replacement'"
        ),
        "target_assembly_id": (
            "ALTER TABLE strategy_revision_batches ADD COLUMN target_assembly_id INTEGER"
        ),
        "target_assembly_fingerprint": (
            "ALTER TABLE strategy_revision_batches "
            "ADD COLUMN target_assembly_fingerprint VARCHAR(64)"
        ),
        "target_snapshot_json": (
            "ALTER TABLE strategy_revision_batches ADD COLUMN target_snapshot_json TEXT"
        ),
        "market_snapshot_json": (
            "ALTER TABLE strategy_revision_batches ADD COLUMN market_snapshot_json TEXT"
        ),
        "advance_claim_token": (
            "ALTER TABLE strategy_revision_batches "
            "ADD COLUMN advance_claim_token VARCHAR(64)"
        ),
        "advance_claimed_at": (
            "ALTER TABLE strategy_revision_batches "
            "ADD COLUMN advance_claimed_at DATETIME"
        ),
    },
    "context_resolution_attempts": {
        "state_fingerprint": (
            "ALTER TABLE context_resolution_attempts "
            "ADD COLUMN state_fingerprint VARCHAR(80)"
        ),
        "trigger_event_json": (
            "ALTER TABLE context_resolution_attempts "
            "ADD COLUMN trigger_event_json TEXT"
        ),
        "claim_token": (
            "ALTER TABLE context_resolution_attempts "
            "ADD COLUMN claim_token VARCHAR(64)"
        ),
        "claimed_at": (
            "ALTER TABLE context_resolution_attempts ADD COLUMN claimed_at DATETIME"
        ),
        "exhausted_notified_at": (
            "ALTER TABLE context_resolution_attempts "
            "ADD COLUMN exhausted_notified_at DATETIME"
        ),
        "last_error": (
            "ALTER TABLE context_resolution_attempts ADD COLUMN last_error TEXT"
        ),
        "rejected_response_diagnostic_json": (
            "ALTER TABLE context_resolution_attempts "
            "ADD COLUMN rejected_response_diagnostic_json TEXT"
        ),
    },
    "message_instruction_items": {
        "retired_at": (
            "ALTER TABLE message_instruction_items ADD COLUMN retired_at DATETIME"
        ),
        "summary_notification_claimed_at": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN summary_notification_claimed_at DATETIME"
        ),
        "summary_notification_status": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN summary_notification_status VARCHAR(32) "
            "NOT NULL DEFAULT 'pending'"
        ),
        "summary_notification_claim_token": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN summary_notification_claim_token VARCHAR(64)"
        ),
        "summary_notification_error": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN summary_notification_error TEXT"
        ),
        "summary_notified_at": (
            "ALTER TABLE message_instruction_items ADD COLUMN summary_notified_at DATETIME"
        ),
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
    "execution_events": {
        "notification_status": (
            "ALTER TABLE execution_events ADD COLUMN notification_status VARCHAR(32)"
        ),
        "notification_fingerprint": (
            "ALTER TABLE execution_events "
            "ADD COLUMN notification_fingerprint VARCHAR(64)"
        ),
        "notification_message_id": (
            "ALTER TABLE execution_events "
            "ADD COLUMN notification_message_id VARCHAR(255)"
        ),
        "notification_error": (
            "ALTER TABLE execution_events ADD COLUMN notification_error VARCHAR(128)"
        ),
        "notification_attempts": (
            "ALTER TABLE execution_events "
            "ADD COLUMN notification_attempts INTEGER NOT NULL DEFAULT 0"
        ),
        "notification_next_attempt_at": (
            "ALTER TABLE execution_events "
            "ADD COLUMN notification_next_attempt_at DATETIME"
        ),
        "notification_claim_token": (
            "ALTER TABLE execution_events "
            "ADD COLUMN notification_claim_token VARCHAR(64)"
        ),
        "notification_claimed_at": (
            "ALTER TABLE execution_events ADD COLUMN notification_claimed_at DATETIME"
        ),
        "notified_at": (
            "ALTER TABLE execution_events ADD COLUMN notified_at DATETIME"
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
    "strategy_management_batches": {
        "execution_mode": (
            "ALTER TABLE strategy_management_batches "
            "ADD COLUMN execution_mode VARCHAR(16) NOT NULL DEFAULT 'disabled'"
        ),
        "visibility_first_failed_at": "ALTER TABLE strategy_management_batches ADD COLUMN visibility_first_failed_at DATETIME",
        "visibility_retry_attempts": "ALTER TABLE strategy_management_batches ADD COLUMN visibility_retry_attempts INTEGER NOT NULL DEFAULT 0",
        "visibility_next_attempt_at": "ALTER TABLE strategy_management_batches ADD COLUMN visibility_next_attempt_at DATETIME",
        "execution_deadline_at": "ALTER TABLE strategy_management_batches ADD COLUMN execution_deadline_at DATETIME",
        "operator_escalation_at": "ALTER TABLE strategy_management_batches ADD COLUMN operator_escalation_at DATETIME",
        "last_progress_at": "ALTER TABLE strategy_management_batches ADD COLUMN last_progress_at DATETIME",
        "escalation_state": "ALTER TABLE strategy_management_batches ADD COLUMN escalation_state VARCHAR(32)",
        "escalation_notified_at": "ALTER TABLE strategy_management_batches ADD COLUMN escalation_notified_at DATETIME",
        "management_contract_json": (
            "ALTER TABLE strategy_management_batches "
            "ADD COLUMN management_contract_json TEXT"
        ),
        "management_contract_fingerprint": (
            "ALTER TABLE strategy_management_batches "
            "ADD COLUMN management_contract_fingerprint VARCHAR(64)"
        ),
        "contract_version": (
            "ALTER TABLE strategy_management_batches ADD COLUMN contract_version INTEGER"
        ),
    },
    "message_instruction_items": {
        "visibility_first_failed_at": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN visibility_first_failed_at DATETIME"
        ),
        "visibility_retry_attempts": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN visibility_retry_attempts INTEGER NOT NULL DEFAULT 0"
        ),
        "visibility_next_attempt_at": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN visibility_next_attempt_at DATETIME"
        ),
        "execution_deadline_at": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN execution_deadline_at DATETIME"
        ),
        "operator_escalation_at": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN operator_escalation_at DATETIME"
        ),
        "last_progress_at": (
            "ALTER TABLE message_instruction_items ADD COLUMN last_progress_at DATETIME"
        ),
        "escalation_state": (
            "ALTER TABLE message_instruction_items ADD COLUMN escalation_state VARCHAR(32)"
        ),
        "escalation_notified_at": (
            "ALTER TABLE message_instruction_items "
            "ADD COLUMN escalation_notified_at DATETIME"
        ),
    },
    "strategy_management_notifications": {
        "claimed_at": (
            "ALTER TABLE strategy_management_notifications ADD COLUMN claimed_at DATETIME"
        ),
        "lease_expires_at": (
            "ALTER TABLE strategy_management_notifications ADD COLUMN lease_expires_at DATETIME"
        ),
    },
    "strategy_management_components": {
        "strategy_management_leg_scope": (
            "ALTER TABLE strategy_management_components "
            "ADD COLUMN strategy_management_leg_scope INTEGER NOT NULL DEFAULT -1"
        ),
    },
    "recovery_decisions": {
        "reason_codes_json": "ALTER TABLE recovery_decisions ADD COLUMN reason_codes_json TEXT NOT NULL DEFAULT '[]'",
        "entry_range_text": "ALTER TABLE recovery_decisions ADD COLUMN entry_range_text VARCHAR(255)",
        "stop_loss_text": "ALTER TABLE recovery_decisions ADD COLUMN stop_loss_text VARCHAR(255)",
        "max_loss_usdt": "ALTER TABLE recovery_decisions ADD COLUMN max_loss_usdt FLOAT NOT NULL DEFAULT 20.0",
        "entry_preamble_assembly_json": "ALTER TABLE recovery_decisions ADD COLUMN entry_preamble_assembly_json TEXT",
        "review_status": "ALTER TABLE recovery_decisions ADD COLUMN review_status VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "reviewed_at": "ALTER TABLE recovery_decisions ADD COLUMN reviewed_at DATETIME",
        "review_note": "ALTER TABLE recovery_decisions ADD COLUMN review_note TEXT",
        "run_at": "ALTER TABLE recovery_decisions ADD COLUMN run_at DATETIME",
        "updated_at": "ALTER TABLE recovery_decisions ADD COLUMN updated_at DATETIME",
    },
    "strategy_lifecycles": {
        "strategy_thread_id": (
            "ALTER TABLE strategy_lifecycles "
            "ADD COLUMN strategy_thread_id INTEGER"
        ),
        "entry_signal_message_id": "ALTER TABLE strategy_lifecycles ADD COLUMN entry_signal_message_id INTEGER",
        "management_signal_message_id": "ALTER TABLE strategy_lifecycles ADD COLUMN management_signal_message_id INTEGER",
        "management_action": "ALTER TABLE strategy_lifecycles ADD COLUMN management_action VARCHAR(64)",
        "management_note": "ALTER TABLE strategy_lifecycles ADD COLUMN management_note TEXT",
        "expiry_review_notified_at": (
            "ALTER TABLE strategy_lifecycles "
            "ADD COLUMN expiry_review_notified_at DATETIME"
        ),
        "expiry_review_next_at": (
            "ALTER TABLE strategy_lifecycles "
            "ADD COLUMN expiry_review_next_at DATETIME"
        ),
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
    MESSAGE_PROCESSING_JOB_RAW_MESSAGE_UNIQUE_INDEX_NAME: (
        MESSAGE_PROCESSING_JOB_RAW_MESSAGE_UNIQUE_INDEX_SQL
    ),
    WORKER_COMMAND_JOB_COMMAND_ID_INDEX_NAME: (
        WORKER_COMMAND_JOB_COMMAND_ID_INDEX_SQL
    ),
    WORKER_COMMAND_JOB_IDEMPOTENCY_INDEX_NAME: (
        WORKER_COMMAND_JOB_IDEMPOTENCY_INDEX_SQL
    ),
    WORKER_COMMAND_JOB_CLAIM_SCAN_INDEX_NAME: (
        WORKER_COMMAND_JOB_CLAIM_SCAN_INDEX_SQL
    ),
    WORKER_COMMAND_JOB_FINGERPRINT_INDEX_NAME: (
        WORKER_COMMAND_JOB_FINGERPRINT_INDEX_SQL
    ),
    "ix_message_evidence_versions_mimo_recognition_run_id": (
        "CREATE INDEX IF NOT EXISTS "
        "ix_message_evidence_versions_mimo_recognition_run_id "
        "ON message_evidence_versions (mimo_recognition_run_id)"
    ),
    "ix_mimo_recognition_runs_message_status_created": (
        "CREATE INDEX IF NOT EXISTS "
        "ix_mimo_recognition_runs_message_status_created "
        "ON mimo_recognition_runs (raw_message_id, status, created_at)"
    ),
    "ix_mimo_recognition_runs_status_created": (
        "CREATE INDEX IF NOT EXISTS ix_mimo_recognition_runs_status_created "
        "ON mimo_recognition_runs (status, created_at)"
    ),
    "ix_mimo_recognition_attempts_run_created": (
        "CREATE INDEX IF NOT EXISTS ix_mimo_recognition_attempts_run_created "
        "ON mimo_recognition_attempts (run_id, created_at)"
    ),
    "ix_mimo_recognition_attempts_status_created": (
        "CREATE INDEX IF NOT EXISTS ix_mimo_recognition_attempts_status_created "
        "ON mimo_recognition_attempts (status, created_at)"
    ),
    "uq_strategy_revision_batches_entry_assembly": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_strategy_revision_batches_entry_assembly "
        "ON strategy_revision_batches (revision_kind, target_assembly_fingerprint) "
        "WHERE revision_kind = 'entry_sizing' "
        "AND target_assembly_fingerprint IS NOT NULL"
    ),
    "uq_strategy_revision_batches_active_entry_binding": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_strategy_revision_batches_active_entry_binding "
        "ON strategy_revision_batches (execution_binding_id) "
        "WHERE revision_kind = 'entry_sizing' "
        "AND status NOT IN ('succeeded', 'blocked')"
    ),
    MANAGEMENT_COMPONENT_IDEMPOTENCY_INDEX_NAME: (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_strategy_management_components_idempotency "
        "ON strategy_management_components (idempotency_key)"
    ),
    "uq_strategy_management_components_batch_scope_kind": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_strategy_management_components_batch_scope_kind "
        "ON strategy_management_components "
        "(management_batch_id, strategy_management_leg_scope, component_kind)"
    ),
    "uq_strategy_management_components_batch_scope_sequence": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_strategy_management_components_batch_scope_sequence "
        "ON strategy_management_components "
        "(management_batch_id, strategy_management_leg_scope, sequence)"
    ),
    "ix_raw_messages_source_status": (
        "CREATE INDEX IF NOT EXISTS ix_raw_messages_source_status "
        "ON raw_messages (source_status)"
    ),
    "ix_raw_messages_deletion_event_fingerprint": (
        "CREATE INDEX IF NOT EXISTS ix_raw_messages_deletion_event_fingerprint "
        "ON raw_messages (deletion_event_fingerprint)"
    ),
    "uq_execution_events_cleanup_notification_fingerprint": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_execution_events_cleanup_notification_fingerprint "
        "ON execution_events (notification_fingerprint) "
        "WHERE notification_fingerprint IS NOT NULL"
    ),
    "uq_message_instruction_items_message_candidate": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_message_instruction_items_message_candidate "
        "ON message_instruction_items (raw_message_id, signal_candidate_id)"
    ),
    "uq_message_instruction_items_idempotency": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_message_instruction_items_idempotency "
        "ON message_instruction_items (idempotency_key)"
    ),
    "ix_message_instruction_items_message_status_sequence": (
        "CREATE INDEX IF NOT EXISTS "
        "ix_message_instruction_items_message_status_sequence "
        "ON message_instruction_items (raw_message_id, status, sequence)"
    ),
    "uq_instruction_execution_contracts_item": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_instruction_execution_contracts_item "
        "ON instruction_execution_contracts (message_instruction_item_id)"
    ),
    "ix_instruction_execution_contracts_state_deadline": (
        "CREATE INDEX IF NOT EXISTS "
        "ix_instruction_execution_contracts_state_deadline "
        "ON instruction_execution_contracts (state, deadline_at)"
    ),
    "ix_instruction_execution_contracts_strategy_instance": (
        "CREATE INDEX IF NOT EXISTS "
        "ix_instruction_execution_contracts_strategy_instance "
        "ON instruction_execution_contracts (strategy_instance_id)"
    ),
    "uq_instruction_execution_transitions_contract_version": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_instruction_execution_transitions_contract_version "
        "ON instruction_execution_transitions (contract_id, state_version)"
    ),
    "ix_instruction_execution_transitions_contract_created": (
        "CREATE INDEX IF NOT EXISTS "
        "ix_instruction_execution_transitions_contract_created "
        "ON instruction_execution_transitions (contract_id, created_at)"
    ),
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
    MANAGEMENT_MARKET_DECISION_BATCH_INDEX_NAME: (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_strategy_management_market_decisions_batch "
        "ON strategy_management_market_decisions (management_batch_id)"
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
    "uq_trigger_protection_intents_venue_parent_trigger": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_trigger_protection_intents_venue_parent_trigger "
        "ON trigger_protection_intents (venue, parent_trigger_order_id) "
        "WHERE parent_trigger_order_id IS NOT NULL AND parent_trigger_order_id != ''"
    ),
    "uq_trigger_protection_intents_venue_adopted_order": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_trigger_protection_intents_venue_adopted_order "
        "ON trigger_protection_intents (venue, adopted_order_id) "
        "WHERE adopted_order_id IS NOT NULL AND adopted_order_id != ''"
    ),
    "ix_trigger_protection_intents_recovery_next_attempt": (
        "CREATE INDEX IF NOT EXISTS "
        "ix_trigger_protection_intents_recovery_next_attempt "
        "ON trigger_protection_intents (recovery_state, next_attempt_at)"
    ),
    "uq_position_mutation_intents_exchange_cancel": (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_position_mutation_intents_exchange_cancel "
        "ON position_mutation_intents "
        "(venue, operation, order_id, request_fingerprint) "
        "WHERE order_id IS NOT NULL AND order_id != ''"
    ),
}


def init_db(engine: Engine) -> None:
    """Create all database tables if they do not already exist."""

    _configure_sqlite(engine)
    Base.metadata.create_all(engine)
    _make_sqlite_entry_assembly_preamble_nullable(engine)
    _backfill_sqlite_columns(engine)
    _backfill_sqlite_expiry_review_state(engine)
    _backfill_sqlite_indexes(engine)


def _configure_sqlite(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
        connection.execute(text("PRAGMA busy_timeout=30000"))


def _make_sqlite_entry_assembly_preamble_nullable(engine: Engine) -> None:
    """Rebuild the small immutable ledger when upgrading its legacy NOT NULL key."""

    if engine.dialect.name != "sqlite":
        return
    raw_connection = engine.raw_connection()
    cursor = raw_connection.cursor()
    foreign_keys_enabled = False
    try:
        columns = cursor.execute(
            "PRAGMA table_info(entry_strategy_assemblies)"
        ).fetchall()
        preamble_column = next(
            (row for row in columns if row[1] == "entry_preamble_id"), None
        )
        if preamble_column is None or int(preamble_column[3]) == 0:
            return
        foreign_keys_enabled = bool(
            int(cursor.execute("PRAGMA foreign_keys").fetchone()[0])
        )
        if foreign_keys_enabled:
            cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("DROP TABLE IF EXISTS entry_strategy_assemblies_nullable")
        cursor.execute(
            """
            CREATE TABLE entry_strategy_assemblies_nullable (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_preamble_id INTEGER UNIQUE,
                strategy_raw_message_id INTEGER NOT NULL,
                signal_candidate_id INTEGER NOT NULL UNIQUE,
                strategy_instance_id VARCHAR(255) NOT NULL UNIQUE,
                risk_multiplier VARCHAR(32) NOT NULL,
                evidence_json TEXT NOT NULL,
                fingerprint VARCHAR(64) NOT NULL UNIQUE,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(entry_preamble_id) REFERENCES entry_preambles(id),
                FOREIGN KEY(strategy_raw_message_id) REFERENCES raw_messages(id),
                FOREIGN KEY(signal_candidate_id) REFERENCES signal_candidates(id)
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO entry_strategy_assemblies_nullable (
                id, entry_preamble_id, strategy_raw_message_id,
                signal_candidate_id, strategy_instance_id, risk_multiplier,
                evidence_json, fingerprint, created_at
            )
            SELECT id, entry_preamble_id, strategy_raw_message_id,
                   signal_candidate_id, strategy_instance_id, risk_multiplier,
                   evidence_json, fingerprint, created_at
            FROM entry_strategy_assemblies
            """
        )
        cursor.execute("DROP TABLE entry_strategy_assemblies")
        cursor.execute(
            "ALTER TABLE entry_strategy_assemblies_nullable "
            "RENAME TO entry_strategy_assemblies"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_entry_strategy_assemblies_strategy_raw_message_id "
            "ON entry_strategy_assemblies (strategy_raw_message_id)"
        )
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        if foreign_keys_enabled:
            cursor.execute("PRAGMA foreign_keys=ON")
        raw_connection.close()


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


def _backfill_sqlite_expiry_review_state(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        table_exists = connection.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='strategy_lifecycles'"
            )
        ).first()
        if table_exists is None:
            return
        connection.execute(
            text(
                "UPDATE strategy_lifecycles "
                "SET expiry_review_notified_at = "
                "COALESCE(last_checked_at, updated_at) "
                "WHERE management_action = 'expiry_review_requested' "
                "AND expiry_review_notified_at IS NULL"
            )
        )
        connection.execute(
            text(
                "UPDATE strategy_lifecycles "
                "SET expiry_review_next_at = "
                "datetime(COALESCE(last_checked_at, updated_at), '+3 hours') "
                "WHERE management_action = 'expiry_review_continued' "
                "AND expiry_review_notified_at IS NULL "
                "AND expiry_review_next_at IS NULL"
            )
        )


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
        MANAGEMENT_MARKET_DECISION_BATCH_INDEX_NAME: (
            "SELECT 1 FROM strategy_management_market_decisions "
            "GROUP BY management_batch_id HAVING COUNT(*) > 1 LIMIT 1"
        ),
        MANAGEMENT_COMPONENT_IDEMPOTENCY_INDEX_NAME: (
            "SELECT 1 FROM strategy_management_components "
            "GROUP BY idempotency_key HAVING COUNT(*) > 1 LIMIT 1"
        ),
        MANAGEMENT_COMPONENT_BATCH_SCOPE_KIND_INDEX_NAME: (
            "SELECT 1 FROM strategy_management_components "
            "GROUP BY management_batch_id, strategy_management_leg_scope, "
            "component_kind HAVING COUNT(*) > 1 LIMIT 1"
        ),
        MANAGEMENT_COMPONENT_BATCH_SCOPE_SEQUENCE_INDEX_NAME: (
            "SELECT 1 FROM strategy_management_components "
            "GROUP BY management_batch_id, strategy_management_leg_scope, "
            "sequence HAVING COUNT(*) > 1 LIMIT 1"
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


def create_existing_session_factory(database_path: str | Path) -> sessionmaker:
    """Open an existing SQLite database without running bootstrap migrations."""

    db_path = Path(database_path).resolve(strict=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30},
        future=True,
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
