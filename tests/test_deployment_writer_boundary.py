from __future__ import annotations

import ast
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
        "tests/test_instruction_execution_fault_injection.py",
    ),
    "trigger_order": (
        "execution_order_legs",
        "position_protection_legs",
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
        "strategy_management_components",
        "strategy_revision_batches",
        "tests/test_strategy_management_reconciliation.py",
    ),
    "cancel_trigger_order": (
        "strategy_management_components",
        "trigger_protection_intents",
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
    },
    "cancel_trigger_order": {
        "deepcoin_execution_actions.py",
        "entry_revision_executor.py",
        "legacy_conditional_cancel.py",
        "native_tpsl_migration.py",
        "strategy_management_executor.py",
    },
}

PRIVATE_POST_CALL_SITES = {
    "_set_position_sltp_unchecked": {"position_mutation_gateway.py"},
    "_cancel_position_sltp_unchecked": {"position_mutation_gateway.py"},
    "_place_position_close_unchecked": {"position_mutation_gateway.py"},
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
