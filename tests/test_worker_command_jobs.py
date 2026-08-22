import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import WorkerCommandJob


NOW = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)


def _claim_worker_commands_in_process(database_path, start_event, result_queue):
    from telegram_kol_research.worker_command_jobs import claim_worker_commands

    try:
        session_factory = create_session_factory(database_path)
        start_event.wait(timeout=5)
        claims = claim_worker_commands(
            session_factory,
            claimed_at=NOW,
            lease_for=timedelta(seconds=30),
            limit=4,
        )
        result_queue.put(
            {
                "command_ids": [claim.command_id for claim in claims],
                "error": None,
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                "command_ids": [],
                "error": f"{type(exc).__name__}:{exc}",
            }
        )


def test_enqueue_canonicalizes_payload_and_generates_durable_identity(tmp_path):
    from telegram_kol_research.worker_command_jobs import enqueue_worker_command

    session_factory = create_session_factory(tmp_path / "enqueue.db")

    job = enqueue_worker_command(
        session_factory,
        command_type="close_bound_position",
        request={"pos_id": "position-7"},
        created_at=NOW,
    )

    assert job.command_id
    assert job.command_type == "close_bound_position"
    assert job.status == "pending"
    assert job.request == {"pos_id": "position-7"}
    assert len(job.request_fingerprint) == 64
    with session_factory() as session:
        row = session.query(WorkerCommandJob).one()
    assert row.command_id == job.command_id
    assert row.request_json == '{"pos_id":"position-7"}'
    assert row.request_fingerprint == job.request_fingerprint
    assert row.created_at == NOW.replace(tzinfo=None)


def test_idempotency_reuses_matching_job_and_rejects_payload_drift(tmp_path):
    from telegram_kol_research.worker_command_jobs import (
        WorkerCommandIdempotencyConflict,
        enqueue_worker_command,
    )

    session_factory = create_session_factory(tmp_path / "idempotency.db")
    first = enqueue_worker_command(
        session_factory,
        command_type="recovery_live_submit",
        request={
            "chat_id": 100,
            "message_id": 55,
            "side": "long",
            "symbol": "BTC",
        },
        idempotency_key="action-7",
        created_at=NOW,
    )
    replay = enqueue_worker_command(
        session_factory,
        command_type="recovery_live_submit",
        request={
            "symbol": "BTC",
            "side": "long",
            "message_id": 55,
            "chat_id": 100,
        },
        idempotency_key="action-7",
        created_at=NOW,
    )

    assert replay.command_id == first.command_id
    with pytest.raises(WorkerCommandIdempotencyConflict) as exc_info:
        enqueue_worker_command(
            session_factory,
            command_type="recovery_live_submit",
            request={
                "chat_id": 100,
                "message_id": 55,
                "side": "short",
                "symbol": "BTC",
            },
            idempotency_key="action-7",
            created_at=NOW,
        )
    assert exc_info.value.command_id == first.command_id
    with session_factory() as session:
        assert session.query(WorkerCommandJob).count() == 1


def test_keyless_requests_create_independent_commands(tmp_path):
    from telegram_kol_research.worker_command_jobs import enqueue_worker_command

    session_factory = create_session_factory(tmp_path / "keyless.db")

    first = enqueue_worker_command(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
        created_at=NOW,
    )
    second = enqueue_worker_command(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
        created_at=NOW,
    )

    assert first.command_id != second.command_id


@pytest.mark.parametrize(
    "request_payload",
    [
        {"authorization": "Bearer secret"},
        {"headers": {"DC-ACCESS-KEY": "secret"}},
        {"payload": {"api_secret": "secret"}},
        {"session": "telegram-session"},
    ],
)
def test_enqueue_rejects_secret_bearing_payloads(tmp_path, request_payload):
    from telegram_kol_research.worker_command_jobs import (
        WorkerCommandValidationError,
        enqueue_worker_command,
    )

    session_factory = create_session_factory(tmp_path / "secret.db")

    with pytest.raises(WorkerCommandValidationError, match="secret"):
        enqueue_worker_command(
            session_factory,
            command_type="sync_deepcoin_execution",
            request=request_payload,
            created_at=NOW,
        )
    with session_factory() as session:
        assert session.query(WorkerCommandJob).count() == 0


def test_enqueue_rejects_unknown_type_and_oversized_payload_or_key(tmp_path):
    from telegram_kol_research.worker_command_jobs import (
        MAX_IDEMPOTENCY_KEY_CHARS,
        MAX_REQUEST_JSON_BYTES,
        WorkerCommandValidationError,
        enqueue_worker_command,
    )

    session_factory = create_session_factory(tmp_path / "bounded.db")

    with pytest.raises(WorkerCommandValidationError, match="command_type"):
        enqueue_worker_command(
            session_factory,
            command_type="arbitrary_exchange_write",
            request={},
            created_at=NOW,
        )
    with pytest.raises(WorkerCommandValidationError, match="request_json"):
        enqueue_worker_command(
            session_factory,
            command_type="sync_deepcoin_execution",
            request={"padding": "x" * MAX_REQUEST_JSON_BYTES},
            created_at=NOW,
        )
    with pytest.raises(WorkerCommandValidationError, match="idempotency_key"):
        enqueue_worker_command(
            session_factory,
            command_type="sync_deepcoin_execution",
            request={},
            idempotency_key="k" * (MAX_IDEMPOTENCY_KEY_CHARS + 1),
            created_at=NOW,
        )


def test_persisted_request_is_valid_canonical_json(tmp_path):
    from telegram_kol_research.worker_command_jobs import enqueue_worker_command

    session_factory = create_session_factory(tmp_path / "canonical.db")
    enqueue_worker_command(
        session_factory,
        command_type="close_bound_position",
        request={"pos_id": "仓位-7"},
        created_at=NOW,
    )

    with session_factory() as session:
        row = session.query(WorkerCommandJob).one()
    assert json.loads(row.request_json) == {"pos_id": "仓位-7"}
    assert " " not in row.request_json


def test_claim_is_atomic_oldest_first_and_increments_attempt(tmp_path):
    from telegram_kol_research.worker_command_jobs import (
        claim_worker_commands,
        enqueue_worker_command,
    )

    session_factory = create_session_factory(tmp_path / "atomic-claim.db")
    first = enqueue_worker_command(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
        created_at=NOW,
    )
    second = enqueue_worker_command(
        session_factory,
        command_type="close_bound_position",
        request={"pos_id": "position-2"},
        created_at=NOW + timedelta(seconds=1),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: claim_worker_commands(
                    session_factory,
                    claimed_at=NOW + timedelta(seconds=2),
                    lease_for=timedelta(seconds=30),
                    limit=1,
                ),
                range(2),
            )
        )

    claims = [claim for result in results for claim in result]
    assert {claim.command_id for claim in claims} == {
        first.command_id,
        second.command_id,
    }
    assert all(claim.attempt_count == 1 for claim in claims)
    assert len({claim.claim_token for claim in claims}) == 2


def test_three_process_claim_has_one_owner_per_command_and_no_sqlite_busy(tmp_path):
    from telegram_kol_research.worker_command_jobs import enqueue_worker_command

    database_path = tmp_path / "three-process-claim.db"
    session_factory = create_session_factory(database_path)
    expected_ids = {
        enqueue_worker_command(
            session_factory,
            command_type="sync_deepcoin_execution",
            request={"ordinal": ordinal},
            created_at=NOW + timedelta(microseconds=ordinal),
        ).command_id
        for ordinal in range(12)
    }
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_worker_commands_in_process,
            args=(database_path, start_event, result_queue),
        )
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [result_queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)

    assert [result["error"] for result in results] == [None, None, None]
    claimed_ids = [
        command_id for result in results for command_id in result["command_ids"]
    ]
    assert set(claimed_ids) == expected_ids
    assert len(claimed_ids) == len(set(claimed_ids)) == 12
    assert all(process.exitcode == 0 for process in processes)


def test_only_expired_pre_execution_claim_is_reclaimed(tmp_path):
    from telegram_kol_research.worker_command_jobs import (
        claim_worker_commands,
        enqueue_worker_command,
    )

    session_factory = create_session_factory(tmp_path / "stale-claim.db")
    command = enqueue_worker_command(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
        created_at=NOW,
    )
    first = claim_worker_commands(
        session_factory,
        claimed_at=NOW,
        lease_for=timedelta(seconds=30),
        limit=1,
    )[0]

    before_expiry = claim_worker_commands(
        session_factory,
        claimed_at=NOW + timedelta(seconds=29),
        lease_for=timedelta(seconds=30),
        limit=1,
    )
    reclaimed = claim_worker_commands(
        session_factory,
        claimed_at=NOW + timedelta(seconds=31),
        lease_for=timedelta(seconds=30),
        limit=1,
    )[0]

    assert before_expiry == []
    assert reclaimed.command_id == command.command_id
    assert reclaimed.claim_token != first.claim_token
    assert reclaimed.attempt_count == 2


def test_executing_boundary_is_durable_before_adapter_and_never_reclaimed(
    tmp_path,
):
    from telegram_kol_research.worker_command_jobs import (
        claim_worker_commands,
        enqueue_worker_command,
        mark_worker_command_executing,
    )

    session_factory = create_session_factory(tmp_path / "executing.db")
    enqueue_worker_command(
        session_factory,
        command_type="close_bound_position",
        request={"pos_id": "position-7"},
        created_at=NOW,
    )
    claim = claim_worker_commands(
        session_factory,
        claimed_at=NOW,
        lease_for=timedelta(seconds=30),
        limit=1,
    )[0]

    assert mark_worker_command_executing(
        session_factory,
        claim=claim,
        started_at=NOW + timedelta(seconds=1),
    ) is True
    with session_factory() as session:
        row = session.get(WorkerCommandJob, claim.job_id)
        assert row.status == "executing"
        assert row.side_effect_started_at == (NOW + timedelta(seconds=1)).replace(
            tzinfo=None
        )
        assert row.claim_token == claim.claim_token

    assert claim_worker_commands(
        session_factory,
        claimed_at=NOW + timedelta(minutes=5),
        lease_for=timedelta(seconds=30),
        limit=1,
    ) == []


def test_wrong_claim_token_cannot_cross_or_settle_execution_boundary(tmp_path):
    from dataclasses import replace

    from telegram_kol_research.worker_command_jobs import (
        claim_worker_commands,
        enqueue_worker_command,
        mark_worker_command_executing,
        settle_worker_command_succeeded,
    )

    session_factory = create_session_factory(tmp_path / "claim-token.db")
    enqueue_worker_command(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
        created_at=NOW,
    )
    claim = claim_worker_commands(session_factory, claimed_at=NOW, limit=1)[0]
    stale_claim = replace(claim, claim_token="wrong-token")

    assert mark_worker_command_executing(
        session_factory, claim=stale_claim, started_at=NOW
    ) is False
    assert mark_worker_command_executing(
        session_factory, claim=claim, started_at=NOW
    ) is True
    assert settle_worker_command_succeeded(
        session_factory,
        claim=stale_claim,
        result={"checked": 1},
        http_status=200,
        completed_at=NOW,
    ) is False
    with session_factory() as session:
        assert session.get(WorkerCommandJob, claim.job_id).status == "executing"


def test_success_and_known_failure_settlement_are_bounded_and_versioned(tmp_path):
    from telegram_kol_research.worker_command_jobs import (
        MAX_ERROR_SUMMARY_CHARS,
        WorkerCommandValidationError,
        claim_worker_commands,
        enqueue_worker_command,
        get_worker_command,
        mark_worker_command_executing,
        settle_worker_command_failed,
        settle_worker_command_succeeded,
    )

    session_factory = create_session_factory(tmp_path / "settlement.db")
    succeeded = enqueue_worker_command(
        session_factory,
        command_type="sync_deepcoin_execution",
        request={},
        created_at=NOW,
    )
    success_claim = claim_worker_commands(session_factory, claimed_at=NOW, limit=1)[0]
    mark_worker_command_executing(session_factory, claim=success_claim, started_at=NOW)

    assert settle_worker_command_succeeded(
        session_factory,
        claim=success_claim,
        result={"checked": 7, "manually_closed": 2},
        http_status=200,
        completed_at=NOW + timedelta(seconds=1),
    ) is True
    success_snapshot = get_worker_command(
        session_factory, command_id=succeeded.command_id
    )
    assert success_snapshot.status == "succeeded"
    assert success_snapshot.result == {"checked": 7, "manually_closed": 2}
    assert success_snapshot.http_status == 200
    assert success_snapshot.result_schema_version == 1
    assert success_snapshot.claim_token is None

    failed = enqueue_worker_command(
        session_factory,
        command_type="close_bound_position",
        request={"pos_id": "position-8"},
        created_at=NOW + timedelta(seconds=2),
    )
    failure_claim = claim_worker_commands(
        session_factory, claimed_at=NOW + timedelta(seconds=2), limit=1
    )[0]
    mark_worker_command_executing(
        session_factory,
        claim=failure_claim,
        started_at=NOW + timedelta(seconds=2),
    )
    assert settle_worker_command_failed(
        session_factory,
        claim=failure_claim,
        result={"detail": "position conflict"},
        http_status=409,
        error_code="DeepcoinExecutionActionError",
        error_summary="x" * (MAX_ERROR_SUMMARY_CHARS + 50),
        completed_at=NOW + timedelta(seconds=3),
    ) is True
    failure_snapshot = get_worker_command(
        session_factory, command_id=failed.command_id
    )
    assert failure_snapshot.status == "failed"
    assert failure_snapshot.result == {"detail": "position conflict"}
    assert failure_snapshot.http_status == 409
    assert failure_snapshot.error_code == "DeepcoinExecutionActionError"
    assert failure_snapshot.error_summary == "x" * MAX_ERROR_SUMMARY_CHARS

    oversized = enqueue_worker_command(
        session_factory,
        command_type="process_next_trade_signal",
        request={},
        created_at=NOW + timedelta(seconds=4),
    )
    oversized_claim = claim_worker_commands(
        session_factory, claimed_at=NOW + timedelta(seconds=4), limit=1
    )[0]
    mark_worker_command_executing(
        session_factory,
        claim=oversized_claim,
        started_at=NOW + timedelta(seconds=4),
    )
    with pytest.raises(WorkerCommandValidationError, match="result_json"):
        settle_worker_command_succeeded(
            session_factory,
            claim=oversized_claim,
            result={"padding": "x" * 70_000},
            http_status=200,
            completed_at=NOW + timedelta(seconds=5),
        )
    assert get_worker_command(
        session_factory, command_id=oversized.command_id
    ).status == "executing"


def test_expired_executing_command_becomes_uncertain_and_never_replays(tmp_path):
    from telegram_kol_research.worker_command_jobs import (
        claim_worker_commands,
        enqueue_worker_command,
        get_worker_command,
        mark_expired_executing_commands_uncertain,
        mark_worker_command_executing,
    )

    session_factory = create_session_factory(tmp_path / "uncertain.db")
    command = enqueue_worker_command(
        session_factory,
        command_type="recovery_live_submit",
        request={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
        },
        created_at=NOW,
    )
    claim = claim_worker_commands(
        session_factory,
        claimed_at=NOW,
        lease_for=timedelta(seconds=30),
        limit=1,
    )[0]
    mark_worker_command_executing(
        session_factory, claim=claim, started_at=NOW + timedelta(seconds=1)
    )

    assert mark_expired_executing_commands_uncertain(
        session_factory, uncertain_at=NOW + timedelta(seconds=29)
    ) == 0
    assert mark_expired_executing_commands_uncertain(
        session_factory, uncertain_at=NOW + timedelta(seconds=31)
    ) == 1
    snapshot = get_worker_command(session_factory, command_id=command.command_id)
    assert snapshot.status == "uncertain"
    assert snapshot.error_code == "worker_lost_after_side_effect_boundary"
    assert snapshot.claim_token is None
    assert claim_worker_commands(
        session_factory,
        claimed_at=NOW + timedelta(hours=1),
        lease_for=timedelta(seconds=30),
        limit=1,
    ) == []
