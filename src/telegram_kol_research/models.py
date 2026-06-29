"""Core SQLAlchemy models for the local research database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    __table_args__ = (
        Index("ix_raw_messages_chat_posted_message", "chat_id", "posted_at", "message_id"),
    )

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


class MessageRecognition(Base):
    __tablename__ = "message_recognitions"
    __table_args__ = (
        UniqueConstraint("raw_message_id", name="uq_message_recognitions_raw_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    engine: Mapped[str] = mapped_column(String(64), default="local_rule_parser", nullable=False)
    prompt_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class RecognitionExperiment(Base):
    __tablename__ = "recognition_experiments"
    __table_args__ = (
        UniqueConstraint(
            "raw_message_id",
            "experiment_name",
            name="uq_recognition_experiments_message_experiment",
        ),
        Index("ix_recognition_experiments_name_created", "experiment_name", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), index=True)
    experiment_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    observed_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    strategy_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    raw_response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


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


class StrategyAlert(Base):
    __tablename__ = "strategy_alerts"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_strategy_alerts_chat_message"),
        Index("ix_strategy_alerts_chat_id_message_id", "chat_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("raw_messages.id"), nullable=True, index=True)
    chat_title: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    original_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending", index=True)
    is_strategy: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    strategy_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    kol_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reason_short: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    forwarded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class TradingSetting(Base):
    __tablename__ = "trading_settings"
    __table_args__ = (
        UniqueConstraint("key", name="uq_trading_settings_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class ExecutionBinding(Base):
    __tablename__ = "execution_bindings"
    __table_args__ = (
        UniqueConstraint(
            "venue",
            "chat_id",
            "message_id",
            "symbol",
            "side",
            name="uq_execution_bindings_signal",
        ),
        Index("ix_execution_bindings_venue_status", "venue", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kol_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False, default="deepcoin")
    order_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    pos_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class RecoveryDecisionRecord(Base):
    __tablename__ = "recovery_decisions"
    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "message_id",
            "symbol",
            "side",
            name="uq_recovery_decisions_signal",
        ),
        Index("ix_recovery_decisions_action_run_at", "action", "run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kol_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    entry_range_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    stop_loss_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    max_loss_usdt: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class RecoveryOrderConfirmation(Base):
    __tablename__ = "recovery_order_confirmations"
    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "message_id",
            "symbol",
            "side",
            name="uq_recovery_order_confirmations_signal",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kol_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False, default="deepcoin")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready_confirmed", index=True)
    confirmation_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class StrategyLifecycle(Base):
    """Track the full lifecycle of a KOL strategy signal.

    A signal starts as *pending_entry*, transitions to *entered* when price
    touches the entry range, then to *exited* when stop-loss / take-profit
    is hit or the KOL publishes a closing signal.
    """

    __tablename__ = "strategy_lifecycles"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "message_id", name="uq_strategy_lifecycles_chat_message"
        ),
        Index("ix_strategy_lifecycles_status", "lifecycle_status"),
        Index("ix_strategy_lifecycles_symbol", "symbol"),
        Index("ix_strategy_lifecycles_chat_status_signal", "chat_id", "lifecycle_status", "signal_at"),
        Index("ix_strategy_lifecycles_chat_status_entered", "chat_id", "lifecycle_status", "entered_at"),
        Index("ix_strategy_lifecycles_chat_status_exited", "chat_id", "lifecycle_status", "exited_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_candidate_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signal_candidates.id"), nullable=True, index=True
    )
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)

    # Core lifecycle state
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending_entry", index=True
    )
    # pending_entry | entered | exited | expired | cancelled
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # None | stop_loss | take_profit | kol_signal | manual | expired

    # Timestamps
    signal_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    entered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Price fields
    entry_range_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_range_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    filled_tp_index: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    entry_price_actual: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price_actual: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Associations
    execution_binding_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("execution_bindings.id"), nullable=True, index=True
    )
    trade_idea_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("trade_ideas.id"), nullable=True, index=True
    )
    exit_signal_candidate_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signal_candidates.id"), nullable=True
    )
    entry_signal_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    exit_signal_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    management_signal_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    management_action: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    management_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
