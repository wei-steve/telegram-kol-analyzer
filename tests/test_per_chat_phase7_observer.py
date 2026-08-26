from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "per_chat_phase7_observer.py"
)
SPEC = importlib.util.spec_from_file_location("per_chat_phase7_observer", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = observer
SPEC.loader.exec_module(observer)


def job(job_id, raw_message_id, chat_id, status, completed_at=None):
    return observer.JobObservation(
        job_id=job_id,
        raw_message_id=raw_message_id,
        chat_id=chat_id,
        status=status,
        completed_at=completed_at,
    )


def test_later_same_chat_pending_behind_claim_is_valid():
    result = observer.evaluate_same_chat_jobs(
        [job(1, 10, 7, "claimed"), job(2, 11, 7, "pending")]
    )

    assert result.violations == ()


def test_two_claimed_jobs_in_one_chat_are_overlap():
    result = observer.evaluate_same_chat_jobs(
        [job(1, 10, 7, "claimed"), job(2, 11, 7, "claimed")]
    )

    assert [row.code for row in result.violations] == [
        "same_chat_multiple_claims"
    ]


def test_claimed_successor_behind_older_pending_job_is_out_of_order():
    result = observer.evaluate_same_chat_jobs(
        [job(1, 10, 7, "pending"), job(2, 11, 7, "claimed")]
    )

    assert [row.code for row in result.violations] == [
        "same_chat_out_of_order_claim"
    ]


def test_completed_at_is_not_used_as_processing_overlap_boundary():
    misleading = datetime(2026, 8, 26, 9, 40, tzinfo=UTC)
    result = observer.evaluate_same_chat_jobs(
        [
            job(1, 10, 7, "succeeded", completed_at=misleading),
            job(2, 11, 7, "claimed", completed_at=misleading),
        ]
    )

    assert result.violations == ()
