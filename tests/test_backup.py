from __future__ import annotations

import hashlib
import json

from email_reader.backup import BackupManager


class StubDatabase:
    def __init__(self) -> None:
        self.calls = 0

    def backup_to(self, target) -> None:
        self.calls += 1
        target.write_bytes(b"encrypted-daily-backup")


def test_daily_backup_keeps_original_manifest_for_immutable_existing_backup(tmp_path):
    database = StubDatabase()
    manager = BackupManager(database, tmp_path, b"k" * 32)

    target = manager.daily_backup()
    manifest_path = target.with_suffix(".manifest.json")
    original_manifest = manifest_path.read_text(encoding="utf-8")
    assert database.calls == 1
    assert json.loads(original_manifest)["sha256"] == hashlib.sha256(
        b"encrypted-daily-backup"
    ).hexdigest()

    # A repeated close neither recopies nor re-hashes the day's immutable file.
    target.write_bytes(b"tampered-after-manifest")
    manager.daily_backup()
    assert database.calls == 1
    assert manifest_path.read_text(encoding="utf-8") == original_manifest


def test_daily_backup_repairs_a_missing_manifest_without_recopying(tmp_path):
    database = StubDatabase()
    manager = BackupManager(database, tmp_path, b"k" * 32)
    target = manager.daily_backup()
    target.with_suffix(".manifest.json").unlink()

    manager.daily_backup()

    assert database.calls == 1
    assert target.with_suffix(".manifest.json").exists()
