"""Deployment gate for protected-entry crash and fault boundaries.

The detailed production-path assertions live beside the subsystems that own
each boundary.  This module deliberately re-exports them as one focused gate
and adds the frozen-history regression required for rollout.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import DateTime, select

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_request_governor import (
    DeepcoinRequestGovernor,
    GovernorMode,
)
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.deepcoin_request_policy import (
    ErrorCategory,
    OutcomeCertainty,
)
from telegram_kol_research.deepcoin_client import RequestAttemptFact
from telegram_kol_research.deepcoin_execution_operations import (
    record_request_attempt,
    reserve_execution_operation,
)
from telegram_kol_research.execution_bindings import (
    load_deepcoin_execution_reconciliation_snapshot_read_only,
)
from telegram_kol_research.models import (
    Base,
    DeepcoinExecutionOperation,
    DeepcoinRequestAttempt,
    StrategyLifecycle,
    TradeSignal,
)
from telegram_kol_research.protected_entry_execution import (
    ProtectedEntryFacts,
    decide_protected_entry_transition,
)
from telegram_kol_research.trade_signals import (
    enqueue_trade_signal,
    list_pending_trade_signals,
    load_trade_signal,
)
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
    test_incomplete_snapshot_appends_safe_evidence_and_preserves_unknown_state
    as test_fault_read_unavailable_never_becomes_absence_proof,
)
from test_recovery_live_submit import (
    _StaticContractSpecProvider,
    _Task10CrashBeforePost,
    _Task10ProtectedClient,
    _submit_recovery_signal_direct,
    _task10_one_stop,
    _task10_two_leg_signal,
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
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
UID_SCOPE_HASH = "f" * 64


@dataclass(frozen=True, slots=True)
class _BoundaryContract:
    name: str
    durable_state: str | None
    event: str
    facts: ProtectedEntryFacts
    exact_posts: int
    next_state: str
    next_action: str


def _facts(
    *,
    live: bool = False,
    writer: bool = False,
    required: int = 0,
    confirmed: int = 0,
    complete: bool = False,
    expired: bool = False,
    exhausted: bool = False,
) -> ProtectedEntryFacts:
    return ProtectedEntryFacts(
        live_exposure=live,
        writer_attempted=writer,
        required_protection_count=required,
        confirmed_protection_count=confirmed,
        snapshot_complete=complete,
        operation_deadline_expired=expired,
        preflight_attempts_exhausted=exhausted,
    )


BOUNDARY_CONTRACTS = (
    _BoundaryContract(
        "before_operation_commit",
        None,
        "prepare_entry",
        _facts(),
        0,
        "entry_prepared",
        "none",
    ),
    _BoundaryContract(
        "after_operation_commit_before_post",
        "entry_prepared",
        "request_entry_submit",
        _facts(),
        0,
        "entry_submitting",
        "submit",
    ),
    _BoundaryContract(
        "immediately_before_http_send",
        "entry_submitting",
        "require_recovery",
        _facts(writer=True),
        0,
        "recovery_required",
        "supervision_only",
    ),
    _BoundaryContract(
        "after_exchange_acceptance_before_response",
        "entry_unknown",
        "require_recovery",
        _facts(writer=True),
        1,
        "recovery_required",
        "supervision_only",
    ),
    _BoundaryContract(
        "after_response_before_local_writer_evidence",
        "entry_unknown",
        "require_recovery",
        _facts(writer=True),
        1,
        "recovery_required",
        "supervision_only",
    ),
    _BoundaryContract(
        "after_writer_evidence_before_operation_transition",
        "entry_submitting",
        "require_recovery",
        _facts(writer=True),
        1,
        "recovery_required",
        "supervision_only",
    ),
    _BoundaryContract(
        "during_each_readback_attempt",
        "entry_unknown",
        "require_recovery",
        _facts(writer=True),
        1,
        "recovery_required",
        "supervision_only",
    ),
    _BoundaryContract(
        "between_protection_one_and_two",
        "protection_unknown",
        "protection_readback_confirmed",
        _facts(
            live=True,
            writer=True,
            required=2,
            confirmed=1,
            complete=True,
        ),
        1,
        "protection_unknown",
        "readback_only",
    ),
    _BoundaryContract(
        "after_all_protections_before_later_leg_baseline",
        "protected",
        "start_next_leg_preflight",
        _facts(live=True, required=2, confirmed=2, complete=True),
        0,
        "next_leg_preflight",
        "readback_only",
    ),
    _BoundaryContract(
        "after_baseline_before_later_leg_post",
        "entry_submitting",
        "require_recovery",
        _facts(
            live=True,
            writer=True,
            required=2,
            confirmed=2,
            complete=True,
        ),
        0,
        "recovery_required",
        "supervision_only",
    ),
    _BoundaryContract(
        "after_later_leg_post_before_result_persistence",
        "entry_unknown",
        "require_recovery",
        _facts(
            live=True,
            writer=True,
            required=2,
            confirmed=2,
            complete=True,
        ),
        1,
        "recovery_required",
        "supervision_only",
    ),
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
    assert {contract.name for contract in BOUNDARY_CONTRACTS} == (
        EXPECTED_BOUNDARIES
    )


@pytest.mark.parametrize(
    "contract",
    BOUNDARY_CONTRACTS,
    ids=lambda contract: contract.name,
)
def test_each_crash_boundary_has_closed_durable_restart_contract(
    tmp_path,
    contract,
):
    session_factory = create_session_factory(tmp_path / f"{contract.name}.db")
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="synthetic",
        chat_id=9100001,
        message_id=9200001,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={},
    )
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        row.status = "processing"
        session.add(
            StrategyLifecycle(
                chat_id=9100001,
                message_id=9200001,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=NOW,
                entered_at=NOW,
            )
        )
        session.commit()

    operation = None
    if contract.durable_state is not None:
        phase = {
            "entry_prepared": "entry_preflight",
            "entry_submitting": "entry_submit",
            "entry_unknown": "entry_readback",
            "protection_unknown": "protection_readback",
            "protected": "protection_readback",
        }[contract.durable_state]
        certainty = (
            "unknown"
            if contract.durable_state in {"entry_unknown", "protection_unknown"}
            else (
                "confirmed"
                if contract.durable_state == "protected"
                else "not_sent"
            )
        )
        operation = reserve_execution_operation(
            session_factory,
            operation_key=(
                f"protected-entry:v1:signal:{signal.id}:leg:1:entry"
            ),
            trade_signal_id=signal.id,
            contract_version="1",
            phase=phase,
            state=contract.durable_state,
            outcome_certainty=certainty,
            request_fingerprint="a" * 64,
            economics_fingerprint="b" * 64,
            deadline_at=NOW + timedelta(seconds=10),
            writer_attempted_at=(NOW if contract.facts.writer_attempted else None),
            evidence={
                "client_order_ref": "c" * 64,
                "expected_entry_leg_indices": [1],
                "leg_index": 1,
                "pre_submit_position_refs": [],
                "uid_scope_hash": UID_SCOPE_HASH,
            },
            created_at=NOW,
        )
        durable_post_facts = (
            0
            if contract.name == "after_response_before_local_writer_evidence"
            else contract.exact_posts
        )
        if durable_post_facts:
            record_request_attempt(
                session_factory,
                operation_id=operation.id,
                expected_operation_key=operation.operation_key,
                expected_request_fingerprint=operation.request_fingerprint,
                uid_scope_hash=UID_SCOPE_HASH,
                fact=RequestAttemptFact(
                    ordinal=1,
                    method="POST",
                    normalized_path="/deepcoin/trade/order",
                    phase="entry_submit",
                    priority=RequestPriority.CRITICAL,
                    correlation_id=f"fault:{contract.name}",
                    outcome_certainty=OutcomeCertainty.UNKNOWN,
                    error_category=ErrorCategory.TRANSPORT_TIMEOUT,
                    safe_code="fault_injected_unknown",
                    http_status=None,
                    business_code=None,
                    governor_wait_ms=0,
                    retry_delay_ms=0,
                    latency_ms=1,
                ),
                started_at=NOW,
                completed_at=NOW + timedelta(milliseconds=1),
            )

    current_state = contract.durable_state or "planned"
    restart = decide_protected_entry_transition(
        current_state=current_state,
        event=contract.event,
        facts=contract.facts,
    )

    assert restart.allowed is True
    assert restart.next_state == contract.next_state
    assert restart.next_action == contract.next_action
    if contract.exact_posts:
        assert restart.next_action != "submit"
    with session_factory() as session:
        stored = (
            session.get(DeepcoinExecutionOperation, operation.id)
            if operation is not None
            else None
        )
        post_count = session.query(DeepcoinRequestAttempt).count()
        lifecycle = session.query(StrategyLifecycle).one()
    assert (stored.state if stored is not None else None) == (
        contract.durable_state
    )
    assert post_count == (
        0
        if contract.name == "after_response_before_local_writer_evidence"
        else contract.exact_posts
    )
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None


def test_fault_property_matrix_states_all_five_hard_invariants():
    # No incomplete protection tuple may authorize a later entry writer.
    for required in (1, 2):
        for confirmed in range(required):
            decision = decide_protected_entry_transition(
                current_state="next_leg_preflight",
                event="next_leg_preflight_ready",
                facts=_facts(
                    live=True,
                    required=required,
                    confirmed=confirmed,
                    complete=True,
                ),
            )
            assert decision.next_action != "submit"

    # An unknown writer is readback/supervision only and has one logical POST.
    unknown = next(
        item
        for item in BOUNDARY_CONTRACTS
        if item.name == "after_exchange_acceptance_before_response"
    )
    assert unknown.exact_posts == 1
    assert unknown.next_action in {"readback_only", "supervision_only"}

    # Unavailable exchange evidence is never equivalent to absence proof.
    unavailable = decide_protected_entry_transition(
        current_state="entry_rejected",
        event="confirm_submission_failed_no_exposure",
        facts=_facts(writer=True, complete=False),
    )
    assert unavailable.allowed is False
    assert unavailable.reason_code == "snapshot_incomplete"

    # Live exposure can only stay live/recovery; it cannot terminalize failure.
    live = decide_protected_entry_transition(
        current_state="entry_rejected",
        event="confirm_submission_failed_no_exposure",
        facts=_facts(live=True, writer=True, complete=True),
    )
    assert live.allowed is False
    assert live.next_state != "submission_failed_no_exposure"

    # Once the later-leg pre-submit deadline expires, the durable action is defer.
    expired = decide_protected_entry_transition(
        current_state="next_leg_preflight",
        event="next_leg_preflight_deferred",
        facts=_facts(
            live=True,
            required=2,
            confirmed=2,
            expired=True,
        ),
    )
    assert expired.allowed is True
    assert expired.next_state == "pre_submit_deferred"
    assert expired.next_action == "defer"


def test_real_later_leg_crash_after_post_before_state_persistence_is_get_only(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as submitter

    session_factory = create_session_factory(tmp_path / "later-post-crash.db")
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(session_factory)
    _task10_one_stop(monkeypatch)
    original = submitter._transition_protected_operation
    crashed = False

    def crash_after_later_post(*args, **kwargs):
        nonlocal crashed
        operation = args[1]
        if (
            not crashed
            and operation.parent_operation_id is not None
            and kwargs.get("state") == "entry_pending_readback"
        ):
            crashed = True
            raise _Task10CrashBeforePost()
        return original(*args, **kwargs)

    monkeypatch.setattr(
        submitter,
        "_transition_protected_operation",
        crash_after_later_post,
    )
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    with pytest.raises(_Task10CrashBeforePost):
        _submit_recovery_signal_direct(
            session_factory,
            trade_signal=load_trade_signal(session_factory, signal.id),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=submitted_at,
        )

    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        lifecycle = session.query(StrategyLifecycle).one()
    assert later.state == "entry_submitting"
    assert later.writer_attempted_at is not None
    assert len(client.trigger_payloads) == 1
    assert lifecycle.lifecycle_status != "invalidated"

    monkeypatch.setattr(
        submitter,
        "_transition_protected_operation",
        original,
    )
    result = _submit_recovery_signal_direct(
        session_factory,
        trade_signal=load_trade_signal(session_factory, signal.id),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=submitted_at + timedelta(seconds=1),
    )
    with session_factory() as session:
        later = session.query(DeepcoinExecutionOperation).filter(
            DeepcoinExecutionOperation.operation_key.like("%:leg:2:entry")
        ).one()
        lifecycle = session.query(StrategyLifecycle).one()
    assert result["submitted"] is True
    assert later.state == "completed"
    assert len(client.trigger_payloads) == 1
    assert lifecycle.lifecycle_status != "invalidated"


def test_concurrent_real_protected_entry_has_one_logical_writer_per_operation(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "concurrent-writer.db")
    signal = _task10_two_leg_signal(session_factory)
    client = _Task10ProtectedClient(session_factory)
    _task10_one_stop(monkeypatch)
    barrier = Barrier(2)
    submitted_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

    def run_once():
        barrier.wait(timeout=5)
        try:
            return _submit_recovery_signal_direct(
                session_factory,
                trade_signal=load_trade_signal(session_factory, signal.id),
                deepcoin_client=client,
                contract_spec_provider=_StaticContractSpecProvider(),
                submitted_at=submitted_at,
            )
        except Exception as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: run_once(), range(2)))

    assert len(client.payloads) == 1, outcomes
    assert len(client.position_protection_payloads) == 1
    assert len(client.trigger_payloads) == 1
    assert any(isinstance(item, dict) for item in outcomes)
    with session_factory() as session:
        operations = session.query(DeepcoinExecutionOperation).all()
        lifecycle = session.query(StrategyLifecycle).one()
    assert len({operation.operation_key for operation in operations}) == len(
        operations
    )
    assert lifecycle.lifecycle_status != "invalidated"


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
