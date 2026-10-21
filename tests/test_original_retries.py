import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from email_reader.contracts import (
    MessagePayload,
    ProviderKind,
    SourceRetryRequest,
    SourceRetryResult,
    SyncPage,
)
from email_reader.database import Database
from email_reader.providers.apple_mail import AppleMailAccountInfo, AppleMailProvider
from email_reader.providers.base import EmailProvider, ProviderOperationError
from email_reader.repository import MailRepository
from email_reader.sync import SyncCoordinator


@pytest.fixture
def persistent_repository(repository, tmp_path):
    path = tmp_path / "persistent-mail.db"
    repository.db.backup_to(path)
    database = Database(path, "test", allow_plaintext=True)
    database.open()
    try:
        yield MailRepository(database, repository.blobs)
    finally:
        database.close()


class RetryProvider(EmailProvider):
    def __init__(self, results=()):
        self.results = list(results)
        self.requests = []
        self.cursors = []
        self.initial_calls = 0

    def authenticate(self):
        raise AssertionError("Not used")

    def initial_sync(self, *, since, page_token=None):
        self.initial_calls += 1
        return SyncPage(messages=[], next_cursor="new-cursor")

    def incremental_sync(self, cursor):
        self.cursors.append(cursor)
        return SyncPage(messages=[], next_cursor="new-cursor")

    def retry_sources(self, requests):
        self.requests.append(requests)
        return self.results

    def create_draft(self, request):
        raise AssertionError("Retrying must not create a draft")

    def source_link(self, provider_message_id):
        return None


def deferred(repository, identifier="deferred@example.net"):
    payload = MessagePayload(provider_id=identifier, provider_thread_id=identifier,
        subject="Delayed original", body_text="", source_complete=False,
        sent_at=datetime.now(UTC) - timedelta(days=30), received_at=datetime.now(UTC) - timedelta(days=30),
        retry_locator={"mailbox": "INBOX", "message_id": identifier, "internal_id": "123"})
    repository.ingest_message("account-1", payload)
    return payload


def test_old_original_retries_after_restart_without_reimporting_completed_mail(persistent_repository):
    repository = persistent_repository
    payload = deferred(repository)
    repository.db.execute("UPDATE accounts SET sync_cursor='recent-cursor' WHERE id='account-1'")
    repository.db.close()
    repository.db.open()
    provider = RetryProvider([SourceRetryResult(provider_id=payload.provider_id,
        message=payload.model_copy(update={"body_text": "The recovered original.", "source_complete": True}))])
    progress = []
    extractor, indexer = Mock(), Mock()
    assert SyncCoordinator(repository, extractor, indexer).sync_account("account-1", provider, since=None, progress=progress.append) == 1
    assert extractor.call_args.args[1] == "The recovered original."
    assert indexer.call_count == 2, "Index both the ordinary page and recovered originals"
    assert provider.cursors == ["recent-cursor"]
    assert provider.requests[0][0].locator["internal_id"] == "123"
    assert repository.db.scalar("SELECT body_text FROM messages WHERE provider_id=?", (payload.provider_id,)) == "The recovered original."
    assert repository.pending_original_count() == 0
    assert any(item.stage == "originals" for item in progress)
    SyncCoordinator(repository).sync_account("account-1", provider, since=None)
    assert len(provider.requests) == 1, "Completed originals must not be fetched again"


def test_missing_original_has_durable_backoff_without_holding_new_mail_cursor(persistent_repository):
    repository = persistent_repository
    deferred(repository)
    provider = RetryProvider()
    coordinator = SyncCoordinator(repository)
    coordinator.sync_account("account-1", provider, since=None)
    assert repository.db.scalar("SELECT sync_cursor FROM accounts WHERE id='account-1'") == "new-cursor"
    assert repository.pending_original_count() == 1
    assert repository.pending_originals("account-1") == []
    first_due = datetime.fromisoformat(repository.db.scalar("SELECT next_attempt_at FROM source_retry_queue"))
    assert timedelta(minutes=29) <= first_due - datetime.now(UTC) <= timedelta(minutes=31)
    repository.db.close()
    repository.db.open()
    assert repository.pending_originals("account-1") == []
    assert len(repository.pending_originals("account-1", force=True)) == 1
    coordinator.sync_account("account-1", provider, since=None, force_original_retries=True)
    assert len(provider.requests) == 2
    assert repository.db.scalar("SELECT attempts FROM source_retry_queue") == 2
    second_due = datetime.fromisoformat(repository.db.scalar("SELECT next_attempt_at FROM source_retry_queue"))
    assert timedelta(minutes=59) <= second_due - datetime.now(UTC) <= timedelta(minutes=61)


def test_explicit_original_retry_skips_mailbox_sync_and_preserves_cursor(repository):
    payload = deferred(repository)
    repository.db.execute("UPDATE accounts SET sync_cursor='cursor-before' WHERE id='account-1'")
    provider = RetryProvider([SourceRetryResult(provider_id=payload.provider_id,
        message=payload.model_copy(update={"body_text": "Recovered original", "source_complete": True}))])
    coordinator = SyncCoordinator(repository)

    assert coordinator.retry_pending_originals("account-1", provider, force=True) == 1
    assert provider.initial_calls == 0 and provider.cursors == []
    assert len(provider.requests) == 1
    assert repository.db.scalar("SELECT sync_cursor FROM accounts WHERE id='account-1'") == "cursor-before"
    assert repository.pending_original_count() == 0


def test_manual_retry_reports_aggregate_failure_without_message_details(repository, monkeypatch):
    deferred(repository)
    provider = RetryProvider()
    monkeypatch.setattr(provider, "retry_sources", lambda requests: (_ for _ in ()).throw(
        ProviderOperationError("private provider failure involving an address")))
    coordinator = SyncCoordinator(repository)
    assert coordinator.retry_pending_originals("account-1", provider, force=True) == 0
    assert coordinator.last_original_retry_outcome == {
        "requested": 1, "recovered": 0, "unresolved": 1,
        "timed_out": 0, "provider_error": 1,
    }
    assert "address" not in str(coordinator.last_original_retry_outcome)


def test_retry_filters_bulk_placeholder_and_rejects_unrequested_result(repository):
    payload = deferred(repository)
    provider = RetryProvider([SourceRetryResult(provider_id=payload.provider_id, excluded=True),
        SourceRetryResult(provider_id="not-requested", message=MessagePayload(provider_id="not-requested", provider_thread_id="other"))])
    SyncCoordinator(repository).sync_account("account-1", provider, since=None)
    assert repository.pending_original_count() == 0
    assert repository.list_threads() == []
    assert repository.db.scalar("SELECT COUNT(*) FROM messages") == 1


def test_legacy_incomplete_rows_backfill_but_remote_deleted_and_trashed_do_not(repository):
    payload = deferred(repository)
    repository.db.execute("DELETE FROM source_retry_queue")
    assert repository.pending_originals("account-1")[0].provider_id == payload.provider_id
    repository.mark_remote_deleted("account-1", [payload.provider_id])
    assert repository.pending_originals("account-1") == []
    second = deferred(repository, "trashed@example.net")
    thread = repository.db.scalar("SELECT thread_id FROM messages WHERE provider_id=?", (second.provider_id,))
    repository.trash_thread(thread)
    assert repository.pending_originals("account-1") == []


def test_hydrated_reply_moves_to_its_real_thread_and_invalidates_placeholder_summary(repository):
    root = MessagePayload(provider_id="root", provider_thread_id="root", body_text="First message")
    repository.ingest_message("account-1", root)
    payload = deferred(repository)
    old_thread = repository.db.scalar("SELECT thread_id FROM messages WHERE provider_id=?", (payload.provider_id,))
    recovered = payload.model_copy(update={"provider_thread_id": "root", "source_complete": True,
        "body_text": "The reply content", "in_reply_to": "<root>"})
    repository.ingest_message("account-1", recovered)
    new_thread = repository.db.scalar("SELECT thread_id FROM messages WHERE provider_id='root'")
    assert repository.db.scalar("SELECT thread_id FROM messages WHERE provider_id=?", (payload.provider_id,)) == new_thread
    assert repository.db.scalar("SELECT message_count FROM threads WHERE id=?", (new_thread,)) == 2
    assert repository.db.scalar("SELECT message_count FROM threads WHERE id=?", (old_thread,)) == 0
    assert repository.db.scalar("SELECT in_reply_to FROM messages WHERE provider_id=?", (payload.provider_id,)) == "<root>"
    assert repository.pending_original_count() == 0


def test_apple_mail_retry_reads_only_requested_identity(monkeypatch):
    info = AppleMailAccountInfo(account_name="Personal", email="person@gmail.com",
        provider=ProviderKind.GMAIL, mailboxes=("INBOX", "Sent", "Drafts"))
    provider = AppleMailProvider(info.account_id, info)
    rows = [{"requestedId": "expected@example.net", "messageId": "expected@example.net", "internalId": "123",
        "subject": "Recovered", "sender": "sender@example.net", "unread": True, "junk": False,
        "mailbox": "INBOX", "source": "Message-ID: <expected@example.net>\nFrom: sender@example.net\nSubject: Recovered\n\nOriginal text"}]
    call = Mock(return_value=rows)
    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript", call)
    result = provider.retry_sources([SourceRetryRequest(provider_id="expected@example.net", locator={"mailbox": "INBOX"})])
    assert result[0].message.body_text == "Original text"
    assert result[0].message.source_complete
    script = call.call_args.args[0]
    assert "set matches to (messages of targetMailbox whose id is wantedInternal)" in script
    assert script.index("set matches to (messages of targetMailbox whose id is wantedInternal)") < script.index(
        "set matches to (messages of targetMailbox whose message id is wantedId"
    )
    assert "candidateInternetId is not wantedId and candidateInternetId is not bareId and candidateInternetId is not wrappedId" in script
    assert "whose message id is wantedId" in script
    assert script.count("whose id is wantedInternal") == len(provider._mailbox_names)
    assert 'set mailboxName to "INBOX"' in script
    assert "source of currentMessage" in script
    assert "messages startIndex thru" not in script
    assert "set read status" not in script and "Mail.save" not in script


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript compiler is macOS-only")
def test_retry_script_compiles_without_running_mail_commands(tmp_path):
    provider = AppleMailProvider("account-1", AppleMailAccountInfo(account_name='Personal "Mail"',
        email="person@gmail.com", provider=ProviderKind.GMAIL, mailboxes=("INBOX", "Sent", "Drafts")))
    script = provider._retry_script([SourceRetryRequest(provider_id="test@example.net")])
    result = subprocess.run(["/usr/bin/osacompile", "-o", str(tmp_path / "retry.scpt"), "-"],
        input=script, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
