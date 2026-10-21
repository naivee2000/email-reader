"""Native QML checks for the search page using isolated fictional mail only."""

import os
import tempfile
from datetime import UTC, datetime

from bs4 import BeautifulSoup

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from check_email_ui import find
from PySide6.QtCore import (
    Q_ARG,
    QCoreApplication,
    QEvent,
    QEventLoop,
    QMetaObject,
    QPointF,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWebEngineQuick import QtWebEngineQuick

from email_reader.app import qml_entrypoint
from email_reader.context import ApplicationContext
from email_reader.contracts import Account, AttachmentPayload, MessagePayload, ProviderKind
from email_reader.controller import AppController


def settle(milliseconds=150):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def main():
    QQuickStyle.setStyle("Basic")
    QtWebEngineQuick.initialize()
    app = QGuiApplication([])
    with tempfile.TemporaryDirectory(prefix="email-reader-search-ui-") as directory:
        os.environ["EMAIL_READER_DATA_DIR"] = directory
        context = ApplicationContext.create(testing=True)
        context.repository.upsert_account(Account(id="fictional", provider=ProviderKind.GMAIL, email="owner@example.com"))
        context.repository.ingest_message("fictional", MessagePayload(
            provider_id="insurance", provider_thread_id="insurance", subject="Your insurance documents",
            sender_name="Maya Chen", sender_email="maya@example.com", to=["owner@example.com"],
            sent_at=datetime.now(UTC), body_text="The policy starts October 12. Three originals are attached.",
            attachments=[AttachmentPayload(provider_id=f"file-{i}", filename=name, mime_type="text/plain", content=f"Fictional original {i}".encode()) for i, name in enumerate(("Policy binder.txt", "Insurance ID card.txt", "Coverage schedule.txt"))],
        ))
        controller = AppController(context)
        controller.timer.stop()
        controller.startSearch("")
        session = controller._search_session()
        files = [dict(row) for row in context.repository.db.query("SELECT a.id,a.filename,m.account_id,m.id AS message_id,m.sent_at AS email_date,1 AS available FROM attachments a JOIN messages m ON m.id=a.message_id")]
        citations = [context.repository.citation_for_chunk(context.repository.db.scalar(
            "SELECT id FROM source_chunks WHERE source_type='attachment' AND source_id=? ORDER BY chunk_index LIMIT 1", (card["id"],),
        )).model_dump(mode="json") for card in files]
        answer_text = " ".join(["These", "documents", "describe", "the", "insurance", "coverage", "and", "its", "effective", "dates."] * 12)
        session["messages"].extend([{"role": "user", "text": "Find my auto insurance documents"}, {"role": "assistant", "text": answer_text, "citations": citations, "attachments": files, "file_cards": files}])
        session["title"] = "Auto insurance documents"
        engine = QQmlApplicationEngine()
        engine.addImageProvider("mail", controller.mail_images)
        engine.rootContext().setContextProperty("App", controller)
        qml_dir = QUrl.fromLocalFile(str(qml_entrypoint().parent)).toString()
        engine.loadData((f'import QtQuick\nimport QtQuick.Controls\nimport "{qml_dir}"\nApplicationWindow {{ width: 1174; height: 790; visible: true; MailAssistant {{ anchors.fill: parent }} }}').encode())
        assert engine.rootObjects(), "Search workspace failed to load"
        window = engine.rootObjects()[0]
        settle()
        workspace = find(window, "mailSearchWorkspace")
        scroll = find(window, "searchResultsScroll")
        viewport_top = scroll.mapToScene(QPointF(0, 0)).y()
        viewport_bottom = viewport_top + scroll.height()
        assert len(workspace.property("confirmedResults").toVariant()) == 3
        for name in ["searchAnswer", "searchSources", *["attachmentCard-" + card["id"] for card in files]]:
            item = find(window, name)
            assert item.isVisible()
            top = item.mapToScene(QPointF(0, 0)).y()
            assert viewport_top <= top and top + item.height() <= viewport_bottom, (name, top, item.height(), viewport_bottom)
        assert not find(window, "showMoreAnswer").isVisible()
        answer_html = find(window, "searchAnswer").property("text")
        for citation in citations:
            assert 'href="' + citation["app_link"] + '"' in answer_html
        first_id = controller.selectedSearchId
        question = find(window, "chatQuestion")
        question.setProperty("text", "What is the start date?")
        QMetaObject.invokeMethod(find(window, "fileDetails-" + files[0]["id"]), "clicked")
        settle()
        source_button = find(window, "fileSource-" + files[0]["id"])
        assert source_button.isVisible()
        assert QMetaObject.invokeMethod(source_button, "clicked")
        settle(1000)
        assert controller.sourcePreview.get("message_id") == files[0]["message_id"]
        assert find(window, "searchSourcePreview").isVisible()
        assert question.property("text") == "What is the start date?"
        assert workspace.property("currentQuestion") == "Find my auto insurance documents"
        assert controller.selectedThread == {}
        source_body = find(window, "searchSourceBody")
        assert source_body.width() > 280 and source_body.height() >= 60
        assert "The policy starts October 12" in source_body.property("documentHtml")
        window.grabWindow().save("/tmp/email-reader-search-source.png")
        QMetaObject.invokeMethod(find(window, "closeSearchSource"), "clicked")
        settle()
        assert not find(window, "searchSourcePreview").isVisible()
        QMetaObject.invokeMethod(find(window, "newSearch"), "clicked")
        settle()
        assert controller.selectedSearchId != first_id
        assert len(controller.searchSessions) == 2
        assert QMetaObject.invokeMethod(workspace, "selectSession", Q_ARG("QVariant", first_id))
        settle()
        assert workspace.property("currentQuestion") == "Find my auto insurance documents"
        assert question.property("text") == "What is the start date?"
        session["messages"][-1]["text"] += " More detail." * 40
        session["messages"][-1]["insufficient_evidence"] = True
        session["messages"][-1]["file_cards"].append({"id": "possible", "filename": "Possible quote.txt", "possible_match": True, "available": False})
        controller.chatChanged.emit()
        settle()
        assert find(window, "showMoreAnswer").isVisible()
        assert find(window, "chatEvidenceWarning").isVisible()
        QMetaObject.invokeMethod(find(window, "morePossibleSources"), "clicked")
        settle()
        assert find(window, "attachmentCard-possible").isVisible()
        QMetaObject.invokeMethod(find(window, "showMoreAnswer"), "clicked")
        settle()
        scroll.property("contentItem").setProperty("contentY", 80)
        controller.openSearchSource(citations[0]["app_link"])
        settle(600)
        controller.closeSearchSource()
        settle()
        assert abs(scroll.property("contentItem").property("contentY") - 80) < 1
        controller.chatChanged.emit()  # A history-list update must not reset the current view.
        settle()
        assert abs(scroll.property("contentItem").property("contentY") - 80) < 1
        assert workspace.property("showFullAnswer") and workspace.property("showPossible")
        context.repository.upsert_account(Account(id="other", provider=ProviderKind.OUTLOOK, email="work@example.com"))
        controller.filterAccount("other")
        settle()
        assert question.property("text") == ""
        assert not workspace.property("showFullAnswer")
        question.setProperty("text", "A separate work question")
        controller.filterAccount("")
        settle()
        assert question.property("text") == "What is the start date?"
        assert workspace.property("showFullAnswer") and workspace.property("showPossible")
        assert abs(scroll.property("contentItem").property("contentY") - 80) < 1
        controller.filterAccount("other")
        settle()
        assert question.property("text") == "A separate work question"
        controller.filterAccount("")
        settle()
        saved_answer = session["messages"][-1]
        # Search payloads retain unique chunk links but also carry the owning
        # message identity, so multiple evidence spans do not repeat email rows.
        canonical_citations = []
        for citation in citations[:2]:
            thread_id, message_id = context.repository.resolve_source_link(citation["source_id"])
            canonical_citations.append({**citation, "thread_id": thread_id, "message_id": message_id})
        session["messages"][-1] = {"role": "assistant", "text": "The original message includes both documents.", "citations": canonical_citations, "attachments": [], "file_cards": []}
        controller.chatChanged.emit()
        settle()
        assert len(workspace.property("confirmedResults").toVariant()) == 1
        session["messages"][-1] = saved_answer
        first_paragraph = " ".join(["The", "documents", "describe", "the", "insurance", "coverage", "and", "its", "effective", "dates."] * 6)
        saved_answer["text"] = (
            first_paragraph + f" [SOURCE:{citations[0]['source_id']}]\n\n"
            f"The original file includes the precise details. [SOURCE:{citations[1]['source_id']}]\n\n"
            + "More relevant details are available in the original documents " * 20 + "."
        )
        controller.chatChanged.emit()
        settle()
        preview_html = find(window, "searchAnswer").property("text")
        assert "<br" in preview_html, "Condensing an answer must preserve paragraph boundaries"
        visible_preview = BeautifulSoup(preview_html, "html.parser").get_text()
        assert "More relevant details" not in visible_preview, "Prefer a complete cited sentence near the cutoff"
        for citation in citations[:2]:
            assert citation["source_id"] not in visible_preview
            assert 'href="' + citation["app_link"] + '"' in preview_html
        assert find(window, "chatEvidenceWarning").isVisible()
        for width, height in [(824, 510), (908, 610), (1248, 790)]:
            window.setWidth(width)
            window.setHeight(height)
            settle()
            for name in ["searchBack", "newSearch", "chatQuestion", "searchSubmit"]:
                item = find(window, name)
                position = item.mapToScene(QPointF(0, 0))
                assert position.x() >= 0 and position.x() + item.width() <= width + 1
                assert position.y() >= 0 and position.y() + item.height() <= height + 1
        window.grabWindow().save("/tmp/email-reader-search-workspace.png")
        window.close()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        controller.pool.waitForDone()
        controller.read_pool.waitForDone()
        context.database.close()
        print("PASS: 120-word answer and three files above the fold; exact source preview; retained sessions; details; warnings; responsive controls.")


if __name__ == "__main__":
    main()
