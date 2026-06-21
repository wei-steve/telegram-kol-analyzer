"""CLI entrypoints for the Telegram KOL research app."""

import asyncio
import json
from enum import Enum
from datetime import UTC, datetime
from pathlib import Path

import typer

from telegram_kol_research.backfill import build_backfill_windows
from telegram_kol_research.binance_market_data import BinanceMarketDataProvider
from telegram_kol_research.candidates import persist_text_signal_candidates
from telegram_kol_research.message_recognition import (
    filter_records_by_inserted_message_keys,
    recognize_records_with_ai_config,
)
from telegram_kol_research.dataset_export import export_dataset_jsonl
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import load_deepcoin_contract_specs
from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.gate_market_data import GateMarketDataProvider
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.llm_adjudication import (
    export_llm_adjudication_pack,
    export_llm_submission_sample,
)
from telegram_kol_research.llm_import import import_llm_adjudication_results
from telegram_kol_research.media_retention import cleanup_media_files
from telegram_kol_research.models import RawMessage
from telegram_kol_research.models import SyncCheckpoint
from telegram_kol_research.reporting import load_leaderboard_rows, write_report
from telegram_kol_research.raw_ingest import (
    NormalizedMessageRecord,
    normalize_message_payload,
    persist_normalized_messages,
    repair_history_checkpoints,
)
from telegram_kol_research.review_queue import (
    apply_review_decision,
    apply_review_decision_to_db,
    list_pending_candidates,
    list_pending_candidates_from_db,
    load_candidates,
    write_candidates,
)
from telegram_kol_research.recovery_runner import (
    RecoveryDryRunProviderMissingError,
    run_recovery_dry_run,
)
from telegram_kol_research.recognition_experiments import run_mimo_direct_experiment
from telegram_kol_research.strategy_alerts import (
    load_strategy_alert_config,
    strategy_alerts_enabled,
)
from telegram_kol_research.telegram_client import (
    create_telegram_client,
    discover_dialogs,
    ensure_telegram_login,
    fetch_dialog_messages,
    filter_target_dialogs,
    load_telegram_auth_config,
    maybe_await,
)
from telegram_kol_research.telegram_session_lock import (
    TelegramSessionLockError,
    acquire_telegram_session_lock,
    describe_session_lock_owner,
    reap_stopped_session_lock_owner,
    release_session_lock_owner,
)
from telegram_kol_research.time_utils import normalize_to_utc_naive
from telegram_kol_research.telegram_live_listener import run_live_listener
from telegram_kol_research.trade_merge import persist_trade_ideas_from_candidates
from telegram_kol_research.web_app import create_web_app

app = typer.Typer(help="Telegram KOL win-rate research CLI.")


class SyncMode(str, Enum):
    discover = "discover"
    backfill = "backfill"
    parse = "parse"
    full = "full"


class ExperimentInputKind(str, Enum):
    all = "all"
    text = "text"
    image = "image"


def _record_within_window(record: NormalizedMessageRecord, *, start_at, end_at) -> bool:
    posted_at = record.posted_at
    if posted_at is None:
        return True
    normalized_start = normalize_to_utc_naive(start_at)
    normalized_end = normalize_to_utc_naive(end_at)
    return normalized_start <= posted_at <= normalized_end


def _load_normalized_records_from_db(
    database_path: Path,
) -> list[NormalizedMessageRecord]:
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_messages = (
            session.query(RawMessage)
            .order_by(RawMessage.chat_id, RawMessage.message_id)
            .all()
        )

    records: list[NormalizedMessageRecord] = []
    for raw_message in raw_messages:
        payload = {}
        if raw_message.raw_payload:
            try:
                payload = json.loads(raw_message.raw_payload)
            except json.JSONDecodeError:
                payload = {}
        records.append(
            normalize_message_payload(
                {
                    "chat_id": raw_message.chat_id,
                    "message_id": raw_message.message_id,
                    "sender_id": raw_message.sender_id,
                    "sender_name": raw_message.sender_name,
                    "text": raw_message.text,
                    "reply_to_msg_id": raw_message.reply_to_message_id,
                    "posted_at": raw_message.posted_at,
                    "edit_date": raw_message.edit_date,
                    "media": payload.get("media"),
                },
                archived_target_group=raw_message.archived_target_group,
            )
        )
    return records


def _run_parse_mode(database_path: Path) -> tuple[int, int]:
    session_factory = create_session_factory(database_path)
    normalized_records = _load_normalized_records_from_db(database_path)
    candidate_stats = recognize_records_with_ai_config(
        session_factory,
        normalized_records,
        fallback_recognizer=persist_text_signal_candidates,
    )
    trade_stats = persist_trade_ideas_from_candidates(session_factory)
    return candidate_stats["inserted_candidates"], trade_stats["inserted_trade_ideas"]


def _load_history_checkpoints(session_factory) -> dict[int, dict[str, int | datetime | None]]:
    with session_factory() as session:
        checkpoints = (
            session.query(SyncCheckpoint)
            .filter(SyncCheckpoint.sync_kind == "history")
            .all()
        )
    return {
        checkpoint.chat_id: {
            "last_message_id": checkpoint.last_message_id,
            "last_message_at": checkpoint.last_message_at,
        }
        for checkpoint in checkpoints
    }


async def _run_telegram_sync(
    *,
    client,
    session_factory,
    target_titles: set[str],
    windows_by_title,
    message_limit: int,
    mode: SyncMode,
) -> tuple[list[dict[str, str | int | bool | None]], int, int, int]:
    await ensure_telegram_login(
        client,
        prompt_phone=lambda: typer.prompt("Telegram phone number"),
        prompt_code=lambda: typer.prompt("Telegram login code"),
        prompt_password=lambda: typer.prompt("Telegram 2FA password", hide_input=True),
        echo=lambda message: typer.echo(message),
    )

    dialogs = await discover_dialogs(client)
    matched_dialogs = filter_target_dialogs(dialogs, target_titles)
    if mode == SyncMode.discover:
        return matched_dialogs, 0, 0, 0

    history_checkpoints = _load_history_checkpoints(session_factory)

    inserted_messages = 0
    inserted_candidates = 0
    inserted_trade_ideas = 0

    for dialog in matched_dialogs:
        payloads = await fetch_dialog_messages(client, dialog, limit=message_limit)
        dialog_id = dialog.get("id")
        checkpoint = None
        if dialog_id is not None:
            checkpoint = history_checkpoints.get(int(dialog_id))
        if checkpoint and checkpoint.get("last_message_id") is not None:
            payloads = [
                payload
                for payload in payloads
                if int(payload.get("message_id") or 0) > int(checkpoint["last_message_id"])
            ]
        normalized_records = [
            normalize_message_payload(payload, archived_target_group=True)
            for payload in payloads
        ]
        window = windows_by_title.get(dialog.get("title"))
        if window is not None:
            normalized_records = [
                record
                for record in normalized_records
                if _record_within_window(
                    record,
                    start_at=window.start_at,
                    end_at=window.end_at,
                )
            ]
        stats = persist_normalized_messages(
            session_factory, normalized_records, sync_kind="history"
        )
        inserted_messages += stats["inserted_messages"]
        if mode == SyncMode.backfill:
            continue
        candidate_stats = recognize_records_with_ai_config(
            session_factory,
            filter_records_by_inserted_message_keys(normalized_records, stats),
            fallback_recognizer=persist_text_signal_candidates,
        )
        inserted_candidates += candidate_stats["inserted_candidates"]
        trade_stats = persist_trade_ideas_from_candidates(session_factory)
        inserted_trade_ideas += trade_stats["inserted_trade_ideas"]

    return matched_dialogs, inserted_messages, inserted_candidates, inserted_trade_ideas


@app.command("mimo-experiment")
def mimo_experiment(
    database_path: Path = Path("data/research.db"),
    ai_config_path: Path = Path("config/ai_recognition.yaml"),
    media_root: Path = Path("data/media"),
    limit: int = typer.Option(100, "--limit", min=1, help="Maximum messages to consider."),
    kind: ExperimentInputKind = typer.Option(
        ExperimentInputKind.all,
        "--kind",
        help="Message input kind to test.",
    ),
    rerun: bool = typer.Option(
        False,
        "--rerun",
        help="Re-run messages that already have this experiment result.",
    ),
) -> None:
    """Run the MiMo direct multimodal recognition experiment as a side channel."""

    session_factory = create_session_factory(database_path)
    try:
        stats = run_mimo_direct_experiment(
            session_factory,
            ai_recognition_config_path=ai_config_path,
            media_root=media_root,
            limit=limit,
            input_kind=kind.value,
            rerun=rerun,
        )
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "MiMo experiment finished: "
        f"considered={stats.considered}, "
        f"succeeded={stats.succeeded}, "
        f"failed={stats.failed}, "
        f"skipped_no_input={stats.skipped_no_input}"
    )


@app.command()
def sync(
    config_path: Path = Path("config/groups.yaml"),
    database_path: Path = Path("data/research.db"),
    message_limit: int = 100,
    mode: SyncMode = SyncMode.full,
) -> None:
    """Sync Telegram messages."""

    group_config = load_group_config(config_path)
    target_titles = {group.chat_title for group in group_config.groups if group.enabled}
    effective_now = datetime.now(UTC)
    windows_by_title = {
        window.chat_title: window
        for window in build_backfill_windows(
            group_config,
            now=effective_now,
        )
    }

    if mode == SyncMode.parse:
        repair_history_checkpoints(session_factory)
        inserted_candidates, inserted_trade_ideas = _run_parse_mode(database_path)
        typer.echo(f"Parse only mode: read raw messages from {database_path}")
        typer.echo(
            f"Persisted {inserted_candidates} signal candidate(s) to {database_path}"
        )
        typer.echo(f"Persisted {inserted_trade_ideas} trade idea(s) to {database_path}")
        return

    try:
        auth_config = load_telegram_auth_config()
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

    try:
        with acquire_telegram_session_lock(auth_config.session_path):
            client = create_telegram_client(auth_config)
            session_factory = create_session_factory(database_path)
            repair_history_checkpoints(session_factory)

            matched_dialogs: list[dict[str, str | int | bool | None]] = []
            inserted_messages = 0
            inserted_candidates = 0
            inserted_trade_ideas = 0
            unmatched_titles: set[str] = set()

            try:
                (
                    matched_dialogs,
                    inserted_messages,
                    inserted_candidates,
                    inserted_trade_ideas,
                ) = asyncio.run(
                    _run_telegram_sync(
                        client=client,
                        session_factory=session_factory,
                        target_titles=target_titles,
                        windows_by_title=windows_by_title,
                        message_limit=message_limit,
                        mode=mode,
                    )
                )
                matched_titles = {str(dialog.get("title")) for dialog in matched_dialogs}
                unmatched_titles = target_titles - matched_titles
                if mode == SyncMode.discover:
                    typer.echo(f"Discovered {len(matched_dialogs)} archived target group(s)")
                    typer.echo("Discovery only mode: no messages were fetched or persisted.")
                    for dialog in matched_dialogs:
                        typer.echo(f"- {dialog.get('title')}")
                    if unmatched_titles:
                        typer.echo("Configured groups not currently matched:")
                        for title in sorted(unmatched_titles):
                            typer.echo(f"- {title}")
                    return
            except Exception as exc:
                typer.echo(f"Telegram sync error: {exc}", err=False)
                raise typer.Exit(code=1) from exc
            finally:
                disconnect = getattr(client, "disconnect", None)
                if callable(disconnect):
                    try:
                        asyncio.run(maybe_await(disconnect()))
                    except RuntimeError:
                        pass

            typer.echo(f"Discovered {len(matched_dialogs)} archived target group(s)")
            typer.echo(f"Persisted {inserted_messages} raw message(s) to {database_path}")
            if mode != SyncMode.backfill:
                typer.echo(
                    f"Persisted {inserted_candidates} signal candidate(s) to {database_path}"
                )
                typer.echo(f"Persisted {inserted_trade_ideas} trade idea(s) to {database_path}")
            for dialog in matched_dialogs:
                typer.echo(f"- {dialog.get('title')}")
            if unmatched_titles:
                typer.echo("Configured groups not currently matched:")
                for title in sorted(unmatched_titles):
                    typer.echo(f"- {title}")
    except TelegramSessionLockError as exc:
        typer.echo(str(exc), err=False)
        raise typer.Exit(code=1) from exc


@app.command()
def report(
    output_path: Path = Path("reports/leaderboard.json"),
    database_path: Path = Path("data/research.db"),
    mode: str = "strict",
) -> None:
    """Generate leaderboard reports."""

    session_factory = create_session_factory(database_path)
    rows = load_leaderboard_rows(session_factory, mode=mode)
    written_path = write_report(
        output_path,
        {
            "mode": mode,
            "database_path": str(database_path),
            "rows": rows,
        },
    )
    typer.echo(f"Report written to {written_path}")


@app.command("recovery-dry-run")
def recovery_dry_run(
    config_path: Path = Path("config/groups.yaml"),
    database_path: Path = Path("data/research.db"),
    lookback_hours: int = 48,
    market_provider: str = "none",
    persist: bool = False,
) -> None:
    """Evaluate restart-recovery candidates without placing orders."""

    group_config = load_group_config(config_path)
    session_factory = create_session_factory(database_path)
    market_data = None
    try:
        market_data = _build_recovery_market_provider(market_provider)
        result = run_recovery_dry_run(
            session_factory,
            group_config=group_config,
            now=datetime.now(UTC),
            lookback_hours=lookback_hours,
            market_data=market_data,
            persist=persist,
        )
    except RecoveryDryRunProviderMissingError as exc:
        typer.echo(f"Recovery dry-run unavailable: {exc}", err=False)
        raise typer.Exit(code=1) from exc
    finally:
        close_provider = getattr(market_data, "close", None)
        if callable(close_provider):
            close_provider()

    typer.echo(f"Recovery dry-run candidates: {result.total_candidates}")
    if not result.action_counts:
        typer.echo("No recovery actions.")
        return
    for action, count in sorted(result.action_counts.items()):
        typer.echo(f"{action}: {count}")


def _build_recovery_market_provider(market_provider: str):
    normalized = market_provider.strip().lower()
    if normalized in {"", "none"}:
        return None
    if normalized == "gate":
        return GateMarketDataProvider()
    if normalized == "binance":
        return BinanceMarketDataProvider()
    raise typer.BadParameter("market-provider must be one of: none, gate, binance")


@app.command("export-dataset")
def export_dataset(
    output_path: Path = Path("exports/llm-dataset.jsonl"),
    database_path: Path = Path("data/research.db"),
    review_only: bool = False,
    confidence_threshold: float = 0.8,
    signal_like_only: bool = False,
) -> None:
    """Export message-centered JSONL rows for model adjudication."""

    session_factory = create_session_factory(database_path)
    written_path = export_dataset_jsonl(
        session_factory,
        output_path,
        review_only=review_only,
        confidence_threshold=confidence_threshold,
        signal_like_only=signal_like_only,
    )
    typer.echo(f"Dataset written to {written_path}")


@app.command("export-llm-pack")
def export_llm_pack(
    output_dir: Path = Path("exports/llm-adjudication"),
    database_path: Path = Path("data/research.db"),
    review_only: bool = True,
    confidence_threshold: float = 0.8,
    signal_like_only: bool = True,
) -> None:
    """Export dataset plus prompt/schema contract for model adjudication."""

    session_factory = create_session_factory(database_path)
    manifest = export_llm_adjudication_pack(
        session_factory,
        output_dir,
        review_only=review_only,
        confidence_threshold=confidence_threshold,
        signal_like_only=signal_like_only,
    )
    typer.echo(
        f"LLM pack written to {output_dir} ({manifest['record_count']} record(s))"
    )


@app.command("export-llm-submit-sample")
def export_llm_submit_sample(
    pack_dir: Path = Path("exports/llm-adjudication"),
    output_path: Path = Path("exports/llm-adjudication/submit-sample.md"),
    limit: int = 5,
) -> None:
    """Export a copy-ready small submission sample for model adjudication."""

    written_path = export_llm_submission_sample(pack_dir, output_path, limit=limit)
    typer.echo(f"LLM submission sample written to {written_path}")


@app.command("import-llm-results")
def import_llm_results(
    input_path: Path = typer.Option(...),
    database_path: Path = Path("data/research.db"),
    confirmation_threshold: float = 0.8,
    report_output_dir: Path = Path("reports"),
) -> None:
    """Import LLM adjudication JSON back into candidates and trade ideas."""

    session_factory = create_session_factory(database_path)
    stats = import_llm_adjudication_results(
        session_factory,
        input_path,
        confirmation_threshold=confirmation_threshold,
    )
    typer.echo(f"Processed {stats['processed_items']} LLM adjudication item(s)")
    typer.echo(f"Created {stats['created_candidates']} candidate(s)")
    typer.echo(f"Updated {stats['updated_candidates']} candidate(s)")
    typer.echo(f"Rejected {stats['rejected_candidates']} candidate(s)")
    typer.echo(f"Persisted {stats['inserted_trade_ideas']} trade idea(s)")
    typer.echo(f"Persisted {stats['inserted_trade_updates']} trade update(s)")

    strict_report_path = write_report(
        report_output_dir / "leaderboard-strict.json",
        {
            "mode": "strict",
            "database_path": str(database_path),
            "rows": load_leaderboard_rows(session_factory, mode="strict"),
        },
    )
    expanded_report_path = write_report(
        report_output_dir / "leaderboard-expanded.json",
        {
            "mode": "expanded",
            "database_path": str(database_path),
            "rows": load_leaderboard_rows(session_factory, mode="expanded"),
        },
    )
    typer.echo(f"Refreshed report {strict_report_path}")
    typer.echo(f"Refreshed report {expanded_report_path}")


@app.command()
def review(
    database_path: Path = Path("data/research.db"),
    candidate_file: Path | None = None,
    candidate_id: int | None = None,
    decision: str | None = None,
    note: str | None = None,
) -> None:
    """List pending candidates or apply a manual review decision."""

    if candidate_file is None:
        session_factory = create_session_factory(database_path)

        if candidate_id is None:
            pending = list_pending_candidates_from_db(session_factory)
            typer.echo(f"Pending candidates: {len(pending)}")
            for candidate in pending:
                typer.echo(str(candidate))
            return

        if decision is None:
            raise typer.BadParameter(
                "decision is required when candidate_id is provided"
            )

        try:
            updated = apply_review_decision_to_db(
                session_factory,
                candidate_id=candidate_id,
                decision=decision,
                note=note,
            )
        except LookupError as exc:
            raise typer.BadParameter(str(exc)) from exc

        typer.echo(f"Review decision written to database for candidate {updated['id']}")
        return

    candidates = load_candidates(candidate_file)

    if candidate_id is None:
        pending = list_pending_candidates(candidates)
        typer.echo(f"Pending candidates: {len(pending)}")
        for candidate in pending:
            typer.echo(str(candidate))
        return

    updated_candidates = []
    found = False
    for candidate in candidates:
        if candidate.get("id") == candidate_id:
            found = True
            if decision is None:
                raise typer.BadParameter(
                    "decision is required when candidate_id is provided"
                )
            updated_candidates.append(
                apply_review_decision(candidate, decision=decision, note=note)
            )
        else:
            updated_candidates.append(candidate)

    if not found:
        raise typer.BadParameter(f"candidate_id {candidate_id} not found")

    written_path = write_candidates(candidate_file, updated_candidates)
    typer.echo(f"Review decision written to {written_path}")


@app.command()
def web(
    host: str = "127.0.0.1",
    port: int = 8000,
    database_path: Path = Path("data/research.db"),
    config_path: Path = Path("config/groups.yaml"),
    deepcoin_contract_specs_path: Path = Path("config/deepcoin_contract_specs.yaml"),
) -> None:
    """Run the local web workbench."""

    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        typer.echo(
            "Web dependencies are not installed in the current environment. "
            "Install project dependencies first.",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    group_config = load_group_config(config_path)
    live_target_titles = {
        group.chat_title
        for group in group_config.groups
        if group.enabled
    }
    group_labels_by_title = {
        group.chat_title: (group.custom_group_label or group.chat_title)
        for group in group_config.groups
        if group.enabled
    }
    deepcoin_contract_spec_provider = load_deepcoin_contract_specs(
        deepcoin_contract_specs_path,
        required=False,
    )

    telegram_client = None
    live_listener_status_reason = None
    telegram_session_lock = None
    telegram_session_lock_entered = False
    try:
        auth_config = load_telegram_auth_config()
        reap_stopped_session_lock_owner(
            auth_config.session_path,
            current_command="telegram-kol-research web",
        )
        telegram_session_lock = acquire_telegram_session_lock(auth_config.session_path)
        telegram_session_lock.__enter__()
        telegram_session_lock_entered = True
        telegram_client = create_telegram_client(auth_config)
    except TelegramSessionLockError as exc:
        live_listener_status_reason = str(exc)
        typer.echo(
            f"Telegram live listener disabled: {exc}",
            err=False,
        )
    except (ValueError, RuntimeError) as exc:
        if telegram_session_lock_entered and telegram_session_lock is not None:
            telegram_session_lock.__exit__(None, None, None)
            telegram_session_lock_entered = False
        live_listener_status_reason = "缺少 Telegram API 凭据或 Telethon 运行依赖"
        typer.echo(
            f"Telegram live listener disabled: {exc}",
            err=False,
        )

    app_instance = create_web_app(
        database_path=database_path,
        live_target_titles=live_target_titles,
        telegram_client=telegram_client,
        live_listener_status_reason=live_listener_status_reason,
        group_labels_by_title=group_labels_by_title,
        group_config=group_config,
        group_config_path=config_path,
        deepcoin_contract_spec_provider=deepcoin_contract_spec_provider,
    )
    try:
        uvicorn.run(app_instance, host=host, port=port)
    finally:
        if telegram_session_lock_entered and telegram_session_lock is not None:
            telegram_session_lock.__exit__(None, None, None)


@app.command("session-status")
def session_status() -> None:
    """Show which process currently owns the Telegram session lock."""

    try:
        auth_config = load_telegram_auth_config()
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

    owner = describe_session_lock_owner(auth_config.session_path)
    if owner is None:
        typer.echo(f"Telegram session is free: {auth_config.session_path}")
        return
    typer.echo(f"Telegram session owner: {owner.format_for_humans()}")


@app.command("session-release")
def session_release(pid: int = typer.Option(..., "--pid")) -> None:
    """Release a Telegram session owner after explicitly confirming its PID."""

    try:
        auth_config = load_telegram_auth_config()
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

    owner = release_session_lock_owner(
        auth_config.session_path,
        expected_pid=pid,
        current_command="telegram-kol-research session-release",
    )
    if owner is None:
        typer.echo(
            "Telegram session owner was not released. "
            "Check `session-status`, then pass the exact same PID.",
            err=False,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Released Telegram session owner: {owner.format_for_humans()}")


@app.command()
def alerts(
    database_path: Path = Path("data/research.db"),
    config_path: Path = Path("config/groups.yaml"),
    media_root: Path = Path("data/media"),
) -> None:
    """Run realtime AI strategy alert forwarding without the web UI."""

    group_config = load_group_config(config_path)
    target_titles = {group.chat_title for group in group_config.groups if group.enabled}
    alert_config = load_strategy_alert_config()
    if not strategy_alerts_enabled(alert_config):
        typer.echo(
            "Strategy alerts are not configured. Set TELEGRAM_KOL_ALERT_BOT_TOKEN and TELEGRAM_KOL_ALERT_CHAT_ID.",
            err=False,
        )
        raise typer.Exit(code=1)

    try:
        auth_config = load_telegram_auth_config()
        reap_stopped_session_lock_owner(
            auth_config.session_path,
            current_command="telegram-kol-research alerts",
        )
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

    try:
        with acquire_telegram_session_lock(auth_config.session_path):
            client = create_telegram_client(auth_config)
            session_factory = create_session_factory(database_path)
            broker = LiveUpdateBroker()
            try:
                asyncio.run(
                    run_live_listener(
                        client=client,
                        session_factory=session_factory,
                        broker=broker,
                        target_titles=target_titles,
                        media_root=media_root,
                        strategy_alert_config=alert_config,
                        strategy_alert_enabled_for_title=lambda title: any(
                            group.enabled
                            and group.ai_strategy_enabled
                            and group.chat_title == title
                            for group in group_config.groups
                        ),
                    )
                )
            finally:
                broker.close()
                disconnect = getattr(client, "disconnect", None)
                if callable(disconnect):
                    try:
                        asyncio.run(maybe_await(disconnect()))
                    except RuntimeError:
                        pass
    except TelegramSessionLockError as exc:
        typer.echo(str(exc), err=False)
        raise typer.Exit(code=1) from exc


@app.command("media-cleanup")
def media_cleanup(
    database_path: Path = Path("data/research.db"),
    media_root: Path = Path("data/media"),
    retain_days: int = 14,
    max_media_dir_gb: float | None = 5.0,
    min_free_disk_gb: float | None = 10.0,
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Preview deletions by default; pass --apply to delete files.",
    ),
) -> None:
    """Clean old local media cache files without deleting message history."""

    session_factory = create_session_factory(database_path)
    result = cleanup_media_files(
        session_factory,
        media_root=media_root,
        retain_days=retain_days,
        max_media_dir_gb=max_media_dir_gb,
        min_free_disk_gb=min_free_disk_gb,
        dry_run=dry_run,
    )
    mode_label = "dry-run" if dry_run else "applied"
    typer.echo(f"Media cleanup {mode_label}")
    typer.echo(f"Scanned assets: {result.scanned_assets}")
    typer.echo(f"Eligible assets: {result.eligible_assets}")
    typer.echo(f"Protected assets: {result.protected_assets}")
    typer.echo(f"Missing files: {result.missing_files}")
    typer.echo(f"Deleted files: {result.deleted_files}")
    typer.echo(f"Cleared local paths: {result.cleared_local_paths}")
    typer.echo(f"Freed bytes: {result.freed_bytes}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
