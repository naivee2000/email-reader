"""Subprocess boundary matters: the regression happened in PySide's atexit."""

import os
import subprocess
import sys

SHUTDOWN_FIXTURE = r'''
import sys
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtQml import QQmlApplicationEngine
from email_reader.app import dispose_qml_engine
from email_reader.mail_images import MailImageProvider

app = QGuiApplication([])
engine = QQmlApplicationEngine(app)
provider = MailImageProvider(None)
picture = QImage(10, 10, QImage.Format_RGB32)
picture.fill(Qt.red)
provider.remote['fixture'] = picture
engine.addImageProvider('mail', provider)
engine.loadData(b"""
import QtQuick
import QtQuick.Controls
ApplicationWindow {
    visible: true; width: 300; height: 200
    property bool rejectClose: true
    property int closeRequests: 0
    onClosing: function(event) {
        closeRequests += 1
        if (rejectClose) event.accepted = false
    }
    Text { textFormat: Text.RichText; text: '<img src="image://mail/fixture" />' }
}
""")
assert engine.rootObjects()
window = engine.rootObjects()[0]

def attempt_quit():
    app.quit()
    QTimer.singleShot(100, check_rejected)

def check_rejected():
    assert window.isVisible(), 'Native quit bypassed close protection'
    assert window.property('closeRequests') >= 1
    window.setProperty('rejectClose', False)
    QTimer.singleShot(0, app.quit)

QTimer.singleShot(250, attempt_quit)
QTimer.singleShot(3000, lambda: app.exit(9))
result = app.exec()
assert result == 0, 'Shutdown deadline reached'
if sys.argv[-1] != 'legacy':
    dispose_qml_engine(engine)
print('clean shutdown', flush=True)
'''


def test_image_engine_closes_before_interpreter_exit_and_quit_respects_draft_guard():
    result = subprocess.run([sys.executable, "-c", SHUTDOWN_FIXTURE, "fixed"],
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "clean shutdown" in result.stdout


PRIVATE_REVIEW_FIXTURE = r'''
import ast
from pathlib import Path
from PySide6.QtCore import QObject, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from email_reader.app import dispose_qml_engine

tree = ast.parse(Path('src/email_reader/app.py').read_text(encoding='utf-8'))
qml = next(
    node.args[0].value for node in ast.walk(tree)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    and isinstance(node.func.value, ast.Name)
    and node.func.value.id == 'review_engine' and node.func.attr == 'loadData'
)
class Bridge(QObject):
    @Slot(str, result=bool)
    def save(self, text):
        return bool(text)

app = QGuiApplication([])
bridge = Bridge(app)
engine = QQmlApplicationEngine(app)
engine.rootContext().setContextProperty('ReviewText', 'Fictional message')
engine.rootContext().setContextProperty('ReviewIntent', 'A fictional preference')
engine.rootContext().setContextProperty('ReviewIntentEnabled', True)
engine.rootContext().setContextProperty('ReviewIntentBridge', bridge)
engine.loadData(qml)
assert engine.rootObjects(), 'Private review QML did not load'
dispose_qml_engine(engine)
print('private review loaded', flush=True)
'''


def test_private_intent_review_qml_loads_offscreen():
    result = subprocess.run(
        [sys.executable, "-c", PRIVATE_REVIEW_FIXTURE],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "private review loaded" in result.stdout


STARTUP_ERROR_FIXTURE = r'''
import sys
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QSignalSpy, QTest
from email_reader.app import dispose_qml_engine, startup_error_qml

app = QGuiApplication([])
QQuickStyle.setStyle('Basic')
engine = QQmlApplicationEngine(app)
qml = startup_error_qml('database /private/fictional-secret password')
assert b'/private/fictional-secret' not in qml
engine.loadData(qml)
assert engine.rootObjects(), 'Startup failure window did not load'
window = engine.rootObjects()[0]
button = window.findChild(QObject, 'closeStartupError')
assert button and button.property('visible') and button.property('text') == '×'
spy = QSignalSpy(engine.quit)
QTest.qWait(100)
for item in window.findChildren(QObject):
    if hasattr(item, 'mapToScene') and item.property('text') == 'Close':
        assert item.mapToScene(item.boundingRect().bottomRight()).y() <= window.height() - 28
if sys.argv[-1] == 'escape':
    QTest.keyClick(window, Qt.Key_Escape)
else:
    point = button.mapToScene(button.boundingRect().center()).toPoint()
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
QTest.qWait(50)
assert spy.count() == 1, 'Startup dismissal did not request quit exactly once'
dispose_qml_engine(engine)
print('startup dismissal passed', flush=True)
'''


def test_startup_error_x_and_escape_dismiss_without_private_context():
    for mode in ('escape', 'close'):
        result = subprocess.run(
            [sys.executable, '-c', STARTUP_ERROR_FIXTURE, mode],
            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert 'startup dismissal passed' in result.stdout
