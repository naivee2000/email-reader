import json
from types import SimpleNamespace

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from email_reader.contracts import AttachmentPayload, MessagePayload
from email_reader.diagnostics import audit_sources


def test_image_audit_distinguishes_one_pixel_original_without_leaking_content(repository):
    value = QImage(1, 1, QImage.Format_RGB32)
    value.fill("white")
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert value.save(buffer, "JPEG")
    repository.ingest_message("account-1", MessagePayload(provider_id="private-message-identifier",
        provider_thread_id="private-thread-identifier", subject="AUTO QUOTE ",
        body_text="private body marker", body_html='<p>private body marker<img src="cid:logo" /></p>',
        attachments=[AttachmentPayload(provider_id="image", filename="example.jpg", mime_type="image/jpeg",
            content_id="logo", content=bytes(buffer.data()))]))
    report = audit_sources(SimpleNamespace(repository=repository, database=repository.db))
    images = report["auto_quote_images"][0]["images"]
    assert images == [{"filename": "example.jpg", "available": True, "decoded": True,
                       "width": 1, "height": 1, "referenced_inline": True}]
    assert "private" not in json.dumps(report)
