"""Inter-frame compression for the screen stream.

Consecutive captures are compared in horizontal bands (BAND_HEIGHT rows,
adjacent changed bands merged); only changed bands are encoded and shipped
in a delta payload, which the client patches onto its previous frame. Bands
keep the comparison and the crop cheap: a band is a contiguous byte range of
the image, so diffing is one C memcmp per band (~3.5 ms for a 4K frame; the
pure-Python memoryview compare it falls back to is ~12x slower).

Delta bands and keyframes are PNG — lossless, so the client's canvas is
pixel-identical to the capture and patches never accumulate artifacts.
Encoding is the expensive step (~13 ms for a small change, ~110 ms for a
full 4K keyframe), so `FrameEncoder` runs it on a worker thread and hands
the bytes back to the GUI thread through a queued signal.

Delta payload wire format: 4-byte big-endian header length, a JSON header
{"w", "h", "bands": [{"y", "h", "len"}, ...]}, then the bands' PNG bytes
concatenated in order.
"""

import ctypes
import ctypes.util
import json
import logging
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from PySide6.QtCore import QBuffer, QObject, Signal
from PySide6.QtGui import QImage, QPainter

_log = logging.getLogger(__name__)

BAND_HEIGHT = 64
# PNG is lossless at any "quality" — the setting only picks the zlib effort.
# 80 encodes a 4K frame ~35% faster than the default for ~20% more bytes;
# time matters more than size on a LAN, and 80 is close to PNG's floor
# (zero compression is no faster, just 17x bigger).
PNG_QUALITY = 80
_HEADER_LEN = struct.Struct(">I")


def encode_image(image: QImage, image_format: str = "PNG", quality: int = -1) -> bytes:
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, image_format, quality)  # ty: ignore[no-matching-overload]
    return bytes(buffer.data())  # ty: ignore[invalid-argument-type]


class _PyBuffer(ctypes.Structure):
    """CPython's Py_buffer — to borrow the address of a read-only
    memoryview (ctypes' from_buffer insists on a writable one)."""

    _fields_ = [
        ("buf", ctypes.c_void_p),
        ("obj", ctypes.c_void_p),
        ("len", ctypes.c_ssize_t),
        ("itemsize", ctypes.c_ssize_t),
        ("readonly", ctypes.c_int),
        ("ndim", ctypes.c_int),
        ("format", ctypes.c_char_p),
        ("shape", ctypes.c_void_p),
        ("strides", ctypes.c_void_p),
        ("suboffsets", ctypes.c_void_p),
        ("internal", ctypes.c_void_p),
    ]


_get_buffer = ctypes.pythonapi.PyObject_GetBuffer
_get_buffer.argtypes = (ctypes.py_object, ctypes.POINTER(_PyBuffer), ctypes.c_int)
_get_buffer.restype = ctypes.c_int
_release_buffer = ctypes.pythonapi.PyBuffer_Release
_release_buffer.argtypes = (ctypes.POINTER(_PyBuffer),)
_release_buffer.restype = None


class _BufferAddress:
    """Context manager yielding the address of a memoryview's bytes."""

    def __init__(self, view: bytes | bytearray | memoryview) -> None:
        self._view = view
        self._buffer = _PyBuffer()

    def __enter__(self) -> int:
        _get_buffer(self._view, ctypes.byref(self._buffer), 0)  # raises on failure
        return self._buffer.buf or 0

    def __exit__(self, *_exc_info: object) -> None:
        _release_buffer(ctypes.byref(self._buffer))


def _load_memcmp() -> Callable[[int, int, int], int] | None:
    try:
        if sys.platform == "win32":
            lib = ctypes.cdll.msvcrt
        else:
            name = ctypes.util.find_library("c")
            if name is None:
                return None
            lib = ctypes.CDLL(name)
        fn = lib.memcmp
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_int
    fn.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
    return fn


_memcmp = _load_memcmp()


def _scan_bands(height: int, differs: Callable[[int, int], bool]) -> list[tuple[int, int]]:
    bands: list[tuple[int, int]] = []
    y = 0
    while y < height:
        h = min(BAND_HEIGHT, height - y)
        if differs(y, h):
            if bands and bands[-1][0] + bands[-1][1] == y:
                bands[-1] = (bands[-1][0], bands[-1][1] + h)
            else:
                bands.append((y, h))
        y += h
    return bands


def changed_bands(previous: QImage, current: QImage) -> list[tuple[int, int]] | None:
    """Bands of `current` that differ from `previous`, as (y, height) pairs.

    Adjacent changed bands are merged. Returns None when the images are not
    comparable (different size or format) — the caller must fall back to a
    full frame.
    """
    if previous.size() != current.size() or previous.format() != current.format():
        return None
    prev_bits = previous.constBits()
    cur_bits = current.constBits()
    stride = current.bytesPerLine()
    height = current.height()
    if _memcmp is not None:
        memcmp = _memcmp
        with _BufferAddress(prev_bits) as prev_addr, _BufferAddress(cur_bits) as cur_addr:
            return _scan_bands(
                height,
                lambda y, h: memcmp(prev_addr + y * stride, cur_addr + y * stride, h * stride)
                != 0,
            )
    return _changed_bands_python(prev_bits, cur_bits, stride, height)


def _changed_bands_python(
    prev_bits: bytes | bytearray | memoryview,
    cur_bits: bytes | bytearray | memoryview,
    stride: int,
    height: int,
) -> list[tuple[int, int]]:
    """The portable fallback (and the reference the memcmp path is tested against)."""
    return _scan_bands(
        height,
        lambda y, h: prev_bits[y * stride : (y + h) * stride]
        != cur_bits[y * stride : (y + h) * stride],
    )


def merge_bands(
    first: Iterable[tuple[int, int]], second: Iterable[tuple[int, int]], height: int
) -> list[tuple[int, int]]:
    """The union of two band lists (as `changed_bands` produces them),
    normalized: sorted, adjacent bands merged, clipped to `height`."""
    rows: set[int] = set()
    for bands in (first, second):
        for y, h in bands:
            rows.update(range(y // BAND_HEIGHT, (y + h + BAND_HEIGHT - 1) // BAND_HEIGHT))
    merged: list[tuple[int, int]] = []
    for row in sorted(rows):
        y = row * BAND_HEIGHT
        h = min(BAND_HEIGHT, height - y)
        if h <= 0:
            continue
        if merged and merged[-1][0] + merged[-1][1] == y:
            merged[-1] = (merged[-1][0], merged[-1][1] + h)
        else:
            merged.append((y, h))
    return merged


def encode_delta(image: QImage, bands: list[tuple[int, int]]) -> bytes:
    entries = []
    blobs = []
    for y, h in bands:
        png = encode_image(image.copy(0, y, image.width(), h), "PNG", PNG_QUALITY)
        entries.append({"y": y, "h": h, "len": len(png)})
        blobs.append(png)
    header = json.dumps({"w": image.width(), "h": image.height(), "bands": entries}).encode()
    return _HEADER_LEN.pack(len(header)) + header + b"".join(blobs)


def apply_delta(canvas: QImage, payload: bytes) -> QImage | None:
    """Patch a delta payload onto `canvas`, in place.

    Returns the patched image (`canvas` itself), or None — with the canvas
    untouched — if the payload is malformed or was produced for a different
    frame size (the caller should then wait for the next keyframe). Every
    band is decoded before any is painted, so a payload that goes bad
    halfway never leaves a half-patched canvas. Painting in place skips a
    full-frame copy per delta; QImage's copy-on-write still protects any
    other holder of the image.
    """
    try:
        (header_len,) = _HEADER_LEN.unpack_from(payload)
        header = json.loads(payload[_HEADER_LEN.size : _HEADER_LEN.size + header_len].decode())
        bands = [(int(b["y"]), int(b["h"]), int(b["len"])) for b in header["bands"]]
        size_ok = int(header["w"]) == canvas.width() and int(header["h"]) == canvas.height()
    except (struct.error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not size_ok:
        return None
    decoded: list[tuple[int, QImage]] = []
    offset = _HEADER_LEN.size + header_len
    for y, h, length in bands:
        band = QImage.fromData(payload[offset : offset + length])
        offset += length
        if band.isNull() or band.height() != h:
            return None
        decoded.append((y, band))
    painter = QPainter(canvas)
    try:
        for y, band in decoded:
            painter.drawImage(0, y, band)
    finally:
        painter.end()
    return canvas


@dataclass
class EncodeResult:
    """What a FrameEncoder job produced. `deltas` maps each requested band
    tuple to its payload. `failed` means nothing usable was produced (the
    encode raised) — the sender should start its viewers over from a
    keyframe."""

    image: QImage | None
    keyframe: bytes | None
    deltas: dict[tuple[tuple[int, int], ...], bytes]
    seconds: float
    failed: bool = False
    thread: str = ""


class FrameEncoder(QObject):
    """PNG-encodes one frame at a time on a worker thread.

    The one place the sharing pipeline leaves the GUI thread: encoding a
    changed 4K frame costs tens to a hundred milliseconds, which on the GUI
    thread would delay every incoming input event by that much. PySide6
    releases the GIL inside QImage.save, so the worker really runs in
    parallel. `submit` accepts one job while idle (`busy`); the result comes
    back on the GUI thread through `encoded`. Nothing is shared with the
    worker but the (immutable, implicitly shared) QImage.
    """

    encoded = Signal(object)  # EncodeResult, delivered on the GUI thread
    _finished = Signal(object)  # worker -> GUI hop

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frame-encoder")
        self._busy = False
        self._closed = False
        self._finished.connect(self._on_finished)

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(
        self, image: QImage, *, keyframe: bool, deltas: Iterable[tuple[tuple[int, int], ...]]
    ) -> None:
        if self._busy:
            raise RuntimeError("an encode is already in flight")
        self._busy = True
        future = self._executor.submit(self._encode, image, keyframe, tuple(deltas))
        future.add_done_callback(self._on_done)

    @staticmethod
    def _encode(
        image: QImage, keyframe: bool, deltas: tuple[tuple[tuple[int, int], ...], ...]
    ) -> EncodeResult:
        started = time.perf_counter()
        png = encode_image(image, "PNG", PNG_QUALITY) if keyframe else None
        payloads = {bands: encode_delta(image, list(bands)) for bands in deltas}
        return EncodeResult(
            image,
            png,
            payloads,
            time.perf_counter() - started,
            thread=threading.current_thread().name,
        )

    def _on_done(self, future: Future) -> None:  # runs on the worker thread
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            _log.error("Frame encode failed: %r", error)
            result = EncodeResult(None, None, {}, 0.0, failed=True)
        else:
            result = future.result()
        if not self._closed:
            self._finished.emit(result)

    def _on_finished(self, result: EncodeResult) -> None:  # GUI thread
        self._busy = False
        if not self._closed:
            self.encoded.emit(result)

    def close(self) -> None:
        """Stop accepting jobs and wait for any in-flight encode, so no
        result is ever delivered to a torn-down owner."""
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
