"""The investigation UI state uses fictional mail and never calls a provider."""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QCoreApplication, QTimer

from email_reader.context import ApplicationContext
from email_reader.controller import AppController
from email_reader.demo import DEMO_QUESTIONS, populate_demo


@pytest.fixture
def demo_controller(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv("EMAIL_READER_DATA_DIR", str(tmp_path / "isolated-demo"))
    context = ApplicationContext.create(testing=True)
    populate_demo(context.repository)
    controller = AppController(context, demo_mode=True)
    controller.timer.stop()
    yield controller
    controller.shutdown()
    app.processEvents()
    context.close()


def test_recorded_walkthrough_is_explicit_and_opens_exact_pdf(demo_controller, tmp_path):
    controller = demo_controller
    controller.startDemoReplay(0)
    assert controller.demoMode
    assert controller.searchMode == "replay"
    assert controller.investigationState["status"] == "replay"
    assert controller.chatMessages[0]["text"] == DEMO_QUESTIONS[0]
    answer = controller.chatMessages[1]
    assert len(answer["citations"]) == 2
    assert len(answer["file_cards"]) >= 2
    for citation in answer["citations"]:
        assert controller.context.repository.resolve_source_link(citation["source_id"])
    pdf = next(item for item in answer["file_cards"] if item["filename"] == "auto-policy-2026.pdf")
    original = controller.context.repository.export_attachment(pdf["id"], tmp_path / "export")
    assert original.read_bytes().startswith(b"%PDF-")
    for index in (1, 2):
        controller.startDemoReplay(index)
        assert controller.searchMode == "replay"
        assert controller.chatMessages[-1]["citations"]
        assert controller.chatMessages[0]["text"] == DEMO_QUESTIONS[index]


def test_late_agent_result_does_not_replace_cancelled_answer(demo_controller, monkeypatch):
    controller = demo_controller
    queued = []
    monkeypatch.setattr(controller, "_run", lambda _label, work, done, **_options: queued.append((work, done)))
    controller.startInvestigation("Find my auto policy")
    session = controller._search_session()
    assert controller.searchMode == "investigate"
    assert controller.investigationState["status"] == "running"
    controller._finish_investigation_partial(session, "cancelled")
    queued[0][1]({"status": "completed", "activity": [], "answer": {"role": "assistant", "text": "late"}})
    assert controller.chatMessages[-1]["status"] == "cancelled"
    assert "late" not in controller.chatMessages[-1]["text"]


def test_deadline_and_cancel_publish_terminal_state_without_waiting_for_backend(
    demo_controller, monkeypatch,
):
    controller = demo_controller
    timers = []
    monkeypatch.setattr(controller, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(QTimer, "singleShot", lambda milliseconds, callback: timers.append(
        (milliseconds, callback)))
    controller.startInvestigation("Find my policy")
    assert 0 <= timers[-1][0] <= 60000
    session = controller._search_session()
    controller._worker_jobs["fictional-agent"] = {"lane": "ai", "label": "Investigating saved mail…"}
    started = time.monotonic()
    controller.cancelAI()
    assert time.monotonic() - started < 1
    assert session["investigation"]["status"] == "cancelled"
    assert controller._worker_jobs["fictional-agent"]["cancelled"]
    controller._worker_jobs.clear()


def test_demo_disables_mail_refresh_and_background_ai(demo_controller, monkeypatch):
    controller = demo_controller
    monkeypatch.setattr(controller, "_provider", lambda *_args: (_ for _ in ()).throw(
        AssertionError("The demo must not call a provider")))
    controller._automatic_sync()
    controller.syncAll()
    controller._start_ai_enrichment()
    assert not controller.aiBusy
    assert "disabled" in controller.status
    # Public replay reads only the fictional local database.
    controller.startDemoReplay(1)
    assert controller.chatMessages[-1]["status"] == "replay"
