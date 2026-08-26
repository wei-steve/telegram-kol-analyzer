"""Live update broker helpers for the Telegram web workbench."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LiveUpdateBroker:
    """Minimal in-process broker used by tests and SSE scaffolding."""

    published_events: list[dict[str, Any]] = field(default_factory=list)
    _subscribers: list[asyncio.Queue[str | None]] = field(default_factory=list)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)

    def publish_message(
        self, *, chat_id: int, message_id: int, event_type: str = "message"
    ) -> dict[str, Any]:
        event = {"event": event_type, "chat_id": chat_id, "message_id": message_id}
        self.published_events.append(event)
        payload = self._format_sse_payload(event_type=event_type, payload=event)
        # Capture the running event loop on first publish so we have a
        # reference for thread-safe queue insertion later.
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for queue in list(self._subscribers):
            if self._loop and self._loop is not current_loop:
                # Called from a background thread — schedule on the event loop
                self._loop.call_soon_threadsafe(queue.put_nowait, payload)
            else:
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
        return event

    def format_message_event(
        self, *, chat_id: int, message_id: int, event_type: str = "message"
    ) -> str:
        event = self.publish_message(
            chat_id=chat_id, message_id=message_id, event_type=event_type
        )
        return self._format_sse_payload(event_type=event_type, payload=event)

    def publish_event(
        self, *, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Publish a generic event (e.g. lifecycle status change) to all
        SSE subscribers.  Unlike ``publish_message`` this does not require
        a chat_id / message_id pair."""
        event: dict[str, Any] = {"event": event_type, **payload}
        self.published_events.append(event)
        formatted = self._format_sse_payload(
            event_type=event_type, payload=event
        )
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for queue in list(self._subscribers):
            if self._loop and self._loop is not current_loop:
                self._loop.call_soon_threadsafe(queue.put_nowait, formatted)
            else:
                try:
                    queue.put_nowait(formatted)
                except asyncio.QueueFull:
                    pass
        return event

    async def stream(self):
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            yield ": keep-alive\n\n"
            while True:
                payload = await queue.get()
                if payload is None:
                    break
                yield payload
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def close(self) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    def _format_sse_payload(self, *, event_type: str, payload: dict[str, Any]) -> str:
        return (
            f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        )
