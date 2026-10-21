from __future__ import annotations

from threading import Event
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QThread, QThreadPool, QTimer

from email_reader.controller import AppController, Worker


class HoldingPool:
    def __init__(self) -> None:
        self.worker: Worker | None = None

    def start(self, worker: Worker, priority: int = 0) -> None:
        self.worker = worker
        self.priority = priority


def test_worker_emits_structured_progress_before_success() -> None:
    updates: list[dict[str, object]] = []
    results: list[object] = []

    def operation(report):
        report(
            {
                "stage": "Account 1 of 3",
                "detail": "20 messages saved",
                "completed": 0,
                "total": 3,
                "items": 20,
            }
        )
        return 20

    worker = Worker(operation, reports_progress=True)
    worker.signals.progressed.connect(lambda _token, update: updates.append(update))
    worker.signals.succeeded.connect(lambda _token, result: results.append(result))

    worker.run()

    assert updates == [
        {
            "stage": "Account 1 of 3",
            "detail": "20 messages saved",
            "completed": 0,
            "total": 3,
            "items": 20,
        }
    ]
    assert results == [20]


def test_worker_reports_failure_without_fake_completion() -> None:
    failures: list[str] = []
    results: list[object] = []

    def operation(report):
        report({"stage": "Reading mailbox"})
        raise RuntimeError("mailbox unavailable")

    worker = Worker(operation, reports_progress=True)
    worker.signals.failed.connect(lambda _token, message: failures.append(message))
    worker.signals.succeeded.connect(lambda _token, result: results.append(result))

    worker.run()

    assert failures == ["RuntimeError: mailbox unavailable"]
    assert results == []


def test_controller_retains_worker_until_completion_signal() -> None:
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller._busy = False
    controller._status = "Ready"
    controller._activity = {}
    controller._worker_jobs = {}
    controller.pool = HoldingPool()
    completed: list[object] = []

    controller._run("Testing…", lambda: 42, completed.append)

    assert controller.pool.worker is not None
    assert controller.pool.worker.token in controller._worker_jobs
    controller.pool.worker.run()
    assert completed == [42]
    assert controller._worker_jobs == {}
    assert not controller.busy


def test_controller_delivers_pool_completion_on_ui_thread() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller._busy = False
    controller._status = "Ready"
    controller._activity = {}
    controller._worker_jobs = {}
    controller.pool = QThreadPool()
    controller.pool.setMaxThreadCount(1)
    loop = QEventLoop()
    completed_on_ui_thread: list[bool] = []

    def completed(_result: object) -> None:
        completed_on_ui_thread.append(QThread.currentThread() is app.thread())

    controller.busyChanged.connect(lambda: loop.quit() if not controller.busy else None)
    controller._run("Testing…", lambda: 42, completed)
    QTimer.singleShot(2000, loop.quit)
    loop.exec()
    controller.pool.waitForDone()

    assert completed_on_ui_thread == [True]
    assert controller._worker_jobs == {}
    assert not controller.busy


def test_ai_runs_while_provider_waits_and_each_lane_keeps_its_own_state():
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller._busy = False
    controller._status = "Ready"
    controller._activity = {}
    controller._worker_jobs = {}
    controller.pool, controller.ai_pool = HoldingPool(), HoldingPool()
    controller.context = SimpleNamespace(openai=SimpleNamespace(reset_cancellation=lambda: None))
    completed = []
    controller._run("Fictional provider page…", lambda: "provider", completed.append)
    controller._run("Fictional answer…", lambda: "AI", completed.append, ai=True)
    assert controller.providerBusy and controller.aiBusy and controller.busy
    controller.ai_pool.worker.run()
    assert completed == ["AI"]
    assert not controller.aiBusy and controller.providerBusy and controller.busy
    controller.pool.worker.run()
    assert completed == ["AI", "provider"]
    assert not controller.busy


def test_explicit_draft_save_gets_priority_in_provider_lane():
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller._busy = False
    controller._status = "Ready"
    controller._activity = {}
    controller._worker_jobs = {}
    controller.pool = HoldingPool()
    controller._run("Saving reviewed draft…", lambda: "saved", lambda _: None)
    assert controller.draftSaving
    assert controller.pool.priority > 0
    controller.pool.worker.run()
    assert not controller.draftSaving


def ai_controller_probe():
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller._busy = False
    controller._status = "Ready"
    controller._activity = {}
    controller._worker_jobs = {}
    controller._pending_search = None
    controller._editor_generation = "draft-generation"
    controller._assistant = {}
    controller.ai_pool = HoldingPool()
    events = []
    controller.context = SimpleNamespace(openai=SimpleNamespace(
        cancel=lambda: events.append("cancel"),
        reset_cancellation=lambda: events.append("reset"),
    ))
    return controller, events


def test_cancelled_late_summary_never_reopens_or_publishes():
    controller, events = ai_controller_probe()
    controller._selected_thread = {"id": "fictional-thread"}
    published = []
    controller.assistantChanged.connect(lambda: published.append(controller._assistant))

    controller.summarize()
    token = controller.ai_pool.worker.token
    controller.cancelAI()
    # A slow backend may complete despite cancellation. Its already-queued
    # success signal must never reopen the summary after the user closes it.
    controller._worker_succeeded(token, {"kind": "summary", "key_points": ["Late result"]})

    assert events == ["cancel"]
    assert published == []
    assert controller._assistant == {}
    assert not controller.aiBusy
    assert controller._activity["phase"] == "cancelled"


def test_foreground_ai_resets_cancellation_only_when_exclusive_worker_starts():
    controller, events = ai_controller_probe()
    controller._ai_enrichment_running = True
    controller._ai_enrichment_cancel = Event()
    completed = []
    controller._run("Creating summary…", lambda: "result", completed.append, ai=True)

    assert controller._ai_enrichment_cancel.is_set()
    assert events == ["cancel"]  # No reset while enrichment still owns the lane.
    assert completed == []
    controller._ai_enrichment_running = False
    controller.ai_pool.worker.run()
    assert events == ["cancel", "reset"]
    assert completed == ["result"]


def test_cancelled_queued_ai_never_resets_or_calls_backend():
    controller, events = ai_controller_probe()
    called = []
    controller._run("Creating summary…", lambda: called.append("backend"), called.append, ai=True)
    controller.cancelAI()
    controller.ai_pool.worker.run()

    assert events == ["cancel"]
    assert called == []
    assert not controller.aiBusy


def test_repeat_apple_import_uses_incremental_sync() -> None:
    assert not AppController._requires_full_apple_import(
        previous_range="2025-09-02T00:00:00+00:00",
        sync_cursor="2026-09-02T00:00:00+00:00",
        page_token=None,
        requested_range="2025-09-02T00:00:00+00:00",
        focused_rebuild=False,
    )
    assert AppController._requires_full_apple_import(
        previous_range="2025-09-02T00:00:00+00:00",
        sync_cursor="2026-09-02T00:00:00+00:00",
        page_token=None,
        requested_range="2021-09-02T00:00:00+00:00",
        focused_rebuild=False,
    )
