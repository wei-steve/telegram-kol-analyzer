"""Deployment gate for protected-entry crash and fault boundaries.

The detailed production-path assertions live beside the subsystems that own
each boundary.  This module deliberately re-exports them as one focused gate
and adds the frozen-history regression required for rollout.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import DateTime, select

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_request_governor import (
    DeepcoinRequestGovernor,
    GovernorMode,
)
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.execution_bindings import (
    load_deepcoin_execution_reconciliation_snapshot_read_only,
)
from telegram_kol_research.models import Base
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.web_queries import load_group_messages

from test_deepcoin_client_retry import (
    test_attempt_recorder_failure_after_post_is_unknown_without_resend
    as test_fault_after_response_before_local_writer_evidence,
    test_post_ambiguous_failure_is_unknown_and_never_retried
    as test_fault_after_exchange_acceptance_before_response,
    test_post_deadline_is_rechecked_after_timestamp_and_signing_before_send
    as test_fault_immediately_before_http_send,
)
from test_deepcoin_execution_operations import (
    operation_store,
    test_concurrent_reservation_and_attempt_ordinals_have_one_owner
    as test_fault_concurrent_same_operation_has_one_owner,
)
from test_deepcoin_request_governor import (
    test_two_processes_cannot_reserve_the_same_strict_slot
    as test_fault_cross_process_governor_contention,
)
from test_protected_entry_execution import (
    test_exhaustive_fact_matrix_never_authorizes_a_stale_or_unsafe_submit
    as test_fault_property_matrix_never_authorizes_unsafe_submit,
)
from test_protected_entry_reconciliation import (
    test_complete_absence_never_authorizes_resend_or_terminal_absence
    as test_fault_read_unavailable_never_becomes_absence_proof,
)
from test_recovery_live_submit import (
    test_later_baseline_attempt_budget_survives_crash_and_restart
    as test_fault_during_readback_attempts_preserves_global_budget,
    test_next_leg_crash_after_writer_boundary_restarts_get_only_without_post
    as test_fault_after_baseline_before_later_leg_post,
    test_protected_entry_crash_after_confirmed_operation_resumes_without_repeating_writer
    as test_fault_after_writer_evidence_before_operation_transition,
    test_protected_entry_market_persists_operations_and_blocks_later_leg_on_protection_failure
    as test_fault_between_first_and_second_protection,
    test_protected_entry_market_rechecks_live_mode_at_writer_boundary
    as test_fault_after_operation_commit_before_post,
    test_protected_entry_market_request_identity_conflict_sends_zero_posts
    as test_fault_before_operation_commit,
    test_protected_entry_market_crash_after_post_resumes_readback_without_second_post
    as test_fault_after_response_before_operation_state_persistence,
    test_restart_reuses_durable_post_protection_snapshot_without_third_get
    as test_fault_after_all_protections_before_later_leg_baseline,
    test_completed_later_child_replays_after_rollout_disable_without_post
    as test_fault_after_later_leg_post_before_result_persistence,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "protected_entry"
    / "frozen_partial_entry.json"
)
FROZEN_TABLES = (
    "trade_signals",
    "strategy_lifecycles",
    "execution_bindings",
    "execution_order_legs",
    "position_protection_ledger",
    "execution_events",
)
EXPECTED_BOUNDARIES = frozenset(
    {
        "before_operation_commit",
        "after_operation_commit_before_post",
        "immediately_before_http_send",
        "after_exchange_acceptance_before_response",
        "after_response_before_local_writer_evidence",
        "after_writer_evidence_before_operation_transition",
        "during_each_readback_attempt",
        "between_protection_one_and_two",
        "after_all_protections_before_later_leg_baseline",
        "after_baseline_before_later_leg_post",
        "after_later_leg_post_before_result_persistence",
    }
)


def _load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _materialize_row(table, row: dict[str, object]) -> dict[str, object]:
    materialized = dict(row)
    for column in table.columns:
        if column.name not in materialized:
            continue
        value = materialized[column.name]
        if isinstance(column.type, DateTime) and isinstance(value, str):
            materialized[column.name] = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        elif column.name.endswith("_json") and isinstance(value, (dict, list)):
            materialized[column.name] = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
    return materialized


def _seed_fixture(session_factory, fixture: dict[str, object]) -> None:
    rows_by_table = fixture["rows"]
    assert isinstance(rows_by_table, dict)
    with session_factory() as session:
        for table_name, rows in rows_by_table.items():
            table = Base.metadata.tables[str(table_name)]
            assert isinstance(rows, list)
            if rows:
                session.execute(
                    table.insert(),
                    [_materialize_row(table, dict(row)) for row in rows],
                )
        session.commit()


def _frozen_rows(session_factory) -> bytes:
    result: dict[str, list[dict[str, object]]] = {}
    with session_factory() as session:
        for table_name in FROZEN_TABLES:
            table = Base.metadata.tables[table_name]
            rows = session.execute(select(table).order_by(table.c.id)).mappings()
            result[table_name] = [
                {
                    key: (
                        value.replace(tzinfo=UTC).isoformat()
                        if isinstance(value, datetime)
                        else value
                    )
                    for key, value in dict(row).items()
                }
                for row in rows
            ]
    return json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class _FrozenSnapshotClient:
    uid_scope_hash = "f" * 64

    def __init__(self, snapshot: dict[str, object]):
        self._snapshot = snapshot

    def _response(self, key: str):
        rows = self._snapshot[key]
        assert isinstance(rows, list)
        return {"code": "0", "data": [dict(row) for row in rows]}

    def read_positions(self):
        return self._response("positions")

    def read_open_orders(self):
        return self._response("open_orders")

    def read_position_history(self, *, inst_id, pos_id=None):
        return self._response("position_history")

    def read_order_history(self, *, inst_id=None):
        return self._response("order_history")

    def read_trade_fills(self, *, inst_id=None):
        return self._response("trade_fills")

    def read_trigger_orders_pending(self, *, inst_id):
        return self._response("pending_trigger_orders")

    def read_trigger_order_history(self, *, inst_id=None):
        return self._response("trigger_history")


def test_fault_boundary_manifest_is_complete():
    fixture = _load_fixture()
    assert frozenset(fixture["crash_boundaries"]) == EXPECTED_BOUNDARIES


def test_synthetic_incident_fixture_is_sanitized_and_has_expected_shape():
    fixture = _load_fixture()
    serialized = json.dumps(fixture, ensure_ascii=False, sort_keys=True).lower()

    assert fixture["schema_version"] == 1
    assert fixture["sanitized"] is True
    assert "authorization" not in serialized
    assert "dc-access" not in serialized
    assert "api_key" not in serialized
    assert "raw_response" not in serialized
    assert len(fixture["rows"]["execution_order_legs"]) == 1
    assert len(fixture["rows"]["position_protection_ledger"]) == 2
    states = {
        row["state"] for row in fixture["rows"]["deepcoin_execution_operations"]
    }
    assert {"protected", "pre_submit_deferred"}.issubset(states)


def test_frozen_partial_entry_is_immutable_across_read_only_consumers(tmp_path):
    fixture = _load_fixture()
    database_path = tmp_path / "frozen-partial-entry.db"
    session_factory = create_session_factory(database_path)
    _seed_fixture(session_factory, fixture)
    before = _frozen_rows(session_factory)

    # Re-running bootstrap must only add/validate schema, never reinterpret rows.
    session_factory = create_session_factory(database_path)

    governor = DeepcoinRequestGovernor(
        base_url="https://api.synthetic.invalid",
        api_key="synthetic-key",
        mode=GovernorMode.TELEMETRY,
        state_directory=tmp_path / "governor",
        monotonic_factory=lambda: 1.0,
        sleep_fn=lambda _seconds: None,
    )
    lease = governor.acquire(
        method="GET",
        request_path="/deepcoin/market/tickers",
        priority=RequestPriority.BACKGROUND,
        deadline_monotonic=2.0,
    )
    assert lease.waited_ms == 0

    snapshot = fixture["exchange_snapshot"]
    assert isinstance(snapshot, dict)
    loaded = load_deepcoin_execution_reconciliation_snapshot_read_only(
        session_factory,
        client=_FrozenSnapshotClient(snapshot),
    )
    assert loaded.errors == {}

    messages = load_group_messages(
        session_factory,
        chat_id=int(fixture["identity"]["chat_id"]),
        limit=10,
    )
    assert len(messages) == 1
    assert list_pending_trade_signals(session_factory, limit=10) == []
    assert _frozen_rows(session_factory) == before
