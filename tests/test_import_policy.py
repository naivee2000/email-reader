from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from email_reader.contracts import MessagePayload, ProviderKind
from email_reader.providers.apple_mail import AppleMailAccountInfo, AppleMailProvider
from email_reader.providers.gmail import GmailProvider
from email_reader.sync import SyncCoordinator


def provider():
    info = AppleMailAccountInfo("Fictional", "owner@gmail.com", ProviderKind.GMAIL,
        ("INBOX", "Sent", "Drafts"))
    return AppleMailProvider("account-1", info)


def row(identifier="message@example.net", *, category=None):
    mail = EmailMessage()
    mail["Message-ID"] = f"<{identifier}>"
    mail["From"] = "sender@example.net"
    mail["To"] = "owner@gmail.com"
    if category:
        mail["X-Gmail-Labels"] = f"Inbox,Category {category}"
    mail.set_content("A fictional original.")
    return {"internalId": "123", "messageId": identifier, "source": mail.as_string(),
        "unread": False, "received": datetime.now(UTC).isoformat()}


@pytest.mark.parametrize("category", ["Social", "Forums", "Updates", None])
def test_apple_mail_retains_all_nonpromotion_categories(category):
    message = provider()._message(row(category=category), "INBOX")
    assert message is not None and message.source_complete


@pytest.mark.parametrize("folder", ["Sent", "Drafts"])
def test_categories_never_exclude_own_sent_or_drafts(folder):
    assert provider()._message(row(category="Promotions"), folder) is not None
    role = "SENT" if folder == "Sent" else "DRAFT"
    assert GmailProvider._in_import_scope([role, "CATEGORY_PROMOTIONS"])


@pytest.mark.parametrize("category", ["CATEGORY_SOCIAL", "CATEGORY_FORUMS", "CATEGORY_UPDATES"])
def test_gmail_keeps_social_forums_and_updates(category):
    assert GmailProvider._in_import_scope(["INBOX", category])
    assert GmailProvider._visible_inbox(["INBOX", category])
    assert not GmailProvider._in_import_scope(["ARCHIVE", category])


def test_known_metadata_is_captured_without_downloading_original(monkeypatch):
    mail = provider()
    mail._mailbox_names = ["INBOX"]
    mail.set_known_message_ids({"message@example.net"})
    monkeypatch.setattr(mail, "_fetch_mailbox_metadata", lambda *args: {
        "rows": [row()], "total": 1, "examined": 1})
    monkeypatch.setattr(mail, "_fetch_mailbox_sources",
        lambda *args, **kwargs: pytest.fail("Known originals must not be downloaded"))
    page = mail.incremental_sync(datetime.now(UTC).isoformat())
    assert not page.messages
    assert page.inbox_scope_ids == ["message@example.net"]


def test_repair_restores_cached_inbox_visibility_without_redownloading_or_resetting_read(repository, monkeypatch):
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="message@example.net", provider_thread_id="thread", labels=["INBOX"],
        body_text="Keep the cached body.", raw_source=b"Fictional original",
    ))
    repository.db.execute("UPDATE messages SET in_main_inbox=0,local_unread=0 WHERE id=?", (message_id,))
    before = repository.db.scalar("SELECT raw_digest FROM messages WHERE id=?", (message_id,))
    mail = provider()
    mail._mailbox_names = ["INBOX"]
    monkeypatch.setattr(mail, "_fetch_mailbox_metadata", lambda *args: {
        "rows": [row()], "total": 1, "examined": 1})
    monkeypatch.setattr(mail, "_fetch_mailbox_sources",
        lambda *args, **kwargs: pytest.fail("Cached repair must not redownload the original"))
    result = SyncCoordinator(repository).sync_account_step("account-1", mail, since=None, job_kind="repair")
    assert result.complete and result.processed == 0
    saved = repository.db.query("SELECT * FROM messages WHERE id=?", (message_id,))[0]
    assert saved["in_main_inbox"] == 1 and saved["local_unread"] == 0
    assert saved["raw_digest"] == before and saved["body_text"] == "Keep the cached body."


def test_recent_refresh_respects_original_retry_backoff(monkeypatch):
    mail = provider()
    mail.set_deferred_source_ids({"message@example.net"})
    monkeypatch.setattr(mail, "_fetch_mailbox_metadata", lambda *args: {
        "rows": [row()], "total": 1, "examined": 1})
    monkeypatch.setattr(mail, "_fetch_mailbox_sources",
        lambda *args, **kwargs: pytest.fail("Original retry is not due"))
    result = mail._fetch_mailbox("INBOX", 0, 10, datetime.now(UTC) - timedelta(days=7))
    assert result["rows"][0]["unchanged"] is True


def test_no_old_date_row_terminates_later_recent_mail(monkeypatch):
    mail = provider()
    rows = [row("old@example.net"), row("recent@example.net")]
    rows[0]["received"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    monkeypatch.setattr(mail, "_fetch_mailbox_metadata", lambda *args: {
        "rows": rows, "total": 300, "examined": 2, "reachedSince": True})
    requested = []
    def sources(*args, rows):
        requested.extend(item["messageId"] for item in rows)
        return [item["source"] for item in rows]
    monkeypatch.setattr(mail, "_fetch_mailbox_sources", sources)
    result = mail._fetch_mailbox("INBOX", 0, 10, datetime.now(UTC) - timedelta(days=7))
    assert requested == ["recent@example.net"]
    assert [item["messageId"] for item in result["rows"]] == requested
    assert result["reached_since"] is False
    script = mail._metadata_script("INBOX", 0, 10, datetime.now(UTC))
    assert "set sourceCount to i - 1" not in script


def test_unknown_original_budget_yields_without_skipping_large_metadata_page(monkeypatch):
    mail = provider()
    mail._mailbox_names = ["INBOX"]
    rows = [{**row(f"new-{index}@example.net"), "internalId": str(index + 1)} for index in range(80)]
    monkeypatch.setattr(mail, "_fetch_mailbox_metadata", lambda *args: {
        "rows": rows, "total": 80, "examined": 80})
    calls = []
    def sources(*args, rows):
        calls.append([item["messageId"] for item in rows])
        return [item["source"] for item in rows]
    monkeypatch.setattr(mail, "_fetch_mailbox_sources", sources)
    batch = mail._fetch_mailbox("INBOX", 0, 80, None)
    assert len(calls) == 1 and len(calls[0]) == 10
    assert batch["examined"] == 10 and len(batch["rows"]) == 10
    assert batch["total"] == 80 and not batch["reached_since"]


def test_budgeted_page_overlap_imports_every_original_once(repository, monkeypatch):
    mail = provider()
    mail._mailbox_names = ["INBOX"]
    rows = [{**row(f"new-{index}@example.net"), "internalId": str(index + 1)} for index in range(80)]
    def metadata(mailbox, offset, limit, since):
        batch = [dict(item) for item in rows[offset:offset + limit]]
        return {"rows": batch, "total": len(rows), "examined": len(batch)}
    monkeypatch.setattr(mail, "_fetch_mailbox_metadata", metadata)
    fetched = []
    def sources(*args, rows):
        fetched.extend(item["messageId"] for item in rows)
        return [item["source"] for item in rows]
    monkeypatch.setattr(mail, "_fetch_mailbox_sources", sources)
    coordinator = SyncCoordinator(repository)
    for _ in range(12):
        if coordinator.sync_account_step("account-1", mail, since=None).complete:
            break
    else:
        pytest.fail("Bounded pages did not finish")
    expected = {item["messageId"] for item in rows}
    assert set(fetched) == expected and len(fetched) == len(expected)
    assert repository.complete_message_ids("account-1") == expected
    assert repository.db.scalar("SELECT sync_page_token FROM accounts") is None


def test_all_history_missing_original_keeps_retryable_metadata(monkeypatch):
    mail = provider()
    mail._mailbox_names = ["INBOX"]
    metadata = {**row(), "source": "", "subject": "Still downloading"}
    monkeypatch.setattr("email_reader.providers.apple_mail._run_mail_script",
        lambda *args, **kwargs: {"rows": [metadata], "total": 1, "examined": 1})
    monkeypatch.setattr(mail, "_fetch_mailbox_sources", lambda *args, **kwargs: [""])
    page = mail.initial_sync(since=None)
    assert len(page.messages) == 1
    assert page.messages[0].source_complete is False
    assert page.messages[0].subject == "Still downloading"
    assert page.next_cursor is not None


def test_last_promotion_page_keeps_auditable_decision(monkeypatch):
    mail = provider()
    mail._mailbox_names = ["INBOX"]
    monkeypatch.setattr(mail, "_fetch_mailbox", lambda *args: {
        "rows": [row(category="Promotions")], "total": 1, "examined": 1,
        "reached_since": False})
    page = mail.initial_sync(since=None)
    assert not page.messages and not page.inbox_scope_ids
    assert page.exclusions == {"message@example.net": "promotions"}


def test_recent_explicit_promotion_cache_avoids_repeated_source_download(monkeypatch):
    mail = provider()
    mail.set_excluded_message_ids({"message@example.net"})
    monkeypatch.setattr(mail, "_fetch_mailbox_metadata", lambda *args: {
        "rows": [row(category="Promotions")], "total": 1, "examined": 1})
    monkeypatch.setattr(mail, "_fetch_mailbox_sources",
        lambda *args, **kwargs: pytest.fail("Known promotion need not be downloaded again today"))
    assert mail._fetch_mailbox("INBOX", 0, 10, None)["rows"] == []


def test_recorded_promotion_is_not_downloaded_or_extracted_on_repeat_refresh(repository, monkeypatch):
    mail = provider()
    mail._mailbox_names = ["INBOX"]
    requested = []
    monkeypatch.setattr(mail, "_fetch_mailbox_metadata", lambda *args: {
        "rows": [row(category="Promotions")], "total": 1, "examined": 1})
    def fetch(*args, rows):
        requested.extend(item["messageId"] for item in rows)
        return [item["source"] for item in rows]
    monkeypatch.setattr(mail, "_fetch_mailbox_sources", fetch)
    monkeypatch.setattr(repository.extractor, "extract",
        lambda *args: pytest.fail("An excluded original must not be extracted"))
    coordinator = SyncCoordinator(repository)
    since = datetime.now(UTC) - timedelta(days=7)
    for _ in range(6):
        coordinator.sync_account_step("account-1", mail, since=since, job_kind="freshness")
    assert requested == ["message@example.net"]
    assert repository.db.scalar("SELECT reason FROM import_exclusions") == "promotions"
    assert repository.db.scalar("SELECT count(*) FROM messages") == 0


def test_originals_are_fetched_using_captured_identity_not_shifting_offset(monkeypatch):
    scripts = []
    monkeypatch.setattr("email_reader.providers.apple_mail._run_mail_script",
        lambda script, **kwargs: scripts.append(script) or ["source"])
    assert provider()._fetch_mailbox_sources("INBOX", 99, 1, rows=[row()]) == ["source"]
    assert "whose id is 123" in scripts[0]
    assert "source of messages startIndex thru endIndex" not in scripts[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript compiler is macOS-only")
def test_identity_source_script_compiles_without_running_mail_commands(monkeypatch, tmp_path):
    scripts = []
    monkeypatch.setattr("email_reader.providers.apple_mail._run_mail_script",
        lambda script, **kwargs: scripts.append(script) or [])
    provider()._fetch_mailbox_sources("INBOX", 99, 1, rows=[row()])
    result = subprocess.run(["/usr/bin/osacompile", "-o", str(tmp_path / "identity.scpt"), "-"],
        input=scripts[0], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
