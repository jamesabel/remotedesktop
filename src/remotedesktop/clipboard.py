"""Clipboard synchronization.

Wraps the local QClipboard: emits `changed` with a serializable payload when
the user copies something locally, and `apply()` writes a payload received
from the peer into the local clipboard.

Echo prevention is by content signature, not just a guard flag: applying a
payload records its signature, and any clipboard-changed notification whose
content matches the last signature is ignored. The signature hashes the
canonical pixels of images (not their PNG encoding), so a value that makes a
round trip through the OS clipboard and back does not loop. This matters
because on Windows `dataChanged` fires asynchronously, after a simple guard
flag would already have been cleared.
"""

import base64
import hashlib

from PySide6.QtCore import QBuffer, QMimeData, QObject, Signal
from PySide6.QtGui import QClipboard, QGuiApplication, QImage


def _image_hash(image: QImage) -> str | None:
    if image is None or image.isNull():
        return None
    canonical = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return hashlib.sha1(bytes(canonical.constBits())).hexdigest()


class ClipboardSync(QObject):
    """Bridges the local clipboard to the network. See module docstring."""

    changed = Signal(dict)

    def __init__(
        self, clipboard: QClipboard | None = None, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._clipboard = clipboard or QGuiApplication.clipboard()
        self._applying = False
        self._last_signature: tuple[str | None, str | None] | None = None
        self._clipboard.dataChanged.connect(self._on_data_changed)

    def _on_data_changed(self) -> None:
        if self._applying:
            return
        text = self._clipboard.text() or None
        image = self._clipboard.image()
        image_hash = _image_hash(image)
        signature = (text, image_hash)
        if signature == self._last_signature or (text is None and image_hash is None):
            return
        self._last_signature = signature
        payload: dict = {}
        if text is not None:
            payload["text"] = text
        if image_hash is not None:
            buffer = QBuffer()
            buffer.open(QBuffer.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            payload["image_png"] = base64.b64encode(bytes(buffer.data())).decode()
        self.changed.emit(payload)

    def apply(self, payload: dict) -> None:
        text = payload.get("text")
        text = text if isinstance(text, str) else None
        image = None
        encoded = payload.get("image_png")
        if isinstance(encoded, str):
            try:
                image = QImage.fromData(base64.b64decode(encoded), "PNG")
            except (ValueError, TypeError):
                image = None
        if image is not None and image.isNull():
            image = None
        signature = (text, _image_hash(image))
        if signature == self._last_signature or signature == (None, None):
            return
        self._last_signature = signature
        # One QMimeData carrying both representations, so a payload with text
        # and an image keeps both halves on the receiving clipboard.
        mime = QMimeData()
        if text is not None:
            mime.setText(text)
        if image is not None:
            mime.setImageData(image)
        self._applying = True
        try:
            self._clipboard.setMimeData(mime)
        finally:
            self._applying = False
