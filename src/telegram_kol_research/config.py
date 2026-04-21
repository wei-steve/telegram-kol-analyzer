"""Shared configuration defaults for the local research app."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    base_dir: Path = Path.cwd()
    data_dir: Path = Path("data")
    session_path: Path = Path("data/telegram.session")
    database_path: Path = Path("data/research.db")

