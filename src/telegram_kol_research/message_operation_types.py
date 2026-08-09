"""Closed, dependency-free message-operation outcome vocabulary."""

from __future__ import annotations


MESSAGE_OPERATION_VIOLATIONS = frozenset(
    {
        "recognition_failed",
        "context_unresolved",
        "context_exhausted",
        "action_refused",
        "no_operation_created",
        "missing_management_descendant",
        "partial_operation",
        "unknown_operation_result",
        "operation_timeout",
        "local_success_unverified",
        "exchange_readback_mismatch",
        "restart_or_lease_skip",
        "reconciliation_disproved_success",
        "missing_instruction_projection",
        "unevaluated_sibling_instruction",
        "hidden_instruction_failure",
    }
)
