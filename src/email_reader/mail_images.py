"""Decrypt verified mail images in memory; never create plaintext display files."""

from collections import OrderedDict
from collections.abc import MutableMapping
from threading import RLock

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtQuick import QQuickImageProvider

from email_reader.image_types import is_mail_image

IMAGE_CACHE_BYTES = 160 * 1024 * 1024
MAX_IMAGE_REGISTRATIONS = 8192


class _ImageCache(MutableMapping):
    """Dictionary-compatible view into one shared, byte-bounded image LRU."""

    def __init__(self, owner, namespace):
        self.owner, self.namespace = owner, namespace

    def __getitem__(self, key):
        with self.owner._cache_lock:
            entry = (self.namespace, key)
            image = self.owner._images[entry]
            self.owner._images.move_to_end(entry)
            return QImage(image)

    def __setitem__(self, key, image):
        with self.owner._cache_lock:
            entry = (self.namespace, key)
            previous = self.owner._images.pop(entry, None)
            if previous is not None:
                self.owner._image_bytes -= previous.sizeInBytes()
            image_bytes = image.sizeInBytes()
            if image_bytes > self.owner.cache_limit_bytes or image.isNull():
                return
            self.owner._images[entry] = QImage(image)
            self.owner._image_bytes += image_bytes
            while self.owner._image_bytes > self.owner.cache_limit_bytes:
                _, removed = self.owner._images.popitem(last=False)
                self.owner._image_bytes -= removed.sizeInBytes()

    def __delitem__(self, key):
        with self.owner._cache_lock:
            image = self.owner._images.pop((self.namespace, key))
            self.owner._image_bytes -= image.sizeInBytes()

    def __iter__(self):
        with self.owner._cache_lock:
            return iter([key for namespace, key in self.owner._images if namespace == self.namespace])

    def __len__(self):
        with self.owner._cache_lock:
            return sum(namespace == self.namespace for namespace, _ in self.owner._images)

    def clear(self):
        with self.owner._cache_lock:
            for entry in [entry for entry in self.owner._images if entry[0] == self.namespace]:
                self.owner._image_bytes -= self.owner._images.pop(entry).sizeInBytes()


class MailImageProvider(QQuickImageProvider):
    def __init__(self, blobs, *, cache_limit_bytes=IMAGE_CACHE_BYTES):
        super().__init__(QQuickImageProvider.Image)
        self.blobs = blobs
        self._cache_lock = RLock()
        self.cache_limit_bytes = max(0, int(cache_limit_bytes))
        self._images: OrderedDict[tuple[str, str], QImage] = OrderedDict()
        self._image_bytes = 0
        self.digests: OrderedDict[str, str] = OrderedDict()
        self.remote = _ImageCache(self, "remote")
        self.decoded = _ImageCache(self, "decoded")
        QImageReader.setAllocationLimit(64)

    def register(self, attachments: list[dict]) -> None:
        with self._cache_lock:
            for item in attachments:
                if item.get("content_digest") and is_mail_image(
                    item.get("mime_type", ""), item.get("filename", "")
                ):
                    self.digests[item["id"]] = item["content_digest"]
                    self.digests.move_to_end(item["id"])
            while len(self.digests) > MAX_IMAGE_REGISTRATIONS:
                self.digests.popitem(last=False)

    @property
    def cached_bytes(self):
        with self._cache_lock:
            return self._image_bytes

    def requestImage(self, identifier, size, requestedSize):
        value = QImage()
        try:
            identifier, _, width_option = identifier.partition("?width=")
            width = min(560, max(1, int(width_option))) if width_option else 0
            with self._cache_lock:
                value = self.remote.get(identifier, QImage())
                digest = self.digests.get(identifier)
                if value.isNull() and digest:
                    value = self.decoded.get(digest, QImage())
            if value.isNull() and digest:
                value.loadFromData(self.blobs.get(digest))
                if not value.isNull():
                    self.decoded[digest] = value
            if not value.isNull():
                bounds = requestedSize if requestedSize.isValid() else QSize()
                if width:
                    bounds = QSize(width, 1000)
                    if requestedSize.isValid():
                        bounds = QSize(min(width, requestedSize.width()), min(1000, requestedSize.height()))
                if bounds.isValid() and (
                    value.width() > bounds.width() or value.height() > bounds.height()
                ):
                    value = value.scaled(bounds, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        except Exception:
            # Neither decrypted paths nor provider content enter Qt diagnostics.
            value = QImage()
        size.setWidth(value.width())
        size.setHeight(value.height())
        return value
