from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from PySide6.QtCore import QObject

from email_reader.contracts import MessagePayload
from email_reader.controller import AppController
from email_reader.search import HybridSearch


def controller_for(repository):
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.context = SimpleNamespace(repository=repository, database=repository.db,
        search=HybridSearch(repository.db, repository))
    controller._selected_account = ""
    controller._selected_thread = {}
    controller._selected_mailbox = "inbox"
    controller._selected_category = "primary"
    controller._threads = []
    controller._busy = False
    controller._clear_keyword_search()
    controller._run = lambda label, work, done, **kwargs: done(work(lambda _: None))
    controller._run_keyword = lambda work, done, token: done(work(lambda _: None))
    controller.reload()
    return controller


def test_keyword_search_keeps_old_results_after_refresh_and_read_change(repository):
    old = repository.ingest_message("account-1", MessagePayload(provider_id="old", provider_thread_id="old",
        body_text="Unique archival certificate", received_at=datetime.now(UTC) - timedelta(days=2000)))
    for number in range(501):
        repository.ingest_message("account-1", MessagePayload(provider_id=f"new-{number}",
            provider_thread_id=f"new-{number}", body_text="Unrelated recent mail"))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (old,))
    assert thread_id not in {item["id"] for item in repository.list_threads(limit=500, mailbox="all")}
    controller = controller_for(repository)
    controller.search("archival certificate")
    assert [item["id"] for item in controller.threads] == [thread_id]
    controller.reload()  # Background refresh must not turn this back into inbox.
    assert [item["id"] for item in controller.threads] == [thread_id]
    controller._update_read_state(thread_id, False)
    assert controller._view_title == "Search results" and controller._view_count == 1
    repository.mark_remote_deleted("account-1", ["old"])
    controller.reload()
    assert [item["id"] for item in controller.threads] == [thread_id]
    repository.trash_thread(thread_id)
    controller.reload()
    assert controller.threads == [] and controller._view_title == "Search results"
    controller.filterMailbox("inbox")
    assert controller._keyword_query == "" and controller._view_title == "Primary"


def test_navigation_invalidates_pending_keyword_result(repository):
    repository.ingest_message("account-1", MessagePayload(provider_id="match", provider_thread_id="match",
        body_text="Searchable document"))
    controller = controller_for(repository)
    pending = []
    controller._run_keyword = lambda work, done, token: pending.append((work, done))
    controller.search("Searchable")
    controller.filterMailbox("sent")
    work, done = pending.pop()
    done(work(lambda _: None))
    assert controller._view_title == "Sent" and controller.threads == []
    controller.search("")
    assert not controller._keyword_query


def test_offline_keyword_search_never_calls_embedding_backend(repository):
    repository.ingest_message("account-1", MessagePayload(provider_id="event", provider_thread_id="event",
        subject="PSD happy hour", body_text="Friday pizza in the atrium"))
    controller = controller_for(repository)
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline keyword search must never request embeddings")
    controller.context.search._semantic = forbidden
    controller.search("happy hour")
    assert len(controller.threads) == 1
    assert controller.threads[0]["subject"] == "PSD happy hour"


def test_subject_only_matches_respect_account_and_app_trash(repository):
    message_id = repository.ingest_message("account-1", MessagePayload(
        provider_id="header-only", provider_thread_id="header-only", subject="Campus happy hour"
    ))
    thread_id = repository.db.scalar("SELECT thread_id FROM messages WHERE id=?", (message_id,))
    assert repository.search_header_thread_ids("happy hour", "account-1") == [thread_id]
    assert repository.search_header_thread_ids("happy hour", "different-account") == []
    repository.trash_thread(thread_id)
    assert repository.search_header_thread_ids("happy hour", "account-1") == []
