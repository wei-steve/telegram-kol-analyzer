"""FastAPI app for the Telegram web workbench."""

from __future__ import annotations

from datetime import UTC, datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import asyncio
import re

import httpx

try:
    from fastapi import FastAPI, Request
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
except (
    ModuleNotFoundError
) as exc:  # pragma: no cover - import guard for missing optional deps
    raise RuntimeError(
        "FastAPI is not installed in the current environment. Install project dependencies first."
    ) from exc

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.ai_recognition_config import (
    AiRecognitionConfig,
    load_ai_recognition_config,
    save_ai_recognition_config,
)
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.gate_market_data import GateMarketDataProvider
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import update_group_automation_settings
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.message_recognition import recognize_message_now
from telegram_kol_research.llm_chat import (
    build_proxy_chat_payload,
    build_scope_context,
    build_source_reference_map,
    extract_recent_message_limit,
    load_llm_proxy_config,
    request_grounded_chat_answer,
)
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import list_recovery_decisions
from telegram_kol_research.recovery_execution_queue import list_recovery_execution_previews
from telegram_kol_research.recovery_live_submit_gate import validate_recovery_live_submit_gate
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_runner import run_recovery_dry_run
from telegram_kol_research.strategy_alerts import (
    load_strategy_alert_config,
    strategy_alerts_enabled,
)
from telegram_kol_research.web_queries import (
    load_database_freshness,
    load_group_messages,
    load_group_rows,
    load_messages_in_time_window,
    load_selected_messages,
)
from telegram_kol_research.telegram_live_listener import launch_live_listener_task, run_live_listener
from telegram_kol_research.telegram_live_listener import run_periodic_reconcile, run_reconcile_once
from telegram_kol_research.telegram_client import create_telegram_client, load_telegram_auth_config, maybe_await
from telegram_kol_research.telegram_session_lock import (
    TelegramSessionLockError,
    acquire_telegram_session_lock,
)


REFRESH_TIMEOUT_SECONDS = 180
SESSION_LOCK_OWNER_PID_PATTERN = re.compile(r"owner pid=(\d+)")


def _build_trader_dashboard_state(
    *,
    groups: list[dict[str, Any]],
    group_config: GroupConfig,
    recovery_decisions: list[dict[str, Any]],
    recovery_execution_queue: list[dict[str, Any]],
    live_listener_enabled: bool,
    refresh_mode_label: str,
) -> dict[str, Any]:
    pending_review_count = sum(
        1
        for decision in recovery_decisions
        if decision.get("review_status") == "pending"
    )
    ready_to_simulate_count = sum(
        1
        for item in recovery_execution_queue
        if not item.get("deepcoin_order_draft", {}).get("blocking_reason_codes")
    )
    blocked_count = sum(
        1
        for item in recovery_execution_queue
        if item.get("deepcoin_order_draft", {}).get("blocking_reason_codes")
    )
    blocked_count += sum(
        1
        for decision in recovery_decisions
        if decision.get("action") == "manual_review"
    )

    work_counts_by_chat_id: dict[int, int] = {}
    for decision in recovery_decisions:
        chat_id = decision.get("chat_id")
        if chat_id is not None:
            work_counts_by_chat_id[int(chat_id)] = (
                work_counts_by_chat_id.get(int(chat_id), 0) + 1
            )
    for item in recovery_execution_queue:
        chat_id = item.get("chat_id")
        if chat_id is not None:
            work_counts_by_chat_id[int(chat_id)] = (
                work_counts_by_chat_id.get(int(chat_id), 0) + 1
            )

    config_by_chat_id = {
        int(item.chat_id): item for item in group_config.groups if item.chat_id is not None
    }
    config_by_title = {item.chat_title: item for item in group_config.groups}
    group_rows = []
    for group in groups:
        config_item = config_by_chat_id.get(int(group["chat_id"])) or config_by_title.get(
            str(group.get("raw_title") or group.get("title") or "")
        )
        group_rows.append(
            {
                **group,
                "strategy_work_count": work_counts_by_chat_id.get(int(group["chat_id"]), 0),
                "ai_strategy_enabled": (
                    bool(config_item.ai_strategy_enabled) if config_item is not None else False
                ),
                "auto_trade_enabled": (
                    config_item.trading_mode == "auto_trade"
                    if config_item is not None
                    else False
                ),
            }
        )

    return {
        "strategy_count": len(recovery_decisions),
        "pending_review_count": pending_review_count,
        "ready_to_simulate_count": ready_to_simulate_count,
        "blocked_count": blocked_count,
        "group_rows": group_rows,
        "live_listener_enabled": live_listener_enabled,
        "refresh_mode_label": refresh_mode_label,
    }


def _extract_session_lock_owner_pid(reason: str | None) -> int | None:
    if not reason or "already in use" not in reason:
        return None
    match = SESSION_LOCK_OWNER_PID_PATTERN.search(reason)
    if match is None:
        return None
    return int(match.group(1))


def _group_ai_strategy_enabled(group_config: GroupConfig, chat_title: str) -> bool:
    return any(
        group.enabled and group.ai_strategy_enabled and group.chat_title == chat_title
        for group in group_config.groups
    )


def create_web_app(
    database_path: str | Path,
    media_root: str | Path | None = None,
    live_target_titles: set[str] | None = None,
    live_listener_runner=None,
    telegram_client: Any | None = None,
    live_listener_status_reason: str | None = None,
    group_labels_by_title: dict[str, str] | None = None,
    now_provider=None,
    reconcile_runner=None,
    reconcile_interval_seconds: int = 300,
    reconcile_startup_delay_seconds: int | None = None,
    group_config: GroupConfig | None = None,
    group_config_path: str | Path | None = None,
    recovery_runner=None,
    recovery_market_data_factory=None,
    deepcoin_contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    message_recognizer=None,
    ai_recognition_config_path: str | Path | None = None,
) -> FastAPI:
    """Create the minimal FastAPI app used by the web command."""

    resolved_database_path = Path(database_path)
    resolved_media_root = Path(media_root) if media_root is not None else resolved_database_path.parent / "media"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if (
            app.state.live_target_titles
            and app.state.telegram_client is not None
            and app.state.live_listener_task is None
        ):
            app.state.live_listener_task = launch_live_listener_task(
                runner=app.state.live_listener_runner,
                client=app.state.telegram_client,
                session_factory=app.state.session_factory,
                broker=app.state.live_update_broker,
                target_titles=set(app.state.live_target_titles),
                media_root=app.state.media_root,
                strategy_alert_config=app.state.strategy_alert_config,
                strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
            )
            app.state.reconcile_task = asyncio.create_task(
                _run_reconcile_after_startup_delay(
                    runner=app.state.reconcile_runner,
                    client=app.state.telegram_client,
                    session_factory=app.state.session_factory,
                    broker=app.state.live_update_broker,
                    target_titles=set(app.state.live_target_titles),
                    media_root=app.state.media_root,
                    interval_seconds=app.state.reconcile_interval_seconds,
                    operation_lock=app.state.telegram_operation_lock,
                    strategy_alert_config=app.state.strategy_alert_config,
                    strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                    startup_delay_seconds=app.state.reconcile_startup_delay_seconds,
                )
            )
        try:
            yield
        finally:
            task = app.state.live_listener_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                app.state.live_listener_task = None
            reconcile_task = app.state.reconcile_task
            if reconcile_task is not None:
                reconcile_task.cancel()
                try:
                    await reconcile_task
                except asyncio.CancelledError:
                    pass
                app.state.reconcile_task = None
            app.state.live_update_broker.close()

    app = FastAPI(title="Telegram KOL Research Web", lifespan=lifespan)
    app.state.database_path = Path(database_path)
    app.state.session_factory = create_session_factory(database_path)
    app.state.media_root = resolved_media_root.resolve()
    app.state.live_update_broker = LiveUpdateBroker()
    app.state.llm_proxy_config = load_llm_proxy_config()
    loaded_strategy_alert_config = load_strategy_alert_config()
    app.state.strategy_alert_config = (
        loaded_strategy_alert_config
        if strategy_alerts_enabled(loaded_strategy_alert_config)
        else None
    )
    app.state.chat_requester = request_grounded_chat_answer
    app.state.live_target_titles = live_target_titles or set()
    app.state.live_listener_runner = live_listener_runner or run_live_listener
    app.state.live_listener_task = None
    app.state.telegram_client = telegram_client
    app.state.live_listener_status_reason = live_listener_status_reason
    app.state.group_labels_by_title = group_labels_by_title or {}
    app.state.group_config = group_config or GroupConfig()
    app.state.group_config_path = Path(group_config_path) if group_config_path else None
    app.state.strategy_alert_enabled_for_title = lambda title: _group_ai_strategy_enabled(
        app.state.group_config,
        title,
    )
    app.state.now_provider = now_provider or (lambda: datetime.now(UTC))
    app.state.reconcile_runner = reconcile_runner or run_periodic_reconcile
    app.state.recovery_runner = recovery_runner or run_recovery_dry_run
    app.state.recovery_market_data_factory = (
        recovery_market_data_factory or GateMarketDataProvider
    )
    app.state.deepcoin_contract_spec_provider = deepcoin_contract_spec_provider
    app.state.message_recognizer = message_recognizer or recognize_message_now
    app.state.ai_recognition_config_path = (
        Path(ai_recognition_config_path)
        if ai_recognition_config_path is not None
        else Path("config/ai_recognition.yaml")
    )
    app.state.reconcile_interval_seconds = reconcile_interval_seconds
    app.state.reconcile_startup_delay_seconds = (
        15
        if reconcile_startup_delay_seconds is None
        else reconcile_startup_delay_seconds
    )
    app.state.reconcile_task = None
    app.state.telegram_auth_loader = load_telegram_auth_config
    app.state.telegram_client_factory = create_telegram_client
    app.state.reconcile_once_runner = run_reconcile_once
    app.state.telegram_session_lock_factory = acquire_telegram_session_lock
    app.state.telegram_operation_lock = asyncio.Lock()
    app.state.asset_version = _static_asset_version()

    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    @app.get("/")
    def index(request: Request):
        groups = load_group_rows(
            app.state.session_factory,
            group_labels_by_title=app.state.group_labels_by_title,
        )
        selected_chat_id = groups[0]["chat_id"] if groups else None
        selected_group = next(
            (group for group in groups if group["chat_id"] == selected_chat_id),
            None,
        )
        messages = (
            load_group_messages(
                app.state.session_factory, chat_id=int(selected_chat_id), limit=50
            )
            if selected_chat_id is not None
            else []
        )
        freshness = load_database_freshness(
            app.state.session_factory,
            now=app.state.now_provider(),
        )
        recovery_decisions = list_recovery_decisions(
            app.state.session_factory,
            limit=20,
        )
        recovery_execution_queue = list_recovery_execution_previews(
            app.state.session_factory,
            limit=20,
            contract_spec_provider=app.state.deepcoin_contract_spec_provider,
        )
        live_listener_enabled = app.state.telegram_client is not None
        refresh_mode_label = (
            "实时监听 + SSE"
            if live_listener_enabled
            else "仅本地快照"
        )
        trader_dashboard = _build_trader_dashboard_state(
            groups=groups,
            group_config=app.state.group_config,
            recovery_decisions=recovery_decisions,
            recovery_execution_queue=recovery_execution_queue,
            live_listener_enabled=live_listener_enabled,
            refresh_mode_label=refresh_mode_label,
        )
        ai_recognition_config = load_ai_recognition_config(
            app.state.ai_recognition_config_path
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "groups": groups,
                "messages": messages,
                "selected_chat_id": selected_chat_id,
                "selected_group": selected_group,
                "live_listener_enabled": live_listener_enabled,
                "live_listener_status_reason": app.state.live_listener_status_reason,
                "session_lock_owner_pid": _extract_session_lock_owner_pid(
                    app.state.live_listener_status_reason
                ),
                "database_latest_message_at": freshness["latest_message_at"],
                "database_stale_hours": freshness["stale_hours"],
                "asset_version": app.state.asset_version,
                "recovery_decisions": recovery_decisions,
                "recovery_execution_queue": recovery_execution_queue,
                "refresh_mode_label": refresh_mode_label,
                "trader_dashboard": trader_dashboard,
                "ai_recognition_config": ai_recognition_config,
            },
        )

    @app.get("/groups")
    def groups_partial(request: Request, selected_chat_id: int | None = None):
        groups = load_group_rows(
            app.state.session_factory,
            group_labels_by_title=app.state.group_labels_by_title,
        )
        trader_dashboard = _build_trader_dashboard_state(
            groups=groups,
            group_config=app.state.group_config,
            recovery_decisions=[],
            recovery_execution_queue=[],
            live_listener_enabled=app.state.telegram_client is not None,
            refresh_mode_label=(
                "实时监听 + SSE"
                if app.state.telegram_client is not None
                else "仅本地快照"
            ),
        )
        return templates.TemplateResponse(
            request,
            "_groups.html",
            {
                "groups": trader_dashboard["group_rows"],
                "selected_chat_id": selected_chat_id,
            },
        )

    @app.post("/api/groups/{chat_id}/automation")
    def update_group_automation(chat_id: int, payload: dict[str, Any]):
        config_path = app.state.group_config_path
        if config_path is None:
            raise HTTPException(
                status_code=503,
                detail="group config path is not configured",
            )
        app.state.group_config = update_group_automation_settings(
            config_path,
            chat_id=chat_id,
            chat_title=str(payload.get("chat_title") or chat_id),
            ai_strategy_enabled=payload.get("ai_strategy_enabled"),
            auto_trade_enabled=payload.get("auto_trade_enabled"),
        )
        group = next(
            item for item in app.state.group_config.groups if item.chat_id == chat_id
        )
        return {
            "chat_id": chat_id,
            "chat_title": group.chat_title,
            "ai_strategy_enabled": group.ai_strategy_enabled,
            "auto_trade_enabled": group.trading_mode == "auto_trade",
        }

    @app.post("/api/messages/{raw_message_id}/recognize")
    def recognize_message(raw_message_id: int):
        try:
            result = app.state.message_recognizer(
                app.state.session_factory,
                raw_message_id=raw_message_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "raw_message_id": result.raw_message_id,
            "status": result.status,
            "summary": result.summary,
            "reason": result.reason,
        }

    @app.post("/api/ai-recognition-config")
    def update_ai_recognition_config(payload: dict[str, Any]):
        recognition_prompt = str(payload.get("recognition_prompt") or "").strip()
        if not recognition_prompt:
            raise HTTPException(status_code=422, detail="recognition_prompt is required")
        config = save_ai_recognition_config(
            app.state.ai_recognition_config_path,
            AiRecognitionConfig(
                recognition_prompt=recognition_prompt,
                mode="local_rule_parser",
            ),
        )
        return {
            "mode": config.mode,
            "recognition_prompt": config.recognition_prompt,
        }

    @app.get("/groups/{chat_id}/messages")
    def group_messages(
        request: Request,
        chat_id: int,
        before_message_id: int | None = None,
        search_text: str | None = None,
        sender_name: str | None = None,
    ):
        messages = load_group_messages(
            app.state.session_factory,
            chat_id=chat_id,
            limit=50,
            before_message_id=before_message_id,
            search_text=search_text,
            sender_name=sender_name,
        )
        return templates.TemplateResponse(
            request,
            "_messages.html",
            {
                "messages": messages,
                "selected_chat_id": chat_id,
                "selected_group": next(
                    (
                        group
                        for group in load_group_rows(
                            app.state.session_factory,
                            group_labels_by_title=app.state.group_labels_by_title,
                        )
                        if group["chat_id"] == chat_id
                    ),
                    None,
                ),
                "search_text": search_text or "",
                "sender_name": sender_name or "",
                "before_message_id": before_message_id,
                "live_listener_enabled": app.state.telegram_client is not None,
                "live_listener_status_reason": app.state.live_listener_status_reason,
                "database_latest_message_at": load_database_freshness(
                    app.state.session_factory,
                    now=app.state.now_provider(),
                )["latest_message_at"],
                "database_stale_hours": load_database_freshness(
                    app.state.session_factory,
                    now=app.state.now_provider(),
                )["stale_hours"],
                "refresh_mode_label": (
                    "实时监听 + SSE"
                    if app.state.telegram_client is not None
                    else "仅本地快照"
                ),
            },
        )

    @app.get("/local-media/{requested_path:path}")
    def local_media(requested_path: str):
        candidate = (app.state.media_root / requested_path).resolve()
        try:
            candidate.relative_to(app.state.media_root)
        except ValueError as exc:
            raise RuntimeError("Invalid media path") from exc
        return FileResponse(candidate)

    @app.post("/api/chat")
    def chat(payload: dict[str, Any]):
        question = str(payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=422, detail="question is required")

        chat_id_value = payload.get("chat_id")
        if chat_id_value is None:
            raise HTTPException(status_code=422, detail="chat_id is required")

        chat_id = int(chat_id_value)
        group_prompt = str(payload.get("group_prompt") or "").strip() or None
        message_limit = extract_recent_message_limit(question) or 50
        messages = load_group_messages(
            app.state.session_factory,
            chat_id=chat_id,
            limit=message_limit,
        )
        scope_context = build_scope_context(list(reversed(messages)))
        config = app.state.llm_proxy_config
        try:
            answer = app.state.chat_requester(
                config=config,
                question=question,
                scope_context=scope_context,
                group_prompt=group_prompt,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=_build_chat_proxy_error_detail(exc),
            ) from exc
        return {
            "answer": answer,
            "scope_mode": "current_group",
            "scope_message_count": len(messages),
            "proxy_payload": build_proxy_chat_payload(
                question=question,
                scope_context=scope_context,
                model=config.model,
                group_prompt=group_prompt,
            ),
            "sources": build_source_reference_map(messages),
        }

    @app.post("/api/refresh")
    async def refresh():
        shared_client = app.state.telegram_client is not None
        session_lock = None
        session_lock_entered = False
        try:
            if shared_client:
                telegram_client = app.state.telegram_client
            else:
                auth_config = app.state.telegram_auth_loader()
                session_lock = app.state.telegram_session_lock_factory(auth_config.session_path)
                session_lock.__enter__()
                session_lock_entered = True
                telegram_client = app.state.telegram_client_factory(auth_config)
        except TelegramSessionLockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            if session_lock_entered and session_lock is not None:
                session_lock.__exit__(None, None, None)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        async def run_refresh():
            async with app.state.telegram_operation_lock:
                await maybe_await(getattr(telegram_client, "connect", lambda: None)())
                try:
                    return await asyncio.wait_for(
                        app.state.reconcile_once_runner(
                            client=telegram_client,
                            session_factory=app.state.session_factory,
                            broker=app.state.live_update_broker,
                            target_titles=set(app.state.live_target_titles),
                            media_root=app.state.media_root,
                            strategy_alert_config=app.state.strategy_alert_config,
                            strategy_alert_enabled_for_title=app.state.strategy_alert_enabled_for_title,
                        ),
                        timeout=REFRESH_TIMEOUT_SECONDS,
                    )
                finally:
                    disconnect = getattr(telegram_client, "disconnect", None)
                    if callable(disconnect) and not shared_client:
                        await maybe_await(disconnect())

        try:
            return await run_refresh()
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=(
                    "Telegram refresh timed out after "
                    f"{REFRESH_TIMEOUT_SECONDS} seconds. Please try again."
                ),
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=_build_refresh_error_detail(exc)) from exc
        finally:
            if session_lock_entered and session_lock is not None:
                session_lock.__exit__(None, None, None)

    @app.post("/api/recovery-dry-run")
    def recovery_dry_run():
        market_data = app.state.recovery_market_data_factory()
        try:
            result = app.state.recovery_runner(
                session_factory=app.state.session_factory,
                group_config=app.state.group_config,
                now=app.state.now_provider(),
                market_data=market_data,
                account_state=None,
                lookback_hours=48,
                persist=True,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            close = getattr(market_data, "close", None)
            if callable(close):
                close()
        return {
            "total_candidates": result.total_candidates,
            "persisted_decisions": len(result.evaluations),
            "action_counts": result.action_counts,
        }

    @app.post("/api/recovery-decisions/review")
    def review_recovery_decision(payload: dict[str, Any]):
        required_fields = ["chat_id", "message_id", "symbol", "side", "review_status"]
        missing_fields = [
            field_name
            for field_name in required_fields
            if payload.get(field_name) in (None, "")
        ]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"missing required fields: {', '.join(missing_fields)}",
            )
        try:
            return apply_recovery_review_decision(
                app.state.session_factory,
                chat_id=int(payload["chat_id"]),
                message_id=int(payload["message_id"]),
                symbol=str(payload["symbol"]),
                side=str(payload["side"]),
                review_status=str(payload["review_status"]),
                note=str(payload.get("note") or "").strip() or None,
                reviewed_at=app.state.now_provider(),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/recovery-execution-queue")
    def recovery_execution_queue():
        return {
            "items": list_recovery_execution_previews(
                app.state.session_factory,
                limit=100,
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
            )
        }

    @app.post("/api/recovery-order-confirm-dry-run")
    def recovery_order_confirm_dry_run(payload: dict[str, Any]):
        required_fields = ["chat_id", "message_id", "symbol", "side"]
        missing_fields = [
            field_name
            for field_name in required_fields
            if payload.get(field_name) in (None, "")
        ]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"missing required fields: {', '.join(missing_fields)}",
            )
        try:
            return confirm_recovery_order_dry_run(
                app.state.session_factory,
                chat_id=int(payload["chat_id"]),
                message_id=int(payload["message_id"]),
                symbol=str(payload["symbol"]),
                side=str(payload["side"]),
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
                persist_ready_confirmation=True,
                confirmed_at=app.state.now_provider(),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/recovery-live-submit-gate")
    def recovery_live_submit_gate(payload: dict[str, Any]):
        required_fields = ["chat_id", "message_id", "symbol", "side"]
        missing_fields = [
            field_name
            for field_name in required_fields
            if payload.get(field_name) in (None, "")
        ]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"missing required fields: {', '.join(missing_fields)}",
            )
        try:
            return validate_recovery_live_submit_gate(
                app.state.session_factory,
                chat_id=int(payload["chat_id"]),
                message_id=int(payload["message_id"]),
                symbol=str(payload["symbol"]),
                side=str(payload["side"]),
                contract_spec_provider=app.state.deepcoin_contract_spec_provider,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/events")
    def events():
        broker = app.state.live_update_broker
        return StreamingResponse(
            broker.stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    return app


async def _run_reconcile_after_startup_delay(
    *,
    runner,
    startup_delay_seconds: int,
    **kwargs,
):
    if startup_delay_seconds > 0:
        await asyncio.sleep(startup_delay_seconds)
    await runner(**kwargs)


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _static_asset_version() -> int:
    static_dir = Path(__file__).parent / "static"
    return int(
        max(
            (static_dir / "app.css").stat().st_mtime,
            (static_dir / "app.js").stat().st_mtime,
        )
    )


def _build_chat_proxy_error_detail(exc: httpx.HTTPError) -> str:
    message = _extract_proxy_error_message(exc)
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return "AI 代理鉴权失败。请检查 TELEGRAM_KOL_LLM_API_KEY 是否已设置且有效。"
    lowered = message.lower()
    if "does not support image input" in lowered or (
        "image" in lowered and "not support" in lowered
    ):
        return "当前模型不支持直接图片理解，本次分析会优先基于文字消息与 OCR 内容。"
    return "AI proxy request failed. Check CLIProxyAPI connectivity and credentials."


def _build_refresh_error_detail(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "authorization key" in lowered and "used under two different" in lowered:
        return (
            "Telegram 登录会话已失效：同一个 session 曾被多个客户端同时使用。"
            "请重新登录生成新的 Telegram session 后再刷新。"
        )
    return message


def _extract_proxy_error_message(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        try:
            payload = response.json()
        except ValueError:
            return response.text
        error_payload = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if isinstance(message, str):
                return message
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, str):
            return detail
    return str(exc)
