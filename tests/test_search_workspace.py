"""Search workspace state uses fictional mail and never calls a provider or model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from email_reader.context import ApplicationContext
from email_reader.contracts import Answer, MessagePayload
from email_reader.controller import AppController
from email_reader.demo import populate_demo
from email_reader.search import SearchHit


@pytest.fixture
def controller(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv("EMAIL_READER_DATA_DIR", str(tmp_path / "fictional-search"))
    context = ApplicationContext.create(testing=True)
    populate_demo(context.repository)
    value = AppController(context)
    value.timer.stop()
    wait_until(lambda: bool(value.threads))
    yield value
    value.shutdown()
    app.processEvents()
    context.close()


def wait_until(condition):
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: loop.quit() if condition() else None)
    QTimer.singleShot(3000, loop.quit)
    if not condition():
        timer.start()
        loop.exec()
    timer.stop()
    assert condition(), "The fictional async operation did not finish"


@pytest.fixture
def queued_searches(controller, monkeypatch):
    pending = []

    def queue(label, work, done, **options):
        pending.append((work, done))

    monkeypatch.setattr(controller, "_run", queue)
    monkeypatch.setattr(controller, "_run_keyword", lambda work, done, token: pending.append((work, done)))
    return pending


def complete(pending, index, text):
    pending[index][1]({**Answer(text=text).model_dump(mode="json"), "role": "assistant",
        "kind": "answer", "citations": [], "related_citations": [], "file_cards": [],
        "attachments": [], "related_attachments": []})


def wait_for_preview(controller, condition):
    if condition():
        return
    loop = QEventLoop()

    def changed():
        if condition():
            loop.quit()

    controller.sourcePreviewChanged.connect(changed)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    controller.sourcePreviewChanged.disconnect(changed)
    assert condition(), "The fictional source preview did not finish"


def test_new_search_retains_pending_result_in_its_original_session(controller, queued_searches):
    controller.startSearch("What is the renewal fee?")
    first_id = controller.selectedSearchId
    controller.startSearch("When is check-in?")
    second_id = controller.selectedSearchId
    changes = []
    controller.chatChanged.connect(lambda: changes.append(True))
    complete(queued_searches, 0, "The fee is $48,000.")
    assert changes == [], "A background answer must not reset another session's view"
    assert [item["text"] for item in controller.chatMessages] == ["When is check-in?"]
    complete(queued_searches, 1, "Friday after 3 PM.")
    assert [item["id"] for item in controller.searchSessions] == [second_id, first_id]
    controller.selectSearch(first_id)
    assert controller.chatMessages[-1]["text"] == "The fee is $48,000."
    assert controller._chats[""] is controller.chatMessages


def test_followup_keeps_context_and_new_search_starts_without_it(
    controller, queued_searches, monkeypatch
):
    requests = []

    def answer(question, account_id, previous):
        requests.append((question, account_id, previous))
        return Answer(text="Fictional answer"), []

    monkeypatch.setattr(controller.context.assistant, "answer", answer)
    controller.startSearch("What is the renewal fee?")
    complete(queued_searches, 0, "The fee is $48,000.")
    controller.askFollowUp("Is there a discount?")
    queued_searches[1][0](lambda update: None)
    assert requests[-1][2] == [
        {"role": "user", "text": "What is the renewal fee?"},
        {"role": "assistant", "text": "The fee is $48,000."},
    ]
    controller.startSearch("When is check-in?")
    queued_searches[2][0](lambda update: None)
    assert requests[-1][2] == []


def test_account_switch_and_new_search_preserve_earlier_history(controller, queued_searches):
    controller.filterAccount("demo-work")
    controller.startSearch("Work question")
    work_id = controller.selectedSearchId
    controller.filterAccount("demo-gmail")
    controller.startSearch("Personal question")
    personal_id = controller.selectedSearchId
    complete(queued_searches, 0, "Work answer")
    assert controller.chatMessages[0]["text"] == "Personal question"
    controller.filterAccount("demo-work")
    assert controller.selectedSearchId == work_id
    assert controller.chatMessages[-1]["text"] == "Work answer"
    assert personal_id not in {item["id"] for item in controller.searchSessions}
    controller.startSearch("")
    assert controller.chatMessages == []
    controller.selectSearch(work_id)
    assert controller.chatMessages[-1]["text"] == "Work answer"


def test_account_switch_clears_pending_preview_but_preserves_draft(controller):
    controller.filterAccount("demo-gmail")
    wait_until(lambda: controller.threads and all(t["account_id"] == "demo-gmail" for t in controller.threads))
    controller.beginDraft("new")
    wait_until(lambda: bool(controller.editorDraft))
    original_draft = controller.editorDraft
    controller.selectThreadAsync(controller.threads[0]["id"])
    assert controller.selectedThread["read_state"] == "loading"
    controller.filterAccount("demo-work")
    controller.read_pool.waitForDone()
    QCoreApplication.processEvents()
    assert controller.selectedThread == {}
    assert controller.editorDraft is original_draft
    assert controller.editorDraft["account_id"] == "demo-gmail"


def test_selecting_same_account_does_not_cancel_current_read(controller):
    controller.filterAccount("demo-gmail")
    wait_until(lambda: controller.threads and all(t["account_id"] == "demo-gmail" for t in controller.threads))
    controller.selectThreadAsync(controller.threads[0]["id"])
    controller.filterAccount("demo-gmail")
    controller.read_pool.waitForDone()
    QCoreApplication.processEvents()
    assert controller.selectedThread["read_state"] == "ready"


def fixture_hit(controller, chunk_id):
    citation = controller.context.repository.citation_for_chunk(chunk_id)
    return SearchHit(chunk_id=chunk_id, content=citation.excerpt, score=1, citation=citation)


def test_answer_citations_include_canonical_message_for_result_dedup(controller, queued_searches, monkeypatch):
    chunks = controller.context.database.query("SELECT id FROM source_chunks LIMIT 2")
    hits = [fixture_hit(controller, row["id"]) for row in chunks]
    controller.startSearch("Find the fictional details")
    monkeypatch.setattr(controller.context.assistant, "answer", lambda *args: (Answer(
        text="Fictional answer", source_ids=[hits[0].chunk_id],
        related_source_ids=[hits[1].chunk_id],
    ), hits))
    queued_searches[0][1](queued_searches[0][0](lambda update: None))
    answer = controller.chatMessages[-1]
    for citation in answer["citations"] + answer["related_citations"]:
        thread_id, message_id = controller.context.repository.resolve_source_link(citation["source_id"])
        assert citation["thread_id"] == thread_id
        assert citation["message_id"] == message_id


def test_offline_results_clear_unrelated_preview_and_keep_matching_preview(controller, queued_searches):
    thread_id = controller.threads[0]["id"]
    controller.selectThread(thread_id)
    wait_until(lambda: controller.selectedThread.get("read_state") == "ready")
    selected = controller.selectedThread
    controller.beginDraft("new")
    wait_until(lambda: bool(controller.editorDraft))
    original_draft = controller.editorDraft
    controller.search("fictional matching result")
    records = controller.context.repository.list_threads(mailbox="all", thread_ids=[thread_id])
    queued_searches[0][1](([thread_id], records))
    assert controller.selectedThread is selected
    controller.search("fictional no results")
    queued_searches[1][1](([], []))
    assert controller.selectedThread == {}
    assert controller.editorDraft is original_draft


def test_clear_and_cancel_do_not_restore_stale_answers(controller, queued_searches, monkeypatch):
    controller.ask("Question to clear")
    controller.clearChat()
    complete(queued_searches, 0, "This must not come back")
    assert controller.chatMessages == []

    controller.ask("Question to cancel")
    controller._worker_jobs["fixture-ai"] = {"lane": "ai", "label": "Fixture AI"}
    cancelled = []
    monkeypatch.setattr(controller.context.openai, "cancel", lambda: cancelled.append(True))
    controller.cancelAI()
    controller._worker_jobs.pop("fixture-ai")
    complete(queued_searches, 1, "This must not arrive after Cancel")
    assert cancelled == [True]
    assert [item["role"] for item in controller.chatMessages] == ["user"]


def test_source_preview_opens_exact_older_message_without_changing_inbox(controller):
    repository = controller.context.repository
    repository.ingest_message(
        "demo-work",
        MessagePayload(
            provider_id="older-proposal",
            provider_thread_id="proposal-thread",
            subject="Renewal proposal and September timeline",
            sender_email="maya@vendor.example",
            sent_at=datetime.now(UTC) - timedelta(days=10),
            body_text="Earlier fictional proposal: $49,000 before the revised quote.",
            is_unread=True,
        ),
    )
    travel = next(item for item in controller.threads if item["subject"] == "Lake house details")
    controller.selectThread(travel["id"])
    wait_until(lambda: controller.selectedThread.get("read_state") == "ready")
    selected = controller.selectedThread
    row = repository.db.query(
        "SELECT c.id,c.source_id,m.thread_id FROM source_chunks c "
        "JOIN messages m ON m.id=c.source_id WHERE m.provider_id='older-proposal'"
    )[0]
    repository.db.execute("UPDATE source_chunks SET locator='Paragraph 2' WHERE id=?", (row["id"],))
    unread_before = repository.db.scalar(
        "SELECT local_unread FROM messages WHERE id=?", (row["source_id"],)
    )
    controller.openSearchSource(f"emailreader://source/{row['id']}")
    wait_for_preview(controller, lambda: controller.sourcePreview.get("read_state") == "ready")
    preview = controller.sourcePreview
    assert controller.selectedThread is selected
    assert preview["message_id"] == row["source_id"]
    assert len(preview["messages"]) == 1
    assert preview["locator"] == "Paragraph 2"
    assert preview["citation"]["source_id"] == row["id"]
    assert "Earlier fictional proposal" in controller.sourceMessageDocument(
        row["source_id"], False, False, False
    )
    assert repository.db.scalar(
        "SELECT local_unread FROM messages WHERE id=?", (row["source_id"],)
    ) == unread_before


def test_newer_source_or_closed_preview_ignores_late_results(controller):
    rows = controller.context.database.query("SELECT id FROM source_chunks LIMIT 2")
    controller.openSearchSource(f"emailreader://source/{rows[0]['id']}")
    controller.openSearchSource(f"emailreader://source/{rows[1]['id']}")
    wait_for_preview(controller, lambda: controller.sourcePreview.get("read_state") == "ready")
    assert controller.sourcePreview["source_id"] == rows[1]["id"]
    controller.openSearchSource(f"emailreader://source/{rows[0]['id']}")
    controller.closeSearchSource()
    controller.read_pool.waitForDone()
    QCoreApplication.processEvents()
    assert controller.sourcePreview == {}


def test_missing_source_is_visible_error_without_discarding_search(controller, queued_searches):
    controller.startSearch("Find a missing document")
    complete(queued_searches, 0, "A fictional answer stays visible.")
    controller.openSearchSource("emailreader://source/no-such-source")
    wait_for_preview(controller, lambda: controller.sourcePreview.get("read_state") == "error")
    assert controller.chatMessages[-1]["text"] == "A fictional answer stays visible."
    assert controller.sourcePreview["error"]


def test_discard_editor_notifies_inline_view(controller):
    changes = []
    controller.editorChanged.connect(lambda: changes.append(controller.editorDraft))
    controller.beginDraft("new")
    wait_until(lambda: bool(controller.editorDraft))
    assert controller.editorDraft
    controller.discardEditor()
    assert changes[-1] == {}
