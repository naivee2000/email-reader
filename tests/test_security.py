from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from email_reader.security import (
    EncryptedBlobStore,
    KeychainVault,
    MemoryVault,
    get_or_create_master_key,
)


def test_master_key_is_stable_in_vault() -> None:
    vault = MemoryVault()
    first = get_or_create_master_key(vault)
    second = get_or_create_master_key(vault)
    assert first == second
    assert len(first) == 32


def test_blob_store_encrypts_deduplicates_and_detects_tampering(tmp_path: Path) -> None:
    store = EncryptedBlobStore(tmp_path, b"k" * 32)
    digest, size = store.put(b"confidential attachment")
    duplicate, duplicate_size = store.put(b"confidential attachment")

    assert digest == duplicate
    assert size == duplicate_size == 23
    assert store.get(digest) == b"confidential attachment"
    encrypted_path = tmp_path / digest[:2] / f"{digest}.enc"
    assert b"confidential attachment" not in encrypted_path.read_bytes()

    payload = bytearray(encrypted_path.read_bytes())
    payload[-1] ^= 1
    encrypted_path.write_bytes(payload)
    with pytest.raises(InvalidTag):
        store.get(digest)


def test_production_database_never_silently_falls_back(tmp_path: Path, monkeypatch) -> None:
    from email_reader.database import Database

    monkeypatch.delenv("EMAIL_READER_TESTING", raising=False)
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    with pytest.raises(RuntimeError, match="SQLCipher is required"):
        Database(tmp_path / "production.db", "secret", allow_plaintext=False).open()


def test_keychain_helper_receives_secrets_only_over_pipe(tmp_path, monkeypatch) -> None:
    import email_reader.security as security

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(security, "__file__", str(tmp_path / "security.py"))
    helper = tmp_path / "helper"
    helper.write_bytes(b"synthetic-executable")
    (tmp_path / "keychain-helper.json").write_text(
        json.dumps({"path": str(helper), "sha256": hashlib.sha256(helper.read_bytes()).hexdigest()})
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"ok": true}', "")

    monkeypatch.setattr(security.process_launch, "run", run)
    vault = KeychainVault()
    vault.set("test-key", "private-value")
    assert calls[0][0] == [str(helper)]
    assert json.loads(calls[0][1]["input"])["value"] == "private-value"
    helper.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="missing or changed"):
        vault.get("test-key")
    assert len(calls) == 1


def test_source_process_cannot_claim_production_keychain_identity(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(RuntimeError, match="signed Email Reader"):
        KeychainVault().get("master-key-v1")


def test_retry_metadata_and_backup_remain_encrypted(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    from email_reader.contracts import Account, ProviderKind
    from email_reader.database import Database
    from email_reader.repository import MailRepository

    monkeypatch.delenv("EMAIL_READER_TESTING", raising=False)
    database = Database(tmp_path / "private.db", "fictional-test-key", allow_plaintext=False)
    database.open()
    try:
        repo = MailRepository(database, EncryptedBlobStore(tmp_path / "blobs", b"x" * 32))
        repo.upsert_account(Account(id="test", provider=ProviderKind.GMAIL, email="fictional@example.net"))
        repo.queue_original_retry("test", "private-source-marker", {"mailbox": "Private mailbox marker"})
        backup = tmp_path / "backup.db"
        database.backup_to(backup)
    finally:
        database.close()
    for path in [tmp_path / "private.db", backup]:
        content = path.read_bytes()
        assert not content.startswith(b"SQLite format 3")
        assert b"private-source-marker" not in content and b"Private mailbox marker" not in content
    restored = Database(backup, "fictional-test-key", allow_plaintext=False)
    restored.open()
    try:
        assert restored.scalar("SELECT provider_id FROM source_retry_queue") == "private-source-marker"
        assert restored.integrity_check()
    finally:
        restored.close()
