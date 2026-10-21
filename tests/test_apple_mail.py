from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from email_reader.contracts import DraftRequest, ProviderKind
from email_reader.providers.apple_mail import (
    PAGE_SIZE,
    AppleMailAccountInfo,
    AppleMailProvider,
    _run_mail_script,
    _run_osascript,
    cached_apple_mail_accounts,
    discover_apple_mail_accounts,
    incremental_cutoff,
)
from email_reader.providers.base import ProviderOperationError


def test_incremental_catchup_rechecks_late_downloads_without_losing_old_cursor():
    now = datetime.now(UTC)
    assert incremental_cutoff(now.isoformat()) <= now - timedelta(days=7)
    assert incremental_cutoff("invalid") <= now - timedelta(days=7)
    assert incremental_cutoff((now + timedelta(days=2)).isoformat()) <= now - timedelta(days=7)
    old = now - timedelta(days=30)
    assert incremental_cutoff(old.isoformat()) == old - timedelta(minutes=5)
    assert incremental_cutoff(now.isoformat()) == incremental_cutoff(now.isoformat())


@pytest.mark.parametrize("header_age", [1, 30])
def test_late_download_and_old_sender_date_are_imported(monkeypatch, header_age):
    now = datetime.now(UTC)
    received = now - timedelta(days=1)
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL, ("INBOX",))
    provider = AppleMailProvider(info.account_id, info)
    mail = EmailMessage()
    mail["Message-ID"] = "<late@example.net>"
    mail["From"] = "friend@example.net"
    mail["Subject"] = "Meeting notes"
    mail["Date"] = format_datetime(now - timedelta(days=header_age))
    mail.set_content("A delayed original, not a new delivery.")
    row = {"internalId": "123", "messageId": "late@example.net", "unread": True,
           "received": received.isoformat(), "source": mail.as_string(),
           "sourcePrefetchAttempted": True}

    def metadata(mailbox, offset, limit, cutoff):
        assert cutoff < received
        return {"rows": [row], "total": 1, "examined": 1, "reachedSince": False}

    monkeypatch.setattr(provider, "_fetch_mailbox_metadata", metadata)
    page = provider.incremental_sync(now.isoformat())
    assert len(page.messages) == 1
    assert page.messages[0].provider_id == "late@example.net"
    assert page.messages[0].received_at == received


def test_recent_window_only_downloads_unknown_originals(monkeypatch):
    now = datetime.now(UTC)
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL, ("INBOX",))
    provider = AppleMailProvider(info.account_id, info)
    provider.set_known_message_ids({"known@example.net"})
    monkeypatch.setattr(provider, "_fetch_mailbox_metadata", lambda *args: {
        "rows": [{"internalId": "1", "messageId": "known@example.net", "unread": False,
                  "received": now.isoformat(), "source": ""}],
        "total": 1, "examined": 1, "reachedSince": False,
    })
    monkeypatch.setattr(provider, "_fetch_mailbox_sources", lambda *args: pytest.fail("Known original re-downloaded"))
    page = provider.incremental_sync(now.isoformat())
    assert not page.messages
    assert page.unchanged_read_states == {"known@example.net": False}


def test_metadata_timeout_yields_once_without_advancing(monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL, ("INBOX",))
    provider = AppleMailProvider(info.account_id, info)
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("Apple Mail timed out during incremental_metadata")
        return {"rows": [], "total": 0}

    monkeypatch.setattr("email_reader.providers.apple_mail._run_mail_script", run)
    with pytest.raises(RuntimeError, match="incremental_metadata"):
        provider._fetch_mailbox_metadata("INBOX", 0, 10, datetime.now(UTC))
    assert len(calls) == 1

    calls.clear()
    def always_timeout(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("Apple Mail timed out during incremental_metadata")
    monkeypatch.setattr("email_reader.providers.apple_mail._run_mail_script", always_timeout)
    with pytest.raises(RuntimeError, match="incremental_metadata"):
        provider._fetch_mailbox_metadata("INBOX", 0, 10, datetime.now(UTC))
    assert len(calls) == 1


def test_metadata_batch_timeout_falls_back_to_individual_pages(monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL, ("INBOX",))
    provider = AppleMailProvider(info.account_id, info)
    def timeout(*args, **kwargs):
        raise RuntimeError("Apple Mail timed out during batched_metadata")
    monkeypatch.setattr("email_reader.providers.apple_mail._run_mail_script", timeout)
    AppleMailProvider.prefetch_recent_metadata([(provider, datetime.now(UTC).isoformat())])
    assert provider._metadata_cache == {}


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript compiler is macOS-only")
def test_metadata_batch_script_compiles_and_reads_fields_in_bulk(tmp_path, monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL,
                                ("INBOX", "Sent Mail", "Drafts"))
    provider = AppleMailProvider(info.account_id, info)
    scripts = [provider._metadata_script("INBOX", 0, 80, datetime.now(UTC))]
    monkeypatch.setenv("EMAIL_READER_FILTERED_DATE_PROBE", "1")
    scripts.append(provider._metadata_script("INBOX", 0, 10, datetime.now(UTC)))
    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript",
                        lambda script, **kwargs: scripts.append(script) or {})
    AppleMailProvider.prefetch_recent_metadata([(provider, datetime.now(UTC).isoformat())])
    for index, script in enumerate(scripts):
        result = subprocess.run(["/usr/bin/osacompile", "-o", str(tmp_path / f"metadata-{index}.scpt"), "-"],
            input=script, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "set internalIds to id of messages startIndex thru sourceEndIndex" in script
        assert "repeat with currentMessage" not in script
        assert "NSDate's |date|()" in script


def test_filtered_date_probe_is_default_off_inbox_only_and_preserves_bounded_fallback(monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL,
                                ("INBOX", "Sent Mail", "Drafts"))
    provider = AppleMailProvider(info.account_id, info)
    cutoff = datetime(2026, 9, 27, 12, 30, 1, 999999, tzinfo=UTC)
    normal = provider._metadata_script("INBOX", 0, 10, cutoff)
    assert "set filteredDateProbe to false" in normal
    monkeypatch.setenv("EMAIL_READER_FILTERED_DATE_PROBE", "1")
    candidate = provider._metadata_script("INBOX", 0, 10, cutoff)
    assert "set filteredDateProbe to true" in candidate
    assert f"dateWithTimeIntervalSince1970:{int(cutoff.timestamp() // 1)}" in candidate
    assert "exists (messages of targetMailbox whose date received ≥ cutoffDate)" in candidate
    assert "set receivedDates to date received of messages startIndex thru endIndex" in candidate
    assert "set allowFastPath to false" in provider._metadata_script("INBOX", 10, 10, cutoff)
    for mailbox in ("Sent Mail", "Drafts"):
        assert "set filteredDateProbe to false" in provider._metadata_script(mailbox, 0, 10, cutoff)


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScriptObjC is macOS-only")
@pytest.mark.parametrize("timestamp", [-1, 0, 1790512201])
def test_foundation_cutoff_date_coercion_preserves_seconds_without_mail(timestamp):
    script = f'''use framework "Foundation"
set cutoffDate to (current application's NSDate's dateWithTimeIntervalSince1970:{timestamp}) as date
set nextDate to (current application's NSDate's dateWithTimeIntervalSince1970:{timestamp + 1}) as date
return nextDate - cutoffDate'''
    result = subprocess.run(["/usr/bin/osascript", "-l", "AppleScript", "-"],
                            input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScriptObjC is macOS-only")
def test_high_resolution_metadata_clock_runs_without_mail():
    result = subprocess.run(
        [
            "/usr/bin/osascript", "-l", "AppleScript",
            "-e", 'use framework "Foundation"',
            "-e", "set startedAt to current application's NSDate's |date|()",
            "-e", "set finishedAt to current application's NSDate's |date|()",
            "-e", "(finishedAt's timeIntervalSinceDate:startedAt) as real",
        ],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) >= 0


def test_mail_timeout_reports_only_fixed_operation(monkeypatch):
    def timed_out(script, *, timeout):
        raise RuntimeError("Apple Mail did not respond before the operation timed out")

    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript", timed_out)
    with pytest.raises(RuntimeError, match="Apple Mail timed out during incremental_metadata"):
        _run_mail_script("private script text", timeout=30, operation="incremental_metadata")
    with pytest.raises(ValueError, match="Unknown Mail operation"):
        _run_mail_script("private script text", timeout=30, operation="private script text")


@pytest.mark.parametrize("error", [
    "Mail got an error: AppleEvent timed out. (-1712) private message body",
    "execution error: private message body (-1712)",
])
def test_native_mail_timeout_is_sanitized(monkeypatch, error):
    monkeypatch.setattr("email_reader.providers.apple_mail.process_launch.run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1, "", error))
    with pytest.raises(RuntimeError) as caught:
        _run_mail_script("private script text", timeout=30, operation="incremental_metadata")
    assert str(caught.value) == "Apple Mail timed out during incremental_metadata"


def test_mail_permission_error_remains_distinct_and_sanitized(monkeypatch):
    from email_reader.providers.base import AuthenticationError

    monkeypatch.setattr("email_reader.providers.apple_mail.process_launch.run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1, "", "private data (-1743)"))
    with pytest.raises(AuthenticationError) as caught:
        _run_osascript("AppleScript", "private script text", timeout=30)
    assert str(caught.value) == (
        "Allow Email Reader to control Mail in System Settings → Privacy & Security → Automation"
    )


def test_first_message_shortcut_is_disabled_for_every_mailbox(monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL,
                                ("INBOX", "Sent Mail", "Drafts"))
    provider = AppleMailProvider(info.account_id, info)
    since = datetime.now(UTC) - timedelta(minutes=5)
    assert "set allowFastPath to false" in provider._metadata_script("INBOX", 0, 10, since)
    assert "set allowFastPath to false" in provider._metadata_script("Sent Mail", 0, 10, since)
    assert "set allowFastPath to false" in provider._metadata_script("Drafts", 0, 10, since)
    assert "set allowFastPath to false" in provider._metadata_script("INBOX", 10, 10, since)

    seen = []

    def fetch(mailbox, offset, limit, cutoff):
        seen.append((mailbox, offset, limit))
        if mailbox in {"INBOX", "Sent Mail"}:
            return {"rows": [], "total": 1, "examined": 1, "reached_since": True}
        return {"rows": [], "total": 0, "examined": 0, "reached_since": True}

    monkeypatch.setattr(provider, "_fetch_mailbox", fetch)
    page = provider.incremental_sync(datetime.now(UTC).isoformat())
    while page.next_page_token:
        page = provider.initial_sync(since=since, page_token=page.next_page_token)
    assert page.next_cursor is not None
    assert [name for name, _, _ in seen] == ["INBOX", "Sent Mail", "Drafts"]


def test_incremental_pages_keep_original_cutoff_and_resume_in_small_batches(monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL, ("INBOX",))
    provider = AppleMailProvider(info.account_id, info)
    seen = []

    def page(mailbox, offset, limit, since):
        seen.append((offset, limit, since))
        count = min(limit, 25 - offset)
        return {"rows": [{"internalId": str(offset + i),
                          "messageId": f"known-{offset + i}@example.com", "unread": False,
                          "unchanged": True} for i in range(count)],
                "total": 25, "examined": count, "reached_since": False}

    monkeypatch.setattr(provider, "_fetch_mailbox", page)
    cursor = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    first = provider.incremental_sync(cursor)
    second = provider.initial_sync(since=datetime(2020, 1, 1, tzinfo=UTC),
                                   page_token=first.next_page_token)
    third = provider.initial_sync(since=datetime(2020, 1, 1, tzinfo=UTC),
                                  page_token=second.next_page_token)
    assert [(offset, limit) for offset, limit, _ in seen] == [(0, 10), (8, 10), (16, 10)]
    assert all(abs((cutoff - seen[0][2]).total_seconds()) < 1 for _, _, cutoff in seen)
    assert len(first.unchanged_read_states) == 10
    assert len(second.unchanged_read_states) == 10
    assert len(third.unchanged_read_states) == 9
    assert third.next_page_token is None
    assert third.next_cursor == json.loads(first.next_page_token)["started_at"]


def test_long_offline_gap_skips_unbounded_prefetch(monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL, ("INBOX",))
    provider = AppleMailProvider(info.account_id, info)
    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no bulk Mail query")))
    AppleMailProvider.prefetch_recent_metadata(
        [(provider, (datetime.now(UTC) - timedelta(days=10)).isoformat())]
    )


def test_batched_draft_sources_preserve_account_identity_and_edited_old_drafts(monkeypatch):
    providers = [AppleMailProvider(f"account-{index}", AppleMailAccountInfo(
        f"Account {index}", f"owner{index}@example.com", kind, ("INBOX", "Sent Mail", "Drafts"),
    )) for index, kind in enumerate((ProviderKind.GMAIL, ProviderKind.OUTLOOK), 1)]
    cursor = datetime.now(UTC).isoformat()
    results = {}
    for index, provider in enumerate(providers):
        for position, mailbox in enumerate(provider._mailbox_names):
            rows = []
            if provider._mailbox_role(mailbox) == "drafts":
                source = EmailMessage()
                source["Message-ID"] = f"<draft-{index}@example.com>"
                source["Date"] = format_datetime(datetime.now(UTC) - timedelta(days=90))
                source["From"] = provider.info.email
                source["To"] = "recipient@example.net"
                source.set_content(f"Edited body from account {index}")
                rows = [{"internalId": str(index), "messageId": f"draft-{index}@example.com",
                         "sourcePrefetchAttempted": True, "prefetchedSource": source.as_string(),
                         "unread": False, "junk": False}]
            results[str(index * 3 + position)] = {"rows": rows, "examined": len(rows),
                                                 "total": len(rows), "reachedSince": False}
    calls = []
    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript",
                        lambda script, **kwargs: calls.append(script) or results)
    AppleMailProvider.prefetch_recent_metadata(
        [(provider, cursor) for provider in providers], prefetch_draft_sources=True,
    )
    assert len(calls) == 1
    assert calls[0].count("set prefetchDraftSources to true") == 2
    assert calls[0].count("set prefetchDraftSources to false") == 4
    assert "with timeout of 8 seconds" in calls[0]
    for index, provider in enumerate(providers):
        monkeypatch.setattr(provider, "_fetch_mailbox_sources", lambda *_:
                            pytest.fail("prefetched sources must not be downloaded twice"))
        page = provider.incremental_sync(cursor)
        while page.next_page_token and not page.messages:
            page = provider.initial_sync(since=None, page_token=page.next_page_token)
        assert len(page.messages) == 1
        assert page.messages[0].body_text.strip() == f"Edited body from account {index}"
        assert page.messages[0].sender_email == provider.info.email
        assert page.messages[0].is_draft and page.messages[0].source_complete
    assert len(calls) == 1


def test_failed_draft_source_prefetch_is_deferred_not_repeated_or_marked_complete(monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL, ("Drafts",))
    provider = AppleMailProvider(info.account_id, info)
    cutoff = datetime.now(UTC)
    provider._metadata_cache[("Drafts", 0, 10, cutoff.isoformat())] = {
        "rows": [{"internalId": "501", "messageId": "old-draft@example.com",
                  "sourcePrefetchAttempted": True, "prefetchedSource": "",
                  "unread": False, "junk": False}],
        "total": 1, "examined": 1, "reachedSince": False,
    }
    monkeypatch.setattr(provider, "_fetch_mailbox_sources", lambda *_:
                        pytest.fail("failed source must enter durable recovery, not retry immediately"))
    batch = provider._fetch_mailbox("Drafts", 0, 10, cutoff)
    payload = provider._message(batch["rows"][0], "Drafts")
    assert payload is not None and payload.is_draft and not payload.source_complete
    assert payload.retry_locator["role"] == "drafts"


def test_draft_prefetch_cannot_be_enabled_for_known_non_draft_folders():
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL,
                                ("INBOX", "Sent", "Drafts"))
    provider = AppleMailProvider(info.account_id, info)
    for mailbox in ("INBOX", "Sent"):
        script = provider._metadata_script(mailbox, 0, 10, datetime.now(UTC),
                                           prefetch_draft_sources=True)
        assert "set prefetchDraftSources to false" in script


def test_normal_metadata_batch_does_not_enable_draft_source_prefetch(monkeypatch):
    info = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL,
                                ("INBOX", "Sent Mail", "Drafts"))
    provider = AppleMailProvider(info.account_id, info)
    scripts = []
    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript",
                        lambda script, **kwargs: scripts.append(script) or {})
    AppleMailProvider.prefetch_recent_metadata([(provider, datetime.now(UTC).isoformat())])
    assert len(scripts) == 1
    assert "set prefetchDraftSources to true" not in scripts[0]
    assert scripts[0].count("set prefetchDraftSources to false") == 3


def test_discovers_enabled_apple_mail_accounts(monkeypatch) -> None:
    monkeypatch.setattr(
        "email_reader.providers.apple_mail._run_jxa",
        lambda script: [
            {
                "name": "Personal",
                "emails": ["person@gmail.com"],
                "enabled": True,
                "mailboxes": ["INBOX", "All Mail"],
            },
            {
                "name": "Work",
                "emails": ["person@example.edu"],
                "enabled": True,
                "mailboxes": ["Inbox", "Sent Items"],
            },
            {"name": "Off", "emails": ["off@example.net"], "enabled": False},
        ],
    )

    accounts = discover_apple_mail_accounts()

    assert [item.email for item in accounts] == ["person@gmail.com", "person@example.edu"]
    assert accounts[0].provider == ProviderKind.GMAIL
    assert accounts[1].provider == ProviderKind.OUTLOOK
    assert accounts[0].account_id.startswith("apple-mail:")


def test_encrypted_discovery_cache_reuses_and_expires_mailbox_names(monkeypatch):
    class SettingsDatabase:
        value = ""

        def scalar(self, _query, _parameters, default=""):
            return self.value or default

        def execute(self, _query, parameters):
            self.value = parameters[1]

    database = SettingsDatabase()
    account = AppleMailAccountInfo("Personal", "person@gmail.com", ProviderKind.GMAIL,
                                   ("INBOX", "Sent Mail", "Drafts"))
    seen = []

    def discover():
        seen.append(True)
        return [account]

    monkeypatch.setattr("email_reader.providers.apple_mail.discover_apple_mail_accounts", discover)
    first, was_cached = cached_apple_mail_accounts(database, {account.email})
    second, was_cached_again = cached_apple_mail_accounts(database, {account.email})
    assert not was_cached and was_cached_again
    assert first == second == {account.email: account}
    assert len(seen) == 1

    old = json.loads(database.value)
    old["checked_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    database.value = json.dumps(old)
    _, was_cached_after_expiry = cached_apple_mail_accounts(database, {account.email})
    assert not was_cached_after_expiry and len(seen) == 2

    _, was_cached_when_account_missing = cached_apple_mail_accounts(
        database, {account.email, "missing@example.com"}
    )
    assert not was_cached_when_account_missing and len(seen) == 3


def test_apple_mail_page_parses_source_and_attachments(monkeypatch) -> None:
    source = EmailMessage()
    source["Message-ID"] = "<message-1@example.com>"
    source["From"] = "Owner <person@gmail.com>"
    source["To"] = "friend@example.net"
    source["Subject"] = "Local draft context"
    source["Date"] = "Mon, 1 Sep 2026 10:00:00 -0400"
    source.set_content("The signed form is attached.")
    source.add_attachment(b"signed", maintype="application", subtype="pdf", filename="form.pdf")
    info = AppleMailAccountInfo(
        account_name="Personal",
        email="person@gmail.com",
        provider=ProviderKind.GMAIL,
        mailboxes=("INBOX", "All Mail", "Sent Mail", "Sent Messages"),
    )
    provider = AppleMailProvider(info.account_id, info)
    monkeypatch.setattr(
        provider,
        "_fetch_mailbox",
        lambda mailbox, offset, limit, since: {
            "rows": [
                {
                    "internalId": "42",
                    "messageId": "message-1@example.com",
                    "received": "2026-09-01T14:00:00Z",
                    "unread": True,
                    "source": source.as_string(),
                }
            ],
            "total": 1,
            "examined": 1,
            "reached_since": False,
        },
    )

    page = provider.initial_sync(since=None)

    token = json.loads(page.next_page_token)
    assert token["mailbox"] == 1 and token["offset"] == 0
    assert token["started_at"]
    assert page.messages[0].provider_id == "message-1@example.com"
    assert page.messages[0].is_sent is True
    assert page.messages[0].is_unread is True
    assert "INBOX" in page.messages[0].labels
    assert page.messages[0].attachments[0].filename == "form.pdf"
    assert page.messages[0].attachments[0].content == b"signed"
    assert provider.inbox_provider_ids == {"message-1@example.com"}


def test_apple_mail_uses_inbox_sent_and_drafts_but_not_all_mail() -> None:
    info = AppleMailAccountInfo(
        account_name="Personal",
        email="person@gmail.com",
        provider=ProviderKind.GMAIL,
        mailboxes=(
            "Important",
            "All Mail",
            "Drafts",
            "Sent Mail",
            "Sent Messages",
            "INBOX",
        ),
    )

    provider = AppleMailProvider(info.account_id, info)

    assert provider._mailbox_names == ["INBOX", "Sent Mail", "Drafts"]
    assert provider.draft_page_token() == '{"mailbox":2,"offset":0}'
    assert PAGE_SIZE == 80
    assert 'mailbox "[Gmail]"' in provider._target_mailbox_assignment("Sent Mail")
    assert 'mailbox "[Gmail]"' in provider._target_mailbox_assignment("Drafts")
    assert 'mailbox "[Gmail]"' not in provider._target_mailbox_assignment("INBOX")


def test_apple_mail_keeps_updates_separate_from_primary(monkeypatch) -> None:
    source = EmailMessage()
    source["Message-ID"] = "<update@example.com>"
    source["From"] = "Security <security@example.com>"
    source["To"] = "person@gmail.com"
    source["Subject"] = "Security alert"
    source["Date"] = "Mon, 1 Sep 2026 10:00:00 -0400"
    source["X-Gmail-Labels"] = "Inbox,Category Updates"
    source.set_content("A new sign-in was detected.")
    info = AppleMailAccountInfo(
        account_name="Personal",
        email="person@gmail.com",
        provider=ProviderKind.GMAIL,
        mailboxes=("INBOX",),
    )
    provider = AppleMailProvider(info.account_id, info)
    monkeypatch.setattr(
        provider,
        "_fetch_mailbox",
        lambda mailbox, offset, limit, since: {
            "rows": [
                {
                    "internalId": "100",
                    "messageId": "update@example.com",
                    "unread": True,
                    "source": source.as_string(),
                }
            ],
            "total": 1,
            "examined": 1,
            "reached_since": False,
        },
    )

    message = provider.initial_sync(since=None).messages[0]

    assert "INBOX" in message.labels
    assert "CATEGORY_UPDATES" in message.labels


def test_apple_mail_fetches_page_properties_in_bulk(monkeypatch) -> None:
    source = EmailMessage()
    source["Message-ID"] = "<bulk@example.com>"
    source["Date"] = "Mon, 1 Sep 2026 10:00:00 -0400"
    source.set_content("Bulk Apple event test")
    scripts: list[str] = []

    def run(script: str, **_kwargs):
        scripts.append(script)
        return {
            "rows": [
                {
                    "internalId": "101",
                    "messageId": "bulk@example.com",
                    "unread": False,
                    "junk": False,
                    "source": source.as_string(),
                }
            ],
            "total": 1,
        }

    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript", run)
    info = AppleMailAccountInfo(
        account_name="Personal",
        email="person@gmail.com",
        provider=ProviderKind.GMAIL,
        mailboxes=("INBOX",),
    )

    batch = AppleMailProvider(info.account_id, info)._fetch_mailbox("INBOX", 0, 80, None)

    assert batch["examined"] == 1
    assert "source of messages startIndex thru sourceEndIndex" in scripts[0]
    assert "repeat with i" in scripts[0]


def test_apple_mail_incremental_fetch_skips_old_message_sources(monkeypatch) -> None:
    scripts: list[str] = []

    def run(script: str, **_kwargs):
        scripts.append(script)
        return {
            "rows": [],
            "total": 500,
            "examined": 80,
            "reachedSince": True,
        }

    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript", run)
    info = AppleMailAccountInfo(
        account_name="Personal",
        email="person@gmail.com",
        provider=ProviderKind.GMAIL,
        mailboxes=("INBOX",),
    )
    since = datetime.fromisoformat("2026-09-03T03:00:00+00:00")

    batch = AppleMailProvider(info.account_id, info)._fetch_mailbox("INBOX", 0, 80, since)

    assert batch == {
        "rows": [],
        "total": 500,
        "examined": 80,
        "reached_since": False,
    }
    assert 'set cutoffText to "2026-09-03T03:00:00Z"' in scripts[0]
    assert "set reachedSince to true" in scripts[0]


def test_apple_mail_incremental_fetch_falls_back_to_fast_metadata(monkeypatch) -> None:
    calls: list[int] = []

    def run(_script: str, *, timeout: int):
        calls.append(timeout)
        if len(calls) == 2:
            raise ProviderOperationError("source is still downloading")
        return {
            "rows": [
                {
                    "internalId": "501",
                    "messageId": "deferred@example.com",
                    "subject": "New message",
                    "sender": "Sender <sender@example.com>",
                    "received": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "unread": True,
                    "junk": False,
                }
            ],
            "total": 500,
            "examined": 80,
            "reachedSince": True,
        }

    monkeypatch.setattr("email_reader.providers.apple_mail._run_applescript", run)
    info = AppleMailAccountInfo(
        account_name="Personal",
        email="person@gmail.com",
        provider=ProviderKind.GMAIL,
        mailboxes=("INBOX",),
    )

    batch = AppleMailProvider(info.account_id, info)._fetch_mailbox(
        "INBOX", 0, 80, datetime.now(UTC) - timedelta(hours=1)
    )

    assert calls == [30, 8]
    assert batch["examined"] == 80
    assert batch["reached_since"] is False
    assert "X-Email-Reader-Deferred-Source: true" in batch["rows"][0]["source"]


def test_known_received_mail_never_fetches_source_but_drafts_are_checked(monkeypatch):
    info = AppleMailAccountInfo(
        account_name="Personal", email="person@gmail.com", provider=ProviderKind.GMAIL,
        mailboxes=("INBOX", "Sent", "Drafts"),
    )
    provider = AppleMailProvider(info.account_id, info)
    provider.set_known_message_ids({"known@example.com"})
    def metadata(*args):
        return {"rows": [{"internalId": "501", "messageId": "known@example.com",
            "subject": "Known", "sender": "sender@example.com",
            "received": datetime.now(UTC).isoformat(), "unread": False,
            "junk": False}], "total": 1, "examined": 1, "reachedSince": True}
    calls = []
    monkeypatch.setattr(provider, "_fetch_mailbox_metadata", metadata)
    monkeypatch.setattr(provider, "_fetch_mailbox_sources", lambda *args, **kwargs: calls.append(args) or [])
    since = datetime.now(UTC) - timedelta(minutes=5)
    for folder in ("INBOX", "Sent"):
        batch = provider._fetch_mailbox(folder, 0, 10, since)
        assert batch["rows"][0]["unchanged"]
    assert calls == []
    batch = provider._fetch_mailbox("INBOX", 0, 80, datetime.now(UTC) - timedelta(days=7))
    assert batch["rows"][0]["unchanged"] and calls == []
    provider._fetch_mailbox("Drafts", 0, 10, since)
    assert calls == [("Drafts", 0, 1)]


def test_old_draft_is_not_lost_to_incremental_date_cutoff(monkeypatch):
    info = AppleMailAccountInfo(
        account_name="Personal", email="person@gmail.com", provider=ProviderKind.GMAIL,
        mailboxes=("Drafts",),
    )
    provider = AppleMailProvider(info.account_id, info)
    old_date = datetime.now(UTC) - timedelta(days=90)
    source = EmailMessage()
    source["Message-ID"] = "<old-draft@example.com>"
    source["Date"] = format_datetime(old_date)
    source["From"] = "person@gmail.com"
    source["To"] = "recipient@example.net"
    source["Subject"] = "Old draft, edited today"
    source.set_content("The current edited Draft text")
    monkeypatch.setattr(provider, "_fetch_mailbox_metadata", lambda *_: {
        "rows": [{"internalId": "501", "messageId": "old-draft@example.com",
                  "received": old_date.isoformat(), "unread": False, "junk": False}],
        "total": 1, "examined": 1, "reachedSince": False,
    })
    monkeypatch.setattr(provider, "_fetch_mailbox_sources", lambda *_, **kwargs: [source.as_string()])
    cutoff = datetime.now(UTC) - timedelta(days=1)
    page = provider.initial_sync(since=cutoff)
    assert len(page.messages) == 1
    assert page.messages[0].provider_id == "old-draft@example.com"
    assert page.messages[0].body_text.strip() == "The current edited Draft text"
    assert 'set scanAllDates to true' in provider._metadata_script("Drafts", 0, 10, cutoff)
    assert 'set scanAllDates to false' in provider._metadata_script("INBOX", 0, 10, cutoff)


def test_source_identity_mismatch_becomes_retryable_without_wrong_bytes():
    info = AppleMailAccountInfo(account_name="Personal", email="person@gmail.com",
        provider=ProviderKind.GMAIL, mailboxes=("INBOX",))
    source = EmailMessage()
    source["Message-ID"] = "<arrived-between-reads@example.com>"
    source.set_content("This belongs to another message.")
    provider = AppleMailProvider(info.account_id, info)
    message = provider._message({"messageId": "expected@example.com", "internalId": "123", "source": source.as_string()}, "INBOX")
    assert message.provider_id == "expected@example.com"
    assert message.source_complete is False
    assert b"This belongs to another message" not in message.raw_source
    assert b"arrived-between-reads" not in message.raw_source


def test_apple_mail_keeps_social_subscriptions_in_main_inbox(monkeypatch) -> None:
    source = EmailMessage()
    source["Message-ID"] = "<subscription@example.com>"
    source["From"] = "Updates <updates@social.example>"
    source["To"] = "person@gmail.com"
    source["Subject"] = "Social digest"
    source["X-Gmail-Labels"] = "Inbox,Category Social"
    source["List-Unsubscribe"] = "<https://social.example/unsubscribe>"
    source.set_content("Here is your weekly digest.")
    info = AppleMailAccountInfo(
        account_name="Personal",
        email="person@gmail.com",
        provider=ProviderKind.GMAIL,
        mailboxes=("INBOX",),
    )
    provider = AppleMailProvider(info.account_id, info)
    monkeypatch.setattr(
        provider,
        "_fetch_mailbox",
        lambda mailbox, offset, limit, since: {
            "rows": [
                {
                    "internalId": "99",
                    "messageId": "subscription@example.com",
                    "received": "2026-09-01T14:00:00Z",
                    "unread": True,
                    "source": source.as_string(),
                }
            ],
            "total": 1,
            "examined": 1,
            "reached_since": False,
        },
    )

    page = provider.initial_sync(since=None)

    assert [item.provider_id for item in page.messages] == ["subscription@example.com"]
    assert "CATEGORY_SOCIAL" in page.messages[0].labels
    assert provider.inbox_provider_ids == {"subscription@example.com"}


def test_apple_mail_creates_draft_without_send(monkeypatch) -> None:
    info = AppleMailAccountInfo(
        account_name="Work",
        email="person@example.edu",
        provider=ProviderKind.OUTLOOK,
        mailboxes=("Inbox", "Sent Items"),
    )
    provider = AppleMailProvider(info.account_id, info)
    scripts: list[str] = []

    def run(script: str):
        scripts.append(script)
        return {"id": "draft-42"}

    monkeypatch.setattr("email_reader.providers.apple_mail._run_jxa", run)
    receipt = provider.create_draft(
        DraftRequest(subject="Review", body_text="Please review.", to=["friend@example.net"])
    )

    assert receipt.provider_draft_id == "draft-42"
    assert "Mail.save(draft)" in scripts[0]
    assert "Mail.send" not in scripts[0]


def test_apple_mail_draft_requires_confirmed_provider_id(monkeypatch) -> None:
    info = AppleMailAccountInfo(
        account_name="Work",
        email="person@example.edu",
        provider=ProviderKind.OUTLOOK,
        mailboxes=("Inbox", "Drafts"),
    )
    provider = AppleMailProvider(info.account_id, info)
    request = DraftRequest(subject="Review", body_text="Please review.", to=[])
    for reply in ({}, {"id": None}, {"id": "undefined"}, None):
        monkeypatch.setattr(
            "email_reader.providers.apple_mail._run_jxa",
            lambda _script, response=reply: response,
        )
        with pytest.raises(RuntimeError, match="check its Drafts folder before retrying"):
            provider.create_draft(request)
