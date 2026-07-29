"""Keep requested exits distinct from exchange-confirmed lifecycle exits."""

from __future__ import annotations

from datetime import datetime

from telegram_kol_research.execution_bindings import (
    binding_has_unresolved_entry_leg,
)
from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle, utc_now


def has_live_execution_binding(session, lifecycle: StrategyLifecycle) -> bool:
    if lifecycle.execution_binding_id is not None:
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        if binding is not None and (
            binding.status in {"open", "active"}
            or binding_has_unresolved_entry_leg(session, binding)
        ):
            return True
    bindings = (
        session.query(ExecutionBinding)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(ExecutionBinding.chat_id == lifecycle.chat_id)
        .filter(ExecutionBinding.message_id == lifecycle.message_id)
        .filter(ExecutionBinding.symbol == lifecycle.symbol.upper())
        .filter(ExecutionBinding.side == lifecycle.side.lower())
        .all()
    )
    return any(
        binding.status in {"open", "active"}
        or binding_has_unresolved_entry_leg(session, binding)
        for binding in bindings
    )


def record_lifecycle_exit_intent(
    session,
    lifecycle: StrategyLifecycle,
    *,
    exit_message_id: int,
    reason: str | None,
    updated_at: datetime | None = None,
) -> StrategyLifecycle:
    """Record a KOL exit request while the exact live binding still exists."""

    now = updated_at or utc_now()
    has_active_position = (
        session.query(ExecutionBinding.id)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(ExecutionBinding.chat_id == lifecycle.chat_id)
        .filter(ExecutionBinding.message_id == lifecycle.message_id)
        .filter(ExecutionBinding.symbol == lifecycle.symbol.upper())
        .filter(ExecutionBinding.side == lifecycle.side.lower())
        .filter(ExecutionBinding.status == "active")
        .filter(ExecutionBinding.pos_id.is_not(None))
        .first()
        is not None
    )
    if lifecycle.lifecycle_status == "entered" or has_active_position:
        lifecycle.lifecycle_status = "entered"
        lifecycle.exit_reason = None
        lifecycle.exited_at = None
    lifecycle.exit_signal_message_id = exit_message_id
    lifecycle.management_signal_message_id = exit_message_id
    lifecycle.management_action = "exit_requested"
    lifecycle.management_note = reason or "KOL requested a full position exit."
    lifecycle.updated_at = now
    return lifecycle
