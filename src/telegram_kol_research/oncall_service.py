"""The on-call watcher process: configuration, main loop, heartbeat.

Phase 1 of the Codex on-call remediation program
(``docs/plans/2026-09-19-codex-oncall-phase1-spec.md``, sections 5.1 and 6).

The process is **dormant by default**: with no ``TELEGRAM_KOL_ONCALL_MODE`` in
the environment it starts, reports that it is off, and exits 0. Nothing here
writes to the production database, talks to an exchange, or sends a command to
the worker; the only outbound traffic is the Telegram send and an optional
read of the worker's own loop-health endpoint.

**The loop is the last line of defence, so it never dies of one bad round.**
Any unexpected exception in a round is logged, recorded in the heartbeat file
as ``last_error`` and stepped over. ``KeyboardInterrupt`` and ``SystemExit``
are re-raised unchanged so systemd can still stop the unit.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from telegram_kol_research.oncall_alerts import (
    AlertPolicy,
    TelegramAlertSender,
    compose_case_alerts,
    deliver_pending_alerts,
    maybe_compose_daily_summary,
)
from telegram_kol_research.oncall_detector import (
    COUNTER_READ_FAILED_ROUNDS,
    COUNTER_SKIPPED_NO_POSITION,
    DetectorConfig,
    ProductionReader,
    RoundOutcome,
    run_detection_round,
)
from telegram_kol_research.oncall_state import OncallStateStore, isoformat


logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_DRY_RUN = "dry_run"
MODE_NOTIFY = "notify"
VALID_MODES = (MODE_OFF, MODE_DRY_RUN, MODE_NOTIFY)

ENV_MODE = "TELEGRAM_KOL_ONCALL_MODE"
ENV_BOT_TOKEN = "TELEGRAM_KOL_ONCALL_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_KOL_ONCALL_CHAT_ID"
ENV_WORKER_HEALTH_URL = "TELEGRAM_KOL_ONCALL_WORKER_HEALTH_URL"
ENV_DAILY_CAP = "TELEGRAM_KOL_ONCALL_DAILY_ALERT_CAP"

WORKER_HEALTH_TIMEOUT_SECONDS = 2.0
HEARTBEAT_FILENAME = "heartbeat.json"


@dataclass(frozen=True, slots=True)
class OncallConfig:
    mode: str = MODE_OFF
    bot_token: str = ""
    chat_id: str = ""
    worker_health_url: str = ""
    daily_alert_cap: int = 30

    @property
    def can_send(self) -> bool:
        return self.mode == MODE_NOTIFY and bool(self.bot_token) and bool(self.chat_id)


def load_oncall_config(env: Mapping[str, str] | None = None) -> OncallConfig:
    """Read the unit's environment once, at start. Never logged."""

    source = os.environ if env is None else env
    mode = str(source.get(ENV_MODE, MODE_OFF) or MODE_OFF).strip().lower()
    if mode not in VALID_MODES:
        logger.warning("oncall mode is not recognised, treating as off: %r", mode)
        mode = MODE_OFF
    try:
        cap = int(str(source.get(ENV_DAILY_CAP, "30") or "30"))
    except ValueError:
        cap = 30
    return OncallConfig(
        mode=mode,
        bot_token=str(source.get(ENV_BOT_TOKEN, "") or "").strip(),
        chat_id=str(source.get(ENV_CHAT_ID, "") or "").strip(),
        worker_health_url=str(source.get(ENV_WORKER_HEALTH_URL, "") or "").strip(),
        daily_alert_cap=max(1, cap),
    )


def build_worker_health_probe(url: str) -> Callable[[], bool] | None:
    """A probe that answers False for *every* failure, including a timeout."""

    if not url:
        return None

    def probe() -> bool:
        try:
            with urllib.request.urlopen(
                url, timeout=WORKER_HEALTH_TIMEOUT_SECONDS
            ) as response:
                return 200 <= int(response.status) < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    return probe


def notify_systemd(state: str) -> bool:
    """``sd_notify`` in ten lines, so the unit can be ``Type=notify``."""

    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    target = "\0" + address[1:] if address.startswith("@") else address
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect(target)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError:
        logger.debug("systemd notify failed state=%s", state)
        return False


def write_heartbeat(
    state_path: Path, *, now: datetime, round_number: int, last_error: str | None
) -> Path:
    """One small file so an outside monitor can see the watcher is alive."""

    heartbeat_path = Path(state_path).parent / HEARTBEAT_FILENAME
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": isoformat(now),
        "round": int(round_number),
        "last_error": last_error,
    }
    temporary = heartbeat_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(heartbeat_path)
    return heartbeat_path


def run_oncall_round(
    *,
    store: OncallStateStore,
    database_path: Path,
    config: OncallConfig,
    detector_config: DetectorConfig,
    now: datetime,
    sender: Callable[[str], None] | None,
    worker_health_probe: Callable[[], bool] | None,
) -> RoundOutcome:
    """Detect, compose, deliver. One round, no sleeping, no process concerns."""

    outcome = run_detection_round(
        reader_factory=lambda: ProductionReader(database_path),
        store=store,
        now=now,
        config=detector_config,
        worker_health_probe=worker_health_probe,
    )
    compose_case_alerts(
        store,
        now=now,
        new_case_ids=outcome.new_case_ids,
        resolved_case_ids=outcome.resolved_case_ids,
        policy=AlertPolicy(daily_cap=config.daily_alert_cap),
    )
    maybe_compose_daily_summary(
        store,
        now=now,
        counters={
            "skipped_no_position": store.get_int_meta(COUNTER_SKIPPED_NO_POSITION, 0),
            "read_failed_rounds": store.get_int_meta(COUNTER_READ_FAILED_ROUNDS, 0),
        },
    )
    deliver_pending_alerts(store, now=now, sender=sender)
    return outcome


def run_oncall_watch(
    *,
    database_path: str | Path,
    state_path: str | Path,
    poll_seconds: int = 60,
    once: bool = False,
    dry_run: bool = False,
    config: OncallConfig | None = None,
    detector_config: DetectorConfig | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleeper: Callable[[float], None] = time.sleep,
    sender: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """The watcher's main loop. Returns a small summary when it stops."""

    settings = config or load_oncall_config()
    if dry_run and settings.mode == MODE_NOTIFY:
        settings = replace(settings, mode=MODE_DRY_RUN)
    if settings.mode == MODE_OFF:
        # READY=1 first, even though the next statement exits: without it a
        # ``Type=notify`` unit records "never sent READY=1", which is a start
        # failure rather than the deliberate dormancy this is.
        notify_systemd("READY=1")
        logger.info("oncall watcher is off, exiting")
        return {"mode": MODE_OFF, "rounds": 0, "stopped": "mode_off"}

    detector = detector_config or DetectorConfig()
    if sender is None and settings.can_send:
        sender = TelegramAlertSender(
            bot_token=settings.bot_token, chat_id=settings.chat_id
        ).send
    elif settings.mode != MODE_NOTIFY:
        sender = None

    probe = build_worker_health_probe(settings.worker_health_url)
    database = Path(database_path)
    state_file = Path(state_path)
    rounds = 0
    last_error: str | None = None
    stopped = "completed"

    with OncallStateStore(state_file) as store:
        notify_systemd("READY=1")
        while True:
            rounds += 1
            now = clock()
            try:
                run_oncall_round(
                    store=store,
                    database_path=database,
                    config=settings,
                    detector_config=detector,
                    now=now,
                    sender=sender,
                    worker_health_probe=probe,
                )
                last_error = None
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - one bad round is not fatal
                last_error = type(exc).__name__
                logger.exception("oncall round failed round=%s", rounds)
            try:
                write_heartbeat(
                    state_file, now=now, round_number=rounds, last_error=last_error
                )
            except OSError:
                logger.warning("oncall heartbeat could not be written", exc_info=True)
            notify_systemd("WATCHDOG=1")
            if once:
                stopped = "once"
                break
            sleeper(max(1, int(poll_seconds)))
    return {
        "mode": settings.mode,
        "rounds": rounds,
        "stopped": stopped,
        "last_error": last_error,
    }
