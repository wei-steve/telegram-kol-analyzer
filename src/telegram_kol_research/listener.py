"""Helpers for routing incremental Telegram events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class RoutedEvent:
    chat_id: int | None
    message_id: int | None
    event_type: str
    should_process: bool


def should_process_event(event: dict[str, Any], tracked_chat_ids: set[int]) -> bool:
    """Return whether an incoming event belongs to a tracked chat."""

    chat_id = event.get("chat_id")
    return chat_id in tracked_chat_ids


def classify_event_type(event: dict[str, Any]) -> str:
    """Classify the event as a new message, edit, or unknown."""

    if event.get("edited") is True or event.get("event_type") == "edit":
        return "edit"
    if event.get("message_id") is not None:
        return "new_message"
    return "unknown"


def route_event(event: dict[str, Any], tracked_chat_ids: set[int]) -> RoutedEvent:
    """Produce a small routing decision object for incremental listeners."""

    return RoutedEvent(
        chat_id=event.get("chat_id"),
        message_id=event.get("message_id"),
        event_type=classify_event_type(event),
        should_process=should_process_event(event, tracked_chat_ids),
    )
