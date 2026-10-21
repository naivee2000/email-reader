"""Stress safe child launches only after a live native WebEngine is ready.

Uses fictional input and harmless system children; no account, Mail, Keychain,
AI backend, database, or network operation is created. Unsafe Python fork paths
raise before execution, so this does not reproduce the old native crash.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QRunnable, QThreadPool, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtWebEngineQuick import QtWebEngineQuick

from email_reader import process_launch
from email_reader.app import dispose_qml_engine
from email_reader.extractor_worker import extract_isolated

QML = b"""
import QtQuick
import QtQuick.Controls
import QtWebEngine
ApplicationWindow {
    id: root; width: 480; height: 240; visible: true
    property bool documentReady: false
    property int beats: 0
    Timer { interval: 20; running: true; repeat: true; onTriggered: root.beats += 1 }
    WebEngineProfile {
        id: privateProfile; offTheRecord: true
        httpCacheType: WebEngineProfile.MemoryHttpCache
        persistentCookiesPolicy: WebEngineProfile.NoPersistentCookies
    }
    WebEngineView {
        id: view; anchors.fill: parent; profile: privateProfile
        Component.onCompleted: loadHtml('<html><body><p id="fixture">Fictional process test</p></body></html>')
        onLoadingChanged: function(request) {
            if (request.status === WebEngineView.LoadSucceededStatus)
                runJavaScript("document.getElementById('fixture').textContent", function(text) {
                    root.documentReady = text === 'Fictional process test'
                })
        }
    }
}
"""

def wait_until(predicate, seconds=15):
    loop, timer, deadline = QEventLoop(), QTimer(), QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    timer.start()
    deadline.start(int(seconds * 1000))
    if not predicate():
        loop.exec()
    timer.stop()
    deadline.stop()
    assert predicate(), "Fictional process stress check exceeded its deadline"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class LaunchLane(QRunnable):
    def __init__(self, index, count, barrier, results, failures, finished, temp_root):
        super().__init__()
        self.index, self.count, self.barrier = index, count, barrier
        self.results, self.failures, self.finished = results, failures, finished
        self.temp_root = temp_root

    def run(self):
        try:
            self.barrier.wait(timeout=5)
            for number in range(self.count):
                expected = f"fictional lane {self.index}, message {number}, café"
                result = process_launch.run(
                    ["/bin/cat"], input=expected,
                    text=True, capture_output=True, timeout=5, check=True,
                    start_new_session=number % 2 == 0,
                )
                assert result.stdout == expected
                assert result.stderr == ""
                self.results.append(expected)
                if number % 3 == 0:
                    extracted = extract_isolated(
                        "fictional.txt", "text/plain", expected.encode(), temp_root=self.temp_root,
                    )
                    assert extracted.text == expected
        except BaseException as error:
            self.failures.append(f"lane {self.index}: {type(error).__name__}: {error}")
        finally:
            self.finished.append(self.index)


def check_timeout_and_cancellation():
    sleeper = ["/bin/sleep", "30"]
    try:
        process_launch.run(sleeper, capture_output=True, text=True, timeout=.25, start_new_session=True)
    except subprocess.TimeoutExpired:
        pass  # All recorded child PIDs are checked for reaping below.
    else:
        raise AssertionError("Timeout did not stop its child")

    # Stop only the session owned by this request; a sibling must remain alive.
    children = []
    try:
        for _ in range(2):
            child = process_launch.Popen(sleeper, stdout=subprocess.PIPE, text=True, start_new_session=True)
            children.append(child)
            assert os.getsid(child.pid) == os.getpgid(child.pid) == child.pid
        target, sibling = children
        assert os.getpgid(target.pid) == target.pid
        os.killpg(target.pid, signal.SIGTERM)
        target.communicate(timeout=2)
        assert target.returncode == -signal.SIGTERM
        assert sibling.poll() is None, "Cancelling one request stopped an unrelated child"
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=2)

    try:
        process_launch.run(["/usr/bin/false"],
                           capture_output=True, text=True, check=True, timeout=5)
    except subprocess.CalledProcessError as error:
        assert error.returncode == 1 and error.stderr == ""
    else:
        raise AssertionError("Failed child did not preserve subprocess error semantics")


def run_probe(*, lanes=4, children_per_lane=12) -> int:
    """Early diagnostic entry point, before any account context or vault opens."""
    if sys.platform != "darwin":
        raise SystemExit("This native crash regression targets macOS")
    if not 2 <= lanes <= 8 or not 1 <= children_per_lane <= 50:
        raise ValueError("Use 2–8 lanes and 1–50 children per lane")
    QtWebEngineQuick.initialize()
    app = QGuiApplication([])
    with tempfile.TemporaryDirectory(prefix="email-reader-process-ui-") as temporary, ExitStack() as stack:
        engine = QQmlApplicationEngine()
        stack.callback(dispose_qml_engine, engine)
        engine.loadData(QML)
        assert engine.rootObjects(), "Fictional WebEngine view did not load"
        window = engine.rootObjects()[0]
        assert isinstance(window, QQuickWindow)
        wait_until(lambda: window.property("documentReady"))
        assert not window.grabWindow().isNull(), "Native frame was unavailable"
        forbidden_calls, spawn_calls = [], []
        lock = threading.Lock()
        original_spawn = process_launch._darwin_spawn

        def no_fork(*_args, **_kwargs):
            forbidden_calls.append(True)
            raise AssertionError("Unsafe fork fallback blocked before execution")

        def counted_spawn(*arguments, **keywords):
            pid = original_spawn(*arguments, **keywords)
            with lock:
                spawn_calls.append((threading.get_ident(), pid))
            return pid

        stack.enter_context(patch.object(subprocess, "_fork_exec", no_fork))
        stack.enter_context(patch.object(os, "fork", no_fork))
        stack.enter_context(patch.object(process_launch, "_darwin_spawn", counted_spawn))
        pools, runners, results, failures, finished = [], [], [], [], []
        barrier = threading.Barrier(lanes)
        baseline_beats = window.property("beats")
        started = time.perf_counter()
        for index in range(lanes):
            pool = QThreadPool()
            pool.setMaxThreadCount(1)
            pools.append(pool)
            stack.callback(pool.waitForDone)
            runner = LaunchLane(index, children_per_lane, barrier, results, failures, finished, Path(temporary))
            runners.append(runner)
            pool.start(runner)
        wait_until(lambda: len(finished) == lanes, seconds=60)
        assert not failures, failures
        assert len(results) == lanes * children_per_lane
        assert len(set(results)) == len(results)
        assert window.property("beats") > baseline_beats, "UI event loop stopped during child launches"
        check_timeout_and_cancellation()
        assert not forbidden_calls
        extraction_count = lanes * ((children_per_lane + 2) // 3)
        assert len(spawn_calls) == len(results) + extraction_count + 4
        assert len({thread for thread, _ in spawn_calls}) >= lanes
        assert all(not alive(pid) for _, pid in spawn_calls), "A helper child was not reaped"
        assert not list(Path(temporary).iterdir())
        report = {"passed": True, "webengine_loaded_before_launches": True,
                  "frozen_application": bool(getattr(sys, "frozen", False)),
                  "qt_worker_lanes": lanes, "completed_parallel_children": len(results),
                  "completed_real_extraction_workers": extraction_count,
                  "native_spawn_calls": len(spawn_calls), "unsafe_fork_calls": len(forbidden_calls),
                  "gui_heartbeat_ticks": window.property("beats") - baseline_beats,
                  "timeout_reaped": True, "cancel_preserved_sibling": True,
                  "seconds": round(time.perf_counter() - started, 3)}
        print(json.dumps(report, indent=2), flush=True)
    app.processEvents()
    return 0
