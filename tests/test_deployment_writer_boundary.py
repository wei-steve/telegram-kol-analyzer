from __future__ import annotations

import ast
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deployment_change_surface import ChangeSurfaceFacts
from telegram_kol_research.deployment_preflight import (
    build_final_deployment_preflight_artifact,
    build_preliminary_deployment_preflight_artifact,
    collect_deployment_preflight_facts,
)
from telegram_kol_research.deployment_work_evidence import WORK_EVIDENCE_ADAPTERS
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "src/telegram_kol_research/deepcoin_client.py"
NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
PRODUCTION = "a" * 40
CANDIDATE = "b" * 40


# Each exchange-writing method must stay linked to a durable pre-submit owner
# and a fault-injection suite. A new public POST method cannot silently bypass
# this reviewed inventory.
PUBLIC_POST_OWNERS = {
    "place_order": (
        "execution_order_legs",
        "trade_signals",
        "strategy_revision_batches",
        "tests/test_instruction_execution_fault_injection.py",
    ),
    "trigger_order": (
        "execution_order_legs",
        "position_protection_legs",
        "trade_signals",
        "trigger_protection_intents",
        "strategy_revision_batches",
        "tests/test_instruction_execution_fault_injection.py",
    ),
    "set_position_sltp": (
        "position_mutation_intents",
        "tests/test_position_mutation_gateway.py",
    ),
    "cancel_position_sltp": (
        "position_mutation_intents",
        "tests/test_position_mutation_gateway.py",
    ),
    "replace_order_sltp": (
        "execution_order_legs",
        "position_protection_legs",
        "tests/test_instruction_execution_fault_injection.py",
    ),
    "cancel_order": (
        "trade_signals",
        "strategy_management_components",
        "strategy_management_batches",
        "strategy_revision_batches",
        "tests/test_strategy_management_reconciliation.py",
    ),
    "cancel_trigger_order": (
        "trade_signals",
        "strategy_management_components",
        "strategy_management_batches",
        "strategy_revision_batches",
        "trigger_protection_intents",
        "position_mutation_intents",
        "position_backup_stop_orders",
        "tests/test_strategy_management_reconciliation.py",
    ),
}

PRIVATE_POST_OWNERS = {
    "_set_position_sltp_unchecked": "position_mutation_intents",
    "_cancel_position_sltp_unchecked": "position_mutation_intents",
    "_place_position_close_unchecked": "position_mutation_intents",
}

PUBLIC_POST_CALL_SITES = {
    "place_order": {
        "entry_revision_executor.py",
        "recovery_live_submit.py",
    },
    "trigger_order": {
        "deepcoin_execution_actions.py",
        "entry_revision_executor.py",
        "recovery_live_submit.py",
    },
    "set_position_sltp": set(),
    "cancel_position_sltp": set(),
    "replace_order_sltp": set(),
    "cancel_order": {
        "deepcoin_execution_actions.py",
        "entry_revision_executor.py",
        "recovery_live_submit.py",
        "strategy_management_executor.py",
        "terminal_entry_cleanup.py",
    },
    "cancel_trigger_order": {
        "deepcoin_execution_actions.py",
        "entry_revision_executor.py",
        "legacy_conditional_cancel.py",
        "native_tpsl_migration.py",
        "strategy_management_executor.py",
        "terminal_entry_cleanup.py",
    },
}

PRIVATE_POST_CALL_SITES = {
    "_set_position_sltp_unchecked": {"position_mutation_gateway.py"},
    "_cancel_position_sltp_unchecked": {"position_mutation_gateway.py"},
    "_place_position_close_unchecked": {"position_mutation_gateway.py"},
}

# (source file, enclosing function, client method) ->
# (number of syntactic calls, durable in-flight owner, exact regression test).
CALL_SITE_CONTRACTS = {
    ("deepcoin_execution_actions.py", "cancel_entry_order", "cancel_trigger_order"): (1, "trade_signals", "tests/test_auto_trade_execution.py::test_unknown_management_submission_does_not_retry_or_block_same_message_entry"),
    ("deepcoin_execution_actions.py", "cancel_entry_order", "cancel_order"): (1, "trade_signals", "tests/test_auto_trade_execution.py::test_unknown_management_submission_does_not_retry_or_block_same_message_entry"),
    ("deepcoin_execution_actions.py", "cancel_revision_entry_leg", "cancel_trigger_order"): (1, "strategy_revision_batches", "tests/test_entry_revision_executor.py::test_entry_revision_unknown_cancel_is_recovery_required"),
    ("deepcoin_execution_actions.py", "cancel_revision_entry_leg", "cancel_order"): (1, "strategy_revision_batches", "tests/test_entry_revision_executor.py::test_entry_revision_unknown_cancel_is_recovery_required"),
    ("deepcoin_execution_actions.py", "cancel_pending_entry_legs", "cancel_trigger_order"): (1, "trade_signals", "tests/test_terminal_entry_cleanup.py::test_terminal_entry_cleanup_does_not_retry_unknown_cancel_while_order_remains"),
    ("deepcoin_execution_actions.py", "cancel_pending_entry_legs", "cancel_order"): (1, "trade_signals", "tests/test_terminal_entry_cleanup.py::test_terminal_entry_cleanup_does_not_retry_unknown_regular_cancel"),
    ("deepcoin_execution_actions.py", "recreate_trigger_entry_tpsl", "cancel_trigger_order"): (1, "trade_signals", "tests/test_auto_trade_execution.py::test_unknown_management_submission_does_not_retry_or_block_same_message_entry"),
    ("deepcoin_execution_actions.py", "recreate_trigger_entry_tpsl", "trigger_order"): (1, "trade_signals", "tests/test_auto_trade_execution.py::test_unknown_management_submission_does_not_retry_or_block_same_message_entry"),
    ("entry_revision_executor.py", "execute_entry_revision", "cancel_trigger_order"): (1, "strategy_revision_batches", "tests/test_entry_revision_executor.py::test_entry_revision_unknown_cancel_is_recovery_required"),
    ("entry_revision_executor.py", "execute_entry_revision", "cancel_order"): (1, "strategy_revision_batches", "tests/test_entry_revision_executor.py::test_entry_revision_unknown_cancel_is_recovery_required"),
    ("entry_revision_executor.py", "execute_entry_revision", "trigger_order"): (1, "strategy_revision_batches", "tests/test_entry_revision_executor.py::test_headroom_change_at_replacement_boundary_fails_closed"),
    ("entry_revision_executor.py", "execute_entry_revision", "place_order"): (1, "strategy_revision_batches", "tests/test_entry_revision_executor.py::test_market_replacement_is_never_submitted_without_protected_path"),
    ("legacy_conditional_cancel.py", "apply_reviewed_legacy_conditional_cancel_plan", "cancel_trigger_order"): (1, "position_mutation_intents", "tests/test_legacy_conditional_cancel.py::test_apply_does_not_mark_cancelled_on_unconfirmed_response"),
    ("native_tpsl_migration.py", "apply_native_tpsl_migration_plan", "cancel_trigger_order"): (1, "position_backup_stop_orders", "tests/test_native_tpsl_migration.py::test_migration_never_marks_legacy_cancelled_without_exact_success_response"),
    ("recovery_live_submit.py", "_submit_recovery_signal_direct", "place_order"): (1, "trade_signals", "tests/test_instruction_execution_fault_injection.py::test_crash_after_http_send_before_response_is_quarantined_by_real_writer"),
    ("recovery_live_submit.py", "_submit_recovery_signal_direct", "trigger_order"): (2, "trade_signals", "tests/test_recovery_live_submit.py::test_v2_generic_post_call_error_is_unknown_and_not_retried"),
    ("recovery_live_submit.py", "_submit_trigger_with_protection_intent", "trigger_order"): (1, "trigger_protection_intents", "tests/test_recovery_live_submit.py::test_trigger_parent_event_is_durable_before_later_submission_bookkeeping_crashes"),
    ("recovery_live_submit.py", "_cancel_unprotected_order", "cancel_order"): (1, "trade_signals", "tests/test_recovery_live_submit.py::test_market_submit_persists_binding_when_position_protection_fails"),
    ("strategy_management_executor.py", "_cancel_deferred_entry_legs", "cancel_trigger_order"): (1, "strategy_management_batches", "tests/test_strategy_management_executor.py::test_deferred_cancel_failure_transition_conflict_is_explicit"),
    ("strategy_management_executor.py", "_cancel_deferred_entry_legs", "cancel_order"): (1, "strategy_management_batches", "tests/test_strategy_management_executor.py::test_deferred_cancel_failure_transition_conflict_is_explicit"),
    ("terminal_entry_cleanup.py", "cancel_trigger_order", "cancel_trigger_order"): (1, "trade_signals", "tests/test_terminal_entry_cleanup.py::test_terminal_entry_cleanup_readback_flap_cannot_repeat_cancel_post"),
    ("terminal_entry_cleanup.py", "cancel_order", "cancel_order"): (1, "trade_signals", "tests/test_terminal_entry_cleanup.py::test_terminal_entry_cleanup_does_not_retry_unknown_regular_cancel"),
}

PRIVATE_CALL_SITE_CONTRACTS = {
    ("position_mutation_gateway.py", "cancel_owned_position_sltp", "_cancel_position_sltp_unchecked"): (1, "position_mutation_intents", "tests/test_position_mutation_gateway.py::test_exact_owner_cancellation_is_submitted_once"),
    ("position_mutation_gateway.py", "set_exact_position_sltp", "_set_position_sltp_unchecked"): (1, "position_mutation_intents", "tests/test_position_mutation_gateway.py::test_submitted_set_intent_is_confirmed_only_by_exact_pending_readback"),
    ("position_mutation_gateway.py", "close_exact_position", "_place_position_close_unchecked"): (1, "position_mutation_intents", "tests/test_position_mutation_gateway.py::test_cancel_and_close_intents_confirm_from_terminal_snapshots"),
}


def _posting_methods() -> set[str]:
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"))
    client_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DeepcoinRestClient"
    )
    methods = {
        node.name: node
        for node in client_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    posting: set[str] = set()
    calls: dict[str, set[str]] = {}
    for name, method in methods.items():
        calls[name] = set()
        for node in ast.walk(method):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = node.func.value
            if not isinstance(receiver, ast.Name) or receiver.id != "self":
                continue
            called = node.func.attr
            calls[name].add(called)
            if (
                called == "_request"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).upper() == "POST"
            ):
                posting.add(name)
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            if name not in posting and callees & posting:
                posting.add(name)
                changed = True
    return posting


def _production_call_sites(method: str) -> set[str]:
    call_sites: set[str] = set()
    source_root = ROOT / "src/telegram_kol_research"
    for source in source_root.glob("*.py"):
        if source == CLIENT:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        if method.startswith("_"):
            found = any(
                isinstance(node, ast.Constant) and node.value == method
                for node in ast.walk(tree)
            )
        else:
            found = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method
                for node in ast.walk(tree)
            )
        if found:
            call_sites.add(source.name)
    return call_sites


def _call_site_counts(*, private_strings: bool = False) -> Counter:
    expected_methods = (
        set(PRIVATE_POST_OWNERS) if private_strings else set(PUBLIC_POST_OWNERS)
    )
    counts: Counter = Counter()
    for source in (ROOT / "src/telegram_kol_research").glob("*.py"):
        if source == CLIENT:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        functions = list(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        functions.extend(
            method
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            for method in node.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for function in functions:
            for node in ast.walk(function):
                if private_strings:
                    if isinstance(node, ast.Constant) and node.value in expected_methods:
                        counts[(source.name, function.name, str(node.value))] += 1
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in expected_methods
                ):
                    counts[(source.name, function.name, node.func.attr)] += 1
    return counts


def _fault_test_exists(reference: str) -> bool:
    path_value, test_name = reference.split("::", 1)
    tree = ast.parse((ROOT / path_value).read_text(encoding="utf-8-sig"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == test_name
        for node in tree.body
    )


def test_every_deepcoin_post_entry_point_has_a_declared_durable_owner():
    posting = _posting_methods()
    public = {name for name in posting if not name.startswith("_")}
    private = {name for name in posting if name.startswith("_")}

    assert public == set(PUBLIC_POST_OWNERS)
    assert private == set(PRIVATE_POST_OWNERS)
    assert set(PUBLIC_POST_CALL_SITES) == public
    assert set(PRIVATE_POST_CALL_SITES) == private
    for method, expected in PUBLIC_POST_CALL_SITES.items():
        assert _production_call_sites(method) == expected
    for method, expected in PRIVATE_POST_CALL_SITES.items():
        assert _production_call_sites(method) == expected
    for declarations in PUBLIC_POST_OWNERS.values():
        test_paths = [value for value in declarations if value.startswith("tests/")]
        assert test_paths
        assert all((ROOT / value).is_file() for value in test_paths)


def test_every_production_post_call_site_binds_owner_and_fault_test():
    registered_tables = {adapter.table for adapter in WORK_EVIDENCE_ADAPTERS}
    expected_public = Counter(
        {key: contract[0] for key, contract in CALL_SITE_CONTRACTS.items()}
    )
    expected_private = Counter(
        {key: contract[0] for key, contract in PRIVATE_CALL_SITE_CONTRACTS.items()}
    )

    assert _call_site_counts() == expected_public
    assert _call_site_counts(private_strings=True) == expected_private
    for contract in (*CALL_SITE_CONTRACTS.values(), *PRIVATE_CALL_SITE_CONTRACTS.values()):
        _, owner_table, fault_test = contract
        assert owner_table in registered_tables
        assert _fault_test_exists(fault_test)


def _surface() -> ChangeSurfaceFacts:
    return ChangeSurfaceFacts(
        registry_version=1,
        effective_change_class="code",
        underdeclared=False,
        changed_path_count=1,
        change_surface_fingerprint="c" * 64,
        restart_compatibility_changed=False,
        restart_handler_fingerprint="d" * 64,
        blocking_reason_codes=(),
    )


def _add_leg(session_factory, *, status: str, updated_at: datetime = NOW) -> int:
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deployment-race",
            kol_id="redacted",
            chat_id=-1001,
            message_id=1,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id="deployment-race",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            venue="deepcoin",
            status=status,
            created_at=NOW,
            updated_at=updated_at,
        )
        session.add(leg)
        session.commit()
        return int(leg.id)


def _collect(database: Path, *, now: datetime):
    return collect_deployment_preflight_facts(
        database_path=database,
        change_class="code",
        now=now,
    )


def _preliminary(facts):
    return build_preliminary_deployment_preflight_artifact(
        production_commit=PRODUCTION,
        candidate_commit=CANDIDATE,
        requested_change_class="code",
        change_surface=_surface(),
        facts=facts,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("status", "decision", "reason"),
    [
        ("submitting", "BLOCK", "deployment_in_flight_write"),
        ("submit_unknown", "BLOCK", "deployment_unknown_outcome"),
        ("submitted", "WARN", "deployment_restart_safe_wait"),
    ],
)
def test_between_phase_new_writer_state_is_reclassified(
    tmp_path, status, decision, reason
):
    database = tmp_path / "research.db"
    session_factory = create_session_factory(database)
    preliminary = _preliminary(_collect(database, now=NOW))

    _add_leg(session_factory, status=status)
    final = build_final_deployment_preflight_artifact(
        preliminary_artifact=preliminary,
        production_commit=PRODUCTION,
        candidate_commit=CANDIDATE,
        requested_change_class="code",
        change_surface=_surface(),
        facts=_collect(database, now=NOW + timedelta(minutes=1)),
        now=NOW + timedelta(minutes=1),
    )

    assert preliminary["decision"] != "BLOCK"
    assert final["decision"] == decision
    assert reason in final["reason_codes"]


def test_heartbeat_only_updated_at_change_does_not_change_gate_evidence(tmp_path):
    database = tmp_path / "research.db"
    session_factory = create_session_factory(database)
    leg_id = _add_leg(session_factory, status="submitted")
    preliminary_facts = _collect(database, now=NOW)
    preliminary = _preliminary(preliminary_facts)

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        assert leg is not None
        leg.updated_at = NOW + timedelta(seconds=30)
        session.commit()
    final_facts = _collect(database, now=NOW + timedelta(minutes=1))
    final = build_final_deployment_preflight_artifact(
        preliminary_artifact=preliminary,
        production_commit=PRODUCTION,
        candidate_commit=CANDIDATE,
        requested_change_class="code",
        change_surface=_surface(),
        facts=final_facts,
        now=NOW + timedelta(minutes=1),
    )

    assert final["decision"] == preliminary["decision"]
    assert final["reason_codes"] == preliminary["reason_codes"]
    assert final_facts.work_evidence_fingerprint == preliminary_facts.work_evidence_fingerprint
