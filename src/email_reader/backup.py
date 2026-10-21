from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from email_reader.database import Database


class BackupManager:
    def __init__(self, database: Database, backup_dir: Path, master_key: bytes) -> None:
        self.database = database
        self.backup_dir = backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = AESGCM(master_key)

    def daily_backup(self, retention_days: int = 7, *, should_cancel=None) -> Path:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        target = self.backup_dir / f"email-reader-{today}.db"
        created = not target.exists()
        if created:
            descriptor, name = tempfile.mkstemp(prefix=".daily-", suffix=".staging", dir=self.backup_dir)
            os.close(descriptor)
            staged = Path(name)
            try:
                if should_cancel is not None:
                    self.database.backup_to(staged, should_cancel=should_cancel)
                else:
                    self.database.backup_to(staged)
                # backup_to verifies the encrypted SQLite snapshot before promotion.
                self._write_manifest(staged, filename=target.name)
                staged.replace(target)
                staged.with_suffix(".manifest.json").replace(target.with_suffix(".manifest.json"))
            finally:
                staged.unlink(missing_ok=True)
                staged.with_suffix(".manifest.json").unlink(missing_ok=True)
        # A daily backup is immutable. Re-hashing a large unchanged SQLCipher
        # file on every normal app exit adds seconds of latency and, worse,
        # would bless later tampering by replacing the original digest. Create
        # the manifest with the backup (or repair a missing sidecar) only.
        if not target.with_suffix(".manifest.json").exists():
            verify = getattr(self.database, "verify_backup", None)
            if verify and not verify(target):
                raise RuntimeError("An unfinished backup failed validation; it was not accepted")
            self._write_manifest(target)
        self.prune(retention_days)
        return target

    def _write_manifest(self, target: Path, *, filename: str | None = None) -> None:
        with target.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        manifest = {
            "file": filename or target.name,
            "sha256": digest,
            "created_at": datetime.now(UTC).isoformat(),
            "scope": "encrypted_database_only; original attachment blobs remain in the local data directory",
        }
        path = target.with_suffix(".manifest.json")
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        path.chmod(0o600)

    def prune(self, retention_days: int) -> int:
        threshold = datetime.now(UTC) - timedelta(days=retention_days)
        removed = 0
        for path in self.backup_dir.glob("email-reader-*.db"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if modified < threshold:
                path.unlink(missing_ok=True)
                path.with_suffix(".manifest.json").unlink(missing_ok=True)
                removed += 1
        return removed

    def export_encrypted_archive(self, destination: Path) -> Path:
        """Export the encrypted database only, not an attachment-complete archive."""
        temporary = self.backup_dir / ".manual-export.db"
        self.database.backup_to(temporary)
        nonce = __import__("os").urandom(12)
        encrypted = self.cipher.encrypt(
            nonce, temporary.read_bytes(), b"private-email-reader-backup-v1"
        )
        destination.write_bytes(b"PERB1" + nonce + encrypted)
        destination.chmod(0o600)
        temporary.unlink(missing_ok=True)
        return destination
