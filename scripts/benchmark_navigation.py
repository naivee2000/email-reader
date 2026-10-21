"""Compare local navigation on fictional mail in isolated processes.

Example:
  .venv/bin/python scripts/benchmark_navigation.py \
      --baseline build/ui-redesign-baseline/email_reader --current src/email_reader

No provider calls, API requests, or private mail are involved. Default timings
measure controller signals. --render additionally loads each version's native
QML interface offscreen and times row clicks through a verified rendered frame.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from time import perf_counter

METRICS = ("cold_content_ready_ms", "warm_content_ready_ms", "selection_feedback_ms")
RENDER_METRICS = ("cold_click_to_frame_ms", "warm_click_to_frame_ms")


class NativeNavigation:
    """Observe the real UI from a temporary QML probe; never modify app QML."""

    def __init__(self, source, controller):
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent

        self.engine = QQmlApplicationEngine()
        self.stage_samples = {"cold": [], "warm": []}
        self.last_stages = {}
        self.message_container = None
        self.engine.addImageProvider("mail", controller.mail_images)
        self.engine.rootContext().setContextProperty("App", controller)
        self.engine.load(QUrl.fromLocalFile(str(source / "qml/Main.qml")))
        if not self.engine.rootObjects():
            raise RuntimeError("Native benchmark interface did not load")
        self.window = self.engine.rootObjects()[0]
        self.window.setWidth(1366)
        self.window.setHeight(900)
        self.component = QQmlComponent(self.engine)
        self.component.setData(b'''
import QtQuick
import QtWebEngine
QtObject {
    id: probe
    property var view: null
    property var list: null
    property int rowIndex: 0
    property string marker: ""
    property bool loadSucceeded: false
    property bool domReady: false
    property bool checking: false
    property Connections observer: Connections {
        target: probe.view || null
        function onLoadingChanged(request) {
            if (request.status === WebEngineView.LoadSucceededStatus) probe.loadSucceeded = true
        }
    }
    function positionRow() { list.positionViewAtIndex(rowIndex, ListView.Center) }
    function checkDocument() {
        if (!view || checking) return
        if (!view.loading && view.loadProgress === 100) loadSucceeded = true
        if (!loadSucceeded) return
        checking = true
        const requestedView = view, expectedMarker = marker
        view.runJavaScript(
            "document.readyState === 'complete' && !!document.getElementById('mail-content') && " +
            "document.getElementById('mail-content').textContent.includes(" + JSON.stringify(marker) + ")",
            function(value) {
                if (probe.view !== requestedView || probe.marker !== expectedMarker) return
                probe.domReady = value === true; probe.checking = false
            }
        )
    }
}''', QUrl())
        self.probe = self.component.create()
        if self.probe is None:
            raise RuntimeError("Native benchmark load observer did not initialize")
        self.list = self.find(lambda item: item.objectName() == "threadList")
        if self.list is None:
            # The previous version did not name its ListView. Resolve it from
            # a real mail row so hidden ComboBox popups cannot be mistaken for it.
            self.wait(lambda: self.find(lambda item: item.objectName().startswith("threadRow-")))
            self.list = self.find(lambda item: item.objectName().startswith("threadRow-")).parentItem()
            while self.list is not None and not self.list.metaObject().className().startswith("QQuickListView"):
                self.list = self.list.parentItem()
        if self.list is None:
            raise RuntimeError("Native benchmark message list is missing")
        self.probe.setProperty("list", self.list)
        self.wait(lambda: self.window.isVisible() and self.list.height() > 100)
        self.window.grabWindow()

    def find(self, predicate, root=None):
        pending, seen = [root or self.window], set()
        while pending:
            item = pending.pop()
            if item in seen:
                continue
            seen.add(item)
            if predicate(item):
                return item
            pending.extend(item.children())
            if hasattr(item, "childItems") and "WebEngine" not in item.metaObject().className():
                pending.extend(item.childItems())
        return None

    def message_body(self, provider_id):
        name = "messageBody-" + provider_id
        if self.message_container is None:
            # One-time discovery occurs in the unmeasured warm-up. Repeater
            # delegates have visual parents, so QObject.findChild misses them.
            body = self.find(lambda item: item.objectName() == name)
            if body is not None:
                card = body.parentItem()
                while card is not None and not card.objectName().startswith("messageCard-"):
                    card = card.parentItem()
                if card is not None:
                    self.message_container = card.parentItem()
            return body
        card = next((item for item in self.message_container.childItems()
                     if item.objectName() == "messageCard-" + provider_id), None)
        if card is None:
            return None
        # Only inspect the selected card's direct content column and optional
        # loader, never unrelated application controls or hidden workspaces.
        for column in card.childItems():
            for item in column.childItems():
                if item.objectName() == name:
                    return item
                if item.metaObject().className().startswith("QQuickLoader"):
                    loaded = item.property("item")
                    if loaded is not None and loaded.objectName() == name:
                        return loaded
        return None

    def wait(self, condition):
        from PySide6.QtCore import QEventLoop, QTimer

        loop, timer, deadline = QEventLoop(), QTimer(), QTimer()
        result = {"ready": False}

        def poll():
            if condition():
                result["ready"] = True
                loop.quit()

        timer.setInterval(5)
        timer.timeout.connect(poll)
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        timer.start()
        deadline.start(10000)
        poll()
        if not result["ready"]:
            loop.exec()
        timer.stop()
        deadline.stop()
        if not result["ready"]:
            raise RuntimeError("Native navigation did not produce its verified frame within 10 seconds")

    def prepare(self, thread_id, row_index):
        from PySide6.QtCore import QMetaObject

        self.probe.setProperty("view", None)
        self.probe.setProperty("loadSucceeded", False)
        self.probe.setProperty("domReady", False)
        self.probe.setProperty("checking", False)
        self.probe.setProperty("rowIndex", row_index)
        QMetaObject.invokeMethod(self.probe, "positionRow")
        def row():
            return next((item for item in self.list.property("contentItem").childItems()
                         if item.objectName() == "threadRow-" + thread_id), None)
        self.wait(row)
        self.row = row()

    def click(self):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtTest import QTest

        point = self.row.mapToScene(QPointF(self.row.width() / 2, self.row.height() / 2)).toPoint()
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier, point)

    def frame(self, provider_id, started):
        from PySide6.QtCore import QMetaObject, QPointF

        lookup_started = perf_counter()
        self.wait(lambda: self.message_body(provider_id))
        body = self.message_body(provider_id)
        web = self.find(lambda item: item.metaObject().className().startswith("QQuickWebEngineView"), body)
        if web is None:
            raise RuntimeError("Native message WebEngine is missing")
        self.probe.setProperty("marker", "Fictional conversation " + provider_id.split("-")[1] + ".")
        self.probe.setProperty("view", web)
        observed = {"observer_lookup_ms": (perf_counter() - lookup_started) * 1000}

        def painted():
            if not self.probe.property("domReady"):
                QMetaObject.invokeMethod(self.probe, "checkDocument")
            if self.probe.property("loadSucceeded"):
                observed.setdefault("webengine_loaded_ms", (perf_counter() - started) * 1000)
            if not self.probe.property("loadSucceeded") or not self.probe.property("domReady"):
                return False
            observed.setdefault("dom_ready_ms", (perf_counter() - started) * 1000)
            frame = self.window.grabWindow()
            captured_at = perf_counter()
            origin = body.mapToScene(QPointF(0, 0))
            scale = frame.devicePixelRatio()
            left, top = max(0, int(origin.x() * scale)), max(0, int(origin.y() * scale))
            right = min(frame.width(), int((origin.x() + min(200, body.width())) * scale))
            bottom = min(frame.height(), int((origin.y() + min(100, body.height())) * scale))
            colored = sum(
                frame.pixelColor(x, y).name() == "#167e95"
                for y in range(top, bottom, 4) for x in range(left, right, 8)
            )
            observed["marker_pixels"] = colored
            if colored < 12:
                return False
            observed["ms"] = (captured_at - started) * 1000
            return True

        try:
            self.wait(painted)
        except RuntimeError as error:
            self.window.grabWindow().save("/tmp/email-reader-benchmark-frame.png")
            raise RuntimeError(
                "Frame verification failed: " + json.dumps({
                    "load": self.probe.property("loadSucceeded"), "dom": self.probe.property("domReady"),
                    "progress": web.property("loadProgress"), "pixels": observed.get("marker_pixels", 0),
                    "body": [body.width(), body.height()],
                    "origin": [body.mapToScene(QPointF(0, 0)).x(), body.mapToScene(QPointF(0, 0)).y()],
                    "visible": body.isVisible(),
                })
            ) from error
        self.last_stages = observed
        return observed["ms"]

    def close(self):
        from email_reader.app import dispose_qml_engine

        self.probe.setProperty("view", None)
        self.window.close()
        dispose_qml_engine(self.engine)


def source_directory(value: str) -> Path:
    path = Path(value).resolve(strict=True)
    if not (path / "controller.py").is_file():
        raise argparse.ArgumentTypeError("Source must be an email_reader package directory")
    return path


def run_fixture(source: Path, rounds: int, thread_count: int, render: bool = False, sample_count: int = 24) -> dict:
    sys.path.insert(0, str(source.parent))
    os.environ["EMAIL_READER_TESTING"] = "1"
    if render:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QML_DISABLE_DISK_CACHE"] = "1"

    from PySide6.QtCore import QBuffer, QCoreApplication, QEventLoop, QIODevice, QTimer
    from PySide6.QtGui import QImage

    from email_reader.context import ApplicationContext
    from email_reader.contracts import Account, AttachmentPayload, MessagePayload, ProviderKind
    from email_reader.controller import AppController

    if Path(sys.modules[AppController.__module__].__file__).resolve() != source / "controller.py":
        raise RuntimeError("Benchmark imported a different source package than requested")

    if render:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQuickControls2 import QQuickStyle
        from PySide6.QtWebEngineQuick import QtWebEngineQuick

        QQuickStyle.setStyle("Basic")
        QtWebEngineQuick.initialize()
        app = QGuiApplication([])
    else:
        app = QCoreApplication([])
    measurements = {name: [] for name in METRICS + (RENDER_METRICS if render else ())}
    with tempfile.TemporaryDirectory(prefix="email-reader-navigation-") as directory:
        os.environ["EMAIL_READER_DATA_DIR"] = directory
        context = ApplicationContext.create(testing=True)
        controller = None
        native = None
        try:
            context.repository.upsert_account(Account(
                id="benchmark-fictional", provider=ProviderKind.GMAIL,
                email="fictional@example.com", display_name="Fictional benchmark",
            ))
            picture = QImage(96, 24, QImage.Format_RGB32)
            picture.fill("#934735")
            image_buffer = QBuffer()
            image_buffer.open(QIODevice.WriteOnly)
            if not picture.save(image_buffer, "PNG"):
                raise RuntimeError("Could not prepare a fictional fixture image")
            image_bytes = bytes(image_buffer.data())
            stamp = datetime(2026, 1, 1, tzinfo=UTC)
            message_count = 0
            for thread_index in range(thread_count):
                for message_index in range(1 + thread_index % 3):
                    variant = thread_index % 4
                    if thread_index < thread_count - sample_count:
                        variant = 0
                    lines = 80 if variant == 3 else 4 + variant * 8
                    text = "\n".join(
                        f"Fictional planning detail {index + 1}: review the draft on Friday."
                        for index in range(lines)
                    )
                    html = ""
                    attachments = []
                    if variant:
                        html = "<p>Fictional planning note.</p><table>" + "".join(
                            f"<tr><td>Item {index}</td><td>Friday</td><td>$125</td></tr>"
                            for index in range(lines)
                        ) + "</table>"
                    if variant in (2, 3):
                        html += '<p><img src="cid:fictional-image"></p>'
                        attachments = [
                            AttachmentPayload(
                                provider_id="fictional-image", filename="fictional.png",
                                mime_type="image/png", content=image_bytes,
                                content_id="fictional-image", is_inline=True,
                            ),
                            AttachmentPayload(
                                provider_id="fictional-notes", filename="fictional-notes.txt",
                                mime_type="text/plain", content=text.encode(),
                            ),
                        ]
                    if render:
                        marker = f"Fictional conversation {thread_index}."
                        html = (
                            '<div style="background-color:#167e95;color:#ffffff;padding:4px">'
                            + marker + "</div><p>" + escape(text).replace("\n", "<br>")
                            + "</p>" + html
                        )
                        text = marker + "\n" + text
                    context.repository.ingest_message(
                        "benchmark-fictional",
                        MessagePayload(
                            provider_id=f"fictional-{thread_index}-{message_index}",
                            provider_thread_id=f"fictional-thread-{thread_index}",
                            subject=f"Fictional planning conversation {thread_index}",
                            sender_name="Fictional colleague", sender_email="colleague@example.net",
                            to=["fictional@example.com"], body_text=text, body_html=html,
                            sent_at=stamp + timedelta(minutes=thread_index * 5 + message_index),
                            received_at=stamp + timedelta(minutes=thread_index * 5 + message_index),
                            attachments=attachments,
                        ),
                    )
                    message_count += 1
            controller = AppController(context)
            controller.timer.stop()
            controller._start_maintenance = lambda: None
            if not controller.threads:
                initial_loop = QEventLoop()
                def initially_loaded():
                    if controller.threads:
                        initial_loop.quit()
                controller.threadsChanged.connect(initially_loaded)
                QTimer.singleShot(10000, initial_loop.quit)
                initial_loop.exec()
                controller.threadsChanged.disconnect(initially_loaded)
            thread_ids = [row["id"] for row in controller.threads]
            if len(thread_ids) < min(sample_count, thread_count):
                raise RuntimeError("Not all fictional threads are visible in the fixture")
            thread_ids = thread_ids[:sample_count]
            if render:
                native = NativeNavigation(source, controller)

            def select(thread_id: str) -> tuple[float, float, float | None]:
                if native:
                    native.prepare(thread_id, thread_ids.index(thread_id))
                loop = QEventLoop()
                observed = {}
                start = perf_counter()

                def changed():
                    selected = controller.selectedThread
                    if selected.get("id") != thread_id:
                        return
                    elapsed = (perf_counter() - start) * 1000
                    observed.setdefault("feedback", elapsed)
                    if selected.get("read_state") == "ready":
                        observed.setdefault("ready", elapsed)
                        loop.quit()

                controller.selectedThreadChanged.connect(changed)
                deadline = QTimer()
                deadline.setSingleShot(True)
                deadline.timeout.connect(loop.quit)
                deadline.start(10000)
                if native:
                    native.click()
                else:
                    controller.selectThreadAsync(thread_id)
                if "ready" not in observed:
                    loop.exec()
                deadline.stop()
                controller.selectedThreadChanged.disconnect(changed)
                if "ready" not in observed:
                    raise RuntimeError("Fictional navigation did not become ready within 10 seconds")
                frame_ms = native.frame(controller.selectedThread["messages"][0]["provider_id"], start) if native else None
                # Finish cache/account-count bookkeeping before measuring the next
                # item, including the background read-state update on a cache hit.
                controller.read_pool.waitForDone()
                app.processEvents()
                return observed["feedback"], observed["ready"], frame_ms

            # One unmeasured traversal removes interpreter/import/Qt startup costs.
            for thread_id in thread_ids:
                select(thread_id)
            for round_index in range(rounds):
                order = thread_ids[round_index:] + thread_ids[:round_index]
                controller._read_cache.clear()
                controller._read_cache_bytes = 0
                for thread_id in order:
                    feedback, ready, frame_ms = select(thread_id)
                    measurements["selection_feedback_ms"].append(feedback)
                    measurements["cold_content_ready_ms"].append(ready)
                    if native:
                        measurements["cold_click_to_frame_ms"].append(frame_ms)
                        native.stage_samples["cold"].append(native.last_stages)
                for thread_id in reversed(order):
                    feedback, ready, frame_ms = select(thread_id)
                    measurements["selection_feedback_ms"].append(feedback)
                    measurements["warm_content_ready_ms"].append(ready)
                    if native:
                        measurements["warm_click_to_frame_ms"].append(frame_ms)
                        native.stage_samples["warm"].append(native.last_stages)
            return {
                "method": (
                    "real row click to WebEngine load success, matching DOM, and first verified native grabWindow frame"
                    if render else "controller signals only; excludes GUI paint and browser rendering"
                ),
                "fixture_threads": thread_count,
                "fixture_messages": message_count,
                "sample_threads": len(thread_ids),
                "rounds": rounds,
                "source_package": str(source),
                "controller_sha256": hashlib.sha256((source / "controller.py").read_bytes()).hexdigest(),
                "main_qml_sha256": hashlib.sha256((source / "qml/Main.qml").read_bytes()).hexdigest(),
                "window_size": [native.window.width(), native.window.height()] if native else None,
                "render_stages": {
                    phase: {
                        stage: statistics.median(item[stage] for item in samples)
                        for stage in ("observer_lookup_ms", "webengine_loaded_ms", "dom_ready_ms")
                    }
                    for phase, samples in native.stage_samples.items()
                } if native else {},
                "metrics": {
                    name: {
                        "median_ms": statistics.median(values),
                        "p95_ms": sorted(values)[int((len(values) - 1) * .95)],
                        "samples": len(values),
                    }
                    for name, values in measurements.items()
                },
            }
        finally:
            if native is not None:
                native.close()
            if controller is not None:
                controller.timer.stop()
                if hasattr(controller, "shutdown"):
                    controller.shutdown()
                else:
                    controller.read_pool.waitForDone()
                app.processEvents()
            context.close()


def compare(args) -> tuple[dict, bool]:
    runs = {"baseline": [], "current": []}
    for iteration in range(args.repetitions):
        order = ("baseline", "current") if (iteration + int(args.reverse_first)) % 2 == 0 else ("current", "baseline")
        for name in order:
            if args.render:
                print(f"Native benchmark {name}, pair {iteration + 1}/{args.repetitions}", file=sys.stderr, flush=True)
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--source", str(getattr(args, name)),
                 "--rounds", str(args.rounds), "--threads", str(args.threads),
                 "--sample-threads", str(args.sample_threads)] + (["--render"] if args.render else []),
                capture_output=True, text=True, timeout=max(180, args.threads // 10), check=False,
            )
            if result.returncode:
                raise RuntimeError(f"Fictional {name} benchmark failed: {result.stderr[-3000:]}")
            runs[name].append(json.loads(result.stdout))
    comparisons = {}
    for metric in METRICS + (RENDER_METRICS if args.render else ()):
        medians = {
            name: statistics.median(run["metrics"][metric]["median_ms"] for run in results)
            for name, results in runs.items()
        }
        comparisons[metric] = {
            "baseline_median_ms": medians["baseline"],
            "current_median_ms": medians["current"],
            "change_percent": (medians["current"] / medians["baseline"] - 1) * 100,
            "within_10_percent": medians["current"] <= medians["baseline"] * 1.10,
        }
    # The ready-time gate covers local navigation. The immediate feedback signal
    # is reported separately because sub-millisecond signal timing is not paint.
    passed = all(comparisons[name]["within_10_percent"] for name in (RENDER_METRICS if args.render else METRICS[:2]))
    return {
        "method": "alternating isolated processes; median of process medians; " + ("native rendered frames" if args.render else "backend signals only"),
        "repetitions_per_version": args.repetitions,
        "comparisons": comparisons,
        "navigation_gate_passed": passed,
        "runs": runs,
    }, passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=source_directory)
    parser.add_argument("--baseline", type=source_directory)
    parser.add_argument("--current", type=source_directory)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--threads", type=int, default=24)
    parser.add_argument("--sample-threads", type=int, default=24, help="Visible threads measured per round; the rest remain in the full mailbox fixture")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--render", action="store_true", help="Load real QML and verify rendered fictional message pixels")
    parser.add_argument("--reverse-first", action="store_true", help="Start comparison with current, then baseline")
    args = parser.parse_args()
    if args.rounds < 1 or args.threads < 20 or args.repetitions < 1 or not 1 <= args.sample_threads <= 200:
        parser.error("Use at least 1 round, 20 threads, and 1 repetition")
    if args.source:
        if args.baseline or args.current:
            parser.error("Use --source alone or the --baseline/--current pair")
        print(json.dumps(run_fixture(args.source, args.rounds, args.threads, args.render, args.sample_threads), indent=2))
        return 0
    if not args.baseline or not args.current:
        parser.error("Supply --source, or both --baseline and --current")
    report, passed = compare(args)
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
