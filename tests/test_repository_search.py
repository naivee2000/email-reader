from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from email_reader.contracts import (
    AttachmentPayload,
    DraftReceipt,
    DraftRequest,
    MessagePayload,
)
from email_reader.extraction import AttachmentExtractor, ExtractionResult
from email_reader.repository import MailRepository
from email_reader.search import HybridSearch


def sample_message(
    provider_id: str = "message-1", body: str = "The renewal deadline is September 12."
) -> MessagePayload:
    return MessagePayload(
        provider_id=provider_id,
        provider_thread_id="thread-1",
        subject="Renewal proposal",
        sender_name="Maya",
        sender_email="maya@example.net",
        to=["owner@example.com"],
        sent_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        body_text=body,
        snippet=body,
        attachments=[
            AttachmentPayload(
                provider_id="attachment-1",
                filename="options.txt",
                mime_type="text/plain",
                content=b"Option 1 costs $48,000. Option 2 has a seven percent discount.",
                size=64,
            )
        ]
        if provider_id == "message-1"
        else [],
    )


def test_unchanged_reimport_preserves_vectors_and_skips_attachment_extraction(repository):
    payload = sample_message()
    message_id = repository.ingest_message("account-1", payload)
    repository.db.execute(
        "UPDATE source_chunks SET embedding_json='[1,0]',embedding_model='fixture'"
    )
    original = [dict(row) for row in repository.db.query("SELECT * FROM source_chunks ORDER BY id")]
    repository.extractor.extract = Mock(wraps=repository.extractor.extract)
    refreshed = payload.model_copy(update={"is_unread": True, "web_link": "https://example.com/mail"})
    assert repository.ingest_message("account-1", refreshed) == message_id
    repository.extractor.extract.assert_not_called()
    assert original == [dict(row) for row in repository.db.query("SELECT * FROM source_chunks ORDER BY id")]
    assert repository.db.scalar("SELECT is_unread FROM messages WHERE id=?", (message_id,)) == 1
    assert repository.db.scalar("SELECT web_link FROM messages WHERE id=?", (message_id,)) == "https://example.com/mail"


def test_real_message_edit_invalidates_text_vectors_not_unchanged_file_vectors(repository):
    payload = sample_message()
    repository.ingest_message("account-1", payload)
    repository.db.execute("UPDATE source_chunks SET embedding_json='[1,0]',embedding_model='fixture'")
    repository.extractor.extract = Mock(wraps=repository.extractor.extract)
    repository.ingest_message("account-1", payload.model_copy(update={"body_text": "A revised deadline."}))
    repository.extractor.extract.assert_not_called()
    for row in repository.db.query("SELECT source_type,chunk_index,embedding_json,embedding_model FROM source_chunks"):
        if row["source_type"] == "message" or row["chunk_index"] == -1:
            assert row["embedding_json"] is None and row["embedding_model"] is None
        else:
            assert row["embedding_json"] == "[1,0]" and row["embedding_model"] == "fixture"


def test_successful_empty_ocr_is_cached_but_changed_image_is_rechecked(repository):
    payload = sample_message()
    repository.extractor.extract = Mock(return_value=ExtractionResult("", {"ocr": "macOS Vision"}))
    repository.ingest_message("account-1", payload)
    repository.ingest_message("account-1", payload)
    assert repository.extractor.extract.call_count == 1
    assert repository.db.scalar("SELECT count(*) FROM source_chunks WHERE source_type='attachment' AND chunk_index>=0") == 0
    changed = payload.attachments[0].model_copy(update={"content": b"changed image"})
    repository.ingest_message("account-1", payload.model_copy(update={"attachments": [changed]}))
    assert repository.extractor.extract.call_count == 2


def test_failed_or_partial_extraction_is_retried_with_identical_bytes(repository):
    payload = sample_message()
    for text in ("", "Partially extracted text"):
        repository.extractor.extract = Mock(return_value=ExtractionResult(text, warnings=["OCR unavailable"]))
        repository.ingest_message("account-1", payload)
        repository.ingest_message("account-1", payload)
        assert repository.extractor.extract.call_count == 2


def test_extraction_version_change_and_legacy_metadata_require_rebuild(repository):
    payload = sample_message()
    repository.ingest_message("account-1", payload)
    repository.extractor.extract = Mock(wraps=repository.extractor.extract)
    metadata = json.loads(repository.db.scalar("SELECT extraction_metadata_json FROM attachments"))
    metadata["_extractor_version"] = 0
    repository.db.execute("UPDATE attachments SET extraction_metadata_json=?", (json.dumps(metadata),))
    repository.ingest_message("account-1", payload)
    repository.extractor.extract.assert_called_once()
    repository.db.execute("UPDATE attachments SET extraction_metadata_json='{}'")
    repository.ingest_message("account-1", payload)
    assert repository.extractor.extract.call_count == 2


def test_outdated_pdfs_reindex_from_local_encrypted_originals_in_resumable_batches(repository):
    repository.extractor.extract = Mock(return_value=ExtractionResult("Old indexed header"))
    for number in (1, 2):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"pdf-{number}", provider_thread_id=f"pdf-{number}",
            subject="Scanned records", body_text="A document is attached.",
            attachments=[AttachmentPayload(
                provider_id=f"attachment-{number}", filename=f"record-{number}.pdf",
                mime_type="application/pdf", content=f"original-{number}".encode(),
            )],
        ))
    before = [row["content_digest"] for row in repository.db.query(
        "SELECT content_digest FROM attachments ORDER BY id"
    )]
    threads = repository.list_threads()
    for thread in threads:
        repository.save_summary(thread["id"], thread["content_hash"], '{"key_points":[]}', "test")
    repository.db.execute(
        "UPDATE attachments SET extraction_metadata_json=?",
        (json.dumps({"_extractor_version": 1, "_extraction_complete": True}),),
    )
    repository.extractor.extract = Mock(return_value=ExtractionResult(
        "Scanned renewal deadline November 9, 2026."
    ))
    assert repository.reindex_outdated_pdf_extractions(limit=1) == {
        "updated": 1, "remaining": 1,
    }
    assert repository.reindex_outdated_pdf_extractions(limit=1) == {
        "updated": 1, "remaining": 0,
    }
    assert repository.extractor.extract.call_count == 2
    assert [row["content_digest"] for row in repository.db.query(
        "SELECT content_digest FROM attachments ORDER BY id"
    )] == before
    assert all(repository.cached_summary(thread["id"], thread["content_hash"]) is None
               for thread in threads)
    assert len(HybridSearch(repository.db, repository).search(
        "Scanned renewal deadline", semantic=False
    )) == 2


def test_outdated_utf16_text_reindexes_locally_without_touching_current_pdf(repository):
    repository.extractor.extract = Mock(return_value=ExtractionResult("Garbled old text"))
    fixtures = (
        ("text-1", "policy.txt", "text/plain", "Insurance effective date November 9".encode("utf-16")),
        ("text-2", "options.csv", "text/csv", "Type,Amount\nCollision,48000".encode("utf-16")),
        ("text-3", "untitled", "text/plain; charset=utf-16", "Unfiled benefit memo".encode("utf-16")),
        ("pdf-1", "current.pdf", "application/pdf", b"fictional PDF original"),
    )
    for provider_id, filename, mime_type, content in fixtures:
        repository.ingest_message("account-1", MessagePayload(
            provider_id=provider_id, provider_thread_id=provider_id,
            subject="Attachment source", body_text="Original preserved.",
            attachments=[AttachmentPayload(
                provider_id=f"attachment-{provider_id}", filename=filename,
                mime_type=mime_type, content=content,
            )],
        ))
    originals = [row["content_digest"] for row in repository.db.query(
        "SELECT content_digest FROM attachments ORDER BY id"
    )]
    threads = repository.list_threads()
    for thread in threads:
        repository.save_summary(thread["id"], thread["content_hash"], '{"key_points":[]}', "test")
    repository.db.execute(
        "UPDATE attachments SET extraction_metadata_json=? WHERE filename IN ('policy.txt','options.csv','untitled')",
        (json.dumps({"_extractor_version": 2, "_extraction_complete": True}),),
    )
    repository.extractor.extract = Mock(wraps=AttachmentExtractor().extract)
    assert repository.reindex_outdated_text_extractions(limit=1) == {
        "updated": 1, "remaining": 2,
    }
    assert repository.reindex_outdated_text_extractions(limit=1) == {
        "updated": 1, "remaining": 1,
    }
    assert repository.reindex_outdated_text_extractions(limit=1) == {
        "updated": 1, "remaining": 0,
    }
    assert repository.reindex_outdated_text_extractions(limit=1) == {
        "updated": 0, "remaining": 0,
    }
    assert repository.extractor.extract.call_count == 3
    assert [row["content_digest"] for row in repository.db.query(
        "SELECT content_digest FROM attachments ORDER BY id"
    )] == originals
    assert len(HybridSearch(repository.db, repository).search(
        "Insurance effective date", semantic=False
    )) >= 1
    assert len(HybridSearch(repository.db, repository).search("Collision", semantic=False)) >= 1
    assert len(HybridSearch(repository.db, repository).search("Unfiled benefit", semantic=False)) >= 1
    text_threads = {row["thread_id"] for row in repository.db.query(
        "SELECT m.thread_id FROM messages m JOIN attachments a ON a.message_id=m.id "
        "WHERE a.filename IN ('policy.txt','options.csv','untitled')"
    )}
    for thread in threads:
        cached = repository.cached_summary(thread["id"], thread["content_hash"])
        assert (cached is None) == (thread["id"] in text_threads)


def test_outdated_office_files_reindex_from_encrypted_originals_only(repository):
    repository.extractor.extract = Mock(return_value=ExtractionResult("Old indexed text"))
    for index, filename in enumerate(("policy.docx", "premium.xlsx", "slides.pptx", "unchanged.pdf"), 1):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"office-{index}", provider_thread_id=f"office-{index}",
            subject="Attached records", body_text="Original is attached.",
            attachments=[AttachmentPayload(
                provider_id=f"file-{index}", filename=filename,
                mime_type="application/octet-stream", content=f"original-{index}".encode(),
            )],
        ))
    originals = [row["content_digest"] for row in repository.db.query(
        "SELECT content_digest FROM attachments ORDER BY filename"
    )]
    repository.db.execute(
        "UPDATE attachments SET extraction_metadata_json=?",
        (json.dumps({"_extractor_version": 2, "_extraction_complete": True}),),
    )
    repository.extractor.extract = Mock(return_value=ExtractionResult(
        "Saved premium amount 52500; policy renewal November 9, 2026"
    ))
    assert repository.reindex_outdated_office_extractions(limit=1) == {
        "updated": 1, "remaining": 2,
    }
    assert repository.reindex_outdated_office_extractions(limit=1) == {
        "updated": 1, "remaining": 1,
    }
    assert repository.reindex_outdated_office_extractions(limit=1) == {
        "updated": 1, "remaining": 0,
    }
    assert repository.reindex_outdated_office_extractions(limit=1) == {
        "updated": 0, "remaining": 0,
    }
    assert repository.extractor.extract.call_count == 3
    assert [row["content_digest"] for row in repository.db.query(
        "SELECT content_digest FROM attachments ORDER BY filename"
    )] == originals
    assert len(HybridSearch(repository.db, repository).search(
        "Saved premium amount 52500", semantic=False
    )) == 3


def test_partial_attachment_index_is_rebuilt(repository):
    payload = sample_message()
    repository.extractor.extract = Mock(return_value=ExtractionResult("Detailed text. " * 1500))
    repository.ingest_message("account-1", payload)
    assert repository.db.scalar("SELECT count(*) FROM source_chunks WHERE source_type='attachment' AND chunk_index>=0") > 1
    repository.db.execute("DELETE FROM source_chunks WHERE source_type='attachment' AND chunk_index=0")
    repository.ingest_message("account-1", payload)
    assert repository.extractor.extract.call_count == 2


def test_changed_empty_attachment_clears_stale_extracted_text_and_chunks(repository):
    payload = sample_message()
    repository.ingest_message("account-1", payload)
    repository.extractor.extract = Mock(wraps=repository.extractor.extract)
    changed = payload.attachments[0].model_copy(update={"content": b"", "size": 0})
    repository.ingest_message("account-1", payload.model_copy(update={"attachments": [changed]}))
    repository.extractor.extract.assert_called_once()
    assert repository.db.scalar("SELECT extracted_text FROM attachments") == ""
    assert repository.db.scalar("SELECT count(*) FROM source_chunks WHERE source_type='attachment' AND chunk_index>=0") == 0
    assert not HybridSearch(repository.db, repository).search("$48,000", semantic=False)


def test_missing_attachment_index_is_rebuilt_despite_identical_bytes(repository):
    payload = sample_message()
    repository.ingest_message("account-1", payload)
    repository.db.execute("DELETE FROM source_chunks WHERE source_type='attachment' AND chunk_index>=0")
    repository.extractor.extract = Mock(wraps=repository.extractor.extract)
    repository.ingest_message("account-1", payload)
    repository.extractor.extract.assert_called_once()
    assert repository.db.scalar("SELECT count(*) FROM source_chunks WHERE source_type='attachment' AND chunk_index>=0") > 0


def test_identical_content_rethread_updates_citation_provenance_without_losing_vectors(repository):
    payload = sample_message()
    repository.ingest_message("account-1", payload)
    repository.db.execute("UPDATE source_chunks SET embedding_json='[1,0]',embedding_model='fixture'")
    message_id = repository.ingest_message("account-1", payload.model_copy(update={"provider_thread_id": "corrected-thread"}))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,))
    for row in repository.db.query("SELECT thread_id,embedding_json FROM source_chunks"):
        assert row["thread_id"] == thread_id
        assert row["embedding_json"] == "[1,0]"


def test_shortened_message_prunes_old_chunks_and_search_hits(repository):
    payload = sample_message("large", "Long content. " * 1500 + "ObsoleteUniquePhrase")
    message_id = repository.ingest_message("account-1", payload)
    assert repository.db.scalar("SELECT count(*) FROM source_chunks WHERE source_id=?", (message_id,)) > 1
    repository.ingest_message("account-1", payload.model_copy(update={"body_text": "Short replacement."}))
    assert repository.db.scalar("SELECT count(*) FROM source_chunks WHERE source_id=?", (message_id,)) == 1
    assert not HybridSearch(repository.db, repository).search("ObsoleteUniquePhrase", semantic=False)


def test_metadata_only_attachment_refresh_keeps_original_extraction_metadata(repository):
    payload = sample_message()
    repository.ingest_message("account-1", payload)
    original = dict(repository.db.query("SELECT extracted_text,extraction_metadata_json,content_digest FROM attachments")[0])
    metadata_only = payload.attachments[0].model_copy(update={"content": None, "is_inline": True})
    repository.ingest_message("account-1", payload.model_copy(update={"attachments": [metadata_only]}))
    assert original == dict(repository.db.query("SELECT extracted_text,extraction_metadata_json,content_digest FROM attachments")[0])
    assert repository.db.scalar("SELECT is_inline FROM attachments") == 1


def test_ingest_search_provenance_and_attachment_export(
    repository: MailRepository, tmp_path: Path
) -> None:
    message_id = repository.ingest_message("account-1", sample_message())
    threads = repository.list_threads()
    assert len(threads) == 1
    assert threads[0]["has_attachments"] == 1

    hits = HybridSearch(repository.db, repository).search("renewal deadline")
    assert hits
    assert hits[0].citation.account_email == "owner@example.com"
    assert hits[0].citation.app_link.startswith("emailreader://source/")

    attachment = repository.db.connection.execute(
        "SELECT id FROM attachments WHERE message_id=?", (message_id,)
    ).fetchone()
    output = repository.export_attachment(attachment["id"], tmp_path / "export")
    assert output.name == "options.txt"
    assert b"$48,000" in output.read_bytes()


def test_keyword_only_search_does_not_request_query_embedding(repository: MailRepository):
    repository.ingest_message("account-1", sample_message())
    embedder = Mock()
    search = HybridSearch(repository.db, repository, embedder)
    hits = search.search("renewal deadline", semantic=False)
    assert hits
    embedder.embed.assert_not_called()


def test_reply_search_reuses_stored_message_vector_without_network(repository: MailRepository):
    anchor = repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="anchor", provider_thread_id="anchor",
            subject="Question", body_text="Could we discuss this?",
        )
    )
    related = repository.ingest_message(
        "account-1", MessagePayload(
            provider_id="related", provider_thread_id="related",
            subject="Other subject", body_text="The coverage details are attached.",
        )
    )
    for message_id, vector in ((anchor, [1.0, 0.0]), (related, [0.9, 0.1])):
        repository.db.execute(
            "UPDATE source_chunks SET embedding_json=?,embedding_model='test-vector' "
            "WHERE source_type='message' AND source_id=?",
            (json.dumps(vector), message_id),
        )
    embedder = Mock()
    search = HybridSearch(repository.db, repository, embedder)
    hits = search.search(
        "unmatched terms", semantic=False, semantic_message_id=anchor,
        account_id="account-1",
    )
    assert any("coverage details" in hit.content for hit in hits)
    embedder.embed.assert_not_called()


def test_answer_cards_put_same_email_pdf_companion_in_top_three(repository: MailRepository) -> None:
    repository.ingest_message("account-1", MessagePayload(
        provider_id="policy", provider_thread_id="policy", subject="Insurance documents",
        body_text="The policy documents are attached.",
        attachments=[
            AttachmentPayload(provider_id="card", filename="BMW ID CARD.pdf",
                              mime_type="application/pdf", content=b"card original"),
            AttachmentPayload(provider_id="binder", filename="BMW BINDER.pdf",
                              mime_type="application/pdf", content=b"binder original"),
            AttachmentPayload(provider_id="logo", filename="logo.png",
                              mime_type="image/png", content=b"tiny logo", is_inline=True),
        ],
    ))
    for name in ("Temporary ID Card.pdf", "Auto Quote.pdf"):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=name, provider_thread_id=name, subject=name, body_text="Another file",
            attachments=[AttachmentPayload(provider_id=name, filename=name,
                                           mime_type="application/pdf", content=name.encode())],
        ))
    ids = {row["filename"]: row["id"] for row in repository.db.query(
        "SELECT id,filename FROM attachments")}
    cards = repository.answer_file_cards(
        [ids["BMW ID CARD.pdf"], ids["Temporary ID Card.pdf"], ids["Auto Quote.pdf"]], []
    )
    assert [card["filename"] for card in cards[:3]] == [
        "BMW ID CARD.pdf", "BMW BINDER.pdf", "Temporary ID Card.pdf",
    ]
    assert [card["possible_match"] for card in cards[:3]] == [False, True, False]
    assert all(card["available"] and card["email_date"] for card in cards[:3])
    assert "logo.png" not in [card["filename"] for card in cards]


def test_summary_cache_invalidates_when_thread_changes(repository: MailRepository) -> None:
    repository.ingest_message("account-1", sample_message())
    thread = repository.list_threads()[0]
    repository.save_summary(thread["id"], thread["content_hash"], '{"key_points":[]}', "test")
    assert repository.cached_summary(thread["id"], thread["content_hash"])

    repository.ingest_message(
        "account-1", sample_message("message-2", "A new reply changes the thread.")
    )
    changed = repository.list_threads()[0]
    assert changed["content_hash"] != thread["content_hash"]
    assert repository.cached_summary(changed["id"], changed["content_hash"]) is None


def test_remote_deletion_is_retained_until_app_trash(repository: MailRepository) -> None:
    repository.ingest_message("account-1", sample_message())
    thread = repository.list_threads()[0]
    repository.mark_remote_deleted("account-1", ["message-1"])
    retained = repository.get_thread(thread["id"])
    assert retained is not None
    assert retained["messages"][0]["source_state"] == "remote_deleted"
    search = HybridSearch(repository.db, repository)
    assert search.search("renewal deadline"), "The encrypted local copy must remain searchable"
    assert search.conversational_search(["discount"]), "Retained original attachments stay searchable too"

    repository.trash_thread(thread["id"])
    assert repository.list_threads() == []
    assert search.search("renewal deadline") == []
    assert search.conversational_search(["discount"]) == []
    repository.db.execute(
        "UPDATE threads SET trashed_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(days=31)).isoformat(), thread["id"]),
    )
    assert repository.purge_expired_trash(30) == 1
    assert repository.get_thread(thread["id"]) is None


def test_refresh_does_not_resurrect_local_trash_or_move_it_to_visible_thread(repository):
    payload = sample_message()
    repository.ingest_message("account-1", payload)
    thread = repository.list_threads()[0]
    repository.trash_thread(thread["id"])
    repository.ingest_message("account-1", payload)
    repository.ingest_message("account-1", payload.model_copy(update={"provider_thread_id": "recovered-thread"}))
    # Even a newly discovered reply must not reset the trashed conversation.
    repository.ingest_message("account-1", sample_message("new-reply"))
    assert repository.list_threads() == []
    assert repository.db.scalar("SELECT COUNT(*) FROM threads") == 1
    assert repository.db.scalar("SELECT COUNT(*) FROM messages") == 2
    assert HybridSearch(repository.db, repository).search("renewal") == []


def test_sent_message_becomes_tone_example(repository: MailRepository) -> None:
    payload = sample_message(
        body="Hi Maya,\n\nThanks for the clear proposal. Option 2 works for us.\n\nBest,\nOwner"
    )
    payload.provider_id = "sent-message"
    payload.provider_thread_id = "sent-thread"
    payload.sender_email = "owner@example.com"
    payload.to = ["maya@example.net"]
    payload.is_sent = True
    payload.labels = ["SENT"]
    repository.ingest_message("account-1", payload)
    examples = repository.get_tone_examples("account-1", "maya@example.net")
    assert examples
    assert "Thanks for the clear proposal" in examples[0]["clean_body"]


def test_mailbox_views_separate_primary_updates_sent_and_drafts(
    repository: MailRepository,
) -> None:
    primary = sample_message("primary", "A personal message.")
    primary.provider_thread_id = "primary-thread"
    primary.labels = ["INBOX", "CATEGORY_PRIMARY"]
    repository.ingest_message("account-1", primary)

    update = sample_message("update", "Your statement is ready.")
    update.provider_thread_id = "update-thread"
    update.labels = ["INBOX", "CATEGORY_UPDATES"]
    repository.ingest_message("account-1", update)

    sent = sample_message("sent", "I sent this message.")
    sent.provider_thread_id = "sent-thread"
    sent.sender_email = "owner@example.com"
    sent.is_sent = True
    sent.labels = ["SENT"]
    repository.ingest_message("account-1", sent)

    repository.record_saved_draft(
        "account-1",
        DraftReceipt(provider_draft_id="provider-draft", provider_message_id="draft-message"),
        DraftRequest(subject="Draft subject", body_text="Draft body", to=["maya@example.net"]),
    )

    assert [item["subject"] for item in repository.list_threads(category="primary")] == [
        "Renewal proposal"
    ]
    assert [item["subject"] for item in repository.list_threads(category="updates")] == [
        "Renewal proposal"
    ]
    assert repository.list_threads(mailbox="sent")[0]["latest_sender"] == "To: owner@example.com"
    assert repository.list_threads(mailbox="drafts")[0]["latest_sender"] == "To: maya@example.net"
    counts = repository.mailbox_counts()
    assert counts["inbox"] == 2
    assert counts["primary_count"] == 1
    assert counts["updates_count"] == 1
    assert counts["sent"] == 1
    assert counts["drafts"] == 1


def test_main_inbox_scope_hides_but_does_not_delete_old_mail(
    repository: MailRepository,
) -> None:
    repository.ingest_message("account-1", sample_message("keep", "Keep this message."))
    repository.ingest_message("account-1", sample_message("hide", "Hide this archive."))

    visible = repository.apply_main_inbox_scope("account-1", {"keep"})

    assert visible == 1
    assert len(repository.list_threads()) == 1
    assert repository.db.scalar("SELECT COUNT(*) FROM messages") == 2
    assert HybridSearch(repository.db, repository).search("archive"), (
        "Hiding archived mail from the focused inbox must not hide it from an explicit search"
    )
    assert len(repository.list_threads(mailbox="all", thread_ids=[
        repository.db.scalar("SELECT thread_id FROM messages WHERE provider_id='hide'")
    ])) == 1


def test_provider_import_replaces_only_matching_saved_draft_mirror(
    repository: MailRepository,
) -> None:
    request = DraftRequest(
        subject="Verification draft", body_text="A reviewed draft body.",
        to=["owner@example.com"],
    )
    receipt = DraftReceipt(provider_draft_id="compose-id", provider_message_id="compose-id")
    mirror_id = repository.record_saved_draft("account-1", receipt, request)
    repository.db.execute(
        """INSERT INTO draft_records(id,account_id,thread_id,provider_draft_id,selected_candidate_json,created_at)
           VALUES(?,?,?,?,?,?)""",
        ("record-1", "account-1", None, "compose-id", "{}", repository.db.now()),
    )
    now = datetime.now(UTC)
    imported = MessagePayload(
        provider_id="rfc-id", provider_thread_id="rfc-id", subject=request.subject,
        sender_email="owner@example.com", to=request.to, body_text=request.body_text,
        labels=["DRAFT"], is_draft=True, sent_at=now, received_at=now,
        raw_source=b"Subject: Verification draft\r\n\r\nA reviewed draft body.",
    )
    imported_id = repository.ingest_message("account-1", imported)
    assert repository.db.scalar("SELECT count(*) FROM messages WHERE is_draft=1") == 1
    assert repository.db.scalar("SELECT count(*) FROM messages WHERE id=?", (mirror_id,)) == 0
    assert repository.db.scalar("SELECT count(*) FROM messages WHERE id=?", (imported_id,)) == 1
    assert repository.mailbox_counts()["drafts"] == 1


def test_ambiguous_saved_draft_mirror_is_not_removed(repository: MailRepository) -> None:
    request = DraftRequest(subject="Same subject", body_text="Same body", to=["owner@example.com"])
    repository.record_saved_draft(
        "account-1", DraftReceipt(provider_draft_id="compose-id", provider_message_id="compose-id"), request
    )
    now = datetime.now(UTC)
    for identifier in ("provider-a", "provider-b"):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=identifier, provider_thread_id=identifier,
            subject=request.subject, sender_email="owner@example.com", to=request.to,
            body_text=request.body_text, labels=["DRAFT"], is_draft=True,
            sent_at=now, received_at=now,
            raw_source=b"Subject: Same subject\r\n\r\nSame body",
        ))
    repository.db.execute(
        """INSERT INTO draft_records(id,account_id,thread_id,provider_draft_id,selected_candidate_json,created_at)
           VALUES(?,?,?,?,?,?)""",
        ("record-1", "account-1", None, "compose-id", "{}", repository.db.now()),
    )
    assert repository.reconcile_saved_draft_mirrors() == 0
    assert repository.db.scalar("SELECT count(*) FROM messages WHERE is_draft=1") == 3


def test_only_later_sent_version_matches_reviewed_draft_and_refreshes_its_tone(
    repository: MailRepository,
) -> None:
    now = datetime.now(UTC)
    incoming_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="incoming", provider_thread_id="reply-thread",
        sender_email="maya@example.net", to=["owner@example.com"],
        body_text="Please let me know what you decide.",
        sent_at=now - timedelta(days=2), received_at=now - timedelta(days=2),
        labels=["INBOX"],
    ))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (incoming_id,))
    repository.db.execute(
        """INSERT INTO draft_records(
            id,account_id,thread_id,provider_draft_id,selected_candidate_json,created_at
        ) VALUES(?,?,?,?,?,?)""",
        ("reviewed", "account-1", thread_id, "provider-draft", json.dumps({
            "subject": "(no subject)",
            "body": "This is the final wording I reviewed and sent from the provider mailbox.",
            "to": ["maya@example.net"], "cc": [], "bcc": [],
        }), now.isoformat()),
    )
    repository.save_tone_note("account-1", "maya@example.net", "Old Maya style", [])
    repository.save_tone_note("account-1", "", "Old account style", [])
    repository.save_tone_note("account-1", "other@example.net", "Unrelated style", [])

    old_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="historical-sent", provider_thread_id="reply-thread",
        sender_email="owner@example.com", to=["maya@example.net"],
        body_text="This earlier answer was sent before the reviewed draft existed.",
        sent_at=now - timedelta(seconds=30), received_at=now - timedelta(seconds=30),
        labels=["SENT"], is_sent=True,
    ))
    assert repository.db.scalar(
        "SELECT matched_sent_message_id FROM draft_records WHERE id='reviewed'"
    ) is None
    assert repository.cached_tone_note("account-1", "maya@example.net") is not None

    approved_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="approved-sent", provider_thread_id="reply-thread",
        sender_email="owner@example.com", to=["maya@example.net"],
        body_text="This is the final wording I reviewed and sent from the provider mailbox.",
        sent_at=now + timedelta(seconds=30), received_at=now + timedelta(seconds=30),
        labels=["SENT"], is_sent=True,
    ))
    assert repository.db.scalar(
        "SELECT matched_sent_message_id FROM draft_records WHERE id='reviewed'"
    ) == approved_id
    assert repository.db.scalar(
        "SELECT approved_sent_version FROM tone_examples WHERE message_id=?", (approved_id,)
    ) == 1
    assert repository.db.scalar(
        "SELECT approved_sent_version FROM tone_examples WHERE message_id=?", (old_id,)
    ) == 0
    assert repository.cached_tone_note("account-1", "maya@example.net") is None
    assert repository.cached_tone_note("account-1", "") is None
    assert repository.cached_tone_note("account-1", "other@example.net") is not None
    assert repository.get_tone_examples("account-1", "maya@example.net")[0]["message_id"] == approved_id


def test_reviewed_reply_matches_rethreaded_sent_copy_but_not_unrelated_same_thread_mail(
    repository: MailRepository,
) -> None:
    now = datetime.now(UTC)
    incoming_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="incoming-rethread", provider_thread_id="reply-source",
        subject="Follow-up", sender_email="maya@example.net", to=["owner@example.com"],
        body_text="Could you confirm?", sent_at=now - timedelta(days=1),
        received_at=now - timedelta(days=1), labels=["INBOX"],
    ))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (incoming_id,))
    repository.db.execute(
        """INSERT INTO draft_records(
            id,account_id,thread_id,provider_draft_id,selected_candidate_json,created_at
        ) VALUES(?,?,?,?,?,?)""",
        ("reply-review", "account-1", thread_id, "draft-reply", json.dumps({
            "subject": "Re: Follow-up", "body": "I can confirm the appointment.",
            "to": ["maya@example.net"], "cc": [], "bcc": [],
        }), now.isoformat()),
    )
    unrelated_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="same-thread-unrelated", provider_thread_id="reply-source",
        subject="Re: Follow-up", sender_email="owner@example.com", to=["maya@example.net"],
        body_text="I have another question about this.",
        sent_at=now + timedelta(seconds=10), received_at=now + timedelta(seconds=10),
        labels=["SENT"], is_sent=True,
    ))
    assert repository.db.scalar(
        "SELECT matched_sent_message_id FROM draft_records WHERE id='reply-review'"
    ) is None
    assert repository.db.scalar(
        "SELECT approved_sent_version FROM tone_examples WHERE message_id=?", (unrelated_id,)
    ) == 0

    approved_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="sent-rethreaded", provider_thread_id="new-provider-thread",
        subject="Re: Follow-up", sender_email="owner@example.com", to=["maya@example.net"],
        body_text="I can confirm the appointment.",
        sent_at=now + timedelta(seconds=20), received_at=now + timedelta(seconds=20),
        labels=["SENT"], is_sent=True,
    ))
    assert repository.db.scalar(
        "SELECT matched_sent_message_id FROM draft_records WHERE id='reply-review'"
    ) == approved_id
    assert repository.db.scalar(
        "SELECT approved_sent_version FROM tone_examples WHERE message_id=?", (approved_id,)
    ) == 1


def test_new_message_draft_matches_unique_exact_sent_copy_across_threads(
    repository: MailRepository,
) -> None:
    now = datetime.now(UTC)
    body = "This is the reviewed wording for the new message I chose to send."
    repository.db.execute(
        """INSERT INTO draft_records(
            id,account_id,thread_id,provider_draft_id,selected_candidate_json,created_at
        ) VALUES(?,? ,NULL,?,?,?)""",
        ("new-review", "account-1", "provider-draft", json.dumps({
            "subject": "New introduction", "body": body,
            "to": ["maya@example.net"], "cc": [], "bcc": [],
        }), now.isoformat()),
    )
    repository.save_tone_note("account-1", "maya@example.net", "Old note", [])
    sent_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="new-sent", provider_thread_id="different-provider-thread",
        subject="New introduction", sender_email="owner@example.com",
        to=["Maya <maya@example.net>"], body_text=body,
        sent_at=now + timedelta(seconds=30), received_at=now + timedelta(seconds=30),
        labels=["SENT"], is_sent=True,
    ))
    assert repository.db.scalar(
        "SELECT matched_sent_message_id FROM draft_records WHERE id='new-review'"
    ) == sent_id
    assert repository.db.scalar(
        "SELECT approved_sent_version FROM tone_examples WHERE message_id=?", (sent_id,)
    ) == 1
    assert repository.cached_tone_note("account-1", "maya@example.net") is None


def test_new_message_draft_does_not_guess_ambiguous_or_wrong_recipient_sent_copy(
    repository: MailRepository,
) -> None:
    now = datetime.now(UTC)
    body = "The same reviewed text could be sent to more than one person."
    candidate = json.dumps({"subject": "Shared subject", "body": body,
                            "to": ["maya@example.net"], "cc": [], "bcc": []})
    for identifier in ("review-a", "review-b"):
        repository.db.execute(
            """INSERT INTO draft_records(
                id,account_id,thread_id,provider_draft_id,selected_candidate_json,created_at
            ) VALUES(?,'account-1',NULL,?,?,?)""",
            (identifier, identifier, candidate, now.isoformat()),
        )
    repository.ingest_message("account-1", MessagePayload(
        provider_id="ambiguous-sent", provider_thread_id="ambiguous-thread",
        subject="Shared subject", sender_email="owner@example.com",
        to=["maya@example.net"], body_text=body,
        sent_at=now + timedelta(seconds=30), received_at=now + timedelta(seconds=30),
        labels=["SENT"], is_sent=True,
    ))
    assert repository.db.scalar(
        "SELECT COUNT(*) FROM draft_records WHERE matched_sent_message_id IS NOT NULL"
    ) == 0
    repository.db.execute("DELETE FROM draft_records WHERE id='review-b'")
    repository.ingest_message("account-1", MessagePayload(
        provider_id="wrong-recipient-sent", provider_thread_id="another-thread",
        subject="Shared subject", sender_email="owner@example.com",
        to=["other@example.net"], body_text=body,
        sent_at=now + timedelta(seconds=40), received_at=now + timedelta(seconds=40),
        labels=["SENT"], is_sent=True,
    ))
    assert repository.db.scalar(
        "SELECT matched_sent_message_id FROM draft_records WHERE id='review-a'"
    ) is None


def test_draft_keeps_mail_wrapped_body_visible_without_quote_toggle(
    repository: MailRepository,
) -> None:
    body = "This is the entire reviewed draft."
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="mail-draft", provider_thread_id="mail-draft",
        subject="Draft", sender_email="owner@example.com", to=["owner@example.com"],
        body_text=body, body_html=f"<div><blockquote>{body}</blockquote></div>",
        labels=["DRAFT"], is_draft=True,
    ))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,))
    message = repository.get_thread(thread_id)["messages"][0]
    assert message["new_content"] == body
    assert message["has_quoted_history"] is False


def test_repair_malformed_html_body_rebuilds_search_text(
    repository: MailRepository,
) -> None:
    raw_html = (
        "<!doctype html><html><head><style>.hidden{display:none}</style></head>"
        "<body><h1>Quarterly statement</h1><p>Net balance is $82.00.</p></body></html>"
    )
    message_id = repository.ingest_message("account-1", sample_message("malformed-html", raw_html))
    # Simulate a legacy database. New imports now repair this at ingestion.
    repository.db.execute(
        "UPDATE messages SET body_text=?,body_html='' WHERE id=?", (raw_html, message_id)
    )

    assert repository.repair_malformed_html_bodies() == 1

    row = repository.db.connection.execute(
        "SELECT body_text,body_html FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    assert "<!doctype" not in row["body_text"].casefold()
    assert "Quarterly statement" in row["body_text"]
    assert row["body_html"] == raw_html
    assert HybridSearch(repository.db, repository).search("net balance")


def test_repair_prose_prefixed_html_template_rebuilds_existing_mail(repository):
    markup = (
        "Open to learn whatâ\x80\x99s changed."
        + "<div>" * 8
        + "â\x80\x8c " * 30
        + "<p>Policy details</p>"
        + "</div>" * 8
    )
    message_id = repository.ingest_message("account-1", sample_message("legacy-template", "Plain"))
    repository.db.execute(
        "UPDATE messages SET body_text=?,body_html='',snippet=? WHERE id=?",
        (markup, markup[:240], message_id),
    )
    assert repository.repair_malformed_html_bodies() == 1
    row = repository.db.connection.execute(
        "SELECT body_text,body_html,snippet FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    assert "what’s changed" in row["body_text"]
    assert "Policy details" in row["body_text"]
    assert "<div>" not in row["snippet"]
    assert row["body_html"] == markup
