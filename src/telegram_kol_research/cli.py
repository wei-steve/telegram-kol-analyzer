"""CLI entrypoints for the Telegram KOL research app."""

import asyncio
import json
from enum import Enum
from datetime import UTC, datetime
from pathlib import Path

import typer

from telegram_kol_research.backfill import build_backfill_windows
from telegram_kol_research.candidates import persist_text_signal_candidates
from telegram_kol_research.dataset_export import export_dataset_jsonl
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.llm_adjudication import (
    export_llm_adjudication_pack,
    export_llm_submission_sample,
)
from telegram_kol_research.llm_import import import_llm_adjudication_results
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
from telegram_kol_research.telegram_client import (
    create_telegram_client,
    discover_dialogs,
    ensure_telegram_login,
    fetch_dialog_messages,
    filter_target_dialogs,
    load_telegram_auth_config,
    maybe_await,
)
from telegram_kol_research.trade_merge import persist_trade_ideas_from_candidates
from telegram_kol_research.web_app import create_web_app

app = typer.Typer(help="Telegram KOL win-rate research CLI.")


class SyncMode(str, Enum):
    discover = "discover"
    backfill = "backfill"
    parse = "parse"
    full = "full"


def _record_within_window(record: NormalizedMessageRecord, *, start_at, end_at) -> bool:
    posted_at = record.posted_at
    if posted_at is None:
        return True
    return start_at <= posted_at <= end_at


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
    candidate_stats = persist_text_signal_candidates(
        session_factory, normalized_records
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
        candidate_stats = persist_text_signal_candidates(
            session_factory, normalized_records
        )
        inserted_candidates += candidate_stats["inserted_candidates"]
        trade_stats = persist_trade_ideas_from_candidates(session_factory)
        inserted_trade_ideas += trade_stats["inserted_trade_ideas"]

    return matched_dialogs, inserted_messages, inserted_candidates, inserted_trade_ideas


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
        client = create_telegram_client(auth_config)
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"Telegram auth/config error: {exc}", err=False)
        raise typer.Exit(code=1) from exc

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
        group.chat_title for group in group_config.groups if group.enabled
    }
    group_labels_by_title = {
        group.chat_title: (group.custom_group_label or group.chat_title)
        for group in group_config.groups
        if group.enabled
    }

    telegram_client = None
    live_listener_status_reason = None
    try:
        auth_config = load_telegram_auth_config()
        telegram_client = create_telegram_client(auth_config)
    except (ValueError, RuntimeError) as exc:
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
    )
    uvicorn.run(app_instance, host=host, port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
