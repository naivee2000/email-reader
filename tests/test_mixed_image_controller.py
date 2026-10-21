"""Fictional image-resource regression through the actual reader worker callbacks."""

from base64 import b64encode
from io import BytesIO
from threading import Event

from bs4 import BeautifulSoup
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from email_reader.context import ApplicationContext
from email_reader.contracts import Account, AttachmentPayload, MessagePayload, ProviderKind
from email_reader.controller import AppController


def wait_until(condition, signal):
    if condition():
        return
    loop = QEventLoop()

    def changed():
        if condition():
            loop.quit()

    signal.connect(changed)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    signal.disconnect(changed)
    assert condition()


def png(color):
    stream = BytesIO()
    Image.new("RGB", (8, 8), color).save(stream, format="PNG")
    return stream.getvalue()


def test_loading_remote_image_preserves_cid_in_all_display_variants(tmp_path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv("EMAIL_READER_DATA_DIR", str(tmp_path / "fictional"))
    monkeypatch.setattr(AppController, "_automatic_sync", lambda *_: None)
    context = ApplicationContext.create(testing=True)
    embedded, remote = png("red"), png("blue")
    remote_url = "https://images.example.test/fictional.png"
    try:
        context.repository.upsert_account(Account(id="images", provider=ProviderKind.GMAIL, email="owner@example.test"))
        payload = MessagePayload(
            provider_id="mixed", provider_thread_id="mixed", subject="Fictional pictures",
            body_text="A fictional signature and a requested picture.",
            body_html=f'<p>Fictional pictures</p><img src="cid:signature"><img src="{remote_url}">',
            attachments=[AttachmentPayload(provider_id="cid", filename="signature.png", mime_type="image/png",
                                           content=embedded, content_id="signature", is_inline=True)],
        )
        identifier = context.repository.ingest_message("images", payload, defer_extraction=True)
        controller = AppController(context)
        controller.timer.stop()
        wait_until(lambda: bool(controller.threads), controller.threadsChanged)
        thread_id = controller.threads[0]["id"]
        controller.selectThreadAsync(thread_id)
        wait_until(lambda: controller.selectedThread.get("read_state") == "ready", controller.selectedThreadChanged)
        before = controller.messageDocument(identifier, False, False, False)
        assert b64encode(embedded).decode() in before
        controller._image_jobs["fictional-job"] = {
            "message_id": identifier, "thread_id": thread_id, "targets": [remote_url], "cancel": Event(),
        }
        controller._image_succeeded("fictional-job", {"loaded": {remote_url: remote}, "cancelled": False})
        wait_until(lambda: controller.selectedThread["messages"][0].get("remote_image_state") == "loaded",
                   controller.selectedThreadChanged)
        def verify_variants(expected_embedded):
            for original, reading, highlighted in ((False, False, False), (True, False, False), (False, True, True)):
                def complete_document(original=original, reading=reading, highlighted=highlighted):
                    value = controller.messageDocument(identifier, original, reading, highlighted)
                    ready = not (original or reading or highlighted) or any(
                        key[0] == identifier and key[-3:] == (original, reading, highlighted) and cached == value
                        for key, cached in controller._document_cache.items()
                    )
                    return (ready and b64encode(remote).decode() in value
                            and b64encode(expected_embedded).decode() in value)
                wait_until(complete_document, controller.documentReady)
                document = controller.messageDocument(identifier, original, reading, highlighted)
                image_sources = [image.get("src", "") for image in BeautifulSoup(document, "html.parser").find_all("img")]
                assert any(b64encode(expected_embedded).decode() in source for source in image_sources)
                assert any(b64encode(remote).decode() in source for source in image_sources)

        verify_variants(embedded)
        assert context.repository.blobs.get(context.database.scalar("SELECT content_digest FROM attachments")) == embedded

        # Same message/body/CID, but a corrected original arrives on a later sync.
        # Neither a cached document nor a cached mixed image map may resurrect the old picture.
        replacement = png("green")
        revised = payload.model_copy(update={"attachments": [
            payload.attachments[0].model_copy(update={"content": replacement}),
        ]})
        old_fingerprint = controller.selectedThread["messages"][0]["content_fingerprint"]
        context.repository.ingest_message("images", revised, defer_extraction=True)
        expected_digest = context.database.scalar("SELECT content_digest FROM attachments")
        controller.selectThreadAsync(thread_id)
        wait_until(lambda: controller.selectedThread.get("read_state") == "ready"
                   and controller.selectedThread["messages"][0]["attachments"][0]["content_digest"] == expected_digest,
                   controller.selectedThreadChanged)
        assert controller.selectedThread["messages"][0]["content_fingerprint"] == old_fingerprint
        verify_variants(replacement)
        for value in controller._document_cache.values():
            if b64encode(replacement).decode() in value:
                assert b64encode(embedded).decode() not in value
    finally:
        context.close()
