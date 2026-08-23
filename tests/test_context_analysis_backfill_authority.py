"""Isolation guards for the standalone historical context-analysis tool."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from telegram_kol_research.context_analysis_backfill import (
    apply_context_analysis_manifest,
    export_context_analysis_incidents,
    rollback_context_analysis_backfill,
    validate_context_analysis_manifest,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageProcessingJob,
    RawMessage,
)


MODULE_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "telegram_kol_research"
    / "context_analysis_backfill.py"
)
FORBIDDEN_IMPORT_FRAGMENTS = (
    "authoritative_recognition",
    "auto_trade_execution",
    "deepcoin_client",
    "position_authority_lock",
    "position_mutation_authority",
    "position_mutation_gateway",
    "strategy_management_executor",
    "strategy_management_composite_executor",
    "system_operator_bot",
    "telegram_live_listener",
    "web_app",
    "worker_command_executor",
    "worker_command_jobs",
)


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _records_sha(records):
    return hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()


def _prepared_manifest(tmp_path):
    database_path = tmp_path / "authority.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw = RawMessage(chat_id=700, message_id=4101, text="analysis only")
        session.add(raw)
        session.flush()
        session.add(
            MessageProcessingJob(
                raw_message_id=raw.id,
                chat_id=raw.chat_id,
                status="failed",
                shadow=False,
            )
        )
        session.add(
            ContextResolutionAttempt(
                raw_message_id=raw.id,
                context_fingerprint="sha256:authority-source",
                state_fingerprint="sha256:authority-state",
                model="deepseek-v4-flash",
                prompt_versions_json='{"context_resolution":"context-resolution-v1"}',
                request_summary_json=_canonical_json(
                    {
                        "current_message": {
                            "raw_message_id": raw.id,
                            "chat_id": raw.chat_id,
                            "message_id": raw.message_id,
                            "text": raw.text,
                        },
                        "candidate_strategy_threads": [],
                    }
                ),
                status="exhausted",
                error_class="network_error",
            )
        )
        session.commit()
    export_path = tmp_path / "export.json"
    manifest = export_context_analysis_incidents(
        database_path,
        run_id="authority-run",
        output_path=export_path,
    )
    record = manifest["records"][0]
    record["status"] = "analysis_only_completed"
    record["decision"] = {
        "decision": "hold",
        "target_thread_ids": [],
        "management_action": None,
        "confidence": 0.5,
        "supporting_message_ids": [4101],
        "opposing_message_ids": [],
        "conflict_types": [],
        "risk_reducing_fanout_allowed": False,
        "reanalysis_triggers": [],
        "reason": "historical analysis only",
    }
    record["skip_reason"] = None
    manifest["records_sha256"] = _records_sha(manifest["records"])
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
    return database_path, manifest, manifest_path


def test_context_analysis_backfill_has_no_authority_imports():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not {
        imported
        for imported in imported_modules
        if any(fragment in imported for fragment in FORBIDDEN_IMPORT_FRAGMENTS)
    }


def test_export_validate_apply_and_rollback_do_not_call_authority_boundaries(
    tmp_path, monkeypatch
):
    import telegram_kol_research.authoritative_recognition as recognition
    import telegram_kol_research.auto_trade_execution as auto_trade
    import telegram_kol_research.deepcoin_client as deepcoin
    import telegram_kol_research.position_authority_lock as position_lock
    import telegram_kol_research.position_mutation_gateway as mutation_gateway
    import telegram_kol_research.strategy_management_executor as management
    import telegram_kol_research.system_operator_bot as notifications
    import telegram_kol_research.web_app as web_app
    import telegram_kol_research.worker_command_executor as worker_commands

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("authority boundary was called")

    boundaries = (
        (recognition, "process_authoritative_message"),
        (auto_trade, "auto_process_message_trade_signal"),
        (management, "execute_management_batch"),
        (worker_commands, "run_sync_command_blocking"),
        (notifications, "run_operator_maintenance_tick"),
        (deepcoin, "build_deepcoin_client_from_env"),
        (position_lock, "position_authority_lock"),
        (mutation_gateway, "submit_exact_position_sltp"),
        (web_app, "create_web_app"),
    )
    for module, name in boundaries:
        monkeypatch.setattr(module, name, forbidden)

    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    validation_path = tmp_path / "validation.json"
    validate_context_analysis_manifest(
        database_path,
        manifest_path=manifest_path,
        output_path=validation_path,
    )
    dry_path = tmp_path / "dry.json"
    apply_context_analysis_manifest(
        database_path,
        manifest_path=manifest_path,
        output_path=dry_path,
    )
    receipt_path = tmp_path / "receipt.json"
    receipt = apply_context_analysis_manifest(
        database_path,
        manifest_path=manifest_path,
        output_path=receipt_path,
        effects="analysis-only",
        apply=True,
        expected_database_identity=manifest["database_identity"],
        expected_records_sha256=manifest["records_sha256"],
        expected_record_count=manifest["record_count"],
    )
    rollback_context_analysis_backfill(
        database_path,
        receipt_path=receipt_path,
        output_path=tmp_path / "rollback.json",
        effects="analysis-only",
        apply=True,
        expected_receipt_sha256=receipt["receipt_sha256"],
    )
