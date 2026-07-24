"""Clipboard synchronization.

Wraps the local QClipboard: emits `changed` with a serializable payload when
the user copies something locally, and `apply()` writes a payload received
from the peer into the local clipboard.

Three content kinds sync: text, images (as PNG), and **files**. A file copy
(Explorer's CF_HDROP, seen by Qt as local-file URLs) ships the file
*contents* — a path from another machine would be meaningless — capped at
`FILES_CAP_BYTES` per copy so a huge transfer cannot choke the connection
the screen stream shares; folders are skipped. `apply()` materializes
received files into a fresh batch folder under the app's data dir and puts
those paths on the clipboard as URLs, so pasting in Explorer works
normally. Only the newest batch is kept (older batch folders are purged
best-effort on each apply), so disk use stays bounded by one copy. When
files are present they travel alone — any text/image on the same clipboard
entry is ignored on both ends, keeping signatures consistent.

Echo prevention is by content signature, not just a guard flag: applying a
payload records its signature, and any clipboard-changed notification whose
content matches the last signature is ignored. The signature hashes the
canonical pixels of images (not their PNG encoding) and the *names +
contents* of files (not their paths, which differ after the temp-folder
round trip), so a value that makes a round trip through the OS clipboard
and back does not loop. This matters because on Windows `dataChanged` fires
asynchronously, after a simple guard flag would already have been cleared.
"""

import base64
import hashlib
import logging
import shutil
import uuid
from pathlib import Path

import humanize
import platformdirs
from PySide6.QtCore import QBuffer, QMimeData, QObject, QUrl, Signal
from PySide6.QtGui import QClipboard, QGuiApplication, QImage

_log = logging.getLogger("remotedesktop.clipboard")

# Total size of one synced file copy. Base64 inflates by a third and the
# whole batch travels as one JSON message on the connection the screen
# stream shares, so the cap keeps well under the 64 MB protocol limit and
# keeps the remote view's freeze during a transfer short.
FILES_CAP_BYTES = 32 * 1024 * 1024


def default_files_dir() -> Path:
    """Where received clipboard files are materialized."""
    return Path(platformdirs.user_data_dir("remotedesktop")) / "clipboard"


def describe_payload(payload: dict) -> str:
    """One human-readable phrase for a clipboard payload, e.g.
    "text (14 chars)" or "2 file(s) (1.3 MiB)" — content kinds and sizes,
    never the content itself (this goes to the Connection log)."""
    parts = []
    text = payload.get("text")
    if isinstance(text, str):
        parts.append(f"text ({len(text)} chars)")
    image = payload.get("image_png")
    if isinstance(image, str):
        size = humanize.naturalsize(len(image) * 3 // 4, binary=True)
        parts.append(f"an image ({size} PNG)")
    files = payload.get("files")
    if isinstance(files, list) and files:
        total = sum(
            len(item.get("data", "")) for item in files if isinstance(item, dict)
        )
        size = humanize.naturalsize(total * 3 // 4, binary=True)
        parts.append(f"{len(files)} file(s) ({size})")
    return " + ".join(parts) or "empty"


def _image_hash(image: QImage | None) -> str | None:
    if image is None or image.isNull():
        return None
    canonical = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return hashlib.sha1(bytes(canonical.constBits())).hexdigest()


def _files_hash(entries: list[tuple[str, bytes]]) -> str:
    """Order-independent hash of (name, contents) pairs — never paths, so
    the receiving side's temp copies produce the same signature."""
    digest = hashlib.sha1()
    for name, data in sorted(entries, key=lambda e: e[0]):
        digest.update(name.encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(hashlib.sha1(data).digest())
    return digest.hexdigest()


def _unique_name(name: str, used: set[str]) -> str:
    if name not in used:
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 2
    while f"{stem} ({counter}){suffix}" in used:
        counter += 1
    return f"{stem} ({counter}){suffix}"


class ClipboardSync(QObject):
    """Bridges the local clipboard to the network. See module docstring."""

    changed = Signal(dict)
    # Problems only (skipped folders, over-cap batches, disk errors) — the
    # transport's own status lines describe routine traffic, so a normal
    # copy never produces two Connection-log lines.
    status = Signal(str)

    def __init__(
        self,
        clipboard: QClipboard | None = None,
        *,
        files_dir: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._clipboard = clipboard or QGuiApplication.clipboard()
        self._files_dir = files_dir if files_dir is not None else default_files_dir()
        self._applying = False
        self._last_signature: tuple | None = None
        # The Preferences toggle: while disabled, local copies are not sent
        # (`changed` never emits) and peer payloads are not applied. Note
        # that after re-enabling, the last recorded signature may suppress
        # one re-copy of identical content — same class of behavior as the
        # normal echo prevention.
        self.enabled = True
        self._clipboard.dataChanged.connect(self._on_data_changed)

    def _read_files(self, mime: QMimeData | None) -> list[tuple[str, bytes]] | None:
        """The clipboard's local files as (name, contents) pairs, or None.

        None means "nothing to sync": no file URLs, only folders, unreadable
        files, or a batch over the size cap (each with a status message).
        """
        if mime is None or not mime.hasUrls():
            return None
        paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
        if not paths:
            return None
        folders = [p for p in paths if p.is_dir()]
        if folders:
            self.status.emit(
                f"Clipboard sync: {len(folders)} folder(s) skipped — only files sync"
            )
        files = [p for p in paths if p.is_file()]
        if not files:
            return None
        try:
            total = sum(p.stat().st_size for p in files)
        except OSError as error:
            self.status.emit(f"Clipboard sync: could not read copied files ({error})")
            return None
        if total > FILES_CAP_BYTES:
            self.status.emit(
                "Clipboard sync: files not synced — "
                f"{humanize.naturalsize(total, binary=True)} exceeds the "
                f"{humanize.naturalsize(FILES_CAP_BYTES, binary=True)} limit"
            )
            return None
        entries: list[tuple[str, bytes]] = []
        for path in files:
            try:
                entries.append((path.name, path.read_bytes()))
            except OSError as error:
                self.status.emit(f"Clipboard sync: skipped {path.name} ({error})")
        return entries or None

    def _on_data_changed(self) -> None:
        if self._applying or not self.enabled:
            return
        mime = self._clipboard.mimeData()
        has_local_files = mime is not None and any(
            url.isLocalFile() for url in mime.urls()
        )
        files = None
        if has_local_files:
            # Files travel alone; a file copy also exposes the path list in
            # the text slot (Qt/Explorer), which would paste as junk text on
            # the peer — so never fall back to text/image, even when the
            # batch itself cannot ship (over the cap, folders, unreadable).
            files = self._read_files(mime)
            if files is None:
                return
            signature = (None, None, _files_hash(files))
            text = None
            image_hash = None
        else:
            text = self._clipboard.text() or None
            image_hash = _image_hash(self._clipboard.image())
            signature = (text, image_hash, None)
        if signature == self._last_signature or signature == (None, None, None):
            return
        self._last_signature = signature
        payload: dict = {}
        if text is not None:
            payload["text"] = text
        if image_hash is not None:
            buffer = QBuffer()
            buffer.open(QBuffer.OpenModeFlag.WriteOnly)
            self._clipboard.image().save(buffer, "PNG")  # ty: ignore[no-matching-overload]
            payload["image_png"] = base64.b64encode(
                bytes(buffer.data())  # ty: ignore[invalid-argument-type]
            ).decode()
        if files is not None:
            payload["files"] = [
                {"name": name, "data": base64.b64encode(data).decode()}
                for name, data in files
            ]
        self.changed.emit(payload)

    def copy_image(self, image: QImage) -> None:
        """Place an app-generated image (a screen capture) on the local
        clipboard WITHOUT echoing it to peers: its signature is recorded
        first, so the asynchronous dataChanged this set fires is ignored —
        the same mechanism apply() uses. Sending a capture of the server's
        own screen back to it would be pure waste.

        Deliberately not gated on `enabled`: that preference governs
        syncing, and this is a local copy.
        """
        if image.isNull():
            return
        self._last_signature = (None, _image_hash(image), None)
        self._applying = True
        try:
            self._clipboard.setImage(image)
        finally:
            self._applying = False

    def _decode_files(self, payload: dict) -> list[tuple[str, bytes]]:
        """Sanitized (name, contents) pairs from a peer payload.

        Names are reduced to their final component so a malicious or
        confused peer cannot write outside the batch folder; malformed
        entries are skipped.
        """
        raw = payload.get("files")
        if not isinstance(raw, list):
            return []
        entries: list[tuple[str, bytes]] = []
        used: set[str] = set()
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            name = Path(item["name"]).name
            if not name or name in (".", ".."):
                continue
            try:
                data = base64.b64decode(item.get("data", ""), validate=True)
            except (ValueError, TypeError):
                continue
            name = _unique_name(name, used)
            used.add(name)
            entries.append((name, data))
        return entries

    def _write_files(self, entries: list[tuple[str, bytes]]) -> list[Path] | None:
        """Materialize a received batch on disk, purging older batches."""
        batch_dir = self._files_dir / uuid.uuid4().hex[:12]
        try:
            batch_dir.mkdir(parents=True, exist_ok=False)
            written = []
            for name, data in entries:
                target = batch_dir / name
                target.write_bytes(data)
                written.append(target)
        except OSError as error:
            self.status.emit(f"Clipboard sync: could not store received files ({error})")
            return None
        # Only the newest batch is referenced by the clipboard; older ones
        # are dead weight. Best-effort: a file locked by a paste in progress
        # just survives until the next batch.
        for stale in self._files_dir.iterdir():
            if stale.is_dir() and stale != batch_dir:
                shutil.rmtree(stale, ignore_errors=True)
        return written

    def apply(self, payload: dict) -> None:
        if not self.enabled:
            return  # peers may still send payloads; they are dropped locally
        files = self._decode_files(payload)
        if files:
            # Files travel alone (mirror of _on_data_changed).
            text = None
            image = None
            signature = (None, None, _files_hash(files))
        else:
            text = payload.get("text")
            text = text if isinstance(text, str) else None
            image = None
            encoded = payload.get("image_png")
            if isinstance(encoded, str):
                try:
                    image = QImage.fromData(base64.b64decode(encoded), "PNG")  # ty: ignore[invalid-argument-type]
                except (ValueError, TypeError):
                    image = None
            if image is not None and image.isNull():
                image = None
            signature = (text, _image_hash(image), None)
        if signature == self._last_signature or signature == (None, None, None):
            return
        written: list[Path] | None = None
        if files:
            written = self._write_files(files)
            if written is None:
                return  # disk trouble; leave the clipboard alone
        self._last_signature = signature
        # One QMimeData carrying both representations, so a payload with text
        # and an image keeps both halves on the receiving clipboard.
        mime = QMimeData()
        if text is not None:
            mime.setText(text)
        if image is not None:
            mime.setImageData(image)
        if written:
            mime.setUrls([QUrl.fromLocalFile(str(path)) for path in written])
        self._applying = True
        try:
            self._clipboard.setMimeData(mime)
        finally:
            self._applying = False
