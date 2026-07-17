import json
import struct

from PySide6.QtGui import QColor, QImage, QPainter

from remotedesktop import frames


def solid(width, height, color):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def fill_rect(image, x, y, w, h, color):
    painter = QPainter(image)
    painter.fillRect(x, y, w, h, QColor(color))
    painter.end()


def test_identical_images_have_no_changed_bands(qapp):
    a = solid(64, 200, "red")
    assert frames.changed_bands(a, solid(64, 200, "red")) == []


def test_a_change_is_localized_to_its_band(qapp):
    a = solid(64, 200, "red")
    b = solid(64, 200, "red")
    fill_rect(b, 10, 70, 4, 4, "blue")  # inside the second 64-row band
    assert frames.changed_bands(a, b) == [(64, 64)]


def test_adjacent_changed_bands_merge(qapp):
    a = solid(64, 200, "red")
    b = solid(64, 200, "red")
    fill_rect(b, 0, 10, 4, 4, "blue")
    fill_rect(b, 0, 70, 4, 4, "blue")
    assert frames.changed_bands(a, b) == [(0, 128)]


def test_incomparable_images_return_none(qapp):
    assert frames.changed_bands(solid(64, 200, "red"), solid(64, 100, "red")) is None


def test_delta_round_trip_is_lossless(qapp):
    base = solid(64, 128, "red")
    modified = solid(64, 128, "red")
    fill_rect(modified, 0, 64, 64, 64, "blue")
    bands = frames.changed_bands(base, modified)
    assert bands is not None and bands == [(64, 64)]
    payload = frames.encode_delta(modified, bands)
    patched = frames.apply_delta(base, payload)
    assert patched is not None
    # PNG is lossless, so the patched canvas is pixel-identical.
    assert patched == modified
    assert patched.pixelColor(5, 5).name() == "#ff0000"
    assert patched.pixelColor(5, 100).name() == "#0000ff"


def test_apply_delta_patches_in_place(qapp):
    base = solid(64, 128, "red")
    modified = solid(64, 128, "red")
    fill_rect(modified, 0, 64, 64, 64, "blue")
    payload = frames.encode_delta(modified, [(64, 64)])
    patched = frames.apply_delta(base, payload)
    assert patched is base  # no full-canvas copy per delta


def test_bad_band_leaves_the_canvas_untouched(qapp):
    base = solid(64, 128, "red")
    # A valid first band followed by an undecodable second band. Nothing may
    # be painted, or the canvas would be half-patched with no keyframe
    # recovery covering the first band.
    good_band = frames.encode_image(solid(64, 64, "blue"))
    header = json.dumps(
        {"w": 64, "h": 128, "bands": [
            {"y": 0, "h": 64, "len": len(good_band)},
            {"y": 64, "h": 64, "len": 10},
        ]}
    ).encode()
    payload = struct.pack(">I", len(header)) + header + good_band + b"\0" * 10
    assert frames.apply_delta(base, payload) is None
    assert base == solid(64, 128, "red")


def test_delta_for_a_different_size_is_rejected(qapp):
    modified = solid(64, 128, "blue")
    payload = frames.encode_delta(modified, [(0, 64)])
    assert frames.apply_delta(solid(32, 32, "red"), payload) is None


def test_malformed_delta_is_rejected(qapp):
    assert frames.apply_delta(solid(32, 32, "red"), b"garbage") is None
    assert frames.apply_delta(solid(32, 32, "red"), b"") is None
