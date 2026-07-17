"""Inter-frame compression for the screen stream.

Consecutive captures are compared in horizontal bands (BAND_HEIGHT rows,
adjacent changed bands merged); only changed bands are encoded and shipped
in a delta payload, which the client patches onto its previous frame. Bands
keep the comparison and the crop cheap: a band is a contiguous byte range of
the image, so diffing is a C-speed memoryview compare.

Delta bands and keyframes for delta-capable clients are PNG — lossless, so
the client's canvas is pixel-identical to the capture and patches never
accumulate artifacts. (Legacy clients still get JPEG full frames: 0.5.0
force-decodes frames as JPEG, so they must never see PNG.)

Delta payload wire format: 4-byte big-endian header length, a JSON header
{"w", "h", "bands": [{"y", "h", "len"}, ...]}, then the bands' PNG bytes
concatenated in order.
"""

import json
import struct

from PySide6.QtCore import QBuffer
from PySide6.QtGui import QImage, QPainter

BAND_HEIGHT = 64
_HEADER_LEN = struct.Struct(">I")


def encode_image(image: QImage, image_format: str = "PNG", quality: int = -1) -> bytes:
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, image_format, quality)  # ty: ignore[no-matching-overload]
    return bytes(buffer.data())  # ty: ignore[invalid-argument-type]


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
    bands: list[tuple[int, int]] = []
    y = 0
    while y < height:
        h = min(BAND_HEIGHT, height - y)
        if prev_bits[y * stride : (y + h) * stride] != cur_bits[y * stride : (y + h) * stride]:
            if bands and bands[-1][0] + bands[-1][1] == y:
                bands[-1] = (bands[-1][0], bands[-1][1] + h)
            else:
                bands.append((y, h))
        y += h
    return bands


def encode_delta(image: QImage, bands: list[tuple[int, int]]) -> bytes:
    entries = []
    blobs = []
    for y, h in bands:
        png = encode_image(image.copy(0, y, image.width(), h))
        entries.append({"y": y, "h": h, "len": len(png)})
        blobs.append(png)
    header = json.dumps({"w": image.width(), "h": image.height(), "bands": entries}).encode()
    return _HEADER_LEN.pack(len(header)) + header + b"".join(blobs)


def apply_delta(canvas: QImage, payload: bytes) -> QImage | None:
    """Patch a delta payload onto a copy of `canvas`.

    Returns the patched image, or None if the payload is malformed or was
    produced for a different frame size (the caller should then wait for
    the next keyframe).
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
    image = canvas.copy()
    painter = QPainter(image)
    offset = _HEADER_LEN.size + header_len
    try:
        for y, h, length in bands:
            band = QImage.fromData(payload[offset : offset + length])
            offset += length
            if band.isNull() or band.height() != h:
                return None
            painter.drawImage(0, y, band)
    finally:
        painter.end()
    return image
