from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / "Desktop" / "Python Files" / "Email Reader" / "data"


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    attachments: Path
    backups: Path
    recovery: Path
    exports: Path

    @classmethod
    def from_environment(cls) -> AppPaths:
        root = Path(os.environ.get("EMAIL_READER_DATA_DIR", DEFAULT_DATA_DIR)).expanduser()
        return cls(
            root=root,
            database=root / "email_reader.db",
            attachments=root / "attachments",
            backups=root / "backups",
            recovery=root / "recovery",
            exports=root / "exports",
        )

    def ensure(self) -> None:
        for path in (
            self.root,
            self.attachments,
            self.backups,
            self.recovery,
            self.exports,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Avoid asking macOS to re-authorize an identical permission change
            # for every launch when the data directory lives under Desktop.
            if path.stat().st_mode & 0o777 != 0o700:
                path.chmod(0o700)


@dataclass(slots=True)
class AppSettings:
    ai_backend: str = "auto"
    response_model: str = "gpt-5.6-terra"
    extraction_model: str = "gpt-5.4-nano"
    embedding_model: str = "text-embedding-3-small"
    reasoning_effort: str = "low"
    compact_reply_experiment: bool = False
    sync_interval_seconds: int = 300
    monthly_limit_usd: float = 25.0
    trash_retention_days: int = 30
    backup_retention_days: int = 7
    max_search_results: int = 12
