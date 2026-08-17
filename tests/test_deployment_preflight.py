from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deployment_preflight import (
    DeploymentPreflightFacts,
    DeploymentPreflightInputError,
    build_final_deployment_preflight_artifact,
    build_preliminary_deployment_preflight_artifact,
    build_deployment_preflight_artifact,
    collect_deployment_preflight_facts,
    verify_deployment_preflight_artifact,
    verify_phase_bound_deployment_preflight_artifact,
)
from telegram_kol_research.deployment_change_surface import ChangeSurfaceFacts


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
EXPECTED_COMMIT = "a" * 40
CANDIDATE_COMMIT = "b" * 40


def _surface(change_class="schema_compatible", **changes):
    base = ChangeSurfaceFacts(
        registry_version=1,
        effective_change_class=change_class,
        underdeclared=False,
        changed_path_count=3,
        change_surface_fingerprint="c" * 64,
        restart_compatibility_changed=False,
        restart_handler_fingerprint="d" * 64,
        blocking_reason_codes=(),
    )
    return replace(base, **changes)


def _facts(**changes) -> DeploymentPreflightFacts:
    base = DeploymentPreflightFacts(
        database_watermark={
            "raw_message_max_id": 10,
            "instruction_item_max_id": 8,
            "trade_signal_max_id": 4,
            "execution_event_max_id": 7,
        },
        work_classification_counts={},
        work_evidence_fingerprint=hashlib.sha256(b"[]").hexdigest(),
        protected_open_position_count=0,
        exchange_snapshot_available=True,
        exchange_snapshot_complete=True,
        exchange_snapshot_fresh=True,
        exchange_snapshot_stable=True,
        schema_backup_valid=None,
        schema_migration_dry_run_valid=True,
        prior_schema_missing_table_count=0,
        reviewed_shadow_evidence=False,
        explicit_live_authorization=False,
        reviewed_shadow_evidence_fingerprint=None,
    )
    return replace(base, **changes)


def _build(change_class: str, facts: DeploymentPreflightFacts):
    return build_deployment_preflight_artifact(
        expected_commit=EXPECTED_COMMIT,
        change_class=change_class,
        facts=facts,
        now=NOW,
    )


def test_in_flight_write_blocks_every_change_class():
    artifact = _build(
        "code",
        _facts(work_classification_counts={"in_flight_write": {"trade_signals": 1}}),
    )

    assert artifact["decision"] == "BLOCK"
    assert "deployment_in_flight_write" in artifact["reason_codes"]


def test_protected_open_position_is_a_warning_not_a_blocker():
    artifact = _build(
        "code",
        _facts(protected_open_position_count=2),
    )

    assert artifact["decision"] == "WARN"
    assert artifact["reason_codes"] == ["protected_open_positions_present"]


def test_unknown_outcome_blocks_even_when_old():
    artifact = _build(
        "code",
        _facts(work_classification_counts={"unknown_outcome": {"execution_contracts": 3}}),
    )

    assert artifact["decision"] == "BLOCK"
    assert artifact["reason_codes"] == ["deployment_unknown_outcome"]


@pytest.mark.parametrize(
    ("change_class", "expected"),
    [("code", "WARN"), ("execution_writer", "BLOCK")],
)
def test_incomplete_exchange_snapshot_depends_on_change_class(
    change_class,
    expected,
):
    artifact = _build(
        change_class,
        _facts(
            exchange_snapshot_complete=False,
            exchange_snapshot_fresh=False,
        ),
    )

    assert artifact["decision"] == expected
    assert "exchange_snapshot_incomplete" in artifact["reason_codes"]


def test_schema_compatible_requires_a_valid_backup():
    artifact = _build(
        "schema_compatible",
        _facts(schema_backup_valid=False),
    )

    assert artifact["decision"] == "BLOCK"
    assert "schema_backup_unavailable" in artifact["reason_codes"]


@pytest.mark.parametrize(
    ("reviewed", "authorized", "reason"),
    [
        (False, True, "reviewed_shadow_evidence_missing"),
        (True, False, "live_promotion_authorization_missing"),
    ],
)
def test_live_promotion_requires_reviewed_shadow_and_explicit_authorization(
    reviewed,
    authorized,
    reason,
):
    artifact = _build(
        "live_promotion",
        _facts(
            reviewed_shadow_evidence=reviewed,
            reviewed_shadow_evidence_fingerprint=("c" * 64 if reviewed else None),
            explicit_live_authorization=authorized,
        ),
    )

    assert artifact["decision"] == "BLOCK"
    assert reason in artifact["reason_codes"]


def test_live_promotion_artifact_records_reviewed_shadow_fingerprint():
    evidence_fingerprint = "c" * 64
    artifact = _build(
        "live_promotion",
        _facts(
            reviewed_shadow_evidence=True,
            reviewed_shadow_evidence_fingerprint=evidence_fingerprint,
            explicit_live_authorization=True,
        ),
    )

    assert artifact["decision"] == "PASS"
    assert (
        artifact["checked_facts"]["reviewed_shadow_evidence_fingerprint"]
        == evidence_fingerprint
    )


def test_shadow_evidence_must_be_a_matching_review_artifact(tmp_path):
    import sqlite3

    database = tmp_path / "research.db"
    evidence = tmp_path / "shadow-review.json"
    _create_preflight_database(database)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO signal_candidates VALUES (1, 'mimo_authoritative')"
    )
    connection.execute(
        "INSERT INTO trading_settings VALUES (1, 'global', ?)",
        (
            json.dumps(
                {
                    "instruction_execution_contract_mode": "shadow",
                    "instruction_execution_entry_after_item_id": 8,
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO message_instruction_items "
        "(id, status, updated_at, signal_candidate_id, instruction_kind, "
        "retired_at, result_json, error_json) VALUES (9, 'submitted', ?, 1, "
        "'entry', NULL, ?, NULL)",
        (
            (NOW - timedelta(minutes=15)).replace(tzinfo=None).isoformat(" "),
            json.dumps(
                {
                    "instruction_execution_contract": {
                        "contract_id": 2,
                        "divergence": False,
                        "state": "verified",
                        "state_version": 3,
                        "terminal_kind": "verified_entry",
                        "completion_scope": "full",
                    }
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO instruction_execution_contracts "
        "(id, state, updated_at, message_instruction_item_id, state_version, "
        "terminal_kind, completion_scope) "
        "VALUES (2, 'verified', ?, 9, 3, 'verified_entry', 'full')",
        ((NOW - timedelta(minutes=15)).replace(tzinfo=None).isoformat(" "),),
    )
    connection.commit(); connection.close()
    body = {
        "schema_version": 1,
        "kind": "instruction_execution_shadow_review",
        "reviewed_commit": EXPECTED_COMMIT,
        "promotion_scope": "entry",
        "activation_watermark": 8,
        "observation_end_item_id": 9,
        "observation_started_at": (NOW - timedelta(hours=2)).isoformat(),
        "observation_ended_at": (NOW - timedelta(minutes=10)).isoformat(),
        "eligible_contract_count": 1,
        "observed_contract_count": 1,
        "unexplained_divergence_count": 0,
        "conclusion": "approved",
    }
    body["fingerprint"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence.write_text(json.dumps(body), encoding="utf-8")

    valid = collect_deployment_preflight_facts(
        database_path=database,
        change_class="live_promotion",
        reviewed_shadow_evidence_path=evidence,
        expected_commit=EXPECTED_COMMIT,
        now=NOW,
    )
    mismatched = collect_deployment_preflight_facts(
        database_path=database,
        change_class="live_promotion",
        reviewed_shadow_evidence_path=evidence,
        expected_commit="b" * 40,
        now=NOW,
    )
    impossible_body = dict(body)
    impossible_body["activation_watermark"] = 9
    impossible_body.pop("fingerprint")
    impossible_body["fingerprint"] = hashlib.sha256(
        json.dumps(impossible_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence.write_text(json.dumps(impossible_body), encoding="utf-8")
    impossible = collect_deployment_preflight_facts(
        database_path=database,
        change_class="live_promotion",
        reviewed_shadow_evidence_path=evidence,
        expected_commit=EXPECTED_COMMIT,
        now=NOW,
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE trading_settings SET value_json = ? WHERE key = 'global'",
        (
            json.dumps(
                {
                    "instruction_execution_contract_mode": "disabled",
                    "instruction_execution_entry_after_item_id": 8,
                }
            ),
        ),
    )
    connection.commit(); connection.close()
    evidence.write_text(json.dumps(body), encoding="utf-8")
    disabled_mode = collect_deployment_preflight_facts(
        database_path=database,
        change_class="live_promotion",
        reviewed_shadow_evidence_path=evidence,
        expected_commit=EXPECTED_COMMIT,
        now=NOW,
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE trading_settings SET value_json = ? WHERE key = 'global'",
        (
            json.dumps(
                {
                    "instruction_execution_contract_mode": "shadow",
                    "instruction_execution_entry_after_item_id": 8,
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO signal_candidates VALUES (2, 'mimo_authoritative')"
    )
    connection.execute(
        "INSERT INTO message_instruction_items "
        "(id, status, updated_at, signal_candidate_id, instruction_kind, "
        "retired_at, result_json, error_json) VALUES (10, 'submitted', ?, 2, "
        "'entry', NULL, NULL, NULL)",
        ((NOW - timedelta(minutes=15)).replace(tzinfo=None).isoformat(" "),),
    )
    connection.commit(); connection.close()
    missing_contract_body = dict(body)
    missing_contract_body["observation_end_item_id"] = 10
    missing_contract_body["eligible_contract_count"] = 2
    missing_contract_body.pop("fingerprint")
    missing_contract_body["fingerprint"] = hashlib.sha256(
        json.dumps(
            missing_contract_body, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    evidence.write_text(json.dumps(missing_contract_body), encoding="utf-8")
    missing_contract = collect_deployment_preflight_facts(
        database_path=database,
        change_class="live_promotion",
        reviewed_shadow_evidence_path=evidence,
        expected_commit=EXPECTED_COMMIT,
        now=NOW,
    )

    assert valid.reviewed_shadow_evidence is True
    assert valid.reviewed_shadow_evidence_fingerprint == body["fingerprint"]
    assert mismatched.reviewed_shadow_evidence is False
    assert impossible.reviewed_shadow_evidence is False
    assert disabled_mode.reviewed_shadow_evidence is False
    assert missing_contract.reviewed_shadow_evidence is False


def test_artifact_has_bounded_contract_and_verifiable_expiry():
    artifact = _build("code", _facts())

    assert artifact["schema_version"] == 1
    assert artifact["expected_commit"] == EXPECTED_COMMIT
    assert artifact["change_class"] == "code"
    assert artifact["decision"] == "PASS"
    assert artifact["created_at"] == NOW.isoformat()
    assert artifact["expires_at"] == (NOW + timedelta(minutes=5)).isoformat()
    assert len(artifact["fingerprint"]) == 64
    assert verify_deployment_preflight_artifact(
        artifact,
        expected_commit=EXPECTED_COMMIT,
        change_class="code",
        now=NOW + timedelta(minutes=4),
    ) == "PASS"

    tampered = dict(artifact, decision="WARN")
    with pytest.raises(DeploymentPreflightInputError, match="fingerprint"):
        verify_deployment_preflight_artifact(
            tampered,
            expected_commit=EXPECTED_COMMIT,
            change_class="code",
            now=NOW,
        )
    with pytest.raises(DeploymentPreflightInputError, match="expired"):
        verify_deployment_preflight_artifact(
            artifact,
            expected_commit=EXPECTED_COMMIT,
            change_class="code",
            now=NOW + timedelta(minutes=6),
        )


def test_preliminary_phase_artifact_binds_commits_class_and_surface():
    artifact = build_preliminary_deployment_preflight_artifact(
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        facts=_facts(
            schema_backup_valid=None,
            schema_migration_dry_run_valid=None,
        ),
        now=NOW,
    )

    assert artifact["schema_version"] == 2
    assert artifact["phase"] == "preliminary"
    assert artifact["production_commit"] == EXPECTED_COMMIT
    assert artifact["candidate_commit"] == CANDIDATE_COMMIT
    assert artifact["requested_change_class"] == "schema_compatible"
    assert artifact["effective_change_class"] == "schema_compatible"
    assert artifact["preliminary_fingerprint"] is None
    assert artifact["decision"] == "PASS"
    assert verify_phase_bound_deployment_preflight_artifact(
        artifact,
        phase="preliminary",
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        now=NOW,
    ) == "PASS"


def test_final_phase_binds_preliminary_and_blocks_new_unknown_outcome():
    preliminary_facts = _facts(
        schema_backup_valid=None,
        schema_migration_dry_run_valid=None,
    )
    preliminary = build_preliminary_deployment_preflight_artifact(
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        facts=preliminary_facts,
        now=NOW,
    )
    final_facts = replace(
        preliminary_facts,
        work_classification_counts={
            "unknown_outcome": {"execution_order_legs": 1}
        },
        schema_backup_valid=True,
        schema_migration_dry_run_valid=True,
    )

    artifact = build_final_deployment_preflight_artifact(
        preliminary_artifact=preliminary,
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        facts=final_facts,
        now=NOW + timedelta(minutes=1),
    )

    assert artifact["phase"] == "final"
    assert artifact["preliminary_fingerprint"] == preliminary["fingerprint"]
    assert artifact["decision"] == "BLOCK"
    assert "deployment_unknown_outcome" in artifact["reason_codes"]


def test_final_phase_rejects_parent_identity_and_watermark_drift():
    preliminary = build_preliminary_deployment_preflight_artifact(
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        facts=_facts(),
        now=NOW,
    )
    final_facts = replace(
        _facts(),
        database_watermark={
            "raw_message_max_id": 9,
            "instruction_item_max_id": 8,
            "trade_signal_max_id": 4,
            "execution_event_max_id": 7,
        },
        schema_backup_valid=True,
        schema_migration_dry_run_valid=True,
    )

    with pytest.raises(DeploymentPreflightInputError, match="watermark_regression"):
        build_final_deployment_preflight_artifact(
            preliminary_artifact=preliminary,
            production_commit=EXPECTED_COMMIT,
            candidate_commit=CANDIDATE_COMMIT,
            requested_change_class="schema_compatible",
            change_surface=_surface(),
            facts=final_facts,
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(DeploymentPreflightInputError, match="class_mismatch"):
        build_final_deployment_preflight_artifact(
            preliminary_artifact=preliminary,
            production_commit=EXPECTED_COMMIT,
            candidate_commit=CANDIDATE_COMMIT,
            requested_change_class="code",
            change_surface=_surface("code"),
            facts=replace(
                _facts(),
                schema_backup_valid=True,
                schema_migration_dry_run_valid=True,
            ),
            now=NOW + timedelta(minutes=1),
        )


def test_final_artifact_cannot_be_reused_as_preliminary_parent():
    preliminary = build_preliminary_deployment_preflight_artifact(
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        facts=_facts(),
        now=NOW,
    )
    final_facts = replace(
        _facts(),
        schema_backup_valid=True,
        schema_migration_dry_run_valid=True,
    )
    final = build_final_deployment_preflight_artifact(
        preliminary_artifact=preliminary,
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        facts=final_facts,
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(DeploymentPreflightInputError, match="phase_mismatch"):
        build_final_deployment_preflight_artifact(
            preliminary_artifact=final,
            production_commit=EXPECTED_COMMIT,
            candidate_commit=CANDIDATE_COMMIT,
            requested_change_class="schema_compatible",
            change_surface=_surface(),
            facts=final_facts,
            now=NOW + timedelta(minutes=2),
        )


def test_final_artifact_requires_parent_on_verify_and_boolean_schema_evidence():
    preliminary = build_preliminary_deployment_preflight_artifact(
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        facts=_facts(),
        now=NOW,
    )
    final_facts = replace(
        _facts(),
        schema_backup_valid=True,
        schema_migration_dry_run_valid=True,
    )
    final = build_final_deployment_preflight_artifact(
        preliminary_artifact=preliminary,
        production_commit=EXPECTED_COMMIT,
        candidate_commit=CANDIDATE_COMMIT,
        requested_change_class="schema_compatible",
        change_surface=_surface(),
        facts=final_facts,
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(DeploymentPreflightInputError, match="parent_required"):
        verify_phase_bound_deployment_preflight_artifact(
            final,
            phase="final",
            production_commit=EXPECTED_COMMIT,
            candidate_commit=CANDIDATE_COMMIT,
            requested_change_class="schema_compatible",
            change_surface=_surface(),
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(DeploymentPreflightInputError, match="schema_evidence_invalid"):
        build_final_deployment_preflight_artifact(
            preliminary_artifact=preliminary,
            production_commit=EXPECTED_COMMIT,
            candidate_commit=CANDIDATE_COMMIT,
            requested_change_class="schema_compatible",
            change_surface=_surface(),
            facts=replace(final_facts, schema_backup_valid="true"),
            now=NOW + timedelta(minutes=1),
        )


def test_blocking_artifact_with_warnings_remains_verifiable():
    artifact = _build(
        "execution_writer",
        _facts(
            work_classification_counts={
                "in_flight_write": {"position_mutations": 1},
                "unknown_outcome": {"execution_contracts": 1},
            },
            protected_open_position_count=2,
            exchange_snapshot_complete=False,
        ),
    )

    assert artifact["decision"] == "BLOCK"
    assert artifact["reason_codes"] == sorted(artifact["reason_codes"])
    assert verify_deployment_preflight_artifact(
        artifact,
        expected_commit=EXPECTED_COMMIT,
        change_class="execution_writer",
        now=NOW,
    ) == "BLOCK"


def _create_preflight_database(path: Path) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY);
        CREATE TABLE message_instruction_items (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            signal_candidate_id INTEGER, instruction_kind TEXT,
            retired_at TEXT, result_json TEXT, error_json TEXT
        );
        CREATE TABLE signal_candidates (
            id INTEGER PRIMARY KEY, parse_source TEXT
        );
        CREATE TABLE trading_settings (
            id INTEGER PRIMARY KEY, key TEXT, value_json TEXT
        );
        CREATE TABLE trade_signals (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE execution_events (id INTEGER PRIMARY KEY);
        CREATE TABLE execution_order_legs (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE instruction_execution_contracts (
            id INTEGER PRIMARY KEY, state TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            message_instruction_item_id INTEGER, state_version INTEGER,
            terminal_kind TEXT, completion_scope TEXT
        );
        CREATE TABLE strategy_revision_batches (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE strategy_management_batches (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE strategy_management_legs (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE strategy_management_components (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE position_mutation_intents (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE bound_position_close_reservations (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE position_protection_legs (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE position_backup_stop_orders (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE position_take_profit_orders (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE trigger_protection_intents (
            id INTEGER PRIMARY KEY, recovery_state TEXT,
            recovery_disposition TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE trigger_protection_stop_rescues (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE trigger_take_profit_convergences (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE strategy_break_even_convergences (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE strategy_break_even_convergence_legs (
            id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE position_protection_ledger (
            id INTEGER PRIMARY KEY, venue TEXT, order_id TEXT, pos_id TEXT,
            instrument_id TEXT, side TEXT, purpose TEXT, status TEXT
        );
        CREATE TABLE source_message_deletion_exits (
            id INTEGER PRIMARY KEY, state TEXT, updated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    fresh = (NOW - timedelta(minutes=1)).replace(tzinfo=None).isoformat(" ")
    old = (NOW - timedelta(days=3)).replace(tzinfo=None).isoformat(" ")
    connection.execute("INSERT INTO raw_messages VALUES (10)")
    connection.execute(
        "INSERT INTO message_instruction_items (id, status, updated_at) "
        "VALUES (8, 'submitted', ?)",
        (old,),
    )
    connection.execute(
        "INSERT INTO trade_signals (id, status, updated_at) "
        "VALUES (4, 'processing', ?)",
        (fresh,),
    )
    connection.execute("INSERT INTO execution_events VALUES (7)")
    connection.execute(
        "INSERT INTO instruction_execution_contracts (id, state, updated_at) "
        "VALUES (1, 'submit_unknown', ?)",
        (old,),
    )
    connection.commit()
    connection.close()


def _write_snapshot(
    path: Path,
    *,
    complete: bool = True,
    version: str = "snapshot-v1",
    captured_at: datetime | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "captured_at": (captured_at or NOW - timedelta(seconds=20)).isoformat(),
                "payload": {
                    "error": None,
                    "_live_source": {
                        "positions": [
                            {
                                "posId": "SECRET-POS-ID",
                                "pos": "1",
                                "instId": "BTC-USDT-SWAP",
                                "posSide": "long",
                                "slTriggerPx": "60000",
                            }
                        ],
                        "tpsl_evidence_available": complete,
                        "tpsl_orders": [],
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_collector_is_read_only_bounded_and_does_not_emit_position_ids(tmp_path):
    database = tmp_path / "research.db"
    snapshot = tmp_path / "deepcoin_live_positions.json"
    _create_preflight_database(database)
    _write_snapshot(snapshot)
    previous = tmp_path / "deepcoin_live_positions.previous.json"
    _write_snapshot(
        previous,
        version="snapshot-v0",
        captured_at=NOW - timedelta(seconds=40),
    )
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="execution_writer",
        live_snapshot_path=snapshot,
        previous_live_snapshot_path=previous,
        now=NOW,
    )

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert facts.work_classification_counts["in_flight_write"] == {
        "trade_signals": 1
    }
    assert facts.work_classification_counts["unknown_outcome"] == {
        "execution_contracts": 1
    }
    assert facts.protected_open_position_count == 1
    assert facts.exchange_snapshot_complete is True
    assert facts.exchange_snapshot_stable is True
    assert "SECRET-POS-ID" not in json.dumps(facts.to_json())


def test_collector_opens_one_explicit_read_transaction(tmp_path, monkeypatch):
    import sqlite3
    import telegram_kol_research.deployment_preflight as module

    database = tmp_path / "research.db"
    _create_preflight_database(database)
    real_connect = sqlite3.connect
    statements: list[str] = []

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, statement, parameters=()):
            statements.append(str(statement))
            return self.connection.execute(statement, parameters)

        def rollback(self):
            return self.connection.rollback()

    monkeypatch.setattr(
        module.sqlite3,
        "connect",
        lambda *args, **kwargs: ConnectionProxy(real_connect(*args, **kwargs)),
    )

    collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        now=NOW,
    )

    assert statements.count("BEGIN") == 1


@pytest.mark.parametrize(
    ("table", "state_column", "state", "fact_name", "classification"),
    [
        (
            "position_mutation_intents",
            "status",
            "submit_unknown",
            "position_mutations",
            "unknown_outcome",
        ),
        (
            "strategy_management_batches",
            "status",
            "protection_ready",
            "management_batches",
            "restart_safe_wait",
        ),
        (
            "message_instruction_items",
            "status",
            "pending",
            "instruction_items",
            "restart_safe_wait",
        ),
        (
            "strategy_management_components",
            "status",
            "definitely_rejected",
            "management_components",
            "restart_safe_wait",
        ),
        (
            "bound_position_close_reservations",
            "status",
            "unknown_exchange_outcome",
            "position_closes",
            "unknown_outcome",
        ),
        (
            "trigger_protection_intents",
            "recovery_state",
            "submitting",
            "protection_intents",
            "in_flight_write",
        ),
        (
            "execution_order_legs",
            "status",
            "cancel_submitting",
            "execution_order_legs",
            "in_flight_write",
        ),
        (
            "strategy_revision_batches",
            "status",
            "submitting_replacements",
            "strategy_revisions",
            "in_flight_write",
        ),
        (
            "strategy_management_legs",
            "status",
            "submit_unknown",
            "management_legs",
            "unknown_outcome",
        ),
        (
            "position_backup_stop_orders",
            "status",
            "unknown_exchange_outcome",
            "backup_stop_orders",
            "unknown_outcome",
        ),
        (
            "position_take_profit_orders",
            "status",
            "cancel_requested",
            "take_profit_orders",
            "unknown_outcome",
        ),
        (
            "strategy_management_batches",
            "status",
            "submit_unknown",
            "management_batches",
            "unknown_outcome",
        ),
        (
            "source_message_deletion_exits",
            "state",
            "recovery_required",
            "source_deletions",
            "unknown_outcome",
        ),
        (
            "trigger_protection_stop_rescues",
            "status",
            "submit_unknown",
            "protection_rescues",
            "unknown_outcome",
        ),
        (
            "trigger_take_profit_convergences",
            "status",
            "reserved",
            "trigger_take_profit_convergences",
            "in_flight_write",
        ),
        (
            "strategy_break_even_convergences",
            "status",
            "executing_market_decisions",
            "break_even_convergences",
            "in_flight_write",
        ),
        (
            "strategy_break_even_convergence_legs",
            "status",
            "decision_reserved",
            "break_even_convergence_legs",
            "in_flight_write",
        ),
    ],
)
def test_collector_blocks_fresh_nonterminal_writer_states(
    tmp_path,
    table,
    state_column,
    state,
    fact_name,
    classification,
):
    database = tmp_path / "research.db"
    _create_preflight_database(database)
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM trade_signals")
    connection.execute(
        f"INSERT INTO {table} (id, {state_column}, updated_at) VALUES (99, ?, ?)",
        (
            state,
            (NOW - timedelta(seconds=30)).replace(tzinfo=None).isoformat(" "),
        ),
    )
    connection.commit()
    connection.close()

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        now=NOW,
    )

    assert facts.work_classification_counts[classification][fact_name] == 1


def test_unprotected_open_position_blocks_deployment(tmp_path):
    database = tmp_path / "research.db"
    snapshot = tmp_path / "snapshot.json"
    _create_preflight_database(database)
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM trade_signals")
    connection.commit()
    connection.close()
    _write_snapshot(snapshot)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["payload"]["_live_source"]["positions"][0]["slTriggerPx"] = ""
    snapshot.write_text(json.dumps(payload), encoding="utf-8")

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        live_snapshot_path=snapshot,
        now=NOW,
    )
    artifact = _build("code", facts)

    assert facts.protected_open_position_count == 0
    assert facts.unprotected_open_position_count == 1
    assert artifact["decision"] == "BLOCK"
    assert "unprotected_open_positions_present" in artifact["reason_codes"]


def test_other_position_stop_cannot_prove_protection(tmp_path):
    database = tmp_path / "research.db"
    snapshot = tmp_path / "snapshot.json"
    _create_preflight_database(database)
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM trade_signals")
    connection.execute(
        "INSERT INTO position_protection_ledger "
        "(id, venue, order_id, pos_id, instrument_id, side, purpose, status) "
        "VALUES (1, 'deepcoin', 'OWNED-ORDER', 'OTHER-POS', "
        "'BTC-USDT-SWAP', 'long', 'stop_loss', 'verified')"
    )
    connection.commit()
    connection.close()
    _write_snapshot(snapshot)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    position = payload["payload"]["_live_source"]["positions"][0]
    position["slTriggerPx"] = ""
    payload["payload"]["_live_source"]["tpsl_orders"] = [
        {
            "ordId": "OWNED-ORDER",
            "posId": "OTHER-POS",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "slTriggerPrice": "59000",
        }
    ]
    snapshot.write_text(json.dumps(payload), encoding="utf-8")

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        live_snapshot_path=snapshot,
        now=NOW,
    )

    assert facts.protected_open_position_count == 0
    assert facts.unprotected_open_position_count == 1


def test_exact_owned_native_sl_trigger_px_proves_protection(tmp_path):
    database = tmp_path / "research.db"
    snapshot = tmp_path / "snapshot.json"
    _create_preflight_database(database)
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM trade_signals")
    connection.execute(
        "INSERT INTO position_protection_ledger "
        "(id, venue, order_id, pos_id, instrument_id, side, purpose, status) "
        "VALUES (1, 'deepcoin', 'OWNED-ORDER', 'SECRET-POS-ID', "
        "'BTC-USDT-SWAP', 'long', 'stop_loss', 'verified')"
    )
    connection.commit(); connection.close()
    _write_snapshot(snapshot)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["payload"]["_live_source"]["positions"][0]["slTriggerPx"] = ""
    payload["payload"]["_live_source"]["tpsl_orders"] = [
        {
            "ordId": "OWNED-ORDER",
            "slTriggerPx": "59000",
        }
    ]
    snapshot.write_text(json.dumps(payload), encoding="utf-8")

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        live_snapshot_path=snapshot,
        now=NOW,
    )

    assert facts.protected_open_position_count == 1
    assert facts.unprotected_open_position_count == 0


def test_ledger_owned_stop_with_conflicting_close_position_is_rejected(tmp_path):
    database = tmp_path / "research.db"
    snapshot = tmp_path / "snapshot.json"
    _create_preflight_database(database)
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM trade_signals")
    connection.execute(
        "INSERT INTO position_protection_ledger "
        "(id, venue, order_id, pos_id, instrument_id, side, purpose, status) "
        "VALUES (1, 'deepcoin', 'OWNED-ORDER', 'SECRET-POS-ID', "
        "'BTC-USDT-SWAP', 'long', 'stop_loss', 'verified')"
    )
    connection.commit(); connection.close()
    _write_snapshot(snapshot)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["payload"]["_live_source"]["positions"][0]["slTriggerPx"] = ""
    payload["payload"]["_live_source"]["tpsl_orders"] = [
        {
            "ordId": "OWNED-ORDER",
            "closePosId": "OTHER-POS",
            "slTriggerPx": "59000",
        }
    ]
    snapshot.write_text(json.dumps(payload), encoding="utf-8")

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        live_snapshot_path=snapshot,
        now=NOW,
    )

    assert facts.protected_open_position_count == 0
    assert facts.unprotected_open_position_count == 1


def test_two_distinct_equal_captures_are_required_for_stability(tmp_path):
    database = tmp_path / "research.db"
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    _create_preflight_database(database)
    _write_snapshot(current, version="v2", captured_at=NOW - timedelta(seconds=10))
    _write_snapshot(previous, version="v1", captured_at=NOW - timedelta(seconds=30))

    stable = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        live_snapshot_path=current,
        previous_live_snapshot_path=previous,
        now=NOW,
    )
    same_file = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        live_snapshot_path=current,
        previous_live_snapshot_path=current,
        now=NOW,
    )

    assert stable.exchange_snapshot_stable is True
    assert same_file.exchange_snapshot_stable is False


def test_market_only_fields_do_not_make_exchange_safety_facts_unstable(tmp_path):
    database = tmp_path / "research.db"
    current = tmp_path / "current.json"
    previous = tmp_path / "previous.json"
    _create_preflight_database(database)
    _write_snapshot(current, version="v2", captured_at=NOW - timedelta(seconds=10))
    _write_snapshot(previous, version="v1", captured_at=NOW - timedelta(seconds=30))
    first = json.loads(current.read_text(encoding="utf-8"))
    second = json.loads(previous.read_text(encoding="utf-8"))
    first["payload"]["_live_source"]["positions"][0]["markPx"] = "65001"
    second["payload"]["_live_source"]["positions"][0]["markPx"] = "65000"
    current.write_text(json.dumps(first), encoding="utf-8")
    previous.write_text(json.dumps(second), encoding="utf-8")

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        live_snapshot_path=current,
        previous_live_snapshot_path=previous,
        now=NOW,
    )

    assert facts.exchange_snapshot_stable is True


def test_live_database_cannot_be_claimed_as_schema_backup(tmp_path):
    database = tmp_path / "research.db"
    snapshot = tmp_path / "snapshot.json"
    _create_preflight_database(database)
    _write_snapshot(snapshot)

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="schema_compatible",
        live_snapshot_path=snapshot,
        schema_backup_path=database,
        now=NOW,
    )
    artifact = _build("schema_compatible", facts)

    assert facts.schema_backup_valid is False
    assert artifact["decision"] == "BLOCK"
    assert "schema_backup_unavailable" in artifact["reason_codes"]


def test_distinct_integrity_checked_schema_backup_is_accepted(tmp_path):
    database = tmp_path / "research.db"
    backup = tmp_path / "research.backup.db"
    snapshot = tmp_path / "snapshot.json"
    create_session_factory(database)
    import sqlite3

    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    target = sqlite3.connect(backup)
    source.backup(target)
    target.close()
    source.close()
    dry_run = tmp_path / "research.dry-run.db"
    source = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    target = sqlite3.connect(dry_run)
    source.backup(target)
    target.close()
    source.close()
    _write_snapshot(snapshot)

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="schema_compatible",
        live_snapshot_path=snapshot,
        schema_backup_path=backup,
        schema_migration_dry_run_path=dry_run,
        now=NOW,
    )
    artifact = _build("schema_compatible", facts)

    assert facts.schema_backup_valid is True
    assert facts.schema_migration_dry_run_valid is True
    assert artifact["decision"] == "WARN"
    assert artifact["reason_codes"] == [
        "exchange_snapshot_incomplete",
        "protected_open_positions_present",
    ]


def test_schema_change_accepts_known_prior_schema_after_dry_run(tmp_path):
    import sqlite3

    database = tmp_path / "prior.db"
    backup = tmp_path / "prior.backup.db"
    dry_run = tmp_path / "prior.dry-run.db"
    create_session_factory(database)
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE trigger_take_profit_convergences")
    connection.commit()
    connection.close()
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    target = sqlite3.connect(backup)
    source.backup(target)
    target.close(); source.close()
    source = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    target = sqlite3.connect(dry_run)
    source.backup(target)
    target.close(); source.close()
    create_session_factory(dry_run)

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="schema_compatible",
        schema_backup_path=backup,
        schema_migration_dry_run_path=dry_run,
        now=NOW,
    )

    assert facts.prior_schema_missing_table_count == 1
    assert facts.schema_backup_valid is True
    assert facts.schema_migration_dry_run_valid is True


def test_schema_change_rejects_unrecognized_missing_table_set(tmp_path):
    import sqlite3

    database = tmp_path / "corrupt.db"
    create_session_factory(database)
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE position_mutation_intents")
    connection.commit()
    connection.close()

    with pytest.raises(
        DeploymentPreflightInputError,
        match="database_prior_schema_unrecognized",
    ):
        collect_deployment_preflight_facts(
            database_path=database,
            change_class="schema_compatible",
            now=NOW,
        )


def test_schema_dry_run_rejects_missing_candidate_model_column(tmp_path):
    import sqlite3

    database = tmp_path / "current.db"
    backup = tmp_path / "current.backup.db"
    dry_run = tmp_path / "current.dry-run.db"
    create_session_factory(database)
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    target = sqlite3.connect(backup)
    source.backup(target)
    target.close(); source.close()
    source = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    target = sqlite3.connect(dry_run)
    source.backup(target)
    target.execute("ALTER TABLE strategy_management_batches DROP COLUMN intent")
    target.commit(); target.close(); source.close()

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="schema_compatible",
        schema_backup_path=backup,
        schema_migration_dry_run_path=dry_run,
        now=NOW,
    )
    artifact = _build("schema_compatible", facts)

    assert facts.schema_backup_valid is True
    assert facts.schema_migration_dry_run_valid is False
    assert artifact["decision"] == "BLOCK"
    assert "schema_migration_dry_run_failed" in artifact["reason_codes"]


def test_schema_dry_run_rejects_partial_replacement_for_global_unique_index(
    tmp_path,
):
    import sqlite3
    import telegram_kol_research.deployment_preflight as module

    dry_run = tmp_path / "partial-index.db"
    create_session_factory(dry_run)
    connection = sqlite3.connect(dry_run)
    connection.execute("DROP INDEX uq_message_instruction_items_idempotency")
    connection.execute(
        "CREATE UNIQUE INDEX uq_message_instruction_items_idempotency "
        "ON message_instruction_items (idempotency_key) WHERE id < 0"
    )
    connection.commit(); connection.close()

    assert module._candidate_model_schema_matches(dry_run) is False


def test_schema_dry_run_rejects_expression_key_in_global_unique_index(tmp_path):
    import sqlite3
    import telegram_kol_research.deployment_preflight as module

    dry_run = tmp_path / "expression-index.db"
    create_session_factory(dry_run)
    connection = sqlite3.connect(dry_run)
    connection.execute("DROP INDEX uq_message_instruction_items_idempotency")
    connection.execute(
        "CREATE UNIQUE INDEX uq_message_instruction_items_idempotency "
        "ON message_instruction_items (idempotency_key, id + 0)"
    )
    connection.commit(); connection.close()

    assert module._candidate_model_schema_matches(dry_run) is False


def test_schema_dry_run_accepts_audited_legacy_position_owner_duplicates(tmp_path):
    import sqlite3
    import telegram_kol_research.deployment_preflight as module

    dry_run = tmp_path / "legacy-position-duplicates.db"
    create_session_factory(dry_run)
    connection = sqlite3.connect(dry_run)
    connection.execute("DROP INDEX uq_execution_order_legs_venue_pos")
    connection.executemany(
        "INSERT INTO execution_order_legs "
        "(execution_binding_id, leg_index, purpose, order_kind, pos_id, status, "
        "venue, attribution_status, created_at, updated_at) VALUES "
        "(?, 1, 'entry', 'unknown', "
        "'legacy-duplicate-pos', 'active', 'deepcoin', 'unassigned', CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP)",
        [(1,), (2,)],
    )
    connection.commit()
    connection.close()
    create_session_factory(dry_run)

    assert module._candidate_model_schema_matches(dry_run) is True


def test_schema_dry_run_rejects_missing_position_owner_index_without_duplicates(
    tmp_path,
):
    import sqlite3
    import telegram_kol_research.deployment_preflight as module

    dry_run = tmp_path / "missing-position-owner-index.db"
    create_session_factory(dry_run)
    connection = sqlite3.connect(dry_run)
    connection.execute("DROP INDEX uq_execution_order_legs_venue_pos")
    connection.commit()
    connection.close()

    assert module._candidate_model_schema_matches(dry_run) is False


@pytest.mark.parametrize(
    "replacement_sql",
    [
        "CREATE INDEX uq_execution_order_legs_venue_pos "
        "ON execution_order_legs (venue, pos_id)",
        "CREATE UNIQUE INDEX uq_execution_order_legs_venue_pos "
        "ON execution_order_legs (venue, pos_id, id + 0)",
    ],
)
def test_schema_dry_run_rejects_invalid_named_position_index_with_duplicates(
    tmp_path,
    replacement_sql,
):
    import sqlite3
    import telegram_kol_research.deployment_preflight as module

    dry_run = tmp_path / "invalid-named-position-index.db"
    create_session_factory(dry_run)
    connection = sqlite3.connect(dry_run)
    connection.execute("DROP INDEX uq_execution_order_legs_venue_pos")
    connection.executemany(
        "INSERT INTO execution_order_legs "
        "(execution_binding_id, leg_index, purpose, order_kind, pos_id, status, "
        "venue, attribution_status, created_at, updated_at) VALUES "
        "(?, 1, 'entry', 'unknown', 'legacy-duplicate-pos', 'active', "
        "'deepcoin', 'unassigned', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        [(1,), (2,)],
    )
    connection.execute(replacement_sql)
    connection.commit()
    connection.close()

    assert module._candidate_model_schema_matches(dry_run) is False


def test_schema_dry_run_rejects_position_index_name_owned_by_another_table(
    tmp_path,
):
    import sqlite3
    import telegram_kol_research.deployment_preflight as module

    dry_run = tmp_path / "cross-table-position-index.db"
    create_session_factory(dry_run)
    connection = sqlite3.connect(dry_run)
    connection.execute("DROP INDEX uq_execution_order_legs_venue_pos")
    connection.executemany(
        "INSERT INTO execution_order_legs "
        "(execution_binding_id, leg_index, purpose, order_kind, pos_id, status, "
        "venue, attribution_status, created_at, updated_at) VALUES "
        "(?, 1, 'entry', 'unknown', 'legacy-duplicate-pos', 'active', "
        "'deepcoin', 'unassigned', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        [(1,), (2,)],
    )
    connection.execute(
        "CREATE UNIQUE INDEX uq_execution_order_legs_venue_pos ON raw_messages(id)"
    )
    connection.commit()
    connection.close()

    assert module._candidate_model_schema_matches(dry_run) is False


def test_collector_rejects_incomplete_database_schema(tmp_path):
    database = tmp_path / "empty.db"
    database.touch()

    with pytest.raises(DeploymentPreflightInputError, match="database_schema"):
        collect_deployment_preflight_facts(
            database_path=database,
            change_class="code",
            now=NOW,
        )


def test_collector_accepts_current_application_schema(tmp_path):
    database = tmp_path / "current.db"
    snapshot = tmp_path / "snapshot.json"
    create_session_factory(database)
    _write_snapshot(snapshot)

    facts = collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        live_snapshot_path=snapshot,
        now=NOW,
    )

    assert facts.work_classification_counts == {}
    assert facts.database_watermark == {
        "raw_message_max_id": 0,
        "instruction_item_max_id": 0,
        "trade_signal_max_id": 0,
        "execution_event_max_id": 0,
    }
