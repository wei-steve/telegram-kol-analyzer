"""Core SQLAlchemy models for the local research database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarative model for the research app."""


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_sender_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255))
    custom_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class RawMessage(Base):
    __tablename__ = "raw_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    message_id: Mapped[int] = mapped_column(Integer, index=True)
    sender_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    sender_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    archived_target_group: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    edit_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), index=True)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(100))
    mime_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    local_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    ocr_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class SignalCandidate(Base):
    __tablename__ = "signal_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), index=True)
    source_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    side: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), default="entry_signal", nullable=False)
    entry_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    stop_loss_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    take_profit_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    leverage_text: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parse_source: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class TradeIdea(Base):
    __tablename__ = "trade_ideas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    primary_signal_candidate_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signal_candidates.id"),
        nullable=True,
        index=True,
    )
    chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    side: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    pnl_r_multiple: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class TradeUpdate(Base):
    __tablename__ = "trade_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_idea_id: Mapped[int] = mapped_column(ForeignKey("trade_ideas.id"), index=True)
    raw_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("raw_messages.id"), nullable=True, index=True)
    update_type: Mapped[str] = mapped_column(String(64))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    sync_kind: Mapped[str] = mapped_column(String(32), default="history", nullable=False)
    last_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
