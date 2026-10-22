from __future__ import annotations

import os
import tempfile
from pathlib import Path


def main() -> int:
    os.environ.setdefault("EMAIL_READER_TESTING", "1")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    temporary = tempfile.TemporaryDirectory(prefix="email-reader-smoke-")
    os.environ["EMAIL_READER_DATA_DIR"] = temporary.name

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle

    from email_reader.context import ApplicationContext
    from email_reader.controller import AppController
    from email_reader.demo import populate_demo

    QQuickStyle.setStyle("Basic")
    application = QGuiApplication([])
    context = ApplicationContext.create(testing=True)
    populate_demo(context.repository)
    demo_capture = os.environ.get("EMAIL_READER_SMOKE_DEMO") == "1"
    controller = AppController(context, demo_mode=demo_capture)
    if controller.threads:
        controller.selectThread(controller.threads[0]["id"])
    controller.setParent(application)
    engine = QQmlApplicationEngine(application)
    engine.rootContext().setContextProperty("App", controller)
    engine.load(
        QUrl.fromLocalFile(str(Path(__file__).parents[1] / "src/email_reader/qml/Main.qml"))
    )
    if not engine.rootObjects():
        raise RuntimeError("QML failed to load")
    if demo_capture:
        controller.startDemoReplay(0)
        window = engine.rootObjects()[0]
        window.setWidth(int(os.environ.get("EMAIL_READER_SMOKE_WIDTH", "1366")))
        window.setHeight(int(os.environ.get("EMAIL_READER_SMOKE_HEIGHT", "900")))
        window.setProperty("viewMode", "search")

    def finish() -> None:
        screenshot = os.environ.get("EMAIL_READER_SMOKE_SCREENSHOT")
        if screenshot:
            engine.rootObjects()[0].grabWindow().save(screenshot)
        application.quit()

    QTimer.singleShot(750, finish)
    exit_code = application.exec()
    engine.rootObjects()[0].setVisible(False)
    engine.deleteLater()
    application.processEvents()
    context.close()
    temporary.cleanup()
    return exit_code


os._exit(main())
