from __future__ import annotations

import ast
import base64
import json
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest

from email_reader.contracts import DraftRequest, MessagePayload
from email_reader.providers.apple_mail import AppleMailAccountInfo, AppleMailProvider
from email_reader.providers.base import EmailProvider, SyncCursorExpiredError
from email_reader.providers.gmail import GmailProvider
from email_reader.providers.outlook import MESSAGE_FIELDS, OutlookProvider
from email_reader.security import MemoryVault


def test_apple_mail_routine_refresh_skips_drafts_but_keeps_inbox_and_sent(monkeypatch):
    info = AppleMailAccountInfo(
        account_name="Fictional", email="owner@example.test", provider="gmail",
        mailboxes=("Inbox", "Sent Mail", "Drafts"),
    )
    provider = AppleMailProvider("apple-mail:fictional", info)
    provider.include_drafts = False
    visited = []

    def fetch(mailbox, offset, limit, since):
        visited.append(mailbox)
        assert mailbox != "Drafts"
        return {"rows": [], "examined": 0, "reached_since": True, "total": 0}

    monkeypatch.setattr(provider, "_fetch_mailbox", fetch)
    page = provider.initial_sync(since=datetime(2026, 9, 27, tzinfo=UTC))
    while page.next_page_token:
        page = provider.initial_sync(
            since=datetime(2026, 9, 27, tzinfo=UTC), page_token=page.next_page_token
        )
    assert visited == ["Inbox", "Sent Mail"]
    assert page.next_cursor


def test_apple_mail_draft_skip_excludes_drafts_from_prefetch(monkeypatch):
    info = AppleMailAccountInfo(
        account_name="Fictional", email="owner@example.test", provider="gmail",
        mailboxes=("Inbox", "Sent Mail", "Drafts"),
    )
    provider = AppleMailProvider("apple-mail:fictional", info)
    provider.include_drafts = False
    scripts = []

    def run(script, **kwargs):
        scripts.append(script)
        return {"0": {}, "1": {}}

    monkeypatch.setattr("email_reader.providers.apple_mail._run_mail_script", run)
    AppleMailProvider.prefetch_recent_metadata(
        [(provider, datetime.now(UTC).isoformat())]
    )
    assert len(scripts) == 1
    assert 'set mailboxName to "Inbox"' in scripts[0]
    assert 'set mailboxName to "Sent Mail"' in scripts[0]
    assert 'set mailboxName to "Drafts"' not in scripts[0]


class GmailDraftCall:
    def __init__(self) -> None:
        self.body = None

    def users(self):
        return self

    def drafts(self):
        return self

    def create(self, *, userId, body):
        assert userId == "me"
        self.body = body
        return self

    def execute(self):
        return {"id": "draft-1", "message": {"id": "message-1"}}


def test_provider_contract_has_no_send_operation() -> None:
    assert not hasattr(EmailProvider, "send")
    assert not hasattr(GmailProvider, "send")
    assert not hasattr(OutlookProvider, "send")
    assert not hasattr(AppleMailProvider, "send")


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/me/sendMail"), ("POST", "/me/messages/id/send"),
    ("post", "/me/messages/id/reply"), ("POST", "/me/messages/id/replyAll"),
    ("POST", "/me/messages/id/forward"), ("POST", "/$batch"),
    ("DELETE", "/me/messages/id"), ("POST", "/me/messages/id/move"),
    ("PUT", "/me/messages/id"), ("POST", "/me/messages/id/%73end"),
])
def test_outlook_network_boundary_rejects_non_draft_writes_before_auth(monkeypatch, method, path):
    from unittest.mock import Mock

    provider = OutlookProvider("account", MemoryVault())
    token = Mock()
    client = Mock()
    monkeypatch.setattr(provider, "_token", token)
    provider._client = client
    with pytest.raises(ValueError, match="draft writes"):
        provider._request(method, path)
    token.assert_not_called()
    client.request.assert_not_called()


@pytest.mark.parametrize("payload", [{"isRead": True}, {"isDraft": False}, {}, None])
def test_outlook_network_boundary_rejects_non_editor_patch_fields(monkeypatch, payload):
    from unittest.mock import Mock

    provider = OutlookProvider("account", MemoryVault())
    token = Mock()
    client = Mock()
    monkeypatch.setattr(provider, "_token", token)
    provider._client = client
    with pytest.raises(ValueError, match="reviewed draft fields"):
        provider._request("PATCH", "/me/messages/id", json=payload)
    token.assert_not_called()
    client.request.assert_not_called()


@pytest.mark.parametrize(("method", "path", "payload"), [
    ("POST", "/me/messages", {"subject": "Reviewed draft"}),
    ("POST", "/me/messages/source/createReply", None),
    ("PATCH", "/me/messages/draft", {"body": {"contentType": "Text", "content": "Reviewed"}}),
    ("POST", "/me/messages/draft/attachments", {"name": "fictional.txt"}),
])
def test_outlook_network_boundary_allows_existing_draft_routes(monkeypatch, method, path, payload):
    from unittest.mock import Mock

    provider = OutlookProvider("account", MemoryVault())
    monkeypatch.setattr(provider, "_token", lambda: "fictional-token")
    provider._client = Mock()
    url = "https://graph.microsoft.com/v1.0" + path
    provider._client.request.return_value = httpx.Response(
        200, json={"id": "draft"}, request=httpx.Request(method, url)
    )
    assert provider._request(method, path, json=payload) == {"id": "draft"}
    assert provider._client.request.call_count == 1
    assert provider._client.request.call_args.args == (method, url)


def test_draft_write_paths_have_no_provider_send_call() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "email_reader"
    for path in (
        source_root / "controller.py",
        source_root / "providers" / "gmail.py",
        source_root / "providers" / "outlook.py",
        source_root / "providers" / "apple_mail.py",
    ):
        source = path.read_text(encoding="utf-8")
        calls = (
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        )
        assert not any(node.func.attr.lower() in {"send", "sendmail", "sendmessage"}
                       for node in calls), path
        assert '"/send"' not in source and '"/sendMail"' not in source, path
        assert "Mail.send(" not in source, path


def test_gmail_create_draft_never_sends(monkeypatch) -> None:
    provider = GmailProvider("account", MemoryVault())
    service = GmailDraftCall()
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    receipt = provider.create_draft(
        DraftRequest(subject="Hello", body_text="Draft body", to=["person@example.net"])
    )
    assert receipt.provider_draft_id == "draft-1"
    assert receipt.provider_message_id == "message-1"
    raw = base64.urlsafe_b64decode(service.body["message"]["raw"] + "===")
    parsed = BytesParser().parsebytes(raw)
    assert parsed["To"] == "person@example.net"
    assert "Draft body" in parsed.get_payload()


def test_gmail_initial_import_keeps_start_watermark_across_pages(monkeypatch):
    class Service:
        def __init__(self):
            self.action = ""
            self.profile_calls = 0
            self.list_calls = []

        def users(self):
            return self

        def getProfile(self, **kwargs):
            self.action = "profile"
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            self.action = "list"
            self.list_calls.append(kwargs)
            return self

        def execute(self):
            if self.action == "profile":
                self.profile_calls += 1
                return {"historyId": "100" if self.profile_calls == 1 else "200"}
            return {"messages": [{"id": "first"}], "nextPageToken": "page-2"} if len(
                self.list_calls
            ) == 1 else {"messages": [{"id": "second"}]}

    provider = GmailProvider("account", MemoryVault())
    service = Service()
    fetched = []
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: ["INBOX"])
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: fetched.append(identifier) or
                        MessagePayload(provider_id=identifier, provider_thread_id=identifier))
    first = provider.initial_sync(since=None)
    second = provider.initial_sync(since=None, page_token=first.next_page_token)
    assert fetched == ["first", "second"]
    assert service.profile_calls == 1
    assert service.list_calls[0]["pageToken"] is None
    assert service.list_calls[1]["pageToken"] == "page-2"
    assert second.next_cursor == "100" and second.next_page_token is None


def test_gmail_dated_import_resumes_into_all_drafts_without_reloading_known_mime(monkeypatch):
    class Service:
        def __init__(self):
            self.action = ""
            self.list_kind = ""
            self.list_calls = []
            self.profile_calls = 0

        def users(self):
            return self

        def getProfile(self, **kwargs):
            self.action = "profile"
            return self

        def messages(self):
            self.list_kind = "messages"
            return self

        def drafts(self):
            self.list_kind = "drafts"
            return self

        def list(self, **kwargs):
            self.action = "list"
            self.list_calls.append((self.list_kind, kwargs))
            return self

        def execute(self):
            if self.action == "profile":
                self.profile_calls += 1
                return {"historyId": "start-100"}
            kind, arguments = self.list_calls[-1]
            if kind == "messages":
                return (
                    {"messages": [{"id": "recent-inbox"}], "nextPageToken": "mail-2"}
                    if arguments["pageToken"] is None else {"messages": []}
                )
            return (
                {"drafts": [{"id": "stable-draft", "message": {"id": "known-draft"}}],
                 "nextPageToken": "draft-2"}
                if arguments["pageToken"] is None else
                {"drafts": [{"id": "older-draft", "message": {"id": "old-unsent"}}]}
            )

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_message_ids({"known-draft"})
    service = Service()
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: ["DRAFT"])
    fetched = []
    monkeypatch.setattr(
        provider, "_fetch_message",
        lambda identifier: fetched.append(identifier) or MessagePayload(
            provider_id=identifier, provider_thread_id=identifier,
            labels=["DRAFT"] if identifier == "old-unsent" else ["INBOX"],
        ),
    )
    since = datetime(2026, 1, 1, tzinfo=UTC)
    pages = []
    page = provider.initial_sync(since=since)
    pages.append(page)
    while page.next_page_token:
        page = provider.initial_sync(since=since, page_token=page.next_page_token)
        pages.append(page)

    assert len(pages) == 4
    assert [item.provider_id for item in pages[-1].messages] == ["old-unsent"]
    assert fetched == ["recent-inbox", "old-unsent"]
    assert pages[2].unchanged_label_states == {"known-draft": ["DRAFT"]}
    assert pages[-1].next_cursor == "start-100"
    assert pages[0].inbox_scope_reset
    assert [page.inbox_scope_complete for page in pages] == [False, False, False, True]
    assert service.profile_calls == 1
    assert [kind for kind, _ in service.list_calls] == [
        "messages", "messages", "drafts", "drafts",
    ]
    assert "in:drafts" not in service.list_calls[0][1]["q"]
    assert f"after:{int(since.timestamp()) - 1}" in service.list_calls[0][1]["q"]
    assert "q" not in service.list_calls[2][1]


def test_gmail_import_range_keeps_hour_and_timezone_in_search_cutoff(monkeypatch):
    class Service:
        action = ""
        query = ""

        def users(self):
            return self

        def getProfile(self, **kwargs):
            self.action = "profile"
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            self.action = "list"
            self.query = kwargs["q"]
            return self

        def execute(self):
            return {"historyId": "100"} if self.action == "profile" else {"messages": []}

    provider = GmailProvider("account", MemoryVault())
    service = Service()
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    since = datetime(2026, 9, 25, 13, 45, tzinfo=UTC)
    provider.initial_sync(since=since)
    assert f"after:{int(since.timestamp()) - 1}" in service.query
    assert "2026/09/25" not in service.query


def test_gmail_full_recovery_uses_metadata_for_complete_messages(monkeypatch):
    class Service:
        def __init__(self):
            self.action = ""
            self.get_calls = []

        def users(self):
            return self

        def getProfile(self, **kwargs):
            self.action = "profile"
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            self.action = "list"
            return self

        def get(self, **kwargs):
            self.action = "get"
            self.get_calls.append(kwargs)
            return self

        def execute(self):
            if self.action == "profile":
                return {"historyId": "200"}
            if self.action == "list":
                return {"messages": [
                    {"id": "complete"}, {"id": "draft-old"}, {"id": "new"}
                ]}
            return {"id": "complete", "labelIds": ["DRAFT"] if
                    self.get_calls[-1]["id"] == "draft-old" else ["INBOX"]}

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_message_ids({"complete", "draft-old"})
    service = Service()
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    fetched = []
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: fetched.append(identifier) or
                        MessagePayload(provider_id=identifier, provider_thread_id=identifier))
    page = provider.initial_sync(since=None)
    assert fetched == ["new"]
    assert page.unchanged_label_states == {
        "complete": ["INBOX"], "draft-old": ["DRAFT"]
    }
    assert page.inbox_scope_ids == ["complete"]
    assert page.inbox_scope_reset and page.inbox_scope_complete
    assert service.get_calls == [
        {"userId": "me", "id": "complete", "format": "metadata", "metadataHeaders": []},
        {"userId": "me", "id": "draft-old", "format": "metadata", "metadataHeaders": []},
    ]


def test_gmail_full_recovery_reconciles_missing_drafts_with_metadata_only(monkeypatch):
    class NotFound(Exception):
        resp = type("Response", (), {"status": 404})()

    class Service:
        def users(self):
            return self

        def getProfile(self, **kwargs):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"historyId": "300", "messages": []}

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_message_ids({"still-draft"})
    provider.set_known_draft_ids({"still-draft", "replaced-draft"})
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    checked = []

    def labels(identifier):
        checked.append(identifier)
        if identifier == "replaced-draft":
            raise NotFound()
        return ["DRAFT"]

    monkeypatch.setattr(provider, "_fetch_labels", labels)
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: pytest.fail(
        "Draft reconciliation must not download MIME"
    ))
    page = provider.initial_sync(since=None)
    assert checked == ["replaced-draft", "still-draft"]
    assert page.remote_deleted_ids == ["replaced-draft"]
    assert page.unchanged_label_states == {"still-draft": ["DRAFT"]}


def test_gmail_full_recovery_does_not_hide_drafts_after_transient_error(monkeypatch):
    class Service:
        def users(self):
            return self

        def getProfile(self, **kwargs):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"historyId": "300", "messages": []}

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_draft_ids({"still-draft"})
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: (_ for _ in ()).throw(
        RuntimeError("Temporary provider failure")
    ))
    with pytest.raises(RuntimeError, match="Temporary provider failure"):
        provider.initial_sync(since=None)


def test_gmail_full_recovery_continues_when_listed_messages_vanish(monkeypatch):
    class NotFound(Exception):
        resp = type("Response", (), {"status": 404})()

    class Service:
        def users(self):
            return self

        def getProfile(self, **kwargs):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"historyId": "300", "messages": [
                {"id": "known-gone"}, {"id": "new-gone"}, {"id": "alive"},
            ]}

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_message_ids({"known-gone"})
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: (_ for _ in ()).throw(
        NotFound()
    ))
    fetched = []

    def fetch(identifier):
        fetched.append(identifier)
        if identifier == "new-gone":
            raise NotFound()
        return MessagePayload(provider_id=identifier, provider_thread_id=identifier,
                              labels=["INBOX"])

    monkeypatch.setattr(provider, "_fetch_message", fetch)
    page = provider.initial_sync(since=None)
    assert fetched == ["new-gone", "alive"]
    assert [message.provider_id for message in page.messages] == ["alive"]
    assert page.remote_deleted_ids == ["known-gone", "new-gone"]
    assert page.inbox_scope_ids == ["alive"]
    assert page.next_cursor == "300"


def test_gmail_history_continues_after_confirmed_404_but_not_other_errors(monkeypatch):
    class NotFound(Exception):
        resp = type("Response", (), {"status": 404})()

    class Service:
        def users(self):
            return self

        def history(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"historyId": "202", "history": [{
                "messagesAdded": [{"message": {"id": identifier, "labelIds": ["INBOX"]}}
                                  for identifier in ("known-gone", "new-gone", "alive")],
                "labelsAdded": [{"message": {"id": "label-only-gone"}}],
            }]}

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_message_ids({"known-gone", "label-only-gone"})
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: (_ for _ in ()).throw(
        NotFound()
    ))

    def fetch(identifier):
        if identifier == "new-gone":
            raise NotFound()
        return MessagePayload(provider_id=identifier, provider_thread_id=identifier)

    monkeypatch.setattr(provider, "_fetch_message", fetch)
    page = provider.incremental_sync("200")
    assert [message.provider_id for message in page.messages] == ["alive"]
    assert page.remote_deleted_ids == ["known-gone", "label-only-gone", "new-gone"]
    assert page.next_cursor == "202"

    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: (_ for _ in ()).throw(
        RuntimeError("Temporary metadata failure")
    ))
    with pytest.raises(RuntimeError, match="Temporary metadata failure"):
        provider.incremental_sync("200")


def test_gmail_draft_replacement_fetches_only_new_message_id(monkeypatch):
    class Service:
        def users(self):
            return self

        def history(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"historyId": "202", "history": [
                {"messagesAdded": [{"message": {"id": "draft-old"}}]},
                {"messagesDeleted": [{"message": {"id": "draft-old"}}],
                 "messagesAdded": [{"message": {"id": "draft-new", "labelIds": ["DRAFT"]}}]},
            ]}

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_message_ids({"draft-old"})
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: pytest.fail(
        "The new Draft's history event already supplies its labels"
    ))
    fetched = []
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: fetched.append(identifier) or
                        MessagePayload(provider_id=identifier, provider_thread_id=identifier))
    page = provider.incremental_sync("200")
    assert fetched == ["draft-new"]
    assert page.remote_deleted_ids == ["draft-old"]
    assert page.unchanged_label_states == {}


def test_gmail_known_draft_history_events_never_reload_unchanged_mime(monkeypatch):
    class Service:
        def users(self):
            return self

        def history(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"historyId": "202", "history": [
                {"messagesAdded": [{"message": {"id": "draft-same"}}]},
                {"labelsAdded": [{"message": {"id": "draft-label"}}]},
            ]}

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_message_ids({"draft-same", "draft-label"})
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    checked = []
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier:
                        checked.append(identifier) or ["DRAFT"])
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: pytest.fail(
        "An unchanged Gmail message ID must not download its MIME"
    ))
    page = provider.incremental_sync("200")
    assert page.messages == []
    assert checked == ["draft-same", "draft-label"]
    assert page.unchanged_label_states == {
        "draft-same": ["DRAFT"], "draft-label": ["DRAFT"]
    }


def test_gmail_incremental_history_yields_pages_before_advancing_cursor(monkeypatch):
    class Service:
        def __init__(self):
            self.calls = []
            self.pages = [
                {"history": [{"messagesAdded": [{"message": {"id": "kept"}},
                                                  {"message": {"id": "removed"}}]}],
                 "nextPageToken": "page-2", "historyId": "120"},
                {"history": [{"messagesDeleted": [{"message": {"id": "removed"}}]},
                             {"messagesAdded": [{"message": {"id": "new"}}]}],
                 "historyId": "140"},
            ]

        def users(self):
            return self

        def history(self):
            return self

        def list(self, **kwargs):
            self.calls.append(kwargs)
            return self

        def execute(self):
            return self.pages.pop(0)

    provider = GmailProvider("account", MemoryVault())
    service = Service()
    fetched = []
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: ["INBOX"])
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: fetched.append(identifier) or
                        MessagePayload(provider_id=identifier, provider_thread_id=identifier))
    page = provider.incremental_sync("100")
    assert len(service.calls) == 1
    assert page.next_cursor is None
    assert [item.provider_id for item in page.messages] == ["kept", "removed"]
    page = provider.initial_sync(since=None, page_token=page.next_page_token)
    assert service.calls[0]["pageToken"] is None
    assert service.calls[1]["pageToken"] == "page-2"
    assert all(call["startHistoryId"] == "100" for call in service.calls)
    assert fetched == ["kept", "removed", "new"]
    assert [item.provider_id for item in page.messages] == ["new"]
    assert page.remote_deleted_ids == ["removed"]
    assert page.next_cursor == "140"


def test_gmail_history_skips_bulk_and_archived_body_downloads(monkeypatch):
    class Service:
        def users(self):
            return self

        def history(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"historyId": "200", "history": [{"messagesAdded": [
                {"message": {"id": "promo", "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]}},
                {"message": {"id": "archived", "labelIds": []}},
                {"message": {"id": "primary", "labelIds": ["INBOX"]}},
            ]}]}

    provider = GmailProvider("account", MemoryVault())
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: pytest.fail(
        "History already supplied labels"
    ))
    fetched = []
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: fetched.append(identifier) or
                        MessagePayload(provider_id=identifier, provider_thread_id=identifier))
    page = provider.incremental_sync("100")
    assert fetched == ["primary"]
    assert [message.provider_id for message in page.messages] == ["primary"]
    assert page.next_cursor == "200"


def test_gmail_history_uses_current_labels_after_same_interval_reclassification(monkeypatch):
    class Service:
        def users(self):
            return self

        def history(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            return {"historyId": "201", "history": [
                {"messagesAdded": [{"message": {
                    "id": "reclassified", "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]
                }}]},
                {"labelsRemoved": [{"message": {"id": "reclassified"},
                                     "labelIds": ["CATEGORY_PROMOTIONS"]}]},
            ]}

    provider = GmailProvider("account", MemoryVault())
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    checked = []
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: checked.append(identifier) or
                        ["INBOX"])
    fetched = []
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: fetched.append(identifier) or
                        MessagePayload(provider_id=identifier, provider_thread_id=identifier))
    page = provider.incremental_sync("200")
    assert checked == ["reclassified"]
    assert fetched == ["reclassified"]
    assert [message.provider_id for message in page.messages] == ["reclassified"]


def test_gmail_history_failure_retains_first_page_without_advancing_final_cursor(monkeypatch):
    class Service:
        def __init__(self):
            self.calls = 0

        def users(self):
            return self

        def history(self):
            return self

        def list(self, **kwargs):
            return self

        def execute(self):
            self.calls += 1
            if self.calls == 1:
                return {"history": [{"messagesAdded": [{"message": {"id": "first"}}]}],
                        "nextPageToken": "page-2", "historyId": "120"}
            raise RuntimeError("Second history page failed")

    provider = GmailProvider("account", MemoryVault())
    service = Service()
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    fetched = []
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: ["INBOX"])
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: fetched.append(identifier) or
        MessagePayload(provider_id=identifier, provider_thread_id=identifier, labels=["INBOX"]))
    first = provider.incremental_sync("100")
    assert first.next_cursor is None
    assert fetched == ["first"]
    with pytest.raises(RuntimeError, match="Second history page failed"):
        provider.initial_sync(since=None, page_token=first.next_page_token)
    assert fetched == ["first"]


def test_gmail_history_rechecks_messages_with_label_changes_once(monkeypatch):
    class Service:
        def users(self):
            return self

        def history(self):
            return self

        def list(self, **kwargs):
            self.request = kwargs
            return self

        def execute(self):
            return {
                "historyId": "150",
                "history": [{
                    "labelsAdded": [{"message": {"id": "changed"}}],
                    "labelsRemoved": [{"message": {"id": "changed"}}],
                }],
            }

    provider = GmailProvider("account", MemoryVault())
    service = Service()
    fetched = []
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    monkeypatch.setattr(provider, "_fetch_labels", lambda identifier: ["INBOX"])
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: fetched.append(identifier) or
                        MessagePayload(provider_id=identifier, provider_thread_id=identifier))
    page = provider.incremental_sync("100")
    assert fetched == ["changed"]
    assert [item.provider_id for item in page.messages] == ["changed"]
    assert set(service.request["historyTypes"]) == {
        "messageAdded", "messageDeleted", "labelAdded", "labelRemoved"
    }


def test_gmail_known_label_change_uses_metadata_without_raw_download(monkeypatch):
    class Service:
        def __init__(self):
            self.action = ""
            self.get_calls = []

        def users(self):
            return self

        def history(self):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            self.action = "history"
            return self

        def get(self, **kwargs):
            self.action = "message"
            self.get_calls.append(kwargs)
            return self

        def execute(self):
            if self.action == "history":
                return {"historyId": "101", "history": [
                    {"labelsRemoved": [{"message": {"id": "known"}, "labelIds": ["UNREAD"]}]}
                ]}
            return {"id": "known", "labelIds": ["INBOX", "CATEGORY_UPDATES"]}

    provider = GmailProvider("account", MemoryVault())
    provider.set_known_message_ids({"known"})
    service = Service()
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    monkeypatch.setattr(provider, "_fetch_message", lambda identifier: pytest.fail(
        "Unchanged body must not be downloaded"
    ))
    page = provider.incremental_sync("100")
    assert page.messages == []
    assert page.unchanged_label_states == {"known": ["INBOX", "CATEGORY_UPDATES"]}
    assert service.get_calls == [
        {"userId": "me", "id": "known", "format": "metadata", "metadataHeaders": []}
    ]


def test_gmail_archived_message_is_not_misclassified_as_inbox(monkeypatch):
    class Service:
        def users(self):
            return self

        def messages(self):
            return self

        def get(self, **kwargs):
            return self

        def execute(self):
            return {"id": "archived", "threadId": "thread", "labelIds": [], "raw": encoded}

    email = EmailMessage()
    email["From"] = "sender@example.net"
    email["Subject"] = "Archived note"
    email.set_content("Stored content")
    encoded = base64.urlsafe_b64encode(email.as_bytes()).decode("ascii")
    provider = GmailProvider("account", MemoryVault())
    monkeypatch.setattr(provider, "_service_or_authenticate", Service)
    payload = provider._fetch_message("archived")
    assert "ARCHIVE" in payload.labels
    assert "INBOX" not in payload.labels


def test_outlook_create_reply_draft_uses_create_reply_and_patch(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/createReply"):
            return {"id": "draft-2"}
        return {"id": "draft-2", "webLink": "https://outlook.example/draft"}

    monkeypatch.setattr(provider, "_request", request)
    receipt = provider.create_draft(
        DraftRequest(
            subject="Re: Hello",
            body_text="Reply body",
            to=["person@example.net"],
            reply_to_provider_message_id="message-2",
        )
    )
    assert receipt.provider_draft_id == "draft-2"
    assert receipt.provider_message_id == "draft-2"
    assert calls[0][:2] == ("POST", "/me/messages/message-2/createReply")
    assert calls[1][0] == "PATCH"
    assert len(calls) == 2


def test_outlook_refreshes_expired_access_token_once(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    provider._access_token = "stale"
    authorizations = []

    class Client:
        def request(self, method, url, *, headers, **kwargs):
            authorizations.append(headers["Authorization"])
            request = httpx.Request(method, url)
            return httpx.Response(401 if len(authorizations) == 1 else 200,
                                  json={} if len(authorizations) == 1 else {"id": "message"},
                                  request=request)

    def token():
        return provider._access_token or "renewed"

    provider._client = Client()
    monkeypatch.setattr(provider, "_token", token)
    assert provider._request("GET", "/me/messages/123") == {"id": "message"}
    assert authorizations == ["Bearer stale", "Bearer renewed"]


def test_outlook_does_not_loop_after_second_unauthorized_response(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    provider._access_token = "stale"
    calls = []

    class Client:
        def request(self, method, url, *, headers, **kwargs):
            calls.append(headers["Authorization"])
            return httpx.Response(401, request=httpx.Request(method, url))

    provider._client = Client()
    monkeypatch.setattr(provider, "_token", lambda: provider._access_token or "renewed")
    with pytest.raises(httpx.HTTPStatusError):
        provider._request("GET", "/me/messages/123")
    assert calls == ["Bearer stale", "Bearer renewed"]


def test_outlook_throttle_waits_then_retries_without_losing_page(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    calls = []
    waits = []

    class Client:
        def request(self, method, url, *, headers, **kwargs):
            calls.append((method, url))
            request = httpx.Request(method, url)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "2"}, request=request)
            return httpx.Response(200, json={"value": []}, request=request)

    provider._client = Client()
    monkeypatch.setattr(provider, "_token", lambda: "token")
    monkeypatch.setattr("email_reader.providers.outlook.time.sleep", waits.append)
    assert provider._request("GET", "/me/mailFolders/inbox/messages") == {"value": []}
    assert calls == [("GET", "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages")] * 2
    assert waits == [2.0]


def test_outlook_throttle_budget_preserves_sync_retry_and_does_not_retry_503_post(
    monkeypatch,
) -> None:
    provider = OutlookProvider("account", MemoryVault())
    calls = []
    waits = []

    class Client:
        def request(self, method, url, *, headers, **kwargs):
            calls.append(method)
            status = 429 if method == "GET" else 503
            return httpx.Response(
                status, headers={"Retry-After": "20"},
                request=httpx.Request(method, url),
            )

    provider._client = Client()
    monkeypatch.setattr(provider, "_token", lambda: "token")
    monkeypatch.setattr("email_reader.providers.outlook.time.sleep", waits.append)
    with pytest.raises(httpx.HTTPStatusError):
        provider._request("GET", "/me/mailFolders/inbox/messages")
    assert calls == ["GET", "GET"] and waits == [20.0]
    with pytest.raises(httpx.HTTPStatusError):
        provider._request("POST", "/me/messages", json={"subject": "unsent"})
    assert calls[-1] == "POST" and calls.count("POST") == 1


def test_outlook_rejects_external_page_link_before_using_token(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    token_calls = []
    monkeypatch.setattr(provider, "_token", lambda: token_calls.append(True) or "secret")
    with pytest.raises(ValueError, match="Unexpected Microsoft Graph URL"):
        provider._request("GET", "https://graph.microsoft.com.evil.example/v1.0/me/messages")
    assert token_calls == []


def test_outlook_attachment_import_follows_all_pages(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    next_link = "https://graph.microsoft.com/v1.0/me/messages/message/attachments?$skiptoken=2"
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return {
                "value": [{"id": "first", "name": "photo.png", "contentType": "image/png",
                           "isInline": True, "contentId": "photo-1",
                           "contentBytes": base64.b64encode(b"first").decode()}],
                "@odata.nextLink": next_link,
            }
        return {"value": [{"id": "second", "name": "binder.pdf",
                           "contentType": "application/pdf",
                           "contentBytes": base64.b64encode(b"second").decode()}]}

    monkeypatch.setattr(provider, "_request", request)
    payload = provider._message({
        "id": "message", "body": {"contentType": "html", "content": "<img src='cid:photo-1'>"},
        "hasAttachments": True,
    })
    assert payload is not None
    assert [item.provider_id for item in payload.attachments] == ["first", "second"]
    assert payload.attachments[0].is_inline
    assert payload.attachments[0].content_id == "photo-1"
    assert payload.attachments[1].content == b"second"
    assert calls == ["/me/messages/message/attachments?$top=100", next_link]


def test_outlook_initial_import_preserves_start_watermark_across_folders(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    next_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=2"
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        return {"value": [], "@odata.nextLink": next_link} if len(calls) == 1 else {"value": []}

    monkeypatch.setattr(provider, "_request", request)
    before = datetime.now(UTC)
    first = provider.initial_sync(since=None)
    first_state = json.loads(first.next_page_token)
    assert first_state["folder"] == 0 and first_state["url"] == next_link
    assert first.inbox_scope_reset and not first.inbox_scope_complete
    second = provider.initial_sync(since=None, page_token=first.next_page_token)
    third = provider.initial_sync(since=None, page_token=second.next_page_token)
    final = provider.initial_sync(since=None, page_token=third.next_page_token)
    after = datetime.now(UTC)
    assert calls[1] == next_link
    assert json.loads(second.next_page_token)["folder"] == 1
    assert json.loads(third.next_page_token)["folder"] == 2
    assert final.next_page_token is None
    assert final.inbox_scope_complete and not final.inbox_scope_reset
    assert json.loads(final.next_cursor)["since"] == first_state["watermark"]
    assert before <= datetime.fromisoformat(first_state["watermark"]) <= after


def test_outlook_initial_range_uses_folder_dates_and_keeps_unsent_drafts(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    since = datetime(2026, 1, 1, tzinfo=UTC)
    calls: list[str] = []

    def request(method, url, **kwargs):
        calls.append(url)
        if "/mailFolders/drafts/messages?" in url:
            return {"value": [{"id": "unsent-old-draft", "changeKey": "v1"}]}
        if "/mailFolders/" in url:
            return {"value": []}
        if url.startswith("/me/messages/unsent-old-draft?"):
            return {
                "id": "unsent-old-draft", "changeKey": "v1", "isDraft": True,
                "body": {"contentType": "text", "content": "Still being edited"},
            }
        raise AssertionError(f"Unexpected full-content request: {url}")

    monkeypatch.setattr(provider, "_request", request)
    page = provider.initial_sync(since=since)
    page = provider.initial_sync(since=since, page_token=page.next_page_token)
    page = provider.initial_sync(since=since, page_token=page.next_page_token)
    assert page.next_page_token is None
    assert [message.provider_id for message in page.messages] == ["unsent-old-draft"]
    assert page.messages[0].is_draft
    assert "receivedDateTime ge 2026-01-01T00:00:00Z" in calls[0]
    assert "$orderby=receivedDateTime desc" in calls[0]
    assert "sentDateTime ge 2026-01-01T00:00:00Z" in calls[1]
    assert "$orderby=sentDateTime desc" in calls[1]
    assert "receivedDateTime" not in calls[1].split("$filter=", 1)[1]
    assert "$filter=" not in calls[2] and "$orderby=" not in calls[2]
    assert "lastModifiedDateTime" in calls[2]


def test_outlook_full_recovery_skips_unchanged_bodies_but_keeps_other_mail(monkeypatch):
    provider = OutlookProvider("account", MemoryVault())
    provider.set_known_message_revisions({"same": "v1"})
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if "/mailFolders/inbox/messages?" in url:
            return {"value": [
                {"id": "same", "changeKey": "v1", "isRead": True,
                 "inferenceClassification": "focused"},
                {"id": "new", "changeKey": "v2",
                 "inferenceClassification": "focused"},
                {"id": "bulk", "changeKey": "v3",
                 "inferenceClassification": "other"},
            ]}
        if url.startswith("/me/messages/new?"):
            return {"id": "new", "changeKey": "v2",
                    "body": {"contentType": "text", "content": "New body"}}
        if url.startswith("/me/messages/bulk?"):
            return {"id": "bulk", "changeKey": "v3", "inferenceClassification": "other",
                    "body": {"contentType": "text", "content": "A useful community announcement"}}
        raise AssertionError(f"Unchanged content was requested: {url}")

    monkeypatch.setattr(provider, "_request", request)
    page = provider.initial_sync(since=None)
    assert [message.provider_id for message in page.messages] == ["new", "bulk"]
    assert page.unchanged_read_states == {"same": False}
    assert page.unchanged_label_states == {"same": ["INBOX"]}
    assert page.filtered_bulk_ids == []
    assert page.inbox_scope_ids == ["same", "new", "bulk"]
    assert page.inbox_scope_reset and not page.inbox_scope_complete
    assert len([url for url in calls if url.startswith("/me/messages/")]) == 2


def test_outlook_delta_bootstrap_fetches_unknown_old_mail_but_skips_known_content(monkeypatch):
    provider = OutlookProvider("account", MemoryVault())
    provider.set_known_message_revisions({"old": "old-v0"})
    calls = []
    delta_links = {
        folder: f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages/delta?$deltatoken=ready"
        for folder in ("inbox", "sentitems", "drafts")
    }

    def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith(
            "/messages/delta?$select=id,lastModifiedDateTime,changeKey,isRead,inferenceClassification"
        ):
            folder = url.split("/mailFolders/", 1)[1].split("/", 1)[0]
            values = []
            if folder == "inbox":
                values = [
                    {"id": "old", "changeKey": "old-v1",
                     "lastModifiedDateTime": "2026-09-01T11:00:00Z"},
                    {"id": "missing", "changeKey": "missing-v1",
                     "lastModifiedDateTime": "2026-09-01T11:10:00Z"},
                    {"id": "new/id+", "changeKey": "new-v1",
                     "lastModifiedDateTime": "2026-09-01T12:01:00Z"},
                ]
            return {"value": values, "@odata.deltaLink": delta_links[folder]}
        if url.startswith("/me/messages/missing?$select="):
            return {"id": "missing", "changeKey": "missing-v1",
                    "body": {"contentType": "text", "content": "Previously missing text"}}
        assert url.startswith("/me/messages/new%2Fid%2B?$select=")
        return {"id": "new/id+", "changeKey": "new-v1",
                "body": {"contentType": "text", "content": "New text"}}

    monkeypatch.setattr(provider, "_request", request)
    first = provider.incremental_sync(json.dumps({"since": "2026-09-01T12:00:00+00:00"}))
    second = provider.initial_sync(since=None, page_token=first.next_page_token)
    third = provider.initial_sync(since=None, page_token=second.next_page_token)
    assert [message.provider_id for message in first.messages] == ["missing", "new/id+"]
    assert first.unchanged_revisions == {"old": "old-v1"}
    assert first.unchanged_label_states == {"old": ["INBOX"]}
    assert second.messages == [] and third.messages == []
    assert len([url for url in calls if url.startswith("/me/messages/")]) == 2
    assert json.loads(third.next_cursor) == {"mode": "delta", "links": delta_links}


def test_outlook_delta_replay_skips_unchanged_body_and_attachments(monkeypatch):
    provider = OutlookProvider("account", MemoryVault())
    provider.set_known_message_revisions({"same": "version-1"})
    links = {
        folder: f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages/delta?$deltatoken=old"
        for folder in ("inbox", "sentitems", "drafts")
    }
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if url == links["inbox"]:
            return {"value": [
                {"id": "same", "changeKey": "version-1", "isRead": True},
                {"id": "changed", "changeKey": "version-2"},
            ], "@odata.deltaLink": url}
        if url.startswith("/me/messages/changed?"):
            return {"id": "changed", "changeKey": "version-2",
                    "body": {"contentType": "text", "content": "Updated"}}
        if url in (links["sentitems"], links["drafts"]):
            return {"value": [], "@odata.deltaLink": url}
        raise AssertionError(f"Unexpected full-content request: {url}")

    monkeypatch.setattr(provider, "_request", request)
    page = provider.incremental_sync(json.dumps({"mode": "delta", "links": links}))
    assert page.labels_include_read_state is False
    assert [message.provider_id for message in page.messages] == ["changed"]
    assert page.unchanged_read_states == {"same": False}
    assert page.unchanged_label_states == {"same": ["INBOX"]}
    assert len([url for url in calls if url.startswith("/me/messages/")]) == 1


def test_outlook_delta_cursor_resumes_page_and_tracks_removals(monkeypatch):
    provider = OutlookProvider("account", MemoryVault())
    links = {
        folder: f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages/delta?$deltatoken=old"
        for folder in ("inbox", "sentitems", "drafts")
    }
    next_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=2"
    new_link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=new"
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if url == links["inbox"]:
            return {"value": [{"id": "gone", "@removed": {"reason": "deleted"}}],
                    "@odata.nextLink": next_link}
        if url == "/me/messages/gone?$select=id,parentFolderId":
            response = httpx.Response(404, request=httpx.Request(method, url))
            response.raise_for_status()
        if url == next_link:
            return {"value": [{"id": "changed"}], "@odata.deltaLink": new_link}
        if url.startswith("/me/messages/changed?"):
            return {"id": "changed", "body": {"contentType": "text", "content": "Changed"}}
        return {"value": [], "@odata.deltaLink": url}

    monkeypatch.setattr(provider, "_request", request)
    first = provider.incremental_sync(json.dumps({"mode": "delta", "links": links}))
    assert first.remote_deleted_ids == ["gone"]
    resumed = provider.initial_sync(since=None, page_token=first.next_page_token)
    assert [message.provider_id for message in resumed.messages] == ["changed"]
    sent = provider.initial_sync(since=None, page_token=resumed.next_page_token)
    drafts = provider.initial_sync(since=None, page_token=sent.next_page_token)
    assert calls[:3] == [
        links["inbox"], "/me/messages/gone?$select=id,parentFolderId", next_link,
    ]
    assert json.loads(drafts.next_cursor)["links"]["inbox"] == new_link


@pytest.mark.parametrize("current_folder,expect_removed", [
    ("archive-id", True), ("inbox-id", False),
])
def test_outlook_folder_delta_removal_checks_global_message_before_hiding(
    monkeypatch, current_folder, expect_removed,
):
    provider = OutlookProvider("account", MemoryVault())
    links = {
        folder: f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages/delta?$deltatoken=old"
        for folder in ("inbox", "sentitems", "drafts")
    }
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if url == links["inbox"]:
            return {"value": [{"id": "moved", "@removed": {"reason": "deleted"}}],
                    "@odata.deltaLink": url}
        if url == "/me/messages/moved?$select=id,parentFolderId":
            return {"id": "moved", "parentFolderId": current_folder}
        if url == "/me/mailFolders/inbox?$select=id":
            return {"id": "inbox-id"}
        raise AssertionError(f"Unexpected content request: {url}")

    monkeypatch.setattr(provider, "_request", request)
    page = provider.incremental_sync(json.dumps({"mode": "delta", "links": links}))
    assert page.remote_deleted_ids == []
    assert page.removed_folder_roles == ({"moved": "inbox"} if expect_removed else {})
    assert calls == [
        links["inbox"], "/me/messages/moved?$select=id,parentFolderId",
        "/me/mailFolders/inbox?$select=id",
    ]


def test_outlook_delta_rejects_missing_change_metadata_without_fetching_body(monkeypatch):
    provider = OutlookProvider("account", MemoryVault())
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        return {"value": [{"id": "unknown"}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=ready"}

    monkeypatch.setattr(provider, "_request", request)
    with pytest.raises(ValueError, match="modification time"):
        provider.incremental_sync(json.dumps({"since": "2026-09-01T12:00:00Z"}))
    assert len(calls) == 1


def test_outlook_expired_delta_token_requests_resync(monkeypatch):
    provider = OutlookProvider("account", MemoryVault())
    links = {
        folder: f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages/delta?$deltatoken=old"
        for folder in ("inbox", "sentitems", "drafts")
    }

    def expired(method, url, **kwargs):
        response = httpx.Response(410, request=httpx.Request(method, url))
        response.raise_for_status()

    monkeypatch.setattr(provider, "_request", expired)
    with pytest.raises(SyncCursorExpiredError, match="full resync"):
        provider.incremental_sync(json.dumps({"mode": "delta", "links": links}))


def test_gmail_draft_preserves_reviewed_recipients_and_reply_headers(monkeypatch):
    provider = GmailProvider("account", MemoryVault())
    service = GmailDraftCall()
    monkeypatch.setattr(provider, "_service_or_authenticate", lambda: service)
    provider.create_draft(DraftRequest(subject="Re: Quote", body_text="Reviewed text",
        to=["help@example.net"], cc=["team@example.net"], bcc=["private@example.net"],
        provider_thread_id="thread", in_reply_to="<parent@example.net>",
        references=["<root@example.net>", "<parent@example.net>"]))
    parsed = BytesParser().parsebytes(base64.urlsafe_b64decode(service.body["message"]["raw"]))
    assert parsed["To"] == "help@example.net" and parsed["Cc"] == "team@example.net"
    assert parsed["Bcc"] == "private@example.net"
    assert parsed["In-Reply-To"] == "<parent@example.net>"
    assert parsed["References"] == "<root@example.net> <parent@example.net>"
    assert service.body["message"]["threadId"] == "thread"


def test_outlook_import_requests_and_preserves_reply_to():
    provider = OutlookProvider("account", MemoryVault())
    assert "replyTo" not in provider._folder_url("inbox", since=None)
    assert "replyTo" in MESSAGE_FIELDS
    payload = provider._message({"id": "message", "from": {"emailAddress": {"address": "notifications@example.net"}},
        "replyTo": [{"emailAddress": {"address": "help@example.net"}}]})
    assert payload.reply_to == ["help@example.net"]


def test_outlook_syncs_standard_folders_and_keeps_other_inbox(monkeypatch) -> None:
    provider = OutlookProvider("account", MemoryVault())
    provider._email = "owner@example.com"
    urls: list[str] = []

    def item(identifier: str, classification: str):
        return {
            "id": identifier,
            "conversationId": identifier,
            "subject": identifier,
            "from": {"emailAddress": {"name": "Maya", "address": "maya@example.net"}},
            "toRecipients": [{"emailAddress": {"address": "owner@example.com"}}],
            "sentDateTime": "2026-09-02T12:00:00Z",
            "receivedDateTime": "2026-09-02T12:00:00Z",
            "body": {"contentType": "text", "content": identifier},
            "bodyPreview": identifier,
            "isRead": False,
            "isDraft": False,
            "hasAttachments": False,
            "inferenceClassification": classification,
        }

    def request(method, url, **kwargs):
        urls.append(url)
        if "/mailFolders/inbox/messages" in url:
            return {"value": [item("focused", "focused"), item("other", "other")]}
        identifier = url.split("/me/messages/", 1)[1].split("?", 1)[0]
        assert identifier in {"focused", "other"}
        return item(identifier, identifier)

    monkeypatch.setattr(provider, "_request", request)
    page = provider.initial_sync(since=datetime(2026, 1, 1, tzinfo=UTC))

    assert "/mailFolders/inbox/messages" in urls[0]
    assert [message.provider_id for message in page.messages] == ["focused", "other"]
    assert page.filtered_bulk_ids == []
    assert len([url for url in urls if url.startswith("/me/messages/")]) == 2
    assert page.messages[0].labels == ["INBOX"]
    assert json.loads(page.next_page_token)["folder"] == 1
