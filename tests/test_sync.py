from __future__ import annotations

import json
from datetime import UTC, datetime

from email_reader.contracts import (
    Account,
    DraftReceipt,
    DraftRequest,
    MessagePayload,
    ProviderKind,
    SyncPage,
)
from email_reader.providers.apple_mail import AppleMailAccountInfo, AppleMailProvider
from email_reader.providers.base import EmailProvider, SyncCursorExpiredError
from email_reader.providers.gmail import GmailProvider
from email_reader.providers.outlook import OutlookProvider
from email_reader.repository import MailRepository
from email_reader.security import MemoryVault
from email_reader.sync import SyncCoordinator


class FakeProvider(EmailProvider):
    def authenticate(self) -> Account:
        raise AssertionError("not used")

    def initial_sync(self, *, since, page_token=None) -> SyncPage:
        return SyncPage(
            messages=[
                MessagePayload(
                    provider_id="sync-message",
                    provider_thread_id="sync-thread",
                    subject="Sync test",
                    sender_email="person@example.net",
                    to=["owner@example.com"],
                    sent_at=datetime.now(UTC),
                    received_at=datetime.now(UTC),
                    body_text="The deadline is tomorrow.",
                )
            ],
            next_cursor="cursor-1",
            examined_count=7,
        )

    def incremental_sync(self, cursor: str) -> SyncPage:
        return SyncPage(messages=[], next_cursor="cursor-2", remote_deleted_ids=["sync-message"])

    def create_draft(self, request: DraftRequest) -> DraftReceipt:
        return DraftReceipt(provider_draft_id="draft")

    def source_link(self, provider_message_id: str) -> str | None:
        return None


class ResumeProbeProvider(FakeProvider):
    def __init__(self) -> None:
        self.received_page_token = None

    def initial_sync(self, *, since, page_token=None) -> SyncPage:
        self.received_page_token = page_token
        return SyncPage(messages=[], next_cursor="fresh-cursor")

    def incremental_sync(self, cursor: str) -> SyncPage:
        raise AssertionError("a forced full import must not use the incremental cursor")


def test_expired_provider_cursor_mid_page_restarts_full_import(repository):
    class ExpiringPageProvider(FakeProvider):
        def __init__(self):
            self.calls = []

        def incremental_sync(self, cursor):
            self.calls.append(("incremental", cursor))
            return SyncPage(messages=[], next_page_token="stale-page")

        def initial_sync(self, *, since, page_token=None):
            self.calls.append(("initial", page_token))
            if page_token:
                raise SyncCursorExpiredError("Expired token")
            return SyncPage(messages=[], next_cursor="fresh-cursor")

    repository.db.execute(
        "UPDATE accounts SET sync_cursor='old-cursor' WHERE id='account-1'"
    )
    provider = ExpiringPageProvider()
    assert SyncCoordinator(repository).sync_account("account-1", provider, since=None) == 0
    assert provider.calls == [
        ("incremental", "old-cursor"), ("initial", "stale-page"), ("initial", None)
    ]
    state = repository.db.connection.execute(
        "SELECT sync_cursor,sync_page_token FROM accounts WHERE id='account-1'"
    ).fetchone()
    assert state["sync_cursor"] == "fresh-cursor" and state["sync_page_token"] is None


def test_deferred_source_preserves_existing_content_and_retries(repository):
    payload = FakeProvider().initial_sync(since=None).messages[0]
    message_id = repository.ingest_message("account-1", payload)
    before = repository.db.scalar("SELECT body_text FROM messages WHERE id=?", (message_id,))
    placeholder = payload.model_copy(update={"body_text": "", "source_complete": False})
    repository.ingest_message("account-1", placeholder)
    assert repository.db.scalar("SELECT body_text FROM messages WHERE id=?", (message_id,)) == before
    assert payload.provider_id not in repository.complete_message_ids("account-1")
    repository.ingest_message("account-1", payload)
    assert payload.provider_id in repository.complete_message_ids("account-1")


def test_provider_revision_persists_and_metadata_update_preserves_body(repository):
    payload = FakeProvider().initial_sync(since=None).messages[0].model_copy(
        update={"provider_revision": "version-1"}
    )
    message_id = repository.ingest_message("account-1", payload)
    assert repository.complete_message_revisions("account-1") == {
        "sync-message": "version-1"
    }
    repository.update_provider_revisions("account-1", {"sync-message": "version-2"})
    row = repository.db.connection.execute(
        "SELECT provider_revision,body_text FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    assert row["provider_revision"] == "version-2"
    assert row["body_text"] == "The deadline is tomorrow."


def test_gmail_label_metadata_updates_mailbox_without_replacing_content(repository):
    payload = FakeProvider().initial_sync(since=None).messages[0].model_copy(
        update={"labels": ["INBOX", "UNREAD"], "is_unread": True}
    )
    message_id = repository.ingest_message("account-1", payload)
    repository.update_provider_labels(
        "account-1", {"sync-message": ["INBOX", "CATEGORY_UPDATES"]}
    )
    row = repository.db.connection.execute(
        "SELECT body_text,labels_json,is_unread,in_main_inbox,mail_category "
        "FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    assert row["body_text"] == "The deadline is tomorrow."
    assert json.loads(row["labels_json"]) == ["INBOX", "CATEGORY_UPDATES"]
    assert row["is_unread"] == 0
    assert row["in_main_inbox"] == 1
    assert row["mail_category"] == "updates"
    repository.update_provider_labels("account-1", {"sync-message": ["TRASH", "ARCHIVE"]})
    assert repository.db.scalar(
        "SELECT in_main_inbox FROM messages WHERE id=?", (message_id,)
    ) == 0


def test_outlook_folder_metadata_does_not_clear_unread_state(repository):
    class FolderOnlyProvider(FakeProvider):
        def __init__(self, read_state):
            self.read_state = read_state

        def incremental_sync(self, cursor):
            return SyncPage(
                messages=[], next_cursor="next",
                unchanged_label_states={"outlook-unread": ["INBOX"]},
                unchanged_read_states=(
                    {"outlook-unread": self.read_state}
                    if self.read_state is not None else {}
                ),
                labels_include_read_state=False,
            )

    now = datetime.now(UTC)
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="outlook-unread", provider_thread_id="outlook-unread-thread",
        subject="Unread", sender_email="person@example.net", labels=["INBOX"],
        is_unread=True, body_text="Keep read state", sent_at=now, received_at=now,
    ))
    repository.db.execute("UPDATE accounts SET sync_cursor='prior' WHERE id='account-1'")
    coordinator = SyncCoordinator(repository)
    coordinator.sync_account("account-1", FolderOnlyProvider(None), since=None)
    assert repository.db.scalar(
        "SELECT is_unread FROM messages WHERE id=?", (message_id,)
    ) == 1
    coordinator.sync_account("account-1", FolderOnlyProvider(False), since=None)
    assert repository.db.scalar(
        "SELECT is_unread FROM messages WHERE id=?", (message_id,)
    ) == 0


def test_outlook_folder_move_removes_old_role_but_keeps_available_content(repository):
    now = datetime.now(UTC)
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="moved-out", provider_thread_id="move-thread",
        subject="Move", sender_email="person@example.net",
        labels=["INBOX"], body_text="Keep this encrypted original.",
        sent_at=now, received_at=now,
    ))
    assert repository.mailbox_counts()["inbox"] == 1
    repository.remove_provider_folder_roles("account-1", {"moved-out": "inbox"})
    row = repository.db.connection.execute(
        "SELECT body_text,source_state,labels_json,in_main_inbox FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    assert row["body_text"] == "Keep this encrypted original."
    assert row["source_state"] == "available"
    assert json.loads(row["labels_json"]) == ["ARCHIVE"]
    assert row["in_main_inbox"] == 0
    assert repository.mailbox_counts()["inbox"] == 0
    repository.update_provider_labels("account-1", {"moved-out": ["SENT"]})
    assert repository.mailbox_counts()["sent"] == 1
    assert repository.db.scalar(
        "SELECT body_text FROM messages WHERE id=?", (message_id,)
    ) == "Keep this encrypted original."


def test_outlook_folder_move_page_updates_local_scope_without_remote_deletion(repository):
    class MovedProvider(FakeProvider):
        def incremental_sync(self, cursor):
            return SyncPage(
                messages=[], removed_folder_roles={"moved": "inbox"},
                next_cursor="next",
            )

    now = datetime.now(UTC)
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="moved", provider_thread_id="moved-thread",
        subject="Move", sender_email="person@example.net", labels=["INBOX"],
        body_text="Retained text", sent_at=now, received_at=now,
    ))
    repository.db.execute(
        "UPDATE accounts SET sync_cursor='prior' WHERE id='account-1'"
    )
    SyncCoordinator(repository).sync_account(
        "account-1", MovedProvider(), since=None
    )
    row = repository.db.connection.execute(
        "SELECT body_text,source_state,in_main_inbox FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    assert row["body_text"] == "Retained text"
    assert row["source_state"] == "available"
    assert row["in_main_inbox"] == 0


def test_gmail_draft_replacement_retains_old_copy_but_shows_only_new_draft(repository):
    now = datetime.now(UTC)
    old_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="old-draft-message", provider_thread_id="draft-thread",
        subject="Draft", sender_email="owner@example.com", is_draft=True,
        labels=["DRAFT"], body_text="First version", sent_at=now, received_at=now,
    ))
    assert repository.available_draft_ids("account-1") == {"old-draft-message"}
    repository.update_provider_labels(
        "account-1", {"old-draft-message": ["DRAFT"]}
    )
    assert repository.db.scalar(
        "SELECT body_text FROM messages WHERE id=?", (old_id,)
    ) == "First version"
    new_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="new-draft-message", provider_thread_id="draft-thread",
        subject="Draft", sender_email="owner@example.com", is_draft=True,
        labels=["DRAFT"], body_text="Edited version", sent_at=now, received_at=now,
    ))
    repository.mark_remote_deleted("account-1", ["old-draft-message"])
    assert repository.available_draft_ids("account-1") == {"new-draft-message"}
    assert repository.mailbox_counts()["drafts"] == 1
    assert repository.db.scalar(
        "SELECT source_state FROM messages WHERE id=?", (old_id,)
    ) == "remote_deleted"
    assert repository.db.scalar(
        "SELECT body_text FROM messages WHERE id=?", (new_id,)
    ) == "Edited version"


def test_only_gmail_reuses_complete_draft_message_ids_on_sync(repository):
    now = datetime.now(UTC)
    repository.ingest_message("account-1", MessagePayload(
        provider_id="complete-draft", provider_thread_id="draft-known",
        subject="Draft", sender_email="owner@example.com", is_draft=True,
        labels=["DRAFT"], body_text="Keep this version", sent_at=now, received_at=now,
    ))
    assert "complete-draft" not in repository.complete_message_ids("account-1")
    assert "complete-draft" in repository.complete_message_ids(
        "account-1", include_drafts=True
    )

    class GmailProbe(GmailProvider):
        def set_known_message_ids(self, identifiers):
            self.received_ids = identifiers

        def set_known_draft_ids(self, identifiers):
            self.received_draft_ids = identifiers

        def initial_sync(self, *, since, page_token=None):
            return SyncPage(messages=[], next_cursor="next")

    class MailLikeProbe(FakeProvider):
        def set_known_message_ids(self, identifiers):
            self.received_ids = identifiers

        def initial_sync(self, *, since, page_token=None):
            return SyncPage(messages=[], next_cursor="next")

    gmail = GmailProbe("account-1", MemoryVault())
    SyncCoordinator(repository).sync_account(
        "account-1", gmail, since=None, force_full=True
    )
    assert "complete-draft" in gmail.received_ids
    assert gmail.received_draft_ids == {"complete-draft"}
    mail_like = MailLikeProbe()
    SyncCoordinator(repository).sync_account(
        "account-1", mail_like, since=None, force_full=True
    )
    assert "complete-draft" not in mail_like.received_ids


def test_complete_gmail_scope_hides_archived_message_without_deleting_source(repository):
    class ScopeProvider(FakeProvider):
        def initial_sync(self, *, since, page_token=None):
            return SyncPage(
                messages=[], next_cursor="fresh-history",
                inbox_scope_reset=True, inbox_scope_complete=True,
                inbox_scope_ids=["still-inbox"],
            )

    now = datetime.now(UTC)
    for identifier in ("still-inbox", "archived"):
        repository.ingest_message(
            "account-1",
            MessagePayload(
                provider_id=identifier, provider_thread_id=identifier,
                labels=["INBOX"], body_text=f"Original {identifier}",
                received_at=now, sent_at=now,
            ),
        )
    SyncCoordinator(repository).sync_account(
        "account-1", ScopeProvider(), since=None, force_full=True
    )
    rows = repository.db.query(
        "SELECT provider_id,in_main_inbox,source_state,body_text FROM messages "
        "WHERE account_id='account-1' ORDER BY provider_id"
    )
    assert [(row["provider_id"], row["in_main_inbox"]) for row in rows] == [
        ("archived", 0), ("still-inbox", 1)
    ]
    assert all(row["source_state"] == "available" and row["body_text"] for row in rows)


def test_gmail_scope_scan_survives_interrupted_page_resume(repository):
    class PagedScopeProvider(FakeProvider):
        def initial_sync(self, *, since, page_token=None):
            return (
                SyncPage(
                    messages=[], next_page_token="second",
                    inbox_scope_reset=True, inbox_scope_ids=["first"],
                ) if page_token is None else SyncPage(
                    messages=[], next_cursor="done", inbox_scope_complete=True,
                    inbox_scope_ids=["second"],
                )
            )

    now = datetime.now(UTC)
    for identifier in ("first", "second", "archived"):
        repository.ingest_message(
            "account-1", MessagePayload(
                provider_id=identifier, provider_thread_id=identifier,
                labels=["INBOX"], body_text=identifier,
                received_at=now, sent_at=now,
            )
        )
    first_run = SyncCoordinator(repository)
    first_run.sync_account(
        "account-1", PagedScopeProvider(), since=None, force_full=True,
        page_callback=lambda page: first_run.pause(),
    )
    assert repository.main_inbox_scan_count("account-1") == 1
    assert repository.db.scalar(
        "SELECT sync_page_token FROM accounts WHERE id='account-1'"
    ) == "second"
    SyncCoordinator(repository).sync_account(
        "account-1", PagedScopeProvider(), since=None
    )
    rows = repository.db.query(
        "SELECT provider_id,in_main_inbox FROM messages WHERE account_id='account-1'"
    )
    assert {row["provider_id"]: row["in_main_inbox"] for row in rows} == {
        "first": 1, "second": 1, "archived": 0,
    }


def test_gmail_scope_keeps_older_locally_retained_mail(repository):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="older", provider_thread_id="older",
            labels=["INBOX"], body_text="Retained original",
            received_at=old, sent_at=old,
        )
    )
    repository.begin_main_inbox_scope_scan("account-1", resume=False)
    repository.reconcile_main_inbox_scope(
        "account-1", datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert repository.db.scalar(
        "SELECT in_main_inbox FROM messages WHERE account_id='account-1' "
        "AND provider_id='older'"
    ) == 1


def test_filtered_outlook_metadata_hides_prior_focused_item_without_body_fetch(repository):
    class FilteredProvider(FakeProvider):
        def initial_sync(self, *, since, page_token=None):
            return SyncPage(
                messages=[], next_cursor="fresh-cursor", filtered_bulk_ids=["prior"]
            )

    repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="prior", provider_thread_id="prior",
            labels=["INBOX"], body_text="Encrypted original",
        )
    )
    SyncCoordinator(repository).sync_account(
        "account-1", FilteredProvider(), since=None, force_full=True
    )
    row = repository.db.connection.execute(
        "SELECT in_main_inbox,is_filtered_bulk,body_text,source_state "
        "FROM messages WHERE account_id='account-1' AND provider_id='prior'"
    ).fetchone()
    assert (row["in_main_inbox"], row["is_filtered_bulk"]) == (0, 1)
    assert row["body_text"] == "Encrypted original"
    assert row["source_state"] == "available"


def test_outlook_full_recovery_reconciles_archived_inbox_without_deletion(
    repository, monkeypatch
):
    repository.db.execute(
        "UPDATE accounts SET provider='outlook' WHERE id='account-1'"
    )
    repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="archived", provider_thread_id="archived",
            labels=["INBOX"], body_text="Retained source",
        )
    )
    provider = OutlookProvider("account-1", MemoryVault())
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        return {"value": []}

    monkeypatch.setattr(provider, "_request", request)
    SyncCoordinator(repository).sync_account(
        "account-1", provider, since=None, force_full=True
    )
    assert len(calls) == 3
    row = repository.db.connection.execute(
        "SELECT in_main_inbox,source_state,body_text FROM messages "
        "WHERE account_id='account-1' AND provider_id='archived'"
    ).fetchone()
    assert row["in_main_inbox"] == 0
    assert row["source_state"] == "available"
    assert row["body_text"] == "Retained source"


def test_sync_is_resumable_and_enrichment_failure_does_not_break_mail(
    repository: MailRepository,
) -> None:
    facts = []
    indexes = []

    def fact_extractor(message_id, content):
        facts.append(message_id)
        raise RuntimeError("simulated API outage")

    coordinator = SyncCoordinator(repository, fact_extractor, lambda: indexes.append(True) or 0)
    assert coordinator.sync_account("account-1", FakeProvider(), since=None) == 1
    assert facts
    assert indexes
    assert (
        repository.db.scalar("SELECT sync_cursor FROM accounts WHERE id='account-1'") == "cursor-1"
    )

    assert coordinator.sync_account("account-1", FakeProvider(), since=None) == 0
    state = repository.db.scalar(
        "SELECT source_state FROM messages WHERE provider_id='sync-message'"
    )
    assert state == "remote_deleted"


def test_sync_progress_distinguishes_examined_from_saved(
    repository: MailRepository,
) -> None:
    updates = []
    coordinator = SyncCoordinator(repository)

    coordinator.sync_account(
        "account-1",
        FakeProvider(),
        since=None,
        progress=updates.append,
        force_full=True,
    )

    page_update = next(update for update in updates if update.stage == "page")
    assert page_update.completed == 1
    assert page_update.details["examined"] == 7


def test_forced_full_import_resumes_from_durable_page_token(
    repository: MailRepository,
) -> None:
    repository.db.execute(
        "UPDATE accounts SET sync_cursor='old-cursor',sync_page_token='resume-page' "
        "WHERE id='account-1'"
    )
    provider = ResumeProbeProvider()

    SyncCoordinator(repository).sync_account("account-1", provider, since=None, force_full=True)

    assert provider.received_page_token == "resume-page"


def test_any_provider_resumes_durable_import_page_before_incremental_cursor(repository):
    repository.db.execute(
        "UPDATE accounts SET sync_cursor='stale-cursor',sync_page_token='resume-page' "
        "WHERE id='account-1'"
    )
    provider = ResumeProbeProvider()
    SyncCoordinator(repository).sync_account("account-1", provider, since=None)
    assert provider.received_page_token == "resume-page"
    assert repository.db.scalar("SELECT sync_cursor FROM accounts WHERE id='account-1'") == "fresh-cursor"
    assert repository.db.scalar("SELECT sync_page_token FROM accounts WHERE id='account-1'") is None


def test_apple_mail_incremental_resumes_from_saved_page_after_pause(repository, monkeypatch):
    cursor = "2026-09-01T00:00:00+00:00"
    repository.db.execute("UPDATE accounts SET sync_cursor=? WHERE id='account-1'", (cursor,))
    info = AppleMailAccountInfo("Personal", "owner@example.com", ProviderKind.OUTLOOK, ("INBOX",))
    provider = AppleMailProvider("account-1", info)
    seen = []

    def page(mailbox, offset, limit, since):
        seen.append((offset, limit))
        count = min(limit, 20 - offset)
        return {"rows": [{"internalId": str(offset + i),
                          "messageId": f"known-{offset + i}@example.com", "unread": False,
                          "unchanged": True} for i in range(count)],
                "total": 20, "examined": count, "reached_since": False}

    monkeypatch.setattr(provider, "_fetch_mailbox", page)
    coordinator = SyncCoordinator(repository)
    coordinator.sync_account("account-1", provider, since=None,
        progress=lambda update: coordinator.pause() if update.stage == "page" else None)
    token = repository.db.scalar("SELECT sync_page_token FROM accounts WHERE id='account-1'")
    assert json.loads(token)["offset"] == 8
    assert repository.db.scalar("SELECT sync_cursor FROM accounts WHERE id='account-1'") == cursor
    coordinator.resume()
    coordinator.sync_account("account-1", provider, since=None)
    assert seen[0] == (0, 10)
    assert seen[-1] == (16, 10)
    assert repository.db.scalar("SELECT sync_page_token FROM accounts WHERE id='account-1'") is None
    assert repository.db.scalar("SELECT sync_cursor FROM accounts WHERE id='account-1'") != cursor


def test_pause_in_middle_of_page_replays_unfinished_page(repository):
    class TwoPages(FakeProvider):
        def __init__(self):
            self.visited = []

        def initial_sync(self, *, since, page_token=None):
            self.visited.append(page_token)
            return SyncPage(
                messages=[
                    MessagePayload(
                        provider_id=f"{page_token or 'first'}-{i}",
                        provider_thread_id="t",
                        body_text="Keep me",
                    )
                    for i in range(3)
                ],
                next_page_token="second" if page_token is None else None,
                next_cursor=None if page_token is None else "finished",
            )

    provider = TwoPages()
    coordinator = SyncCoordinator(repository)
    coordinator.sync_account(
        "account-1",
        provider,
        since=None,
        progress=lambda update: coordinator.pause() if update.completed == 1 else None,
    )
    assert repository.db.scalar("SELECT count(*) FROM messages") == 1
    assert repository.db.scalar("SELECT sync_page_token FROM accounts") is None
    assert repository.db.scalar("SELECT sync_cursor FROM accounts") is None
    assert repository.db.scalar("SELECT status FROM sync_events") == "paused"
    coordinator.resume()
    coordinator.sync_account("account-1", provider, since=None)
    assert provider.visited == [None, None, "second"]
    assert repository.db.scalar("SELECT count(*) FROM messages") == 6
    assert repository.db.scalar("SELECT sync_cursor FROM accounts") == "finished"


def test_main_inbox_scan_items_survive_resume_until_scope_is_applied(
    repository: MailRepository,
) -> None:
    def message(provider_id: str) -> MessagePayload:
        return MessagePayload(
            provider_id=provider_id,
            provider_thread_id=provider_id,
            subject=provider_id,
            sender_email="person@example.net",
            to=["owner@example.com"],
            sent_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            body_text="Focused message",
            labels=["INBOX"],
        )

    first = message("focused-one")
    second = message("focused-two")
    repository.ingest_message("account-1", first)
    repository.ingest_message("account-1", second)

    repository.begin_main_inbox_scope_scan("account-1", resume=False)
    repository.capture_main_inbox_page("account-1", [first])
    repository.begin_main_inbox_scope_scan("account-1", resume=True)
    repository.capture_main_inbox_page("account-1", [second])

    assert repository.main_inbox_scan_count("account-1") == 2
    assert repository.apply_main_inbox_scope("account-1") == 2
    assert repository.main_inbox_scan_count("account-1") == 0
