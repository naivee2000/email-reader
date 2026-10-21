"""Native QML acceptance checks using fictional mail and an isolated database."""

import os
import tempfile
import threading
from base64 import b64encode
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from unittest.mock import Mock

from bs4 import BeautifulSoup

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import (
    QBuffer,
    QCoreApplication,
    QEventLoop,
    QIODevice,
    QMetaObject,
    QPoint,
    QPointF,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QGuiApplication, QImage, QWheelEvent
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlProperty
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWebEngineQuick import QtWebEngineQuick

from email_reader.app import dispose_qml_engine, qml_entrypoint
from email_reader.context import ApplicationContext
from email_reader.contracts import (
    Account,
    Answer,
    AttachmentPayload,
    DraftReceipt,
    MessagePayload,
    ProviderKind,
    SyncPage,
)
from email_reader.controller import AppController
from email_reader.rendering import email_document


def wait(milliseconds=100):
    """Run Qt's native loop so Python read/index workers can acquire the GIL."""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def await_ready(predicate, label, timeout=4000):
    until = perf_counter() + timeout / 1000
    while not predicate() and perf_counter() < until:
        wait(25)
    assert predicate(), label


def walk(root):
    pending, seen = [root], set()
    while pending:
        item = pending.pop()
        if item in seen:
            continue
        seen.add(item)
        yield item
        pending.extend(item.children())
        if hasattr(item, "childItems") and "WebEngine" not in item.metaObject().className():
            pending.extend(item.childItems())


def find(root, name):
    for item in walk(root):
        if item.objectName() == name:
            return item
    raise AssertionError(f"Missing UI object: {name}")


def has_item(root, name):
    return any(item.objectName() == name for item in walk(root))


def activate(item):
    assert QMetaObject.invokeMethod(item, "clicked"), item.objectName()
    wait()


def click(window: QQuickWindow, item: QQuickItem):
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
    wait()


def type_text(window, text):
    for char in text:
        QTest.keyClick(window, char)
    wait()


def within(item, container, *, vertical=True):
    point = item.mapToItem(container, QPointF(0, 0))
    assert point.x() >= -1 and point.x() + item.width() <= container.width() + 1, (
        item.objectName(),
        "horizontal overflow",
        point.x(),
        item.width(),
        container.width(),
    )
    if vertical:
        assert point.y() >= -1 and point.y() + item.height() <= container.height() + 1, (
            item.objectName(),
            "vertical overflow",
            point.y(),
            item.height(),
            container.height(),
        )


def contrast_ratio(foreground, background):
    def luminance(color):
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return sum(
            value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
        )

    assert foreground.alphaF() == 1 and background.alphaF() == 1
    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def assert_loaded_contrast(item, background, minimum=4.5, color_property="color"):
    ancestor = item
    while ancestor is not None:
        assert ancestor.opacity() == 1, "Ancestor opacity must not wash out content"
        ancestor = ancestor.parentItem()
    ratio = contrast_ratio(item.property(color_property), background)
    assert ratio >= minimum, f"{item.objectName()} {color_property}: {ratio:.2f}:1"
    return ratio


def main():
    QQuickStyle.setStyle("Basic")
    QtWebEngineQuick.initialize()
    app = QGuiApplication([])
    with tempfile.TemporaryDirectory(prefix="email-reader-ui-check-") as directory, ExitStack() as cleanup:
        os.environ["EMAIL_READER_DATA_DIR"] = directory
        context = ApplicationContext.create(testing=True)
        cleanup.callback(context.close)
        context.repository.upsert_account(
            Account(id="a", provider=ProviderKind.GMAIL, email="owner@example.com")
        )
        logo = QImage(1200, 200, QImage.Format_RGB32)
        logo.fill("#174EA6")
        image_buffer = QBuffer()
        image_buffer.open(QIODevice.WriteOnly)
        assert logo.save(image_buffer, "PNG")
        logo_bytes = bytes(image_buffer.data())
        for identifier, days, body in [
            ("old", 2, "Can we meet Tuesday?"),
            (
                "new",
                0,
                "Tuesday works. See you then!\n\nOn Monday, Maya wrote:\nCan we meet Tuesday?",
            ),
        ]:
            context.repository.ingest_message(
                "a",
                MessagePayload(
                    provider_id=identifier,
                    provider_thread_id="t",
                    subject="Meeting Tuesday",
                    snippet='Details <a href="https://example.net target="_blank">open</a> today'
                    if identifier == "new"
                    else "",
                    sender_name="Maya Chen",
                    sender_email="maya@example.net",
                    to=["owner@example.com"],
                    body_text=body,
                    labels=["INBOX"],
                    is_unread=True,
                    body_html='<p>Tuesday works. See you then!</p><p><a href="https://example.com/meeting/long/link">Meeting details</a></p><img src="cid:logo" /><div class="gmail_quote">Can we meet Tuesday?</div>'
                    if identifier == "new"
                    else "",
                    attachments=[
                        AttachmentPayload(
                            provider_id="logo",
                            content_id="logo",
                            filename="logo.png",
                            mime_type="image/png",
                            content=logo_bytes,
                            is_inline=True,
                        ),
                        AttachmentPayload(
                            provider_id="details",
                            filename="meeting-details.txt",
                            mime_type="text/plain",
                            content=b"Exact fictional meeting details\n",
                        ),
                    ]
                    if identifier == "new"
                    else [],
                    sent_at=datetime.now(UTC) - timedelta(days=days),
                    received_at=datetime.now(UTC) - timedelta(days=days),
                ),
            )
        for number in range(15):
            context.repository.ingest_message(
                "a",
                MessagePayload(
                    provider_id=f"filler-{number}",
                    provider_thread_id=f"filler-{number}",
                    subject=f"Project notes {number + 1}",
                    sender_name="Alex Morgan",
                    sender_email="alex@example.net",
                    to=["owner@example.com"],
                    labels=["INBOX"],
                    snippet="The notes for our next conversation are ready.",
                    body_text="Fictional project notes. No action is required.",
                    sent_at=datetime.now(UTC) - timedelta(days=number + 4),
                    received_at=datetime.now(UTC) - timedelta(days=number + 4),
                ),
            )
        historical_bytes = {}
        for index in range(4):
            historical_bytes[f"Insurance document {index + 1}.txt"] = (
                f"Fictional insurance document {index + 1}. Effective date September 12, 2026.\n"
            ).encode()
        context.repository.ingest_message(
            "a",
            MessagePayload(
                provider_id="source-old",
                provider_thread_id="insurance",
                subject="Insurance documents",
                sender_name="Maya Chen",
                sender_email="maya@example.net",
                to=["owner@example.com"],
                labels=["INBOX"],
                body_text="The original insurance details are attached.",
                sent_at=datetime.now(UTC) - timedelta(days=3),
                received_at=datetime.now(UTC) - timedelta(days=3),
                attachments=[
                    AttachmentPayload(
                        provider_id=f"historical-{i}",
                        filename=name,
                        mime_type="text/plain",
                        content=data,
                    )
                    for i, (name, data) in enumerate(historical_bytes.items())
                ],
            ),
        )
        context.save_setting("ai_backend", "codex")
        controller = AppController(context)
        cleanup.callback(controller.shutdown)
        controller.timer.stop()
        controller._start_maintenance = lambda: None
        controller._provider = Mock(
            side_effect=AssertionError("UI fixtures must not access a provider")
        )
        context.assistant.answer = Mock(
            return_value=(Answer(text="A fictional answer grounded in this fixture."), [])
        )
        context.openai.cancel = Mock()
        engine = QQmlApplicationEngine()
        cleanup.callback(dispose_qml_engine, engine)
        engine.addImageProvider("mail", controller.mail_images)
        engine.rootContext().setContextProperty("App", controller)
        engine.load(QUrl.fromLocalFile(str(qml_entrypoint())))
        assert engine.rootObjects(), "QML failed to load"
        window = engine.rootObjects()[0]
        await_ready(lambda: bool(controller.threads), "Initial inbox did not load")
        window.setWidth(1366)
        window.setHeight(900)
        wait(150)
        # Frozen-Mail confirmation is exercised without touching real Mail.
        window.setWidth(880)
        window.setHeight(620)
        controller._pending_mail_restart = True
        controller.mailRestartApprovalRequired.emit()
        wait(100)
        recovery_dialog = find(window, "mailRecoveryDialog")
        assert recovery_dialog.property("visible")
        assert recovery_dialog.property("width") <= 840
        assert recovery_dialog.property("height") < 620
        window.grabWindow().save("/tmp/email-reader-mail-recovery.png")
        QTest.keyClick(window, Qt.Key_Escape)
        wait(100)
        assert not recovery_dialog.property("visible")
        assert not controller._pending_mail_restart
        window.setWidth(1366)
        window.setHeight(900)
        wait(100)
        window.grabWindow().save("/tmp/email-reader-redesign-inbox.png")
        threads = context.repository.list_threads()
        main_thread = next(item for item in threads if item["subject"] == "Meeting Tuesday")
        other_thread = next(item for item in threads if item["id"] != main_thread["id"])
        assert main_thread["snippet"] == "Details open today"
        thread_list = find(window, "threadList")
        rows = [item for item in walk(thread_list) if item.objectName().startswith("threadRow-")]
        visible_rows = [
            row
            for row in rows
            if row.y() - thread_list.property("contentY") >= 0
            and row.y() - thread_list.property("contentY") + row.height() <= thread_list.height()
        ]
        assert len(visible_rows) >= 9, (
            f"Only {len(visible_rows)} complete inbox rows fit (list {thread_list.width()}x{thread_list.height()}, rows {len(rows)}, threads {len(controller.threads)})"
        )
        assert all(row.height() == 72 for row in rows)
        assert find(window, "navigationSidebar").width() == 192
        assert find(window, "readingPane").width() >= 400
        toolbar = find(window, "readingToolbar")
        actions = [item for item in toolbar.childItems() if item.property("text")]
        assert [item.property("text") for item in actions] == [
            "Reply",
            "Reply All",
            "Forward",
            "More",
        ]
        assert len([item for item in walk(window) if item.objectName() == "refreshAction"]) == 1
        assert len([item for item in walk(window) if item.objectName() == "quietStatus"]) == 1
        reply_action = find(window, "messageReplyAction")
        assert not reply_action.isEnabled()
        row = find(window, "threadRow-" + main_thread["id"])
        preview_label = next(
            item for item in walk(row) if item.property("text") == "Details open today"
        )
        assert preview_label is not None
        preview_source = (
            qml_entrypoint()
            .read_text()
            .split("text: root.displayMessageText(modelData.snippet)", 1)[1][:100]
        )
        assert "textFormat: Text.PlainText" in preview_source, (
            "Inbox previews must remain plain text"
        )
        started = perf_counter()
        assert QMetaObject.invokeMethod(row, "activate")
        assert perf_counter() - started < 1
        await_ready(
            lambda: controller.selectedThread.get("read_state") == "ready",
            "Thread did not finish loading",
        )
        wait(800)
        assert reply_action.isEnabled()
        assert not next(item for item in controller.threads if item["id"] == main_thread["id"])[
            "is_unread"
        ]
        new_card, old_card = find(window, "messageCard-new"), find(window, "messageCard-old")
        assert new_card.y() < old_card.y()
        rich_body = find(window, "messageBody-new")
        assert "data:image/png;base64," in rich_body.property("documentHtml")
        assert "default-src 'none'" in rich_body.property("documentHtml")
        assert rich_body.height() < 320
        file_id = context.database.scalar(
            "SELECT id FROM attachments WHERE filename='meeting-details.txt'"
        )
        assert find(window, "messageExport-" + file_id).isEnabled()
        window.grabWindow().save("/tmp/email-reader-redesign-inbox-1366.png")
        window.grabWindow().save("/tmp/email-reader-redesign-inbox.png")
        for width, height in ((880, 620), (1100, 720), (1440, 900)):
            window.setWidth(width)
            window.setHeight(height)
            wait(400)
            assert find(window, "navigationSidebar").width() == (56 if width < 1100 else 192)
            for name in (
                "refreshAction",
                "composeAction",
                "readingToolbar",
                "messageReplyAction",
                "readingMore",
            ):
                within(find(window, name), window.contentItem(), vertical=False)
            assert thread_list.property("contentWidth") <= thread_list.width() + 1
            window.grabWindow().save(f"/tmp/email-reader-redesign-inbox-{width}.png")
        window.setWidth(1050)
        wait(500)
        rich_body = find(window, "messageBody-new")
        rendered = window.grabWindow()
        origin = rich_body.mapToScene(QPointF(0, 0))
        blue = [
            (x, y)
            for y in range(
                max(0, int(origin.y())),
                min(rendered.height(), int(origin.y() + rich_body.height())),
            )
            for x in range(
                max(0, int(origin.x())), min(rendered.width(), int(origin.x() + rich_body.width()))
            )
            if rendered.pixelColor(x, y).name() == "#174ea6"
        ]
        assert len(blue) > 3000, "Embedded image pixels are missing"
        blue_width = max(x for x, _ in blue) - min(x for x, _ in blue) + 1
        blue_height = max(y for _, y in blue) - min(y for _, y in blue) + 1
        assert abs((blue_width / blue_height) / 6 - 1) <= 0.01
        toggle = find(window, "quoteToggle-new")
        activate(toggle)
        assert new_card.property("showOriginal")
        activate(toggle)
        assert not new_card.property("showOriginal")
        assert not any(item.objectName() == "messageBody-old" for item in walk(window)), (
            "Collapsed mail should not create a browser view"
        )
        older_expand = next(
            item
            for item in walk(find(window, "messageCard-old"))
            if hasattr(item, "clicked") and item.property("text") == "Show message"
        )
        activate(older_expand)
        await_ready(
            lambda: any(item.objectName() == "messageBody-old" for item in walk(window)),
            "Older message did not create its browser when expanded",
        )
        assert "Can we meet Tuesday?" in find(window, "messageBody-old").property("documentHtml")
        newest_expand = next(
            item
            for item in walk(find(window, "messageCard-new"))
            if hasattr(item, "clicked") and item.property("text") == "Show message"
        )
        activate(newest_expand)
        await_ready(
            lambda: any(item.objectName() == "messageBody-new" for item in walk(window)),
            "Newest message did not reopen",
        )
        assert not any(item.objectName() == "messageBody-old" for item in walk(window)), (
            "Collapsing an older message must release its browser"
        )
        print(
            f"PASS: {len(visible_rows)} inbox rows, 72px density, one refresh/status, four reading actions, responsive bounds"
        )

        window.setWidth(880)
        window.setHeight(620)
        controller.beginDraft("new")
        await_ready(lambda: bool(controller.editorDraft), "New editor did not open")
        editor = find(window, "draftEditorDialog")
        field = find(window, "editorTo")
        assert editor.property("active") and editor.isVisible()
        assert (
            not find(window, "editorCc").isVisible() and not find(window, "editorBcc").isVisible()
        )
        field.forceActiveFocus()
        type_text(window, "maya")
        await_ready(lambda: has_item(window, "editorTo-suggestion-0"), "Recipient lookup did not finish")
        assert (
            find(window, "editorTo-suggestion-0").property("text") == "Maya Chen <maya@example.net>"
        )
        address_root = field.parentItem().parentItem()
        QTest.keyClick(window, Qt.Key_Escape)
        wait()
        remaining = address_root.property("suggestions")
        assert (remaining.toVariant() if hasattr(remaining, "toVariant") else remaining) == []
        assert field.property("text") == "maya" and not find(window, "confirmCloseDraft").property(
            "visible"
        )
        QMetaObject.invokeMethod(address_root, "suggest")
        await_ready(lambda: has_item(window, "editorTo-suggestion-0"), "Recipient lookup did not reopen")
        QTest.keyClick(window, Qt.Key_Return)
        wait()
        assert field.property("text") == "maya@example.net, "
        for field_name, toggle_name in (
            ("editorCc", "toggleEditorCc"),
            ("editorBcc", "toggleEditorBcc"),
        ):
            activate(find(window, toggle_name))
            extra = find(window, field_name)
            assert extra.isVisible()
            extra.forceActiveFocus()
            type_text(window, "maya")
            await_ready(
                lambda name=field_name: has_item(window, name + "-suggestion-0"),
                "CC/BCC lookup did not finish",
            )
            assert find(window, field_name + "-suggestion-0")
            QTest.keyClick(window, Qt.Key_Return)
            wait()
            assert extra.property("text") == "maya@example.net, "
        body = find(window, "manualDraftBody")
        body.setProperty("text", "My original wording")
        controller._editor_candidates = [
            {
                "label": label,
                "body": label + " suggested reply",
                "stance": "Accept",
                "assumption": "The date works",
            }
            for label in ("Concise", "Warm", "Detailed")
        ]
        controller.editorCandidatesChanged.emit()
        assert body.property("text") == "My original wording"
        editor.setProperty("showAssistant", True)
        wait()
        choices = [find(window, "useOption-" + label) for label in ("Concise", "Warm", "Detailed")]
        assert sum(item.isVisible() for item in choices) == 1
        tabs = find(window, "replyCandidateTabs")
        tabs.setProperty("currentIndex", 1)
        wait()
        assert choices[1].isVisible() and not choices[0].isVisible()
        assert body.property("text") == "My original wording"
        for name in (
            "editorAccount",
            "editorTo",
            "editorCc",
            "editorBcc",
            "manualDraftBody",
            "replyAssistantToggle",
            "reviewedDraftSave",
            "closeDraftEditor",
        ):
            within(find(window, name), editor, vertical=False)
        text_ratios, control_ratios = [], []
        for name in (
            "manualDraftBody",
            "editorSubject",
            "editorTo",
            "editorCc",
            "editorBcc",
            "replyInstruction",
        ):
            control = find(window, name)
            background = control.property("background").property("color")
            text_ratios.append(assert_loaded_contrast(control, background))
            text_ratios.append(
                assert_loaded_contrast(control, background, color_property="placeholderTextColor")
            )
            text_ratios.append(
                contrast_ratio(
                    control.property("selectedTextColor"), control.property("selectionColor")
                )
            )
            control_ratios.append(
                contrast_ratio(QQmlProperty.read(control, "background.border.color"), background)
            )
        assert min(text_ratios) >= 4.5 and min(control_ratios) >= 3
        window.grabWindow().save("/tmp/email-reader-redesign-editor-880.png")
        window.setProperty("viewMode", "search")
        wait()
        assert editor.property("active") and not editor.isVisible()
        assert body.property("text") == "My original wording"
        QMetaObject.invokeMethod(editor, "requestClose")
        wait()
        confirm = find(window, "confirmCloseDraft")
        assert confirm.property("visible")
        assert 0 <= confirm.property("x") <= window.width() - confirm.property("width")
        assert 0 <= confirm.property("y") <= window.height() - confirm.property("height")
        activate(find(window, "cancelCloseDraft"))
        window.setProperty("viewMode", "mail")
        wait()
        controller.beginDraft("reply")
        assert body.property("text") == "My original wording", (
            "Begin draft replaced unsaved compose"
        )
        assert find(window, "editorCc").property("text") == "maya@example.net, "
        activate(choices[1])
        assert body.property("text") == "Warm suggested reply"
        QMetaObject.invokeMethod(editor, "requestClose")
        wait()
        activate(find(window, "discardDraft"))
        assert not editor.property("active") and not controller.editorDraft
        controller.beginDraft("reply")
        await_ready(lambda: bool(controller.editorDraft), "Reply editor did not open")
        body.setProperty("text", "An unsaved reply")
        assert (
            find(window, "messageCard-new").y() < editor.y() < find(window, "messageCard-old").y()
        ), "Reply must sit directly below the latest message"
        controller.selectThread(other_thread["id"])
        await_ready(lambda: controller.selectedThread.get("read_state") == "ready", "Alternate thread did not open")
        assert not editor.isVisible() and editor.property("active")
        QMetaObject.invokeMethod(editor, "open")
        await_ready(lambda: controller.selectedThread.get("id") == main_thread["id"] and controller.selectedThread.get("read_state") == "ready", "Draft source did not reopen")
        assert controller.selectedThread["id"] == main_thread["id"]
        assert window.property("selectedThreadId") == main_thread["id"]
        assert editor.isVisible() and body.property("text") == "An unsaved reply"
        QMetaObject.invokeMethod(editor, "requestClose")
        wait()
        activate(find(window, "discardDraft"))
        print(
            f"PASS: inline editor retention, To/Cc/Bcc, tabbed choices, dirty guards; contrast {min(text_ratios):.2f}:1 text / {min(control_ratios):.2f}:1 controls"
        )

        source_files = [
            dict(row)
            for row in context.database.query(
                "SELECT a.*,m.id AS source_message_id,m.thread_id,m.sent_at FROM attachments a "
                "JOIN messages m ON m.id=a.message_id WHERE a.filename LIKE 'Insurance document%' ORDER BY a.filename"
            )
        ]
        cards = [
            {
                "id": row["id"],
                "filename": row["filename"],
                "message_id": row["source_message_id"],
                "thread_id": row["thread_id"],
                "email_date": row["sent_at"],
                "available": True,
                "account_email": "owner@example.com",
                "provider_link": "https://example.net/message",
            }
            for row in source_files
        ]
        cards[0]["document_dates"] = [
            {
                "label": "Effective date",
                "value": "September 12, 2026",
                "source_id": source_files[0]["id"],
                "locator": "Page 1",
                "app_link": "emailreader://source/" + source_files[0]["id"],
            }
        ]
        cards[0]["duplicate_sources"] = [
            {
                "id": source_files[3]["id"],
                "filename": "Identical original.txt",
                "app_link": "emailreader://source/" + source_files[3]["id"],
                "cited": True,
            }
        ]
        citations = [
            {
                "source_id": row["id"],
                "source_type": "attachment",
                "message_id": row["source_message_id"],
                "title": row["filename"],
                "account_email": "owner@example.com",
                "sender": "maya@example.net",
                "sent_at": row["sent_at"],
                "locator": "Page 1",
                "excerpt": "Fictional insurance details.",
                "app_link": "emailreader://source/" + row["id"],
                "provider_link": "https://example.net/message",
            }
            for row in source_files[:3]
        ]
        words = (
            "The saved documents contain the fictional coverage information requested. " * 20
        ).split()[:119]
        answer_text = " ".join(words) + f" [SOURCE:{source_files[0]['id']}]"
        answer = {
            "role": "assistant",
            "text": answer_text,
            "citations": citations,
            "file_cards": cards[:3],
            "attachments": cards[:3],
            "related_attachments": [],
            "related_citations": [],
            "insufficient_evidence": False,
        }
        controller.startSearch("")
        session = controller._search_session()
        session["messages"].extend(
            [{"role": "user", "text": "Find my insurance documents."}, answer]
        )
        session["title"] = "Find my insurance documents."
        controller.chatChanged.emit()
        window.setWidth(1366)
        window.setHeight(900)
        window.setProperty("viewMode", "search")
        wait(200)
        search = find(window, "mailSearchWorkspace")
        results = find(window, "searchResultsScroll")
        sources = find(window, "searchSources")
        assert sources.isVisible() and sources.property("text") == "Sources (3)"
        assert results.property("contentItem").property("contentY") == 0
        for name in (
            "searchQuestionTitle",
            "searchAnswer",
            "searchSources",
            *["attachmentCard-" + row["id"] for row in source_files[:3]],
        ):
            item = find(search, name)
            assert item.isVisible()
            within(item, results)
        html = find(search, "searchAnswer").property("text")
        assert "emailreader://source/" + source_files[0]["id"] in html
        assert ">[1]</a>" in html
        assert source_files[0]["id"] not in BeautifulSoup(html, "html.parser").get_text()
        window.grabWindow().save("/tmp/email-reader-redesign-search-1366.png")
        for width, height in ((880, 620), (1100, 720), (1440, 900)):
            window.setWidth(width)
            window.setHeight(height)
            wait(100)
            for name in (
                "searchBack",
                "searchHistory",
                "newSearch",
                "chatQuestion",
                "searchSubmit",
            ):
                within(find(search, name), search)
            window.grabWindow().save(f"/tmp/email-reader-redesign-search-{width}.png")
        window.setWidth(1366)
        window.setHeight(900)
        wait()
        activate(find(search, "fileDetails-" + source_files[0]["id"]))
        assert find(search, "documentDate-" + source_files[0]["id"] + "-Effective date").isVisible()
        assert find(search, "duplicateSource-" + source_files[3]["id"]).isVisible()
        assert find(search, "fileSource-" + source_files[0]["id"]).isVisible()
        activate(sources)
        source_labels = [
            str(item.property("text") or "")
            for item in walk(search)
            if hasattr(item, "isVisible") and item.isVisible()
        ]
        assert any(
            "maya@example.net" in text and "owner@example.com" in text for text in source_labels
        )
        assert any("Page 1" in text for text in source_labels)
        selected_before = controller.selectedThread["id"]
        activate(find(search, "fileSource-" + source_files[0]["id"]))
        await_ready(
            lambda: controller.sourcePreview.get("read_state") == "ready",
            "Source preview did not load",
        )
        assert controller.sourcePreview["message_id"] == source_files[0]["source_message_id"]
        assert controller.selectedThread["id"] == selected_before, (
            "Source preview replaced inbox selection"
        )
        await_ready(
            lambda: bool(find(search, "searchSourceBody").property("documentHtml")),
            "Source body missing",
        )
        for width, height in ((880, 620), (1100, 720), (1440, 900)):
            window.setWidth(width)
            window.setHeight(height)
            wait(120)
            within(find(search, "closeSearchSource"), search)
            within(find(search, "chatQuestion"), search)
            within(find(search, "searchSourcePreview"), search)
        activate(find(search, "closeSearchSource"))
        assert not controller.sourcePreview
        window.setWidth(1366)
        window.setHeight(900)
        for row in source_files:
            target = Path(directory) / ("export-" + row["filename"])
            controller.exportAttachment(row["id"], QUrl.fromLocalFile(str(target)).toString())
            await_ready(target.exists, "Attachment export did not finish")
            assert target.read_bytes() == historical_bytes[row["filename"]]
            assert target.stat().st_mode & 0o077 == 0, "Export is not private"
        answer["file_cards"] = [
            *cards,
            {
                **cards[3],
                "id": "possible",
                "filename": "Earlier possible quote.txt",
                "possible_match": True,
                "available": False,
            },
        ]
        answer["related_citations"] = [
            {
                "title": "Earlier discussion",
                "excerpt": "Previous quote at <a href='https://example.net/insurance/long/path'>https://example.net/insurance/long/path</a>.",
                "app_link": citations[0]["app_link"],
            }
        ]
        answer["insufficient_evidence"] = True
        controller.chatChanged.emit()
        wait()
        assert find(search, "chatEvidenceWarning").isVisible()
        activate(find(search, "moreConfirmedResults"))
        assert find(search, "attachmentCard-" + source_files[3]["id"]).isVisible()
        activate(find(search, "morePossibleSources"))
        assert find(search, "attachmentCard-possible").isVisible()
        assert 'href="https://example.net/insurance/long/path"' in find(
            search, "relatedCitationExcerpt"
        ).property("text")
        answer["text"] = answer_text + "\n\n" + ("Additional supported fictional detail. " * 200)
        controller.chatChanged.emit()
        wait()
        activate(find(search, "showMoreAnswer"))
        results.property("contentItem").setProperty("contentY", 180)
        scroll_before = results.property("contentItem").property("contentY")
        controller.openSearchSource(citations[0]["app_link"])
        await_ready(
            lambda: controller.sourcePreview.get("read_state") == "ready",
            "Long-answer source did not load",
        )
        wait(100)
        activate(find(search, "closeSearchSource"))
        wait(100)
        assert abs(results.property("contentItem").property("contentY") - scroll_before) <= 2, (
            "Preview close lost answer scroll"
        )
        follow_up = find(search, "chatQuestion")
        follow_up.setProperty("text", "Which documents are current?")
        activate(find(search, "searchSubmit"))
        await_ready(lambda: not controller.busy, "Fictional follow-up did not finish")
        previous = context.assistant.answer.call_args.args[2]
        assert previous[0]["text"] == "Find my insurance documents."
        assert controller.chatMessages[-1]["role"] == "assistant"
        old_session = controller.selectedSearchId
        activate(find(search, "newSearch"))
        assert not controller.chatMessages and controller.selectedSearchId != old_session
        controller.selectSearch(old_session)
        wait()
        assert controller.chatMessages[-1]["role"] == "assistant"
        controller.filterAccount("a")
        wait()
        assert controller.selectedAccount == "a" and not controller.chatMessages
        controller.filterAccount("")
        wait()
        assert controller.selectedSearchId == old_session and controller.chatMessages
        controller._worker_jobs["fixture-ai"] = {"lane": "ai", "label": "Fixture cancellation"}
        controller._set_busy(True)
        wait()
        assert find(search, "searchCancel").isVisible()
        activate(find(search, "searchCancel"))
        context.openai.cancel.assert_called_once()
        controller._worker_jobs.pop("fixture-ai")
        controller._set_busy(False)
        follow_up.setProperty("text", "Keep this unsent question")
        activate(find(search, "searchBack"))
        assert window.property("viewMode") == "mail"
        assert follow_up.property("text") == "Keep this unsent question"
        # A real provider worker waits for a delayed fictional page, then the
        # actual sync coordinator ingests it. No live provider or network is used.
        provider_guard = controller._provider
        entered, release = threading.Event(), threading.Event()
        cleanup.callback(release.set)
        fixture_provider = Mock()
        fixture_provider.draft_message_ids_are_immutable.return_value = False
        def delayed_page(*args, **kwargs):
            entered.set()
            assert release.wait(15), "Fictional provider was not released"
            return SyncPage(messages=[MessagePayload(
                provider_id=f"background-{index}", provider_thread_id=f"background-{index}",
                subject=f"Background fixture {index}", body_text="Fictional concurrent import.",
                labels=["INBOX"],
            ) for index in range(25)], next_cursor="fictional-current")
        fixture_provider.initial_sync.side_effect = delayed_page
        fixture_provider.incremental_sync.side_effect = delayed_page
        fixture_provider.recent_head.side_effect = delayed_page
        fixture_provider.create_draft.return_value = DraftReceipt(provider_draft_id="fictional-reviewed-draft")
        controller._provider = Mock(return_value=fixture_provider)
        controller.syncAll()
        await_ready(lambda: entered.is_set() or not controller.syncBusy, "Fictional provider did not start")
        assert entered.is_set(), "Fictional provider did not start: " + controller.status
        assert controller.syncBusy and not controller.aiBusy
        assert find(window, "pauseResumeSync").isVisible()
        find(window, "globalSearch").setProperty("text", "Fictional project notes")
        wait(50)
        assert find(window, "offlineKeywordAction").property("enabled")
        controller.search("Fictional project notes")
        await_ready(lambda: controller._keyword_query == "Fictional project notes", "Offline keyword search did not finish")
        assert controller.threads
        controller.selectThreadAsync(controller.threads[0]["id"])
        await_ready(lambda: controller.selectedThread.get("read_state") == "ready", "Offline result did not open during import")
        follow_up.setProperty("text", "Fictional project notes")
        assert find(search, "searchSubmit").isEnabled()
        answers_before = context.assistant.answer.call_count
        answer_release = threading.Event()
        cleanup.callback(answer_release.set)
        def delayed_answer(*args):
            assert answer_release.wait(10), "Fictional AI was not released"
            return Answer(text="A fictional answer grounded in this fixture."), []
        context.assistant.answer.side_effect = delayed_answer
        window.setProperty("viewMode", "search")
        controller.ask("Fictional project notes")
        await_ready(lambda: bool(controller.searchPreview.get("citations") or controller.searchPreview.get("file_cards")),
                    "Local candidates waited behind the fictional AI response")
        assert controller.aiBusy
        assert find(search, "localPreviewLabel").property("visible")
        assert "not yet checked" in find(search, "localPreviewLabel").property("text")
        answer_release.set()
        await_ready(lambda: context.assistant.answer.call_count > answers_before and not controller.aiBusy,
                    "Smart search waited behind provider work")
        context.assistant.answer.side_effect = None
        assert not controller.searchPreview
        controller.beginDraft("new")
        await_ready(lambda: bool(controller.editorDraft), "Compose waited behind import")
        find(window, "editorTo").setProperty("text", "maya@example.net")
        find(window, "editorSubject").setProperty("text", "Fictional saved draft")
        find(window, "manualDraftBody").setProperty("text", "Preserve this fictional draft.")
        assert find(window, "createReplyOptions").isEnabled()
        activate(find(window, "reviewedDraftSave"))
        assert controller.draftSaving and not find(window, "reviewedDraftSave").isEnabled()
        assert find(window, "manualDraftBody").property("readOnly")
        fixture_provider.create_draft.assert_not_called()
        release.set()
        await_ready(lambda: not controller.syncBusy and not controller.draftSaving, "Fictional import/save did not finish", timeout=10000)
        assert fixture_provider.create_draft.call_count == 1
        assert context.database.scalar("SELECT COUNT(*) FROM messages WHERE provider_id LIKE 'background-%'") == 25
        assert context.database.scalar("SELECT COUNT(*) FROM draft_records WHERE provider_draft_id='fictional-reviewed-draft'") == 1
        controller._provider = provider_guard
        controller.timer.stop()
        controller.filterMailbox("inbox")
        controller.selectThread(main_thread["id"])
        await_ready(lambda: controller.selectedThread.get("read_state") == "ready", "Inbox source did not return")
        controller._provider.assert_not_called()
        print(
            "PASS: 120-word answer + three files + Sources fit; source preview, details, possible matches, exact exports, history, follow-ups, cancellation and offline search"
        )

        # The full-resolution preview is app-local. Exercise every control
        # without opening the original in another app.
        controller.showAttachmentPreview(
            context.repository.db.scalar("SELECT id FROM attachments WHERE provider_id='logo'")
        )
        wait(250)
        preview_dialog = find(window, "imagePreviewDialog")
        preview_image = find(window, "imagePreviewImage")
        preview_zoom = find(window, "imagePreviewZoom")
        preview_close = find(window, "imagePreviewClose")
        assert preview_dialog.property("visible")
        assert preview_image.property("source").toString().startswith("image://mail/")
        assert preview_zoom.property("text") == "100%"
        click(window, preview_zoom)
        assert preview_zoom.property("text") == "Fit"
        click(window, preview_zoom)
        assert preview_zoom.property("text") == "100%"
        click(window, preview_close)
        assert not preview_dialog.property("visible")

        # Render a real WebEngine document at the three acceptance pane
        # widths. A deliberately wide table may scroll inside its wrapper,
        # while the page and embedded image must stay within the pane.
        width_window = QQuickWindow()
        width_window.setHeight(360)
        component = QQmlComponent(
            engine,
            QUrl.fromLocalFile(str(qml_entrypoint().with_name("MailHtmlView.qml"))),
        )
        assert not component.isError(), [error.toString() for error in component.errors()]
        width_view = component.create()
        assert width_view is not None
        width_view.setParentItem(width_window.contentItem())
        width_view.setProperty(
            "documentHtml",
            email_document(
                '<img alt="Wide logo" src="data:image/png;base64,'
                + b64encode(logo_bytes).decode("ascii")
                + '"><table><tr><td style="width:1400px">Wide table cell</td>'
                "<td>Second cell</td></tr></table>"
            ),
        )
        web_views = [
            child
            for child in width_view.children()
            if "WebEngine" in child.metaObject().className()
        ]
        assert len(web_views) == 1
        width_web = web_views[0]
        for pane_width in (320, 600, 1000):
            width_window.setWidth(pane_width)
            width_view.setWidth(pane_width)
            width_window.show()
            wait(350)
            page_width = width_web.property("contentsSize").width()
            assert page_width <= pane_width + 2, (
                f"Whole message overflowed {pane_width}px pane: {page_width}px"
            )
            width_image = width_window.grabWindow()
            blue_pixels = [
                (x, y)
                for y in range(width_image.height())
                for x in range(width_image.width())
                if width_image.pixelColor(x, y).name() == "#174ea6"
            ]
            assert blue_pixels, f"Embedded image missing at {pane_width}px"
            measured_width = max(x for x, _ in blue_pixels) - min(x for x, _ in blue_pixels) + 1
            measured_height = max(y for _, y in blue_pixels) - min(y for _, y in blue_pixels) + 1
            assert abs((measured_width / measured_height) / 6 - 1) <= 0.01, (
                f"Image aspect ratio changed by more than one percent at {pane_width}px"
            )
            width_image.save(f"/tmp/email-reader-pane-{pane_width}.png")
        if os.environ.get("EMAIL_READER_CHECK_STABILITY") == "1":
            width_view.setProperty("documentHtml", email_document("<p>Stable fictional email.</p>"))
            wait(1800)
            settled_height = width_view.property("documentHeight")
            stable_loop = QEventLoop()
            QTimer.singleShot(30_000, stable_loop.quit)
            stable_loop.exec()
            height_drift = abs(width_view.property("documentHeight") - settled_height)
            assert height_drift <= 2, f"Unchanged email grew {height_drift}px in 30 seconds"
            print(f"PASS: 30-second unchanged-email height drift {height_drift}px")
        width_view.setVisible(False)
        scrolling_component = QQmlComponent(engine)
        scrolling_component.setData(
            b"""import QtQuick
import QtQuick.Controls
ScrollView {
    id: scroll
    width: 600; height: 360; contentWidth: availableWidth; contentHeight: contents.height
    property string mailHtml: ""
    Column {
        id: contents
        width: scroll.availableWidth
        MailHtmlView { id: mail; objectName: "longMailView"; width: contents.width; documentHtml: scroll.mailHtml; scrollTarget: scroll.contentItem }
        Rectangle { width: contents.width; height: 400; color: "white" }
    }
}""",
            QUrl.fromLocalFile(str(qml_entrypoint().with_name("ScrollingProbe.qml"))),
        )
        assert not scrolling_component.isError(), [
            error.toString() for error in scrolling_component.errors()
        ]
        scrolling_probe = scrolling_component.create()
        assert scrolling_probe is not None
        scrolling_probe.setParentItem(width_window.contentItem())
        scrolling_probe.setProperty(
            "mailHtml", email_document("<p>Long fictional email paragraph.</p>" * 800)
        )
        width_window.setWidth(600)
        wait(1800)
        long_view = find(scrolling_probe, "longMailView")
        assert long_view.property("documentHeight") > 10000
        assert long_view.height() <= scrolling_probe.height()
        web_view = next(
            child for child in long_view.children() if "WebEngine" in child.metaObject().className()
        )
        for _ in range(10):
            QCoreApplication.sendEvent(
                width_window,
                QWheelEvent(
                    QPointF(250, 200),
                    QPointF(250, 200),
                    QPoint(),
                    QPoint(0, -120),
                    Qt.NoButton,
                    Qt.NoModifier,
                    Qt.ScrollUpdate,
                    False,
                ),
            )
        wait(800)
        assert web_view.property("scrollPosition").y() > 0, (
            "Wheel scrolling inside long mail stalled"
        )
        for _ in range(400):
            QCoreApplication.sendEvent(
                width_window,
                QWheelEvent(
                    QPointF(250, 200),
                    QPointF(250, 200),
                    QPoint(),
                    QPoint(0, -120),
                    Qt.NoButton,
                    Qt.NoModifier,
                    Qt.ScrollUpdate,
                    False,
                ),
            )
        wait(1200)
        assert web_view.property("scrollPosition").y() >= (
            long_view.property("documentHeight") - long_view.height() - 30
        ), "The final paragraph is not reachable"
        assert scrolling_probe.property("contentItem").property("contentY") > 0, (
            "Scrolling past the end of an email must reach attachment cards"
        )
        scrolling_probe.deleteLater()
        width_window.close()
        width_view.deleteLater()
        print(
            "PASS: 320/600/1000px containment, inline image pixels/aspect, "
            "image preview Fit/100%/Close, "
            "quotes, local read timing, To/CC/BCC suggestions, draft preservation, "
            "candidate selection, cancel/discard, search result navigation, "
            "long-mail wheel scrolling through the final paragraph and footer"
        )
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
