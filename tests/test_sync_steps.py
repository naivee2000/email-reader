from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from email_reader.categories import IMPORT_POLICY_VERSION
from email_reader.contracts import MessagePayload, SyncPage
from email_reader.providers.base import EmailProvider, SyncCursorExpiredError
from email_reader.sync import SyncCoordinator


class Pages(EmailProvider):
    def __init__(self):
        self.calls = []

    def authenticate(self):
        raise AssertionError("No authentication in fixture")

    def initial_sync(self, *, since, page_token=None):
        self.calls.append((since, page_token))
        return SyncPage(
            messages=[MessagePayload(provider_id=page_token or "first", provider_thread_id="thread",
                labels=["INBOX"], body_text="Stored before attachment extraction.")],
            next_page_token="second" if page_token is None else None,
            next_cursor="new-cursor" if page_token else None,
            examined_count=10,
        )

    def incremental_sync(self, cursor):
        return self.initial_sync(since=None)

    def create_draft(self, request):
        raise AssertionError("No draft writes in fixture")

    def source_link(self, provider_message_id):
        return None


def test_step_handles_one_page_without_ai_or_original_retries(repository):
    facts, indexer = Mock(), Mock()
    coordinator = SyncCoordinator(repository, facts, indexer)
    provider = Pages()
    provider.retry_sources = Mock(side_effect=AssertionError("Separate retry lane"))
    result = coordinator.sync_account_step("account-1", provider, since=None)
    assert result.processed == 1 and result.examined == 10 and not result.complete
    assert len(provider.calls) == 1
    assert repository.db.scalar("SELECT sync_page_token FROM accounts") == "second"
    result = coordinator.sync_account_step("account-1", provider, since=None)
    assert result.complete and len(provider.calls) == 2
    assert repository.db.scalar("SELECT sync_cursor FROM accounts") == "new-cursor"
    facts.assert_not_called()
    indexer.assert_not_called()
    provider.retry_sources.assert_not_called()


def test_repair_uses_original_boundary_and_independent_restart_checkpoint(repository):
    original_since = datetime(2021, 1, 1, tzinfo=UTC)
    repository.db.execute("UPDATE accounts SET sync_cursor='live',sync_page_token='live-page'")
    provider = Pages()
    first = SyncCoordinator(repository).sync_account_step(
        "account-1", provider, since=original_since, job_kind="repair")
    assert not first.complete
    checkpoint = repository.get_import_checkpoint("account-1", "repair", IMPORT_POLICY_VERSION)
    assert checkpoint["page_token"] == "second"
    second = SyncCoordinator(repository).sync_account_step(
        "account-1", provider, since=datetime.now(UTC), job_kind="repair")
    assert second.complete
    assert provider.calls == [(original_since, None), (original_since, "second")]
    assert repository.db.scalar("SELECT sync_cursor FROM accounts") == "live"
    assert repository.db.scalar("SELECT sync_page_token FROM accounts") == "live-page"
    SyncCoordinator(repository).sync_account_step("account-1", provider, since=None, job_kind="repair")
    assert len(provider.calls) == 2


def test_freshness_never_resumes_or_changes_other_checkpoints(repository):
    repository.db.execute("UPDATE accounts SET sync_cursor='live',sync_page_token='unfinished'")
    provider = Pages()
    coordinator = SyncCoordinator(repository)
    since = datetime.now(UTC) - timedelta(days=7)
    for _ in range(2):
        result = coordinator.sync_account_step("account-1", provider, since=since, job_kind="freshness")
        assert result.processed == 1
    assert provider.calls == [(since, None), (since, None)]
    assert repository.db.scalar("SELECT sync_page_token FROM accounts") == "unfinished"
    assert repository.db.scalar("SELECT count(*) FROM import_checkpoints") == 0


def test_expired_cursor_yields_before_fresh_request(repository):
    provider = Pages()
    provider.incremental_sync = Mock(side_effect=SyncCursorExpiredError("expired"))
    repository.db.execute("UPDATE accounts SET sync_cursor='expired'")
    coordinator = SyncCoordinator(repository)
    first = coordinator.sync_account_step("account-1", provider, since=None)
    assert not first.complete and provider.calls == []
    assert repository.db.scalar("SELECT sync_cursor FROM accounts") is None
    coordinator.sync_account_step("account-1", provider, since=None)
    assert len(provider.calls) == 1


def test_partial_page_pause_replays_current_page(repository):
    provider = Pages()
    provider.initial_sync = lambda **kwargs: SyncPage(messages=[
        MessagePayload(provider_id=str(i), provider_thread_id="t", body_text="Keep") for i in range(3)
    ], next_page_token="next")
    coordinator = SyncCoordinator(repository)
    result = coordinator.sync_account_step("account-1", provider, since=None,
        progress=lambda update: coordinator.pause() if update.stage == "messages" else None)
    assert result.paused and result.processed == 1
    assert repository.db.scalar("SELECT sync_page_token FROM accounts") is None
    coordinator.resume()
    result = coordinator.sync_account_step("account-1", provider, since=None)
    assert result.processed == 3 and repository.db.scalar("SELECT count(*) FROM messages") == 3
