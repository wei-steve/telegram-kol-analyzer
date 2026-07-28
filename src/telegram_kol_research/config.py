"""Shared configuration defaults for the local research app."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from telegram_kol_research.llm_chat import _load_env_file_values


@dataclass(slots=True)
class AppConfig:
    base_dir: Path = Path.cwd()
    data_dir: Path = Path("data")
    session_path: Path = Path("data/telegram.session")
    database_path: Path = Path("data/research.db")


@dataclass(frozen=True, slots=True)
class RuntimeIncidentConfig:
    """Fail-closed Phase 2 feature flags for incident capture and delivery."""

    capture_types: frozenset[str] = frozenset()
    telegram_notifications_enabled: bool = False
    notification_lease_seconds: float = 120.0
    feature_policy_version: str = "runtime-incident-phase-2-v1"
    prompt_version: str = "none"
    tool_policy_version: str = "none"

    def captures(self, incident_type: str) -> bool:
        return incident_type in self.capture_types


def _enabled_flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_runtime_incident_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> RuntimeIncidentConfig:
    """Load dormant-by-default runtime incident flags from bounded settings."""

    paths = (
        [".env", "config/telegram.env", "config/runtime_incident_agent.env"]
        if env_file_paths is None
        else env_file_paths
    )
    env = dict(_load_env_file_values(paths) if paths else {})
    env.update(os.environ if environ is None else environ)
    capture_types = frozenset(
        item.strip().lower()
        for item in env.get(
            "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES", ""
        ).split(",")
        if item.strip()
    )
    try:
        lease_seconds = float(
            env.get(
                "TELEGRAM_KOL_RUNTIME_INCIDENT_NOTIFICATION_LEASE_SECONDS",
                "120",
            )
        )
    except (TypeError, ValueError):
        lease_seconds = 120.0
    return RuntimeIncidentConfig(
        capture_types=capture_types,
        telegram_notifications_enabled=_enabled_flag(
            env.get("TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED")
        ),
        notification_lease_seconds=max(5.0, min(lease_seconds, 3600.0)),
    )
