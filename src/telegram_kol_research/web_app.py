"""FastAPI app for the Telegram web workbench."""

from __future__ import annotations

from datetime import UTC, datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import asyncio

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
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.llm_chat import (
    build_proxy_chat_payload,
    build_scope_context,
    build_source_reference_map,
    extract_recent_message_limit,
    load_llm_proxy_config,
    request_grounded_chat_answer,
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
            )
            app.state.reconcile_task = asyncio.create_task(
                app.state.reconcile_runner(
                    client=app.state.telegram_client,
                    session_factory=app.state.session_factory,
                    broker=app.state.live_update_broker,
                    target_titles=set(app.state.live_target_titles),
                    media_root=app.state.media_root,
                    interval_seconds=app.state.reconcile_interval_seconds,
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
    app.state.chat_requester = request_grounded_chat_answer
    app.state.live_target_titles = live_target_titles or set()
    app.state.live_listener_runner = live_listener_runner or run_live_listener
    app.state.live_listener_task = None
    app.state.telegram_client = telegram_client
    app.state.live_listener_status_reason = live_listener_status_reason
    app.state.group_labels_by_title = group_labels_by_title or {}
    app.state.now_provider = now_provider or (lambda: datetime.now(UTC))
    app.state.reconcile_runner = reconcile_runner or run_periodic_reconcile
    app.state.reconcile_interval_seconds = reconcile_interval_seconds
    app.state.reconcile_task = None
    app.state.telegram_auth_loader = load_telegram_auth_config
    app.state.telegram_client_factory = create_telegram_client
    app.state.reconcile_once_runner = run_reconcile_once

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
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "groups": groups,
                "messages": messages,
                "selected_chat_id": selected_chat_id,
                "selected_group": selected_group,
                "live_listener_enabled": app.state.telegram_client is not None,
                "live_listener_status_reason": app.state.live_listener_status_reason,
                "database_latest_message_at": freshness["latest_message_at"],
                "database_stale_hours": freshness["stale_hours"],
                "refresh_mode_label": (
                    "实时监听 + SSE"
                    if app.state.telegram_client is not None
                    else "仅本地快照"
                ),
            },
        )

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
    def refresh():
        try:
            auth_config = app.state.telegram_auth_loader()
            telegram_client = app.state.telegram_client_factory(auth_config)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        async def run_refresh():
            await maybe_await(getattr(telegram_client, "connect", lambda: None)())
            try:
                return await app.state.reconcile_once_runner(
                    client=telegram_client,
                    session_factory=app.state.session_factory,
                    broker=app.state.live_update_broker,
                    target_titles=set(app.state.live_target_titles),
                    media_root=app.state.media_root,
                )
            finally:
                disconnect = getattr(telegram_client, "disconnect", None)
                if callable(disconnect):
                    await maybe_await(disconnect())

        return asyncio.run(run_refresh())

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


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


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
