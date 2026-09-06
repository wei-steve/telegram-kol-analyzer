"""Worker-owned adapters for durable exchange-authority commands."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from telegram_kol_research.deepcoin_execution_actions import (
    DeepcoinExecutionActionError,
    close_bound_position_market,
)
from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.execution_bindings import (
    reconcile_deepcoin_execution_bindings,
    reconcile_deepcoin_execution_bindings_read_only,
    sync_manual_closed_deepcoin_positions,
)
from telegram_kol_research.models import utc_now
from telegram_kol_research.recovery_live_submit import (
    RecoveryLiveSubmitError,
    process_next_trade_signal_live,
    submit_recovery_order_live,
)
from telegram_kol_research.system_operator_bot import (
    SystemOperatorBotConfig,
    deliver_pending_position_attribution_incidents,
    deliver_pending_position_protection_incidents,
    deliver_terminal_entry_cleanup_notifications,
)
from telegram_kol_research.worker_command_jobs import WorkerCommandClaim
from telegram_kol_research.worker_command_jobs import (
    claim_worker_commands,
    mark_expired_executing_commands_uncertain,
    mark_worker_command_executing,
    settle_worker_command_failed,
    settle_worker_command_succeeded,
)


logger = logging.getLogger(__name__)
DEFAULT_WORKER_COMMAND_LEASE = timedelta(minutes=5)
SYNC_EFFECTS_FULL = "full"
SYNC_EFFECTS_RECONCILE_ONLY = "reconcile_only"
ENTRY_SUBMISSION_COMMAND_TYPES = frozenset(
    {"recovery_live_submit", "process_next_trade_signal"}
)
ENTRY_FROZEN_ALLOWED_COMMAND_TYPES = frozenset(
    {"sync_deepcoin_execution", "close_bound_position"}
)
_SYNC_READ_METHODS = frozenset(
    {
        "list_positions",
        "list_open_orders",
        "read_trigger_orders_pending",
        "list_trigger_orders_pending",
        "list_order_history",
        "list_trade_fills",
        "list_trigger_order_history",
        "list_position_history",
    }
)


IncidentDeliverer = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class WorkerCommandDependencies:
    session_factory: Any
    deepcoin_client_factory: Callable[[], Any]
    contract_spec_provider: Any
    now_provider: Callable[[], datetime] = utc_now
    notification_bot_config: Any | None = None
    system_operator_bot_config: Any | None = None
    attribution_incident_deliverer: IncidentDeliverer = (
        deliver_pending_position_attribution_incidents
    )
    protection_incident_deliverer: IncidentDeliverer = (
        deliver_pending_position_protection_incidents
    )
    cleanup_notification_deliverer: IncidentDeliverer = (
        deliver_terminal_entry_cleanup_notifications
    )
    entry_admission_frozen: bool = False


@dataclass(frozen=True, slots=True)
class WorkerCommandExecutionResult:
    http_status: int
    body: Any


class WorkerCommandMappedError(RuntimeError):
    """A bounded durable rendering of an existing route exception."""

    def __init__(
        self,
        *,
        http_status: int,
        body: dict[str, Any],
        error_code: str,
        error_summary: str,
    ) -> None:
        super().__init__(error_summary)
        self.http_status = int(http_status)
        self.body = body
        self.error_code = error_code
        self.error_summary = error_summary


class _ReadOnlyDeepcoinClientFacade:
    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name not in _SYNC_READ_METHODS:
            raise AttributeError(name)
        return getattr(self._client, name)


@dataclass(frozen=True, slots=True)
class WorkerCommandWorkerResult:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    uncertain: int = 0


async def run_worker_command_tick(
    session_factory,
    *,
    dependencies: WorkerCommandDependencies,
    now: datetime | None = None,
    lease_for: timedelta = DEFAULT_WORKER_COMMAND_LEASE,
    limit: int = 1,
    adapter: Callable[..., Awaitable[WorkerCommandExecutionResult]] | None = None,
) -> WorkerCommandWorkerResult:
    """Consume durable commands sequentially."""

    command_adapter = adapter or execute_worker_command_adapter
    tick_time = now or utc_now()
    uncertain = await asyncio.to_thread(
        mark_expired_executing_commands_uncertain,
        session_factory,
        uncertain_at=tick_time,
    )
    claims = await asyncio.to_thread(
        claim_worker_commands,
        session_factory,
        claimed_at=tick_time,
        lease_for=lease_for,
        limit=limit,
        allowed_command_types=(
            ENTRY_FROZEN_ALLOWED_COMMAND_TYPES
            if dependencies.entry_admission_frozen
            else None
        ),
    )
    succeeded = 0
    failed = 0
    for claim in claims:
        crossed = await asyncio.to_thread(
            mark_worker_command_executing,
            session_factory,
            claim=claim,
            started_at=tick_time,
        )
        if not crossed:
            continue
        try:
            result = await command_adapter(claim, dependencies=dependencies)
        except asyncio.CancelledError:
            raise
        except WorkerCommandMappedError as exc:
            settled = await asyncio.to_thread(
                settle_worker_command_failed,
                session_factory,
                claim=claim,
                result=exc.body,
                http_status=exc.http_status,
                error_code=exc.error_code,
                error_summary=exc.error_summary,
                completed_at=tick_time,
            )
            if settled:
                failed += 1
        except BaseException:
            # The durable execution boundary has already been crossed. Leave the
            # row executing so lease expiry freezes it uncertain; never replay.
            logger.exception(
                "worker command adapter escaped mapped handling command_id=%s",
                claim.command_id,
            )
        else:
            settled = await asyncio.to_thread(
                settle_worker_command_succeeded,
                session_factory,
                claim=claim,
                result=result.body,
                http_status=result.http_status,
                completed_at=tick_time,
            )
            if settled:
                succeeded += 1
    return WorkerCommandWorkerResult(
        claimed=len(claims),
        succeeded=succeeded,
        failed=failed,
        uncertain=int(uncertain),
    )


async def run_worker_command_loop(
    session_factory,
    *,
    dependencies: WorkerCommandDependencies,
    interval_seconds: float = 0.5,
    **tick_kwargs,
) -> None:
    """Run bounded ticks until cancelled."""

    while True:
        await run_worker_command_tick(
            session_factory,
            dependencies=dependencies,
            **tick_kwargs,
        )
        await asyncio.sleep(max(0.01, float(interval_seconds)))


async def supervise_worker_command_mode(
    session_factory,
    *,
    dependencies: WorkerCommandDependencies,
    queue_runner=run_worker_command_loop,
    interval_seconds: float = 0.25,
) -> None:
    """Run the durable worker-command consumer for the ``worker`` role."""

    await queue_runner(
        session_factory,
        dependencies=dependencies,
        interval_seconds=max(0.01, float(interval_seconds)),
    )


async def execute_worker_command_adapter(
    claim: WorkerCommandClaim,
    *,
    dependencies: WorkerCommandDependencies,
) -> WorkerCommandExecutionResult:
    """Invoke exactly one approved adapter from worker authority."""

    try:
        if claim.command_type == "sync_deepcoin_execution":
            return await _execute_sync(claim.request, dependencies)
        if claim.command_type == "close_bound_position":
            return await _execute_close(claim.request, dependencies)
        if claim.command_type == "recovery_live_submit":
            return await _execute_recovery(claim.request, dependencies)
        if claim.command_type == "process_next_trade_signal":
            return await _execute_process_next(dependencies)
        raise WorkerCommandMappedError(
            http_status=500,
            body={"detail": "unsupported worker command type"},
            error_code="unsupported_worker_command_type",
            error_summary="unsupported worker command type",
        )
    except WorkerCommandMappedError:
        raise
    except BaseException as exc:
        raise _mapped_adapter_error(claim.command_type, exc) from exc


def _mapped_adapter_error(
    command_type: str,
    error: BaseException,
) -> WorkerCommandMappedError:
    status = 500
    detail = "worker command failed"
    if isinstance(error, DeepcoinClientError):
        status = 502
        detail = str(error)
    elif command_type == "sync_deepcoin_execution":
        detail = str(error)
    elif command_type == "close_bound_position":
        if isinstance(error, DeepcoinExecutionActionError):
            status = 409
            detail = str(error)
        else:
            detail = "bound position close failed"
    elif command_type in {
        "recovery_live_submit",
        "process_next_trade_signal",
    }:
        if isinstance(error, RecoveryLiveSubmitError):
            status = 409
            detail = str(error)
        elif command_type == "recovery_live_submit" and isinstance(
            error, ValueError
        ):
            status = 422
            detail = str(error)
    return WorkerCommandMappedError(
        http_status=status,
        body={"detail": detail},
        error_code=type(error).__name__,
        error_summary=str(error),
    )


def _parse_sync_effects_policy(request: dict[str, Any]) -> str:
    if request == {}:
        return SYNC_EFFECTS_FULL
    if request == {"effects_policy": SYNC_EFFECTS_RECONCILE_ONLY}:
        return SYNC_EFFECTS_RECONCILE_ONLY
    raise ValueError("invalid sync effects policy")


async def _execute_sync(
    request: dict[str, Any],
    dependencies: WorkerCommandDependencies,
) -> WorkerCommandExecutionResult:
    effects_policy = _parse_sync_effects_policy(request)
    body = await asyncio.to_thread(
        run_sync_command_blocking,
        dependencies,
        effects_policy=effects_policy,
    )
    if effects_policy == SYNC_EFFECTS_FULL and isinstance(
        dependencies.notification_bot_config,
        SystemOperatorBotConfig,
    ):
        await dependencies.attribution_incident_deliverer(
            dependencies.session_factory,
            config=dependencies.notification_bot_config,
            delivered_at=dependencies.now_provider(),
        )
        await dependencies.protection_incident_deliverer(
            dependencies.session_factory,
            config=dependencies.notification_bot_config,
            delivered_at=dependencies.now_provider(),
        )
    if effects_policy == SYNC_EFFECTS_FULL and isinstance(
        dependencies.system_operator_bot_config,
        SystemOperatorBotConfig,
    ):
        await dependencies.cleanup_notification_deliverer(
            dependencies.session_factory,
            config=dependencies.system_operator_bot_config,
            delivered_at=dependencies.now_provider(),
        )
    return WorkerCommandExecutionResult(http_status=200, body=body)


def run_sync_command_blocking(
    dependencies: WorkerCommandDependencies,
    *,
    effects_policy: str = SYNC_EFFECTS_FULL,
) -> dict[str, int]:
    original_client = dependencies.deepcoin_client_factory()
    client = (
        _ReadOnlyDeepcoinClientFacade(original_client)
        if effects_policy == SYNC_EFFECTS_RECONCILE_ONLY
        else original_client
    )
    if effects_policy == SYNC_EFFECTS_RECONCILE_ONLY:
        reconcile_result = reconcile_deepcoin_execution_bindings_read_only(
            dependencies.session_factory,
            client=client,
            recovered_at=dependencies.now_provider(),
        )
        result = sync_manual_closed_deepcoin_positions(
            dependencies.session_factory,
            client=client,
            synced_at=dependencies.now_provider(),
            allow_exchange_mutations=False,
        )
        return {
            "checked": result.checked,
            "manually_closed": result.manually_closed,
            "skipped_without_pos_id": result.skipped_without_pos_id,
            "reconciled_active": reconcile_result.active,
            "reconciled_open": reconcile_result.open,
            "reconciled_stale": reconcile_result.stale,
        }
    reconcile_result = (
        reconcile_deepcoin_execution_bindings(
            dependencies.session_factory,
            client=client,
            recovered_at=dependencies.now_provider(),
            contract_spec_provider=dependencies.contract_spec_provider,
        )
        if hasattr(client, "list_open_orders")
        else None
    )
    result = sync_manual_closed_deepcoin_positions(
        dependencies.session_factory,
        client=client,
        synced_at=dependencies.now_provider(),
    )
    return {
        "checked": result.checked,
        "manually_closed": result.manually_closed,
        "skipped_without_pos_id": result.skipped_without_pos_id,
        "reconciled_active": reconcile_result.active if reconcile_result else 0,
        "reconciled_open": reconcile_result.open if reconcile_result else 0,
        "reconciled_stale": reconcile_result.stale if reconcile_result else 0,
    }


async def _execute_close(
    request: dict[str, Any],
    dependencies: WorkerCommandDependencies,
) -> WorkerCommandExecutionResult:
    body = await asyncio.to_thread(
        _run_close_blocking,
        request,
        dependencies,
    )
    if isinstance(
        dependencies.system_operator_bot_config,
        SystemOperatorBotConfig,
    ):
        await dependencies.cleanup_notification_deliverer(
            dependencies.session_factory,
            config=dependencies.system_operator_bot_config,
            delivered_at=dependencies.now_provider(),
        )
    return WorkerCommandExecutionResult(http_status=200, body=body)


def _run_close_blocking(
    request: dict[str, Any],
    dependencies: WorkerCommandDependencies,
) -> Any:
    return close_bound_position_market(
        dependencies.session_factory,
        pos_id=str(request["pos_id"]),
        deepcoin_client=dependencies.deepcoin_client_factory(),
        executed_at=dependencies.now_provider(),
    )


async def _execute_recovery(
    request: dict[str, Any],
    dependencies: WorkerCommandDependencies,
) -> WorkerCommandExecutionResult:
    body = await asyncio.to_thread(
        _run_recovery_blocking,
        request,
        dependencies,
    )
    return WorkerCommandExecutionResult(http_status=200, body=body)


def _run_recovery_blocking(
    request: dict[str, Any],
    dependencies: WorkerCommandDependencies,
) -> Any:
    return submit_recovery_order_live(
        dependencies.session_factory,
        chat_id=int(request["chat_id"]),
        message_id=int(request["message_id"]),
        symbol=str(request["symbol"]),
        side=str(request["side"]),
        deepcoin_client=dependencies.deepcoin_client_factory(),
        contract_spec_provider=dependencies.contract_spec_provider,
        submitted_at=dependencies.now_provider(),
    )


async def _execute_process_next(
    dependencies: WorkerCommandDependencies,
) -> WorkerCommandExecutionResult:
    result = await asyncio.to_thread(
        process_next_trade_signal_live,
        dependencies.session_factory,
        deepcoin_client_factory=dependencies.deepcoin_client_factory,
        contract_spec_provider=dependencies.contract_spec_provider,
        processed_at=dependencies.now_provider(),
    )
    return WorkerCommandExecutionResult(
        http_status=200,
        body={"processed": result is not None, "result": result},
    )
