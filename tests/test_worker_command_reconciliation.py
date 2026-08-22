from dataclasses import replace
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.worker_command_jobs import (
    enqueue_worker_command,
    get_worker_command,
)


NOW = datetime(2026, 8, 22, 13, 0, tzinfo=UTC)


def _uncertain(session_factory, command_type, request):
    job = enqueue_worker_command(
        session_factory,
        command_type=command_type,
        request=request,
        created_at=NOW,
    )
    with session_factory() as session:
        row = session.query(__import__(
            "telegram_kol_research.models", fromlist=["WorkerCommandJob"]
        ).WorkerCommandJob).filter_by(command_id=job.command_id).one()
        row.status = "uncertain"
        row.uncertain_at = NOW
        session.commit()
    return get_worker_command(session_factory, command_id=job.command_id)


@pytest.mark.parametrize(
    ("command_type", "command_payload"),
    [
        ("sync_deepcoin_execution", {}),
        ("close_bound_position", {"pos_id": "position-7"}),
        (
            "recovery_live_submit",
            {"chat_id": 1, "message_id": 2, "symbol": "BTC", "side": "long"},
        ),
        ("process_next_trade_signal", {}),
    ],
)
def test_evaluator_supports_all_closed_outcomes_for_each_command(
    tmp_path, command_type, command_payload
):
    from telegram_kol_research.worker_command_reconciliation import (
        WorkerCommandEvidence,
        evaluate_worker_command_evidence,
    )

    session_factory = create_session_factory(tmp_path / f"{command_type}.db")
    command = _uncertain(session_factory, command_type, command_payload)
    base = WorkerCommandEvidence(
        snapshot_complete=True,
        identity_chain_complete=True,
        operation_matches=True,
        submission_found=True,
        direct_exchange_proof=True,
    )

    assert evaluate_worker_command_evidence(command, base).outcome == "confirmed_succeeded"
    assert evaluate_worker_command_evidence(
        command,
        replace(base, submission_found=False, direct_exchange_proof=False),
    ).outcome == "confirmed_no_submission"
    assert evaluate_worker_command_evidence(
        command,
        replace(base, operation_matches=False),
    ).outcome == "conflict"
    assert evaluate_worker_command_evidence(
        command,
        replace(base, snapshot_complete=False),
    ).outcome == "evidence_incomplete"


def test_incomplete_external_read_retries_once_then_fails_closed(tmp_path):
    from telegram_kol_research.worker_command_reconciliation import (
        collect_worker_command_evidence,
    )

    attempts = []

    def incomplete_reader(_command):
        attempts.append(True)
        return {"complete": False, "items": []}

    evidence = collect_worker_command_evidence(
        object(), external_reader=incomplete_reader
    )

    assert evidence.snapshot_complete is False
    assert evidence.external_attempts == 2
    assert attempts == [True, True]


def test_clordid_without_parent_unique_child_posid_chain_is_incomplete(tmp_path):
    from telegram_kol_research.worker_command_reconciliation import (
        WorkerCommandEvidence,
        evaluate_worker_command_evidence,
    )

    session_factory = create_session_factory(tmp_path / "clordid.db")
    command = _uncertain(
        session_factory,
        "recovery_live_submit",
        {"chat_id": 1, "message_id": 2, "symbol": "BTC", "side": "long"},
    )
    evidence = WorkerCommandEvidence(
        snapshot_complete=True,
        identity_chain_complete=False,
        operation_matches=True,
        submission_found=True,
        direct_exchange_proof=False,
        client_order_id_only=True,
    )

    report = evaluate_worker_command_evidence(command, evidence)

    assert report.outcome == "evidence_incomplete"
    assert "parent-child-posId" in report.reason


@pytest.mark.parametrize("outcome", ["conflict", "evidence_incomplete"])
def test_guarded_apply_never_changes_uncertain_for_unresolved_outcome(
    tmp_path, outcome
):
    from telegram_kol_research.worker_command_reconciliation import (
        WorkerCommandEvidence,
        reconcile_uncertain_worker_command,
    )

    session_factory = create_session_factory(tmp_path / f"refuse-{outcome}.db")
    command = _uncertain(session_factory, "close_bound_position", {"pos_id": "p7"})
    evidence = WorkerCommandEvidence(
        snapshot_complete=(outcome == "conflict"),
        identity_chain_complete=True,
        operation_matches=False,
        submission_found=True,
        direct_exchange_proof=True,
    )

    report = reconcile_uncertain_worker_command(
        session_factory,
        command_id=command.command_id,
        evidence=evidence,
        apply_confirmed=True,
        reconciled_at=NOW,
    )

    assert report.outcome == outcome
    assert get_worker_command(session_factory, command_id=command.command_id).status == "uncertain"


def test_guarded_apply_cas_terminalizes_only_still_uncertain_row(tmp_path):
    from telegram_kol_research.worker_command_reconciliation import (
        WorkerCommandEvidence,
        WorkerCommandReconciliationDrift,
        reconcile_uncertain_worker_command,
    )

    session_factory = create_session_factory(tmp_path / "apply.db")
    command = _uncertain(session_factory, "close_bound_position", {"pos_id": "p7"})
    evidence = WorkerCommandEvidence(
        snapshot_complete=True,
        identity_chain_complete=True,
        operation_matches=True,
        submission_found=True,
        direct_exchange_proof=True,
    )
    applied = reconcile_uncertain_worker_command(
        session_factory,
        command_id=command.command_id,
        evidence=evidence,
        apply_confirmed=True,
        reconciled_at=NOW,
    )

    assert applied.applied is True
    row = get_worker_command(session_factory, command_id=command.command_id)
    assert row.status == "succeeded"

    with pytest.raises(WorkerCommandReconciliationDrift):
        reconcile_uncertain_worker_command(
            session_factory,
            command_id=command.command_id,
            evidence=evidence,
            apply_confirmed=True,
            reconciled_at=NOW,
        )
