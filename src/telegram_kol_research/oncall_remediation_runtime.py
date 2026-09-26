"""Worker-process wiring for the phase-3 remediation library.

``oncall_remediation.py`` (phase-3 batch 2) is a synchronous,
dependency-injectable library with no transport of its own. This module is
the third batch's connective tissue: it owns the one piece of coordination
state the library deliberately left out -- the in-process half of gate C1's
single-flight guarantee (spec 4.4 C1; the library only implements the
database CAS half, see ``oncall_remediation.py``'s ``_handle_step2``
docstring and ``docs/codex-oncall-status.md`` section 9.2 deviation 4) -- and
the background loop that turns ``requested`` proposal rows into sent
Telegram messages.

Every call into the library is synchronous and is therefore always wrapped in
``asyncio.to_thread`` here (the 2026-09-15 event-loop-freeze incident is the
reason ``build_position_management_remediation_plan`` and every other
synchronous, DB/exchange-touching call in this module is never awaited
directly on the event loop). Every step of the loop has its own
``try/except`` so one bad proposal can never stop the loop, and this module
never calls ``execute_proposal`` on its own initiative -- only
``execute_proposal_locked`` does that, and it is only ever invoked by the
system-bot callback handler in ``telegram_bot_commands.py`` after gate G-B's
second confirmation already promoted the row to ``executing``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from telegram_kol_research.config import OncallRemediationConfig
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.models import OncallRemediationProposal
from telegram_kol_research.oncall_remediation import (
    ExecutionOutcome,
    compute_requested_proposal,
    execute_proposal,
    expire_stale_proposals,
    finalize_executing_proposals,
    record_proposal_message,
    recover_after_restart,
)
from telegram_kol_research.system_operator_bot import (
    SystemOperatorBotConfig,
    send_system_operator_bot_message,
)

logger = logging.getLogger(__name__)


# Global in-process mutex for gate C1's "at most one proposal executing at a
# time" (spec 4.4 C1). Module-level and shared by every call to
# ``execute_proposal_locked`` in this process -- there is exactly one worker
# process per deployment, so one lock per process is the whole guarantee.
_EXECUTION_LOCK = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class OncallRemediationWiring:
    """Everything the system-bot command loop needs to reach batch 2/3.

    Built once per background-task (re)start and passed into
    ``run_system_operator_bot_command_loop`` (default ``None`` -- see
    ``telegram_bot_commands.py``'s dormant-by-default guarantee: when this is
    ``None`` an ``orm:`` callback or ``/fix``/``/oncall_off``/``/oncall_on``
    text command answers "补救未启用" without importing or touching anything
    else in this module).
    """

    config: OncallRemediationConfig
    session_factory: Any
    deepcoin_client_factory: Callable[[], Any]
    group_config_provider: Callable[[], GroupConfig]
    now_provider: Callable[[], Any]


def _keyboard_to_reply_markup(
    keyboard: tuple[tuple[str, str], ...] | None,
) -> dict[str, Any] | None:
    if not keyboard:
        return None
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data}] for label, data in keyboard
        ]
    }


async def clear_system_operator_bot_reply_markup(
    *, config: SystemOperatorBotConfig, message_id: int
) -> None:
    """Remove a message's inline keyboard. Best-effort: a 4xx (message
    already edited/deleted, or already has no markup) is not an error worth
    surfacing -- this is cosmetic cleanup for an expired proposal, never the
    thing that decides whether a proposal is still actionable (the state
    machine in ``oncall_remediation.py`` is the only source of truth for
    that)."""

    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{config.bot_token}/editMessageReplyMarkup",
            json={
                "chat_id": config.chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )
        if response.status_code >= 500:
            response.raise_for_status()


async def execute_proposal_locked(
    session_factory: Any,
    *,
    config: OncallRemediationConfig,
    proposal_id: int,
    deepcoin_client_factory: Callable[[], Any],
    group_config: GroupConfig,
    now_provider: Callable[[], Any],
) -> ExecutionOutcome:
    """The G-C promotion path: acquire the process-wide execution lock, then
    run the library's ``execute_proposal`` (which reruns every gate) on a
    worker thread. Only the callback handler for step 2 ("确认执行") calls
    this -- see module docstring."""

    async with _EXECUTION_LOCK:
        # The client is created inside execute_proposal (on the worker
        # thread) so a factory failure settles the proposal as "failed"
        # instead of leaving it stuck in "executing" -- which would also hold
        # the single-flight slot until the next restart.
        created: list[Any] = []

        def _factory() -> Any:
            client = deepcoin_client_factory()
            created.append(client)
            return client

        try:
            return await asyncio.to_thread(
                execute_proposal,
                session_factory,
                config=config,
                proposal_id=proposal_id,
                deepcoin_client_factory=_factory,
                group_config=group_config,
                now=now_provider(),
            )
        finally:
            deepcoin_client = created[0] if created else None
            close = getattr(deepcoin_client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    logger.warning(
                        "oncall remediation deepcoin client cleanup failed "
                        "(execute) proposal_id=%s",
                        proposal_id,
                    )


async def _list_requested_proposal_ids(session_factory: Any) -> list[int]:
    def _query() -> list[int]:
        with session_factory() as session:
            rows = (
                session.query(OncallRemediationProposal.id)
                .filter(OncallRemediationProposal.state == "requested")
                .order_by(OncallRemediationProposal.id.asc())
                .all()
            )
            return [int(row[0]) for row in rows]

    return await asyncio.to_thread(_query)


async def _process_one_requested_proposal(
    *,
    proposal_id: int,
    config: OncallRemediationConfig,
    session_factory: Any,
    deepcoin_client_factory: Callable[[], Any],
    group_config_provider: Callable[[], GroupConfig],
    bot_config: SystemOperatorBotConfig | None,
    now_provider: Callable[[], Any],
) -> None:
    group_config = group_config_provider()
    deepcoin_client = await asyncio.to_thread(deepcoin_client_factory)
    try:
        outcome = await asyncio.to_thread(
            compute_requested_proposal,
            session_factory,
            config=config,
            proposal_id=proposal_id,
            deepcoin_client=deepcoin_client,
            group_config=group_config,
            now=now_provider(),
            group_label=lambda chat_id: _group_label(group_config, chat_id),
        )
    finally:
        close = getattr(deepcoin_client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.warning(
                    "oncall remediation deepcoin client cleanup failed "
                    "(propose) proposal_id=%s",
                    proposal_id,
                )

    if not outcome.should_send or not outcome.text:
        return
    if bot_config is None:
        logger.warning(
            "oncall remediation proposal message not sent: system operator "
            "bot is not configured proposal_id=%s",
            proposal_id,
        )
        return
    try:
        message_id = await send_system_operator_bot_message(
            config=bot_config,
            text=outcome.text,
            reply_markup=_keyboard_to_reply_markup(outcome.keyboard),
        )
    except Exception:  # noqa: BLE001 - spec 8.1: send failure never blocks the loop
        logger.warning(
            "oncall remediation proposal message failed to send "
            "proposal_id=%s",
            proposal_id,
        )
        return
    # Spec 6.3: a send failure leaves the row in ``proposed`` with no
    # message id, forever un-clickable, until it expires on its own -- "没有
    # 按钮就不可能被批准". Only a successful send is worth recording.
    if message_id is not None and outcome.state == "proposed":
        await asyncio.to_thread(
            record_proposal_message,
            session_factory,
            proposal_id=proposal_id,
            telegram_message_id=int(message_id),
        )


def _group_label(group_config: GroupConfig, chat_id: int) -> str:
    for group in group_config.groups:
        if int(getattr(group, "chat_id", -1) or -1) == int(chat_id):
            return str(
                getattr(group, "custom_group_label", None)
                or getattr(group, "chat_title", None)
                or chat_id
            )
    return "群组"


async def run_oncall_remediation_background_loop(
    *,
    config: OncallRemediationConfig,
    session_factory: Any,
    deepcoin_client_factory: Callable[[], Any],
    group_config_provider: Callable[[], GroupConfig],
    bot_config: SystemOperatorBotConfig | None,
    now_provider: Callable[[], Any],
    wake_event: asyncio.Event,
    poll_interval_seconds: float = 10.0,
    follow_timeout_minutes: float = 15.0,
) -> None:
    """Consume ``requested`` rows and finalize/expire settled ones.

    Runs forever; the caller wraps this in
    ``_supervise_restartable_background_task`` like every other worker loop,
    so an unhandled exception here just means the whole loop restarts with
    backoff -- which is why every individual step below still has its own
    ``try/except``: a single proposal's failure should never cost the loop
    the rest of its work (expiry, finalize) on the same tick.
    """

    from datetime import timedelta

    recovered_texts = await asyncio.to_thread(
        recover_after_restart, session_factory, now=now_provider()
    )
    for text in recovered_texts:
        if bot_config is None:
            logger.warning(
                "oncall remediation restart-recovery notice not sent: "
                "system operator bot is not configured"
            )
            continue
        try:
            await send_system_operator_bot_message(config=bot_config, text=text)
        except Exception:  # noqa: BLE001
            logger.warning("oncall remediation restart-recovery notice failed to send")

    while True:
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=poll_interval_seconds)
        except asyncio.TimeoutError:
            pass
        wake_event.clear()

        try:
            stale_message_ids = await asyncio.to_thread(
                expire_stale_proposals, session_factory, now=now_provider()
            )
        except Exception:  # noqa: BLE001
            logger.warning("oncall remediation expire_stale_proposals failed")
            stale_message_ids = []
        for message_id in stale_message_ids:
            if bot_config is None:
                continue
            try:
                await clear_system_operator_bot_reply_markup(
                    config=bot_config, message_id=message_id
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "oncall remediation failed to clear expired keyboard "
                    "message_id=%s",
                    message_id,
                )

        try:
            requested_ids = await _list_requested_proposal_ids(session_factory)
        except Exception:  # noqa: BLE001
            logger.warning("oncall remediation requested-proposal scan failed")
            requested_ids = []
        for proposal_id in requested_ids:
            try:
                await _process_one_requested_proposal(
                    proposal_id=proposal_id,
                    config=config,
                    session_factory=session_factory,
                    deepcoin_client_factory=deepcoin_client_factory,
                    group_config_provider=group_config_provider,
                    bot_config=bot_config,
                    now_provider=now_provider,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "oncall remediation compute_requested_proposal failed "
                    "proposal_id=%s",
                    proposal_id,
                )

        try:
            finalized = await asyncio.to_thread(
                finalize_executing_proposals,
                session_factory,
                config=config,
                now=now_provider(),
                follow_timeout=timedelta(minutes=follow_timeout_minutes),
            )
        except Exception:  # noqa: BLE001
            logger.warning("oncall remediation finalize_executing_proposals failed")
            finalized = []
        for finalize_outcome in finalized:
            if bot_config is None:
                continue
            try:
                await send_system_operator_bot_message(
                    config=bot_config, text=finalize_outcome.text
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "oncall remediation finalize result message failed to "
                    "send proposal_id=%s",
                    finalize_outcome.proposal_id,
                )
