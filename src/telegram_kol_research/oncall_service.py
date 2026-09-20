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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from telegram_kol_research.oncall_alerts import (
    AlertPolicy,
    TelegramAlertSender,
    beijing_date,
    codex_daily_cap_note,
    codex_unavailable_note,
    compose_case_alerts,
    compose_codex_state_alert,
    compose_diagnosis_alert,
    deliver_pending_alerts,
    maybe_compose_daily_summary,
)
from telegram_kol_research.oncall_casefile import CasefileConfig, build_case_file
from telegram_kol_research.oncall_codex import (
    CODEX_DOWN,
    CODEX_UP,
    DEFAULT_SPOOL,
    FAILURE_CONTRACT,
    FAILURE_TIMEOUT,
    MAX_ATTEMPTS,
    PROMPT_VERSION,
    RUN_OK,
    Spool,
    VerdictContractError,
    next_availability,
    validate_verdict,
)
from telegram_kol_research.oncall_detector import (
    COUNTER_READ_FAILED_ROUNDS,
    COUNTER_SKIPPED_NO_POSITION,
    WATCH_PROCESSING_JOB,
    DetectorConfig,
    ProductionReadError,
    ProductionReader,
    RoundOutcome,
    run_detection_round,
)
from telegram_kol_research.oncall_state import (
    DIAGNOSIS_DONE,
    DIAGNOSIS_FAILED,
    DIAGNOSIS_SKIPPED,
    MESSAGE_QUEUED,
    MESSAGE_SUPPRESSED,
    OncallStateStore,
    isoformat,
)


logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_DRY_RUN = "dry_run"
MODE_NOTIFY = "notify"
VALID_MODES = (MODE_OFF, MODE_DRY_RUN, MODE_NOTIFY)

ENV_MODE = "TELEGRAM_KOL_ONCALL_MODE"
ENV_BOT_TOKEN = "TELEGRAM_KOL_ONCALL_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_KOL_ONCALL_CHAT_ID"
#: The existing system operator bot (``config/system_operator_bot.env``, a file
#: that holds exactly these two keys). The unit loads that file directly, so
#: nobody has to copy a token; the watcher still sends on its own, from its own
#: process, which is the independence that matters.
ENV_SYSTEM_BOT_TOKEN = "TELEGRAM_KOL_SYSTEM_BOT_TOKEN"
ENV_SYSTEM_BOT_CHAT_ID = "TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID"
EXIT_CONFIG = 78  # EX_CONFIG; the unit lists it in RestartPreventExitStatus
ENV_WORKER_HEALTH_URL = "TELEGRAM_KOL_ONCALL_WORKER_HEALTH_URL"
ENV_DAILY_CAP = "TELEGRAM_KOL_ONCALL_DAILY_ALERT_CAP"

#: Phase 2. ``off`` is the default in code, so an environment that has never
#: heard of Codex behaves exactly like phase 1.
ENV_CODEX_MODE = "TELEGRAM_KOL_ONCALL_CODEX_MODE"
ENV_CODEX_SPOOL = "TELEGRAM_KOL_ONCALL_CODEX_SPOOL"
ENV_CODEX_DAILY_CAP = "TELEGRAM_KOL_ONCALL_CODEX_DAILY_CAP"

CODEX_MODE_OFF = "off"
CODEX_MODE_SHADOW = "shadow"
CODEX_MODE_ON = "on"
VALID_CODEX_MODES = (CODEX_MODE_OFF, CODEX_MODE_SHADOW, CODEX_MODE_ON)

#: A health case that fixes itself in four minutes is not worth a token: both
#: 2026-09-20 stalls recovered on their own (spec 6.1).
HEALTH_DIAGNOSIS_DELAY = timedelta(minutes=10)

#: How long a queued request may go unanswered before the watcher writes it
#: off. The runner's own call timeout is 480 s, so this is not a race with a
#: slow answer -- it is what stops a stopped runner from silently holding the
#: single-flight slot forever.
DIAGNOSIS_ABANDON_AFTER = timedelta(minutes=30)

META_CODEX_STATE = "codex:state"
META_CODEX_FAILURES = "codex:consecutive_failures"
META_CODEX_DOWN_CLASS = "codex:down_class"
META_CODEX_EPISODE = "codex:episode"
META_CODEX_HEALTH_AT = "codex:health_checked_at"

WORKER_HEALTH_TIMEOUT_SECONDS = 2.0


class OncallConfigError(RuntimeError):
    """The watcher was told to notify but has nothing to notify with."""
HEARTBEAT_FILENAME = "heartbeat.json"


@dataclass(frozen=True, slots=True)
class OncallConfig:
    mode: str = MODE_OFF
    bot_token: str = ""
    chat_id: str = ""
    worker_health_url: str = ""
    daily_alert_cap: int = 30
    codex_mode: str = CODEX_MODE_OFF
    codex_spool: str = DEFAULT_SPOOL
    codex_daily_cap: int = 20

    @property
    def can_send(self) -> bool:
        return self.mode == MODE_NOTIFY and bool(self.bot_token) and bool(self.chat_id)

    def effective_codex_mode(self) -> str:
        """``on`` while the watcher itself is rehearsing means ``shadow``.

        A dry-run watcher composes alerts it never sends; a dry-run watcher
        that queued diagnosis messages would be claiming to have told somebody
        something it did not.
        """

        if self.codex_mode == CODEX_MODE_ON and self.mode != MODE_NOTIFY:
            return CODEX_MODE_SHADOW
        return self.codex_mode


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
    codex_mode = str(
        source.get(ENV_CODEX_MODE, CODEX_MODE_OFF) or CODEX_MODE_OFF
    ).strip().lower()
    if codex_mode not in VALID_CODEX_MODES:
        logger.warning("oncall codex mode is not recognised, treating as off: %r", codex_mode)
        codex_mode = CODEX_MODE_OFF
    try:
        codex_cap = int(str(source.get(ENV_CODEX_DAILY_CAP, "20") or "20"))
    except ValueError:
        codex_cap = 20
    return OncallConfig(
        mode=mode,
        bot_token=(
            str(source.get(ENV_BOT_TOKEN, "") or "").strip()
            or str(source.get(ENV_SYSTEM_BOT_TOKEN, "") or "").strip()
        ),
        chat_id=(
            str(source.get(ENV_CHAT_ID, "") or "").strip()
            or str(source.get(ENV_SYSTEM_BOT_CHAT_ID, "") or "").strip()
        ),
        worker_health_url=str(source.get(ENV_WORKER_HEALTH_URL, "") or "").strip(),
        daily_alert_cap=max(1, cap),
        codex_mode=codex_mode,
        codex_spool=str(source.get(ENV_CODEX_SPOOL, "") or "").strip() or DEFAULT_SPOOL,
        codex_daily_cap=max(0, codex_cap),
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


# --------------------------------------------------------------------------
# Codex diagnosis (phase 2). Everything here is optional, bounded, and
# incapable of delaying or suppressing the phase 1 alert.
# --------------------------------------------------------------------------


def _codex_daily_key(now: datetime) -> str:
    return f"codex:daily_calls:{beijing_date(now)}"


def codex_state(store: OncallStateStore) -> str:
    state = store.get_meta(META_CODEX_STATE, CODEX_UP) or CODEX_UP
    return state if state in {CODEX_UP, CODEX_DOWN} else CODEX_UP


def codex_note_for_new_cases(
    store: OncallStateStore, *, config: OncallConfig, now: datetime
) -> str | None:
    """The extra line an opening alert carries when no diagnosis is coming.

    Derived from state the watcher already holds: no call, no file read, no
    way for this to slow the alert down.
    """

    if config.effective_codex_mode() == CODEX_MODE_OFF:
        return None
    if codex_state(store) == CODEX_DOWN:
        return codex_unavailable_note(store.get_meta(META_CODEX_DOWN_CLASS))
    if store.get_int_meta(_codex_daily_key(now), 0) >= config.codex_daily_cap:
        return codex_daily_cap_note(config.codex_daily_cap)
    return None


def _record_availability(
    store: OncallStateStore,
    *,
    now: datetime,
    success: bool,
    failure_class: str | None,
) -> None:
    """Fold one observation in, and say so out loud when the state flips."""

    decision = next_availability(
        state=codex_state(store),
        consecutive_failures=store.get_int_meta(META_CODEX_FAILURES, 0),
        success=success,
        failure_class=failure_class,
    )
    store.set_meta(META_CODEX_STATE, decision.state)
    store.set_meta(META_CODEX_FAILURES, str(decision.consecutive_failures))
    if not decision.changed:
        return
    if decision.state == CODEX_DOWN:
        episode = isoformat(now)
        store.set_meta(META_CODEX_EPISODE, episode)
        store.set_meta(META_CODEX_DOWN_CLASS, str(failure_class or ""))
        compose_codex_state_alert(
            store, now=now, down=True, failure_class=failure_class, episode=episode
        )
        logger.warning("oncall codex is down class=%s", failure_class)
        return
    compose_codex_state_alert(
        store,
        now=now,
        down=False,
        episode=store.get_meta(META_CODEX_EPISODE) or isoformat(now),
    )
    store.set_meta(META_CODEX_DOWN_CLASS, "")
    logger.info("oncall codex recovered")


def _consume_health_report(
    store: OncallStateStore, spool: Spool, *, now: datetime
) -> None:
    """Read the runner's self-check, once per report it writes."""

    payload = spool.read_health()
    if not isinstance(payload, dict):
        return
    checked_at = str(payload.get("checked_at") or "")
    if not checked_at or store.get_meta(META_CODEX_HEALTH_AT) == checked_at:
        return
    store.set_meta(META_CODEX_HEALTH_AT, checked_at)
    _record_availability(
        store,
        now=now,
        success=bool(payload.get("available")),
        failure_class=payload.get("failure_class"),
    )


def _is_abandoned(record: Any, *, now: datetime) -> bool:
    """A queued request nobody answered within the give-up window.

    The window is comfortably longer than the runner's own 480-second call
    timeout plus its polling interval, so a slow answer is never mistaken for
    a missing one.
    """

    requested = record.requested_at
    return requested is not None and (now - requested) > DIAGNOSIS_ABANDON_AFTER


def _diagnosis_candidates(store: OncallStateStore, *, now: datetime) -> list[Any]:
    """Open cases with no diagnosis row yet, oldest first.

    A management case is eligible the moment it opens; a health case has to
    survive ten minutes first.
    """

    candidates = []
    for case in store.open_cases():
        if store.get_diagnosis(case.id) is not None:
            continue
        if case.case_key.startswith("health:"):
            opened = case.first_seen_at
            if opened is None or (now - opened) < HEALTH_DIAGNOSIS_DELAY:
                continue
        candidates.append(case)
    return candidates


def _build_and_enqueue(
    store: OncallStateStore,
    spool: Spool,
    *,
    case: Any,
    attempt: int,
    database_path: Path,
    now: datetime,
    casefile_config: CasefileConfig | None = None,
    journal_runner: Callable[..., str] | None = None,
) -> bool:
    is_health = case.case_key.startswith("health:")
    stalled_job_ids: list[int] = []
    try:
        with ProductionReader(database_path) as reader:
            if is_health:
                stalled_job_ids = [
                    item.object_id
                    for item in store.open_watch_items(WATCH_PROCESSING_JOB, 20)
                ]
            payload = build_case_file(
                reader,
                case=case,
                now=now,
                config=casefile_config,
                stalled_job_ids=stalled_job_ids,
                journal_runner=journal_runner,
            )
    except ProductionReadError as exc:
        logger.warning("oncall case file could not be exported: %s", exc)
        return False

    try:
        fingerprint = spool.enqueue(
            case_id=case.id,
            attempt=attempt,
            kind="health" if is_health else "management",
            case_payload=payload,
            now=now,
        )
    except OSError as exc:
        # A spool the watcher cannot write to (wrong owner, wrong mode, not
        # created yet) is a deployment problem, not a reason to stop watching.
        logger.warning(
            "oncall could not write to the codex spool: %s", type(exc).__name__
        )
        return False
    store.record_diagnosis_request(
        case_id=case.id,
        attempt=attempt,
        fingerprint=fingerprint,
        prompt_version=PROMPT_VERSION,
        now=now,
    )
    store.bump_counter(_codex_daily_key(now))
    logger.info(
        "oncall queued a diagnosis case=%s attempt=%s redactions=%s",
        case.id,
        attempt,
        payload.get("redactions"),
    )
    return True


def _handle_finished_run(
    store: OncallStateStore,
    spool: Spool,
    *,
    record: Any,
    run: Mapping[str, Any],
    config: OncallConfig,
    now: datetime,
    policy: AlertPolicy,
    database_path: Path,
    casefile_config: CasefileConfig | None,
    journal_runner: Callable[..., str] | None,
) -> str:
    """Turn one finished run into a stored verdict, a retry, or a failure."""

    case = store.get_case(record.case_id)
    failure_class: str | None = None
    verdict_payload: dict[str, Any] | None = None

    if str(run.get("status")) == RUN_OK:
        try:
            verdict_payload = validate_verdict(
                spool.read_verdict(record.case_id), case_id=record.case_id
            ).as_dict()
        except VerdictContractError as exc:
            logger.warning(
                "oncall rejected a verdict case=%s reason=%s", record.case_id, exc
            )
            failure_class = FAILURE_CONTRACT
    else:
        failure_class = str(run.get("failure_class") or "other")

    if failure_class is None and verdict_payload is not None:
        store.record_diagnosis_result(
            case_id=record.case_id,
            status=DIAGNOSIS_DONE,
            now=now,
            verdict=verdict_payload,
        )
        _record_availability(store, now=now, success=True, failure_class=None)
        _maybe_send_diagnosis(
            store,
            case=case,
            verdict=verdict_payload,
            config=config,
            now=now,
            policy=policy,
        )
        return "done"

    _record_availability(store, now=now, success=False, failure_class=failure_class)
    retry_allowed = (
        record.attempts < MAX_ATTEMPTS
        and codex_state(store) != CODEX_DOWN
        and store.get_int_meta(_codex_daily_key(now), 0) < config.codex_daily_cap
        and case is not None
        and case.status == "open"
    )
    if retry_allowed and _build_and_enqueue(
        store,
        spool,
        case=case,
        attempt=record.attempts + 1,
        database_path=database_path,
        now=now,
        casefile_config=casefile_config,
        journal_runner=journal_runner,
    ):
        return "retried"
    store.record_diagnosis_result(
        case_id=record.case_id,
        status=DIAGNOSIS_FAILED,
        now=now,
        failure_class=failure_class,
    )
    return "failed"


def _maybe_send_diagnosis(
    store: OncallStateStore,
    *,
    case: Any,
    verdict: Mapping[str, Any],
    config: OncallConfig,
    now: datetime,
    policy: AlertPolicy,
) -> None:
    if case is None:
        return
    if config.effective_codex_mode() != CODEX_MODE_ON:
        store.set_diagnosis_message_state(case.id, MESSAGE_SUPPRESSED)
        return
    if case.status != "open":
        # The problem fixed itself while Codex was thinking. The diagnosis is
        # still worth keeping; telling somebody about a solved problem is not.
        store.set_diagnosis_message_state(case.id, MESSAGE_SUPPRESSED)
        return
    if compose_diagnosis_alert(
        store, case=case, verdict=verdict, now=now, policy=policy
    ):
        store.set_diagnosis_message_state(case.id, MESSAGE_QUEUED)


def run_codex_cycle(
    store: OncallStateStore,
    *,
    config: OncallConfig,
    database_path: Path,
    now: datetime,
    policy: AlertPolicy | None = None,
    spool: Spool | None = None,
    casefile_config: CasefileConfig | None = None,
    journal_runner: Callable[..., str] | None = None,
) -> dict[str, int]:
    """Poll finished diagnoses, then queue at most one new one.

    Order matters: reading a result can free the single-flight slot, and a
    result that arrives while the case is still open is the whole point.
    """

    settings = policy or AlertPolicy(daily_cap=config.daily_alert_cap)
    mode = config.effective_codex_mode()
    counters = {"polled": 0, "queued": 0, "failed": 0, "skipped": 0}
    if mode == CODEX_MODE_OFF:
        return counters

    # ``Path("")`` is the current directory, which is the last place a case
    # file should ever be written; fall back to the real spool instead.
    shared = spool or Spool(root=Path(config.codex_spool or DEFAULT_SPOOL))
    _consume_health_report(store, shared, now=now)

    for record in store.queued_diagnoses():
        run = shared.read_run(record.case_id)
        answered = isinstance(run, dict) and str(
            run.get("request_fingerprint") or ""
        ) == str(record.request_fingerprint or "")
        if not answered:
            if _is_abandoned(record, now=now):
                # Nobody answered. The runner may be stopped, may have been
                # killed mid-call, or may never have been installed -- and a
                # request nobody will ever answer would otherwise hold the
                # single-flight slot for good and silently end diagnosis for
                # every later case. Three of these take Codex down loudly,
                # which is the behaviour the user asked for: never silent.
                store.record_diagnosis_result(
                    case_id=record.case_id,
                    status=DIAGNOSIS_FAILED,
                    now=now,
                    failure_class=FAILURE_TIMEOUT,
                )
                _record_availability(
                    store, now=now, success=False, failure_class=FAILURE_TIMEOUT
                )
                counters["failed"] += 1
                logger.warning(
                    "oncall diagnosis was never answered case=%s", record.case_id
                )
            continue
        counters["polled"] += 1
        result = _handle_finished_run(
            store,
            shared,
            record=record,
            run=run,
            config=config,
            now=now,
            policy=settings,
            database_path=database_path,
            casefile_config=casefile_config,
            journal_runner=journal_runner,
        )
        if result == "failed":
            counters["failed"] += 1
        elif result == "retried":
            counters["queued"] += 1

    if codex_state(store) == CODEX_DOWN:
        # New cases wait for recovery rather than burning an eight-minute
        # timeout each; the opening alert already says so.
        return counters
    if store.queued_diagnoses():
        return counters  # single flight

    for case in _diagnosis_candidates(store, now=now):
        if store.get_int_meta(_codex_daily_key(now), 0) >= config.codex_daily_cap:
            store.record_diagnosis_result(
                case_id=case.id,
                status=DIAGNOSIS_SKIPPED,
                now=now,
                skip_reason="daily_cap",
            )
            counters["skipped"] += 1
            continue
        if _build_and_enqueue(
            store,
            shared,
            case=case,
            attempt=1,
            database_path=database_path,
            now=now,
            casefile_config=casefile_config,
            journal_runner=journal_runner,
        ):
            counters["queued"] += 1
            break  # single flight: one outstanding call at a time
    return counters


def run_oncall_round(
    *,
    store: OncallStateStore,
    database_path: Path,
    config: OncallConfig,
    detector_config: DetectorConfig,
    now: datetime,
    sender: Callable[[str], None] | None,
    worker_health_probe: Callable[[], bool] | None,
    spool: Spool | None = None,
    casefile_config: CasefileConfig | None = None,
    journal_runner: Callable[..., str] | None = None,
) -> RoundOutcome:
    """Detect, compose, deliver. One round, no sleeping, no process concerns.

    The Codex step sits *between* composing the phase 1 alerts and delivering
    them, and every failure inside it is swallowed. So a diagnosis can join
    the same delivery pass when it is ready, while nothing Codex does -- being
    down, being slow, throwing -- can delay or suppress the alert that says a
    message was not carried out. The tests assert both halves of that.
    """

    policy = AlertPolicy(daily_cap=config.daily_alert_cap)
    outcome = run_detection_round(
        reader_factory=lambda: ProductionReader(database_path),
        store=store,
        now=now,
        config=detector_config,
        worker_health_probe=worker_health_probe,
    )
    try:
        codex_note = codex_note_for_new_cases(store, config=config, now=now)
    except Exception:  # noqa: BLE001 - a note is never worth a missed alert
        logger.exception("oncall could not derive the codex note")
        codex_note = None
    compose_case_alerts(
        store,
        now=now,
        new_case_ids=outcome.new_case_ids,
        resolved_case_ids=outcome.resolved_case_ids,
        policy=policy,
        codex_note=codex_note,
    )
    try:
        run_codex_cycle(
            store,
            config=config,
            database_path=Path(database_path),
            now=now,
            policy=policy,
            spool=spool,
            casefile_config=casefile_config,
            journal_runner=journal_runner,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:  # noqa: BLE001 - diagnosis is an extra, never a gate
        logger.exception("oncall codex cycle failed")
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
    spool: Spool | None = None,
    journal_runner: Callable[..., str] | None = None,
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
    if settings.mode == MODE_NOTIFY and sender is None and not settings.can_send:
        # A watcher that detects and then cannot speak is the exact failure it
        # exists to prevent; refuse to run rather than run silently.
        raise OncallConfigError(
            "oncall mode is notify but no bot token / chat id is configured"
        )
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
                    spool=spool,
                    journal_runner=journal_runner,
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
