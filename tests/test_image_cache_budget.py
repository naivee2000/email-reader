"""Image browsing must not grow a count-bounded but byte-unbounded cache."""

from types import SimpleNamespace

from PySide6.QtCore import QBuffer, QIODevice, QSize
from PySide6.QtGui import QImage

from email_reader.mail_images import MailImageProvider


def picture(width=16, height=16):
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill("#934735")
    return image


def test_decoded_and_remote_images_share_one_byte_budget():
    image = picture()
    image_bytes = image.sizeInBytes()
    provider = MailImageProvider(None, cache_limit_bytes=image_bytes * 2)
    provider.decoded["first"] = image
    provider.remote["second"] = image
    assert provider.cached_bytes == image_bytes * 2
    provider.decoded["first"]  # Make the original the most recently used.
    provider.remote["third"] = image
    assert "first" in provider.decoded
    assert "second" not in provider.remote
    assert "third" in provider.remote
    assert provider.cached_bytes == image_bytes * 2
    provider.decoded.clear()
    provider.remote.clear()
    assert provider.cached_bytes == 0


def test_cache_overwrite_and_oversized_image_do_not_exceed_budget():
    provider = MailImageProvider(None, cache_limit_bytes=1024)
    provider.remote["logo"] = picture()
    provider.remote["logo"] = picture(8, 8)
    assert provider.cached_bytes == 256
    provider.remote["large"] = picture(32, 32)
    assert "large" not in provider.remote
    assert provider.cached_bytes == 256


def test_evicted_original_is_decrypted_again_without_losing_its_registration():
    source = picture()
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert source.save(buffer, "PNG")
    calls = []

    def get(digest):
        calls.append(digest)
        return bytes(buffer.data())

    provider = MailImageProvider(SimpleNamespace(get=get), cache_limit_bytes=source.sizeInBytes())
    provider.register([{"id": "image", "mime_type": "image/png", "content_digest": "original"}])
    assert not provider.requestImage("image", QSize(), QSize()).isNull()
    provider.remote["new"] = source
    assert "original" not in provider.decoded
    assert not provider.requestImage("image", QSize(), QSize()).isNull()
    assert calls == ["original", "original"]


def test_image_registrations_are_bounded(monkeypatch):
    monkeypatch.setattr("email_reader.mail_images.MAX_IMAGE_REGISTRATIONS", 4)
    provider = MailImageProvider(None)
    for index in range(100):
        provider.register([{"id": str(index), "mime_type": "image/png", "content_digest": str(index)}])
    assert list(provider.digests) == ["96", "97", "98", "99"]

