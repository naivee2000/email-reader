"""The native recipient control debounces and ignores obsolete worker results."""

import os
import subprocess
import sys

FIXTURE = r"""
from pathlib import Path
from PySide6.QtCore import QObject, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from email_reader.app import dispose_qml_engine

class Bridge(QObject):
    recipientSuggestionsReady = Signal(str, str, str, 'QVariantList')
    def __init__(self):
        super().__init__(); self.calls = []
    @Slot(str, str, str)
    def requestRecipientSuggestions(self, account, query, token):
        self.calls.append((account, query, token))

QQuickStyle.setStyle('Basic')
app = QGuiApplication([])
bridge = Bridge()
engine = QQmlApplicationEngine()
engine.rootContext().setContextProperty('App', bridge)
engine.loadData(b'''
import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    visible: true; width: 500; height: 260
    AddressField { objectName: "address"; fieldName: "testTo"; accountId: "first"; width: 460 }
}
''', QUrl.fromLocalFile(str(Path('src/email_reader/qml/address-test.qml').resolve())))
assert engine.rootObjects(), 'Recipient field did not load'
window = engine.rootObjects()[0]
address = window.findChild(QObject, 'address')
field = window.findChild(QObject, 'testTo')
def suggestions():
    value = address.property('suggestions')
    return value.toVariant() if hasattr(value, 'toVariant') else value
field.forceActiveFocus()
QTest.qWait(200)
bridge.calls.clear()
for key in [Qt.Key_A, Qt.Key_B, Qt.Key_C]:
    QTest.keyClick(window, key)
    QTest.qWait(25)
assert not bridge.calls, 'Lookup was not debounced'
QTest.qWait(180)
assert len(bridge.calls) == 1 and bridge.calls[0][:2] == ('first', 'abc'), bridge.calls
account, query, token = bridge.calls[0]
bridge.recipientSuggestionsReady.emit('stale-token', account, query, [{'email':'wrong@example.test'}])
QTest.qWait(10)
assert suggestions() == []
bridge.recipientSuggestionsReady.emit(token, account, query, [{'email':'right@example.test', 'label':'Right'}])
QTest.qWait(10)
assert suggestions()[0]['email'] == 'right@example.test'
QTest.keyClick(window, Qt.Key_Escape)
QTest.qWait(10)
bridge.recipientSuggestionsReady.emit(token, account, query, [{'email':'wrong@example.test'}])
assert suggestions() == [], 'Dismissed results reappeared'
dispose_qml_engine(engine)
print('async address field passed')
"""


def test_recipient_field_debounces_and_discards_stale_results():
    result = subprocess.run(
        [sys.executable, "-c", FIXTURE],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "async address field passed" in result.stdout
