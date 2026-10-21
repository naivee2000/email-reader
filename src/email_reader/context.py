from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import uuid4

from email_reader.ai import AssistantService, FactList, OpenAIService
from email_reader.backup import BackupManager
from email_reader.config import AppPaths, AppSettings
from email_reader.database import Database
from email_reader.extraction import AttachmentExtractor
from email_reader.extractor_worker import extract_isolated
from email_reader.repository import MailRepository
from email_reader.search import HybridSearch
from email_reader.security import (
    EncryptedBlobStore,
    KeychainVault,
    MemoryVault,
    SecretVault,
    database_passphrase,
    get_or_create_master_key,
)
from email_reader.sync import SyncCoordinator
from email_reader.usage import UsageLedger


@dataclass(slots=True, weakref_slot=True)
class ApplicationContext:
    paths: AppPaths
    settings: AppSettings
    vault: SecretVault
    database: Database
    repository: MailRepository
    openai: OpenAIService
    search: HybridSearch
    assistant: AssistantService
    sync: SyncCoordinator
    backups: BackupManager
    runtime_dir: Path
    runtime_lock_fd: int
    _maintenance_reconciled: bool = False
    _maintenance_backup_day: str = ""
    _close_callbacks: list = field(default_factory=list)

    @classmethod
    def create(cls, *, testing: bool = False) -> ApplicationContext:
        paths = AppPaths.from_environment()
        paths.ensure()
        vault: SecretVault = MemoryVault() if testing else KeychainVault()
        master_key = get_or_create_master_key(vault)
        database = Database(
            paths.database,
            database_passphrase(master_key),
            allow_plaintext=testing,
        )
        database.open()
        settings = _load_settings(database)
        blob_store = EncryptedBlobStore(paths.attachments, master_key)
        repository = MailRepository(database, blob_store, AttachmentExtractor())
        usage = UsageLedger(database, settings.monthly_limit_usd)
        openai = OpenAIService(vault, settings, usage)
        search = HybridSearch(database, repository, openai)
        assistant = AssistantService(repository, search, openai, settings)
        # Provider freshness must never wait for OCR or a remote AI request.
        # The controller schedules local maintenance and API enrichment separately.
        sync = SyncCoordinator(repository)
        backups = BackupManager(database, paths.backups, master_key)
        runtime_dir = paths.recovery / f"runtime-{os.getpid()}-{uuid4().hex[:12]}"
        _clean_stale_runtime(paths.recovery)
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime_lock_fd = os.open(runtime_dir / ".active.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(runtime_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(runtime_lock_fd)
            shutil.rmtree(runtime_dir, ignore_errors=True)
            database.close()
            raise
        if not testing:
            repository.extraction_runner = partial(extract_isolated, temp_root=runtime_dir)
        return cls(
            paths,
            settings,
            vault,
            database,
            repository,
            openai,
            search,
            assistant,
            sync,
            backups,
            runtime_dir,
            runtime_lock_fd,
        )

    def save_setting(self, key: str, value: str) -> None:
        self.database.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        if hasattr(self.settings, key):
            current = getattr(self.settings, key)
            setattr(self.settings, key, type(current)(value))

    def run_maintenance(self, *, should_cancel=None, limit: int = 4) -> dict:
        """Run on the maintenance lane after the window is usable, never in a UI callback."""
        cancelled = should_cancel or (lambda: False)
        if cancelled():
            return {"remaining": self.repository.pending_enrichment_count(), "requires_more": False}
        indexes = self.repository.maintain_local_indexes(200)
        metadata = self.repository.backfill_attachment_metadata(limit=200)
        if not self._maintenance_reconciled and not cancelled():
            self.repository.reconcile_saved_draft_mirrors()
            self._maintenance_reconciled = True
        enrichment = self.repository.process_enrichment(limit, should_cancel=cancelled)
        legacy_remaining = self.repository.maintain_legacy_indexes(limit=100) if not cancelled() else False
        today = datetime.now(UTC).date().isoformat()
        if self._maintenance_backup_day != today and not cancelled():
            self.backups.daily_backup(self.settings.backup_retention_days, should_cancel=cancelled)
            self._maintenance_backup_day = today
        due = self.database.scalar("SELECT count(*) FROM enrichment_jobs WHERE next_attempt_at<=?", (self.database.now(),), 0)
        return {**enrichment, "index_rows": indexes["updated"],
                "requires_more": bool(indexes["remaining"] or metadata == 200 or due or legacy_remaining)}

    def process_ai_enrichment(self, *, should_cancel=None, limit: int = 2) -> dict:
        """API-only durable work; call exclusively from the idle AI lane, not OCR/sync."""
        if self.openai.backend_name() != "api":
            return {"completed": 0, "requires_more": False}
        cancelled = should_cancel or (lambda: False)
        rows = self.database.query(
            """SELECT j.*,m.body_text FROM ai_enrichment_jobs j JOIN messages m ON m.id=j.message_id
               JOIN threads t ON t.id=m.thread_id WHERE j.next_attempt_at<=? AND t.trashed_at IS NULL
               ORDER BY j.next_attempt_at LIMIT ?""", (self.database.now(), limit),
        )
        completed = 0
        for row in rows:
            if cancelled():
                break
            try:
                result = self.openai.structured(
                    FactList, operation="fact_extraction", model=self.settings.extraction_model,
                    instructions="Extract only explicit important dates, deadlines, amounts, commitments, contacts, decisions, and document references. Keep source excerpts verbatim and short. Return an empty list when there are no useful facts.",
                    input_text=row["body_text"], max_output_tokens=1400,
                )
                if cancelled():
                    break
                with self.database.transaction() as connection:
                    current = connection.execute("SELECT body_text FROM messages WHERE id=?", (row["message_id"],)).fetchone()
                    if current and hashlib.sha256(current["body_text"].encode()).hexdigest() == row["body_hash"]:
                        self.repository.save_facts(row["message_id"], result.facts)
                    connection.execute("DELETE FROM ai_enrichment_jobs WHERE message_id=? AND body_hash=?", (row["message_id"], row["body_hash"]))
                completed += 1
            except Exception:
                self.database.execute(
                    "UPDATE ai_enrichment_jobs SET attempts=attempts+1,next_attempt_at=? WHERE message_id=? AND body_hash=?",
                    ((datetime.now(UTC) + timedelta(minutes=15)).isoformat(), row["message_id"], row["body_hash"]),
                )
                break
        if not cancelled():
            self.search.index_missing_embeddings(16)
        due = self.database.scalar("SELECT count(*) FROM ai_enrichment_jobs WHERE next_attempt_at<=?", (self.database.now(),), 0)
        return {"completed": completed, "requires_more": bool(due)}

    def register_shutdown(self, callback) -> None:
        self._close_callbacks.append(weakref.WeakMethod(callback))

    def close(self, *, backup: bool = False) -> None:
        """After all jobs stop, release storage. Backups normally run before shutdown."""
        for reference in self._close_callbacks:
            callback = reference()
            if callback is not None:
                callback()
        self._close_callbacks.clear()
        try:
            if backup:
                self.backups.daily_backup(self.settings.backup_retention_days)
        finally:
            try:
                self.database.close()
            finally:
                shutil.rmtree(self.runtime_dir, ignore_errors=True)
                os.close(self.runtime_lock_fd)


def _load_settings(database: Database) -> AppSettings:
    settings = AppSettings()
    for row in database.query("SELECT key,value FROM app_settings"):
        if hasattr(settings, row["key"]):
            current = getattr(settings, row["key"])
            setattr(settings, row["key"], type(current)(row["value"]))
    return settings


def _clean_stale_runtime(recovery: Path) -> None:
    for path in recovery.glob("runtime-*"):
        # A diagnostic or second launch must not remove files currently opened
        # by another running instance. Unknown/denied owners are left intact.
        if path.is_symlink() or not path.is_dir():
            continue
        lock_path = path / ".active.lock"
        if lock_path.exists():
            try:
                fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    continue  # Another live instance still owns this folder.
                shutil.rmtree(path, ignore_errors=True)
            finally:
                os.close(fd)
            continue
        # Legacy builds used only a PID in the name. Keep a folder when the
        # former owner might still be running; new builds always use locks.
        try:
            pid = int(path.name.removeprefix("runtime-").split("-", 1)[0])
        except ValueError:
            continue
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            shutil.rmtree(path, ignore_errors=True)
        except (PermissionError, OSError):
            continue
