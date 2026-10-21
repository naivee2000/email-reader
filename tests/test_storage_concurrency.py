"""Fictional storage/work-queue regressions; never use a user's mail or keys."""

from datetime import UTC, datetime, timedelta
from threading import Event, Thread, Timer
from time import perf_counter
from unittest.mock import Mock

import pytest

from email_reader.backup import BackupManager
from email_reader.contracts import AttachmentPayload, MessagePayload
from email_reader.database import Database


def test_legacy_read_facade_never_observes_uncommitted_writer(tmp_path):
    db = Database(tmp_path / "fictional.db", "test", allow_plaintext=True)
    db.open()
    entered, release = Event(), Event()

    def write():
        try:
            with db.transaction() as connection:
                connection.execute("INSERT INTO app_settings VALUES('probe','uncommitted')")
                entered.set()
                assert release.wait(3)
                raise ValueError("rollback fictional transaction")
        except ValueError:
            pass

    worker = Thread(target=write)
    worker.start()
    try:
        assert entered.wait(2)
        started = perf_counter()
        assert db.connection.execute("SELECT value FROM app_settings WHERE key='probe'").fetchone() is None
        assert perf_counter() - started < 0.5
    finally:
        release.set()
        worker.join(3)
    assert db.scalar("SELECT value FROM app_settings WHERE key='probe'") is None
    db.close()
    with pytest.raises(RuntimeError, match="closed"):
        db.query("SELECT 1")


def test_read_snapshot_stays_consistent_while_writer_commits(tmp_path):
    db = Database(tmp_path / "fictional.db", "test", allow_plaintext=True)
    db.open()
    db.execute("INSERT INTO app_settings VALUES('probe','before')")
    with db.read_snapshot():
        assert db.scalar("SELECT value FROM app_settings WHERE key='probe'") == "before"
        worker = Thread(target=lambda: db.execute("UPDATE app_settings SET value='after' WHERE key='probe'"))
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        assert db.scalar("SELECT value FROM app_settings WHERE key='probe'") == "before"
    assert db.scalar("SELECT value FROM app_settings WHERE key='probe'") == "after"
    db.close()


def attachment_message(content=b"Important fictional insurance text"):
    return MessagePayload(
        provider_id="deferred", provider_thread_id="deferred", subject="Fictional insurance",
        sender_email="person@example.net", body_text="The original file is attached.",
        attachments=[AttachmentPayload(provider_id="document", filename="policy.txt", mime_type="text/plain", content=content)],
    )


def test_deferred_extraction_preserves_original_and_invalidates_summary(repository):
    repository.extractor.extract = Mock(wraps=repository.extractor.extract)
    identifier = repository.ingest_message("account-1", attachment_message(), defer_extraction=True)
    repository.extractor.extract.assert_not_called()
    row = repository.db.query("SELECT * FROM attachments WHERE message_id=?", (identifier,))[0]
    assert repository.blobs.get(row["content_digest"]) == attachment_message().attachments[0].content
    assert row["extracted_text"] == "" and repository.pending_enrichment_count() == 1
    thread = repository.get_thread(repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (identifier,)))
    repository.save_summary(thread["id"], thread["content_hash"], "{}", "fictional")
    assert repository.process_enrichment(limit=1) == {"completed": 1, "failed": 0, "remaining": 0}
    assert repository.db.scalar("SELECT extracted_text FROM attachments WHERE id=?", (row["id"],))
    assert repository.get_thread(thread["id"])["content_hash"] != thread["content_hash"]
    assert repository.db.scalar("SELECT count(*) FROM summaries") == 0


def test_superseded_extraction_cannot_overwrite_new_original(repository):
    original_extract = repository.extractor.extract
    repository.ingest_message("account-1", attachment_message(b"old contents"), defer_extraction=True)

    def changed_during_extract(*args):
        repository.ingest_message("account-1", attachment_message(b"new contents"), defer_extraction=True)
        return original_extract(*args)

    repository.extractor.extract = Mock(side_effect=changed_during_extract)
    assert repository.process_enrichment(limit=1)["remaining"] == 1
    row = repository.db.query("SELECT content_digest,extracted_text FROM attachments")[0]
    assert repository.blobs.get(row["content_digest"]) == b"new contents"
    assert row["extracted_text"] == ""


def test_lazy_thread_only_hydrates_requested_body(repository):
    stamp = datetime.now(UTC)
    for index in range(10):
        repository.ingest_message("account-1", MessagePayload(
            provider_id=f"body-{index}", provider_thread_id="long-thread", body_text=f"Body {index}",
            sent_at=stamp + timedelta(seconds=index),
        ))
    thread_id = repository.db.scalar("SELECT id FROM threads WHERE provider_thread_id='long-thread'")
    thread = repository.get_thread_for_read(thread_id)
    assert len(thread["messages"]) == 10
    assert sum(bool(message["body_text"]) for message in thread["messages"]) == 1
    assert thread["messages"][-1]["body_text"] == "Body 9"
    assert repository.prepare_message_for_read(thread["messages"][0]["id"])["body_text"] == "Body 0"


def test_indexes_contacts_and_exclusion_checkpoints_are_scoped(repository):
    repository.ingest_message("account-1", MessagePayload(
        provider_id="header", provider_thread_id="header", subject="Fictional happy hour",
        sender_name="Alex", sender_email="alex@example.net", body_text="", is_sent=True,
        to=["Sam <sam@example.net>"],
    ))
    repository.db.execute("DELETE FROM message_header_fts")
    assert repository.search_header_thread_ids("happy hour", "account-1")
    assert repository.maintain_local_indexes()["remaining"] == 0
    assert repository.search_header_thread_ids("happy hour", "account-1")
    assert not repository.search_header_thread_ids("happy hour", "other")
    assert repository.recipient_suggestions("account-1", "sam")[0]["email"] == "sam@example.net"
    repository.record_import_exclusions("account-1", {"ad": "promotions"}, "v2")
    assert repository.known_import_exclusions("account-1", "v2") == {"ad"}
    assert not repository.known_import_exclusions("account-1", "v1")
    repository.db.execute("UPDATE import_exclusions SET recorded_at=?", ((datetime.now(UTC)-timedelta(days=2)).isoformat(),))
    assert not repository.known_import_exclusions("account-1", "v2")
    repository.save_import_checkpoint("account-1", "repair", "v2", cursor="checkpoint", page_token="page", since="2025-01-01")
    assert repository.get_import_checkpoint("account-1", "repair", "v2")["page_token"] == "page"
    assert repository.get_import_checkpoint("account-1", "repair", "v1") is None
    repository.ingest_message("account-1", MessagePayload(
        provider_id="chinese", provider_thread_id="chinese", subject="汽车保险保单文件", body_text="",
    ))
    assert repository.search_header_thread_ids("汽车保险")


def test_failed_backup_never_publishes_partial_file(tmp_path):
    class FailingDatabase:
        def backup_to(self, path):
            path.write_bytes(b"unfinished fictional backup")
            raise RuntimeError("interrupted")
    manager = BackupManager(FailingDatabase(), tmp_path, b"k"*32)
    with pytest.raises(RuntimeError):
        manager.daily_backup()
    assert not list(tmp_path.glob("email-reader-*.db"))
    assert not list(tmp_path.glob("*.staging"))


def test_existing_unfinished_backup_is_not_blessed(tmp_path):
    db = Database(tmp_path / "fixture.db", "test", allow_plaintext=True)
    db.open()
    manager = BackupManager(db, tmp_path / "backups", b"k"*32)
    target = manager.backup_dir / f"email-reader-{datetime.now(UTC):%Y-%m-%d}.db"
    target.write_bytes(b"unfinished")
    with pytest.raises(RuntimeError, match="failed validation"):
        manager.daily_backup()
    assert not target.with_suffix(".manifest.json").exists()
    db.close()


def test_close_waits_for_native_read_then_rejects_new_work(tmp_path):
    db = Database(tmp_path / "fictional.db", "test", allow_plaintext=True)
    db.open()
    entered, release, closing = Event(), Event(), Event()
    results, errors = [], []

    def slow_query():
        def pause():
            entered.set()
            assert release.wait(3)
            return 7
        try:
            db._reader().create_function("fictional_pause", 0, pause)
            results.extend(db.query("SELECT fictional_pause()"))
        except Exception as error:
            errors.append(error)

    reader = Thread(target=slow_query)
    reader.start()
    assert entered.wait(2)

    def close():
        closing.set()
        db.close()

    closer = Thread(target=close)
    closer.start()
    assert closing.wait(1)
    try:
        closer.join(.05)
        assert closer.is_alive(), "Close destroyed an active native reader"
    finally:
        release.set()
        reader.join(3)
        closer.join(3)
    assert not errors and results[0][0] == 7
    assert not closer.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        db.query("SELECT 1")


def test_long_lived_reader_fts_tracks_updates_and_deletes(repository, tmp_path):
    from email_reader.repository import MailRepository
    database_path = tmp_path / "fictional-fts.db"
    repository.db.backup_to(database_path)
    db = Database(database_path, "test", allow_plaintext=True)
    db.open()
    repo = MailRepository(db, repository.blobs, repository.extractor)
    previous = ""
    for term in ("fixturealpha", "fixturebeta", "fixturegamma"):
        repo.ingest_message("account-1", MessagePayload(
            provider_id="changing", provider_thread_id="changing", subject=term, body_text=term,
        ))
        assert db.scalar("SELECT count(*) FROM source_fts WHERE source_fts MATCH ?", (term,)) == 1
        assert repo.search_header_thread_ids(term)
        if previous:
            assert db.scalar("SELECT count(*) FROM source_fts WHERE source_fts MATCH ?", (previous,)) == 0
            assert not repo.search_header_thread_ids(previous)
        assert db.integrity_check() and db.verify_backup(database_path)
        assert not db._reader().in_transaction
        previous = term
    repo._purge_thread(db.scalar("SELECT id FROM threads"))
    assert db.scalar("SELECT count(*) FROM source_fts WHERE source_fts MATCH ?", (previous,)) == 0
    assert not repo.search_header_thread_ids(previous)
    assert db.integrity_check() and db.verify_backup(database_path)
    db.close()


def test_isolated_extraction_roundtrip_and_owned_worker_cancellation(tmp_path):
    import sys

    from email_reader.extractor_worker import ExtractionCancelled, extract_isolated

    result = extract_isolated("private-fictional.txt", "text/plain", b"Fictional text only", temp_root=tmp_path)
    assert result.text == "Fictional text only"
    assert not list(tmp_path.iterdir())
    cancelled = Event()
    timer = Timer(.2, cancelled.set)
    timer.start()
    started = perf_counter()
    try:
        with pytest.raises(ExtractionCancelled):
            extract_isolated("private-fictional.pdf", "application/pdf", b"fictional bytes", temp_root=tmp_path,
                             should_cancel=cancelled.is_set,
                             command=[sys.executable, "-c", "import time; time.sleep(20)"])
        assert perf_counter() - started < 2
        assert not list(tmp_path.iterdir())
    finally:
        timer.cancel()
    with pytest.raises(TimeoutError):
        extract_isolated("fictional.pdf", "application/pdf", b"fictional", temp_root=tmp_path, timeout=.2,
                         command=[sys.executable, "-c", "import time; time.sleep(20)"])
    assert not list(tmp_path.iterdir())


def test_sqlcipher_readers_and_backup_stay_encrypted(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    monkeypatch.delenv("EMAIL_READER_TESTING", raising=False)
    path = tmp_path / "fictional-encrypted.db"
    db = Database(path, "fictional-test-key")
    db.open()
    try:
        db.execute("INSERT INTO app_settings VALUES('probe','fictional private text')")
        assert db.scalar("SELECT value FROM app_settings WHERE key='probe'") == "fictional private text"
        backup = tmp_path / "fictional-backup.db"
        db.backup_to(backup)
        assert db.verify_backup(backup)
        for target in (path, backup):
            assert not target.read_bytes().startswith(b"SQLite format")
            assert b"fictional private text" not in target.read_bytes()
    finally:
        db.close()


def test_duplicate_original_only_extracts_once_with_independent_citations(repository):
    repository.extractor.extract = Mock(wraps=repository.extractor.extract)
    for number in range(2):
        payload = attachment_message().model_copy(update={"provider_id": f"copy-{number}", "provider_thread_id": f"copy-{number}"})
        repository.ingest_message("account-1", payload, defer_extraction=True)
    result = repository.process_enrichment(limit=2)
    assert result["completed"] == 2
    assert repository.extractor.extract.call_count == 1
    chunks = repository.db.query("SELECT id,source_id,thread_id FROM source_chunks WHERE source_type='attachment' AND chunk_index=0")
    assert len(chunks) == 2 and chunks[0]["source_id"] != chunks[1]["source_id"]
    for chunk in chunks:
        assert repository.citation_for_chunk(chunk["id"]).app_link.endswith(chunk["id"])


def test_frozen_extractor_command_is_private_and_uses_early_dispatch(tmp_path, monkeypatch):
    import json
    import sys

    from email_reader import extractor_worker

    commands = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **options):
            commands.append(command)
            assert options["stderr"] is extractor_worker.subprocess.DEVNULL

        def communicate(self, input=None, timeout=None):
            request = json.loads(input)
            assert request["filename"] == "fictional-private.pdf"
            return '{"text":"fictional", "metadata":{}, "warnings":[]}', None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(extractor_worker.process_launch, "Popen", FakeProcess)
    result = extractor_worker.extract_isolated("fictional-private.pdf", "application/pdf", b"fictional", temp_root=tmp_path)
    assert result.text == "fictional"
    assert commands == [[sys.executable, "--extract-worker"]]
