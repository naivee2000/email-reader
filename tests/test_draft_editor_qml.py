"""Exercise the real QML editor with fictional data in an isolated Qt process."""

import os
import subprocess
import sys
from pathlib import Path

QML_DIR = Path(__file__).resolve().parents[1] / "src/email_reader/qml"

EDITOR_FIXTURE = r'''
import sys
from PySide6.QtCore import QObject, Property, QMetaObject, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from email_reader.app import dispose_qml_engine


class Bridge(QObject):
    editorChanged = Signal()
    editorCandidatesChanged = Signal()
    editorSaved = Signal()

    def __init__(self):
        super().__init__()
        self.draft = {}
        self.candidates = []

    @Property('QVariantMap', notify=editorChanged)
    def editorDraft(self):
        return self.draft

    @Property('QVariantList', notify=editorCandidatesChanged)
    def editorCandidates(self):
        return self.candidates

    @Property('QVariantList', constant=True)
    def accounts(self):
        return [{'id': 'fictional', 'email': 'owner@example.net'}]

    @Property(bool, constant=True)
    def busy(self):
        return False

    @Property(bool, constant=True)
    def aiBusy(self):
        return False

    @Property(str, constant=True)
    def status(self):
        return 'Ready'

    @Slot()
    def discardEditor(self):
        self.draft = {}

    @Slot('QVariantMap')
    def saveEditor(self, _form):
        pass

    @Slot('QVariantMap', str)
    def assistEditor(self, _form, _instruction):
        pass

    @Slot()
    def cancelAI(self):
        pass


QQuickStyle.setStyle('Basic')
app = QGuiApplication([])
bridge = Bridge()
engine = QQmlApplicationEngine(app)
engine.rootContext().setContextProperty('App', bridge)
qml_dir = QUrl.fromLocalFile(sys.argv[1]).toString()
qml = f"""
import QtQuick
import QtQuick.Controls
import "{qml_dir}" as Email
ApplicationWindow {{
    visible: true; width: 1200; height: 900
    Email.DraftEditor {{ objectName: "reviewedEditor" }}
}}
"""
engine.loadData(qml.encode('utf-8'))
assert engine.rootObjects(), 'Editor QML did not load'
window = engine.rootObjects()[0]
editor = window.findChild(QObject, 'reviewedEditor')
body = window.findChild(QObject, 'manualDraftBody')
assert editor and body

bridge.draft = {'mode': 'new', 'account_id': 'fictional', 'to': 'friend@example.net',
                'cc': '', 'bcc': '', 'subject': 'Hello', 'body': ''}
bridge.editorChanged.emit()
app.processEvents()
assert editor.property('visible'), 'Compose did not open the reviewed editor'
body.setProperty('text', 'My own words')
editor.setProperty('showAssistant', True)
bridge.candidates = [
    {'label': label, 'stance': 'Greeting', 'assumption': 'No commitment',
     'body': label + ' suggestion'}
    for label in ('Concise', 'Warm', 'Detailed')
]
bridge.editorCandidatesChanged.emit()
app.processEvents()
QTest.qWait(100)
assert body.property('text') == 'My own words', 'Suggestions overwrote the user draft'
assert len(bridge.candidates) == 3
def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)

choices = {item.objectName(): item for item in visual_items(window.contentItem())
           if item.objectName().startswith('useOption-')}
assert set(choices) == {'useOption-Concise', 'useOption-Warm', 'useOption-Detailed'}, repr([
    item.objectName() for item in visual_items(window.contentItem()) if item.objectName()
])
option = choices['useOption-Concise']
option.clicked.emit()
app.processEvents()
assert body.property('text') == 'Concise suggestion'

QMetaObject.invokeMethod(editor, 'requestClose')
app.processEvents()
confirm = window.findChild(QObject, 'confirmCloseDraft')
assert confirm and confirm.property('visible'), 'Unsaved-draft guard did not open'
for label in ('Save draft', 'Discard', 'Cancel'):
    assert any(child.property('text') == label for child in confirm.findChildren(QObject))

dispose_qml_engine(engine)
print('reviewed editor behavior passed', flush=True)
'''


def test_reviewed_editor_preserves_user_text_and_guards_close():
    result = subprocess.run(
        [sys.executable, "-c", EDITOR_FIXTURE, str(QML_DIR)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert result.returncode == 0, result.stderr
    assert "reviewed editor behavior passed" in result.stdout
