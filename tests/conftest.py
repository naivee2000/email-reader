from __future__ import annotations

from pathlib import Path

import pytest

from email_reader.contracts import Account, ProviderKind
from email_reader.database import Database
from email_reader.extraction import AttachmentExtractor
from email_reader.repository import MailRepository
from email_reader.security import EncryptedBlobStore


@pytest.fixture(autouse=True)
def testing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_READER_TESTING", "1")


@pytest.fixture
def database() -> Database:
    value = Database(":memory:", "test-only", allow_plaintext=True)
    value.open()
    yield value
    value.close()


@pytest.fixture
def repository(database: Database, tmp_path: Path) -> MailRepository:
    repo = MailRepository(
        database,
        EncryptedBlobStore(tmp_path / "attachments", b"a" * 32),
        AttachmentExtractor(),
    )
    repo.upsert_account(
        Account(
            id="account-1",
            provider=ProviderKind.GMAIL,
            email="owner@example.com",
            display_name="Owner",
        )
    )
    return repo
