#!/usr/bin/env python3
"""Read-only Phase 7 per-chat acceptance observer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


NONTERMINAL_STATUSES = frozenset({"pending", "claimed"})


@dataclass(frozen=True)
class JobObservation:
    job_id: int
    raw_message_id: int
    chat_id: int
    status: str
    completed_at: datetime | None = None


@dataclass(frozen=True)
class InvariantViolation:
    code: str
    chat_id: int
    job_ids: tuple[int, ...]


@dataclass(frozen=True)
class SameChatEvaluation:
    violations: tuple[InvariantViolation, ...]
    claimed_chat_ids: frozenset[int]


def evaluate_same_chat_jobs(jobs: list[JobObservation]) -> SameChatEvaluation:
    by_chat: dict[int, list[JobObservation]] = defaultdict(list)
    for row in jobs:
        if row.status in NONTERMINAL_STATUSES:
            by_chat[row.chat_id].append(row)

    violations: list[InvariantViolation] = []
    claimed_chat_ids: set[int] = set()
    for chat_id, rows in sorted(by_chat.items()):
        ordered = sorted(rows, key=lambda row: (row.raw_message_id, row.job_id))
        claimed = [row for row in ordered if row.status == "claimed"]
        if claimed:
            claimed_chat_ids.add(chat_id)
        if len(claimed) > 1:
            violations.append(
                InvariantViolation(
                    code="same_chat_multiple_claims",
                    chat_id=chat_id,
                    job_ids=tuple(row.job_id for row in claimed),
                )
            )
            continue
        if claimed and claimed[0].job_id != ordered[0].job_id:
            violations.append(
                InvariantViolation(
                    code="same_chat_out_of_order_claim",
                    chat_id=chat_id,
                    job_ids=(ordered[0].job_id, claimed[0].job_id),
                )
            )
    return SameChatEvaluation(
        violations=tuple(violations),
        claimed_chat_ids=frozenset(claimed_chat_ids),
    )
