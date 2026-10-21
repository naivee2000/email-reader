import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, QUrl

from email_reader.contracts import (
    AttachmentPayload,
    DraftReceipt,
    DraftRequest,
    MessagePayload,
    ProviderKind,
)
from email_reader.controller import AppController
from email_reader.drafts import addresses, draft_request, initial_draft
from email_reader.providers.apple_mail import AppleMailAccountInfo, AppleMailProvider


def source(repository):
    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="source",
            provider_thread_id="thread",
            subject="Insurance question",
            sender_name="Maya",
            sender_email="maya@example.net",
            to=["owner@example.com", "colleague@example.net"],
            cc=["team@example.net", "owner@example.com"],
            bcc=["hidden@example.net"],
            body_text="Please review.",
        ),
    )
    return repository.get_thread(repository.list_threads()[0]["id"])


def test_reply_all_forward_and_identity(repository):
    thread = source(repository)
    accounts = repository.list_accounts()
    reply = initial_draft("reply", thread, accounts)
    assert reply["to"] == "maya@example.net" and reply["cc"] == reply["bcc"] == ""
    reply_all = initial_draft("reply_all", thread, accounts)
    assert addresses(reply_all["to"]) == ["maya@example.net", "colleague@example.net"]
    assert reply_all["cc"] == "team@example.net" and reply_all["bcc"] == ""
    assert reply_all["account_id"] == "account-1"
    forwarded = initial_draft("forward", thread, accounts)
    assert forwarded["to"] == "" and forwarded["cc"] == "" and forwarded["bcc"] == ""
    assert forwarded["subject"] == "Fwd: Insurance question"
    assert "Please review." in forwarded["body"]
    edited = {**reply, "cc": "CC <cc@example.net>", "bcc": "bcc@example.net", "body": "My words"}
    request = draft_request(edited, reply)
    assert (
        request.body_text == "My words"
        and request.cc == ["cc@example.net"]
        and request.bcc == ["bcc@example.net"]
    )
    assert request.reply_to_provider_message_id == "source"
    edited["account_id"] = "another-account"
    request = draft_request(edited, reply)
    assert request.reply_to_provider_message_id is None and request.provider_thread_id is None


def test_forward_keeps_inline_image_originals_without_cid_markers(repository):
    from email_reader.contracts import AttachmentPayload

    repository.ingest_message(
        "account-1",
        MessagePayload(
            provider_id="inline-forward",
            provider_thread_id="inline-forward",
            subject="With inline logo",
            sender_email="maya@example.net",
            body_text="See logo [cid:logo@example.net] and policy attached.",
            body_html='<p>See logo <img src="cid:logo@example.net"></p>'
                      '<a href="https://example.net/review?token=abc"><img src="cid:logo@example.net"></a>',
            attachments=[
                AttachmentPayload(provider_id="logo", content_id="logo@example.net",
                    filename="logo.png", mime_type="image/png", content=b"original logo", is_inline=True),
                AttachmentPayload(provider_id="policy", filename="policy.pdf",
                    mime_type="application/pdf", content=b"original PDF"),
            ],
        ),
    )
    thread = repository.get_thread(next(item["id"] for item in repository.list_threads()
                                        if item["subject"] == "With inline logo"))
    form = initial_draft("forward", thread, repository.list_accounts())
    assert "cid:" not in form["body"]
    assert "[Inline image attached: logo.png]" in form["body"]
    assert "https://example.net/review?token=abc" in form["body"]
    assert set(form["attachment_ids"]) == {item["id"] for item in thread["messages"][0]["attachments"]}


def test_address_validation_and_deduplication():
    assert addresses('"Maya, Chen" <maya@example.net>, MAYA@example.net; other@example.net') == [
        "maya@example.net",
        "other@example.net",
    ]
    with pytest.raises(ValueError):
        addresses("not-an-address")


def test_reply_to_precedence_references_and_no_recipient_mutation(repository):
    thread = source(repository)
    latest = thread["messages"][0]
    latest["internet_message_id"] = "<parent@example.net>"
    latest["recipients"].update(reply_to=["help@example.net", "HELP@example.net"],
                                references=["<root@example.net>"])
    latest["recipients"]["cc"] += ["TEAM@example.net", "HELP@example.net"]
    original_to = list(latest["recipients"]["to"])
    accounts = repository.list_accounts()
    reply = initial_draft("reply", thread, accounts)
    assert reply["to"] == "help@example.net"
    reply_all = initial_draft("reply_all", thread, accounts)
    assert reply_all["to"] == "help@example.net, colleague@example.net"
    assert reply_all["cc"] == "team@example.net" and reply_all["bcc"] == ""
    assert latest["recipients"]["to"] == original_to
    request = draft_request(reply_all, reply_all)
    assert request.in_reply_to == "<parent@example.net>"
    assert request.references == ["<root@example.net>", "<parent@example.net>"]
    other = draft_request({**reply_all, "account_id": "different"}, reply_all)
    assert other.provider_thread_id is None and other.reply_to_provider_message_id is None
    assert other.in_reply_to == request.in_reply_to, "Internet references are portable across accounts"
    forward = initial_draft("forward", thread, accounts)
    assert not draft_request(forward, forward).in_reply_to


def test_reply_ignores_later_unsent_draft(repository):
    thread = source(repository)
    draft = {**thread["messages"][0], "is_draft": True, "provider_id": "draft-not-sent",
             "sent_at": "2099-01-01", "sender_email": "different@example.net"}
    thread["messages"].append(draft)
    form = initial_draft("reply", thread, repository.list_accounts())
    assert form["reply_to_provider_message_id"] == "source"
    assert form["to"] == "maya@example.net"
    thread["messages"] = [draft]
    with pytest.raises(ValueError, match="received or sent"):
        initial_draft("reply", thread, repository.list_accounts())


def test_legacy_reply_headers_are_recovered_once_from_encrypted_original(repository, monkeypatch):
    import json
    raw = (b"From: notifications@example.net\r\nReply-To: Maya <maya@example.net>\r\n"
           b"References: <earlier@example.net>\r\n\r\nOriginal body")
    identifier = repository.ingest_message("account-1", MessagePayload(provider_id="legacy",
        provider_thread_id="legacy", sender_email="notifications@example.net", raw_source=raw))
    repository.db.execute("UPDATE messages SET recipients_json=? WHERE id=?",
                          (json.dumps({"to": ["owner@example.com"], "cc": [], "bcc": []}), identifier))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (identifier,))
    getter = Mock(wraps=repository.blobs.get)
    monkeypatch.setattr(repository.blobs, "get", getter)
    for _ in range(2):
        thread = repository.prepare_reply_thread(repository.get_thread(thread_id))
        form = initial_draft("reply", thread, repository.list_accounts())
        assert form["to"] == "maya@example.net"
        assert form["references"] == ["<earlier@example.net>"]
    assert getter.call_count == 1, "No provider fetch or repeated decrypt is needed"


def test_export_returns_exact_original_and_preserves_destination_on_failure(repository, tmp_path):
    identifier = repository.ingest_message(
        "account-1", MessagePayload(provider_id="export", provider_thread_id="export",
            attachments=[AttachmentPayload(provider_id="original", filename="original.txt",
                mime_type="text/plain", content=b"Exact original bytes\r\n")]),
    )
    attachment = repository.db.query("SELECT * FROM attachments WHERE message_id=?", (identifier,))[0]
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.context = SimpleNamespace(database=repository.db, repository=repository)
    controller._status = ""
    destination = tmp_path / "export.txt"
    controller.exportAttachment(attachment["id"], QUrl.fromLocalFile(str(destination)).toString())
    assert destination.read_bytes() == b"Exact original bytes\r\n"
    assert destination.stat().st_mode & 0o777 == 0o600
    repository.blobs.get = Mock(side_effect=ValueError("tampered encrypted original"))
    controller.exportAttachment(attachment["id"], QUrl.fromLocalFile(str(destination)).toString())
    assert destination.read_bytes() == b"Exact original bytes\r\n"
    assert "could not be exported" in controller._status


def test_trashed_source_cannot_be_opened_or_exported_from_stale_answer(
    repository, tmp_path, monkeypatch,
):
    message_id = repository.ingest_message(
        "account-1", MessagePayload(provider_id="stale-file", provider_thread_id="stale-file",
            attachments=[AttachmentPayload(provider_id="original", filename="record.txt",
                mime_type="text/plain", content=b"Exact private original")]),
    )
    attachment_id = repository.db.scalar(
        "SELECT id FROM attachments WHERE message_id=?", (message_id,)
    )
    chunk_id = repository.db.scalar(
        "SELECT id FROM source_chunks WHERE source_type='attachment' AND source_id=? "
        "ORDER BY chunk_index DESC LIMIT 1", (attachment_id,)
    )
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,))
    destination = tmp_path / "stale-export.txt"
    opened = []
    monkeypatch.setattr(
        "email_reader.controller.QDesktopServices",
        SimpleNamespace(openUrl=lambda url: opened.append(url.toLocalFile()) or True),
    )
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.context = SimpleNamespace(
        database=repository.db, repository=repository, runtime_dir=tmp_path / "runtime"
    )
    controller._status = ""

    assert repository.resolve_source_link(chunk_id) == (thread_id, message_id)
    assert repository.answer_file_cards([attachment_id], [])
    repository.trash_thread(thread_id)
    assert repository.resolve_source_link(attachment_id) is None
    assert repository.resolve_source_link(message_id) is None
    assert repository.resolve_source_link(chunk_id) is None
    assert repository.answer_file_cards([attachment_id], []) == []
    with pytest.raises(FileNotFoundError):
        repository.export_attachment(attachment_id, tmp_path / "direct-export")
    controller.openAttachment(attachment_id)
    controller.exportAttachment(attachment_id, QUrl.fromLocalFile(str(destination)).toString())
    assert not opened and not destination.exists()
    assert not (tmp_path / "runtime").exists()
    assert "not available" in controller._status


def test_opening_same_named_attachments_keeps_each_exact_original(
    repository, tmp_path, monkeypatch,
):
    message_id = repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="same-name-files", provider_thread_id="same-name-files",
            attachments=[
                AttachmentPayload(provider_id="first", filename="document.pdf",
                                  mime_type="application/pdf", content=b"first original"),
                AttachmentPayload(provider_id="second", filename="document.pdf",
                                  mime_type="application/pdf", content=b"second original"),
            ],
        ),
    )
    attachments = repository.db.query(
        "SELECT id FROM attachments WHERE message_id=? ORDER BY provider_id", (message_id,)
    )
    opened = []
    monkeypatch.setattr(
        "email_reader.controller.QDesktopServices",
        SimpleNamespace(openUrl=lambda url: opened.append(url.toLocalFile()) or True),
    )
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.context = SimpleNamespace(repository=repository, runtime_dir=tmp_path / "runtime")
    controller.openAttachment(attachments[0]["id"])
    controller.openAttachment(attachments[1]["id"])
    assert len(opened) == 2 and opened[0] != opened[1]
    assert [Path(path).read_bytes() for path in opened] == [
        b"first original", b"second original",
    ]


def test_manual_draft_only_saves_after_explicit_call(repository):
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller._selected_thread = source(repository)
    controller._accounts = repository.list_accounts()
    controller._selected_account = ""
    controller._editor_draft = {}
    controller._busy = False
    controller.context = SimpleNamespace(repository=repository, database=repository.db)
    controller.reload = Mock()
    controller._set_status = Mock()
    provider = Mock()
    provider.create_draft.return_value = DraftReceipt(
        provider_draft_id="draft", provider_message_id="draft-message"
    )
    controller._provider = Mock(return_value=provider)
    controller._run = lambda label, work, done, **kw: done(work(lambda _: None))
    controller.beginDraft("reply")
    controller._provider.assert_not_called()
    assert controller.editorDraft["body"] == ""
    controller.saveEditor(
        {
            **controller.editorDraft,
            "body": "Human reviewed",
            "cc": "cc@example.net",
            "bcc": "bcc@example.net",
        }
    )
    controller._provider.assert_called_once_with("account-1")
    request = provider.create_draft.call_args.args[0]
    assert request.body_text == "Human reviewed"
    assert request.to == ["maya@example.net"]
    assert request.cc == ["cc@example.net"] and request.bcc == ["bcc@example.net"]
    provider.send.assert_not_called()
    assert not controller.editorDraft
    assert repository.list_threads(mailbox="drafts")
    recorded = json.loads(repository.db.scalar(
        "SELECT selected_candidate_json FROM draft_records WHERE provider_draft_id='draft'"
    ))
    assert recorded["to"] == ["maya@example.net"]
    assert recorded["cc"] == ["cc@example.net"]
    assert recorded["bcc"] == ["bcc@example.net"]


def test_compose_uses_reviewed_editor_with_selected_identity(repository):
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller._selected_thread = {}
    controller._accounts = repository.list_accounts()
    controller._selected_account = "account-1"
    controller._editor_draft = {}
    controller._set_status = Mock()
    controller._provider = Mock()
    controller.context = SimpleNamespace(repository=repository)

    controller.beginDraft("new")

    assert controller.editorDraft["mode"] == "new"
    assert controller.editorDraft["account_id"] == "account-1"
    assert controller.editorDraft["to"] == ""
    assert controller.editorDraft["cc"] == ""
    assert controller.editorDraft["bcc"] == ""
    controller._provider.assert_not_called()

    main_qml = (Path(__file__).resolve().parents[1] / "src/email_reader/qml/Main.qml").read_text()
    editor_qml = (Path(__file__).resolve().parents[1] / "src/email_reader/qml/DraftEditor.qml").read_text()
    assert 'onClicked: root.startDraft("new")' in main_qml
    assert "composeDialog" not in main_qml
    assert "App.composeDraft" not in main_qml
    assert "App.draftChoices" not in main_qml
    assert "App.saveReplyDraft" not in main_qml
    assert "App.saveComposeDraft" not in main_qml
    assert "id: ccField" in editor_qml and "id: bccField" in editor_qml
    assert "if (dirty) confirmClose.open()" in editor_qml


def test_apple_mail_draft_attachment_is_private_and_cleaned(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_READER_DATA_DIR", str(tmp_path))
    info = AppleMailAccountInfo("Fictional", "owner@example.com", ProviderKind.GMAIL, ())
    provider = AppleMailProvider(info.account_id, info)
    visited = []

    def fake_create(request, files):
        from pathlib import Path

        for value in files:
            path = Path(value)
            assert path.is_relative_to(tmp_path / "recovery")
            assert path.read_bytes() == b"fictional attachment"
            assert path.stat().st_mode & 0o777 == 0o600
            visited.append(path)
        return DraftReceipt(provider_draft_id="fake")

    monkeypatch.setattr(provider, "_create_draft_with_files", fake_create)
    provider.create_draft(
        DraftRequest(
            subject="Draft",
            body_text="Reviewed",
            attachments=[
                AttachmentPayload(
                    provider_id="a", filename="../../document.txt", content=b"fictional attachment"
                )
            ],
        )
    )
    assert visited and all(not path.exists() for path in visited)
