"""Real worker transitions on fictional mail, without provider or AI access."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import perf_counter

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from email_reader.context import ApplicationContext
from email_reader.contracts import MessagePayload
from email_reader.controller import AppController
from email_reader.demo import populate_demo


def wait_for(condition, signal, timeout_ms: int = 3000) -> None:
    if condition():
        return
    loop = QEventLoop()

    def changed() -> None:
        if condition():
            loop.quit()

    signal.connect(changed)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    signal.disconnect(changed)
    assert condition(), "The background read did not complete"


def test_rapid_selection_keeps_the_latest_email(tmp_path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv("EMAIL_READER_DATA_DIR", str(tmp_path / "test-data"))
    context = ApplicationContext.create(testing=True)
    try:
        populate_demo(context.repository)
        controller = AppController(context)
        controller.timer.stop()
        wait_for(lambda: bool(controller.threads), controller.threadsChanged)
        first, second = controller.threads[:2]
        controller.selectThreadAsync(first["id"])
        controller.selectThreadAsync(second["id"])
        assert controller.selectedThread["id"] == second["id"]
        wait_for(
            lambda: controller.selectedThread.get("read_state") == "ready",
            controller.selectedThreadChanged,
        )
        assert controller.selectedThread["id"] == second["id"]
        assert controller.selectedThread["messages"][0]["prepared"]
        controller.read_pool.waitForDone()
    finally:
        if 'controller' in locals():
            controller.shutdown()
        context.close()


def test_older_message_is_prepared_only_when_expanded(tmp_path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv("EMAIL_READER_DATA_DIR", str(tmp_path / "test-data"))
    context = ApplicationContext.create(testing=True)
    try:
        populate_demo(context.repository)
        context.repository.ingest_message(
            "demo-gmail",
            MessagePayload(
                provider_id="fictional-old",
                provider_thread_id="travel-thread",
                subject="Lake house details",
                sender_name="Jordan Lee",
                sender_email="jordan@example.net",
                to=["alex.personal@example.com"],
                sent_at=datetime.now(UTC) - timedelta(days=3),
                body_text="Old fictional message, prepared on demand.",
            ),
        )
        controller = AppController(context)
        controller.timer.stop()
        wait_for(lambda: bool(controller.threads), controller.threadsChanged)
        thread_id = next(
            item["id"] for item in controller.threads if item["subject"] == "Lake house details"
        )
        controller.selectThreadAsync(thread_id)
        wait_for(
            lambda: controller.selectedThread.get("read_state") == "ready",
            controller.selectedThreadChanged,
        )
        newest, older = controller.selectedThread["messages"]
        assert newest["prepared"]
        assert not older["prepared"]
        assert "display_html" not in older

        controller.prepareMessage(older["id"])
        wait_for(
            lambda: controller.selectedThread["messages"][1].get("prepared"),
            controller.selectedThreadChanged,
        )
        assert "Old fictional message" in controller.selectedThread["messages"][1]["display_html"]
        controller.read_pool.waitForDone()
    finally:
        if 'controller' in locals():
            controller.shutdown()
        context.close()


def test_offline_keyword_result_opens_without_ai_or_provider(tmp_path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv("EMAIL_READER_DATA_DIR", str(tmp_path / "offline-data"))
    context = ApplicationContext.create(testing=True)
    controller = None
    try:
        populate_demo(context.repository)
        context.repository.ingest_message("demo-gmail", MessagePayload(
            provider_id="campus-event", provider_thread_id="campus-event",
            subject="PSD happy hour", body_text="Friday 10/9, 5:30 pm in the atrium. Cash only.",
            labels=["INBOX", "CATEGORY_UPDATES"],
        ))
        controller = AppController(context)
        controller.timer.stop()
        wait_for(lambda: bool(controller.threads), controller.threadsChanged)
        def forbidden(*args, **kwargs):
            raise AssertionError("Offline search/open contacted AI or a provider")
        context.search._semantic = forbidden
        controller._provider = forbidden
        started = perf_counter()
        controller._busy = True  # Simulate a slow import; local reads must still work.
        controller.search("happy hour")
        wait_for(lambda: controller._view_title == "Search results", controller.threadsChanged)
        assert len(controller.threads) == 1
        thread_id = controller.threads[0]["id"]
        controller.selectThreadAsync(thread_id)
        wait_for(lambda: controller.selectedThread.get("read_state") == "ready",
                 controller.selectedThreadChanged)
        assert "Cash only" in controller.selectedThread["messages"][0]["display_html"]
        assert perf_counter() - started < 2.0
    finally:
        if controller:
            controller.shutdown()
        context.close()


def test_superseded_queued_clicks_skip_read_work(tmp_path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv("EMAIL_READER_DATA_DIR", str(tmp_path / "queued-data"))
    context = ApplicationContext.create(testing=True)
    try:
        populate_demo(context.repository)
        controller = AppController(context)
        controller.timer.stop()
        wait_for(lambda: bool(controller.threads), controller.threadsChanged)
        queued, read = [], []
        from types import SimpleNamespace
        real_pool = controller.read_pool
        controller.read_pool = SimpleNamespace(start=queued.append)
        original = context.repository.get_thread_for_read
        def get_thread(thread_id):
            read.append(thread_id)
            return original(thread_id)
        context.repository.get_thread_for_read = get_thread
        first, second = controller.threads[:2]
        controller.selectThreadAsync(first["id"])
        controller.selectThreadAsync(second["id"])
        for worker in queued:
            worker.run()
        assert read == [second["id"]]
        assert controller.selectedThread["id"] == second["id"]
    finally:
        if 'real_pool' in locals():
            controller.read_pool = real_pool
        if 'controller' in locals():
            controller.shutdown()
        context.close()
