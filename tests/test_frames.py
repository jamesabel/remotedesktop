import json
import random
import struct
import time

from PySide6.QtCore import QEventLoop
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


def test_native_memcmp_is_in_use():
    # The ~12x faster path; a silent fallback to the Python compare would
    # put the 4K diff back over the frame budget.
    assert frames._memcmp is not None


def test_memcmp_path_matches_the_python_reference(qapp):
    rng = random.Random(1)
    base = solid(64, 1000, "red")  # 16 bands, the last one short (1000 = 15*64 + 40)
    for _trial in range(25):
        modified = base.copy()
        for _change in range(rng.randint(0, 5)):
            fill_rect(modified, rng.randrange(64), rng.randrange(1000), 1, 1, "blue")
        expected = frames._changed_bands_python(
            base.constBits(), modified.constBits(), modified.bytesPerLine(), modified.height()
        )
        assert frames.changed_bands(base, modified) == expected


def test_merge_bands_unions_and_normalizes(qapp):
    assert frames.merge_bands([], [(64, 64)], 200) == [(64, 64)]
    assert frames.merge_bands([(0, 64)], [(64, 64)], 200) == [(0, 128)]
    assert frames.merge_bands([(0, 128)], [(64, 64), (192, 8)], 200) == [(0, 128), (192, 8)]
    assert frames.merge_bands([(0, 128)], [(128, 64), (192, 8)], 200) == [(0, 200)]
    assert frames.merge_bands([(192, 8)], [(0, 64)], 200) == [(0, 64), (192, 8)]
    assert frames.merge_bands([(0, 64)], [(0, 64)], 200) == [(0, 64)]


def wait_for(qapp, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met"
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)


def test_frame_encoder_runs_off_the_gui_thread(qapp):
    encoder = frames.FrameEncoder()
    results = []
    encoder.encoded.connect(results.append)
    try:
        encoder.submit(solid(64, 128, "red"), keyframe=True, deltas=[((64, 64),)])
        assert encoder.busy
        wait_for(qapp, lambda: results)
    finally:
        encoder.close()
    (result,) = results
    assert not encoder.busy and not result.failed
    assert result.thread.startswith("frame-encoder")
    assert QImage.fromData(result.keyframe).pixelColor(5, 5).name() == "#ff0000"
    canvas = solid(64, 128, "blue")
    assert frames.apply_delta(canvas, result.deltas[((64, 64),)]) is canvas
    assert canvas.pixelColor(5, 100).name() == "#ff0000"
    assert canvas.pixelColor(5, 5).name() == "#0000ff"


def test_frame_encoder_reports_a_failed_encode(qapp, monkeypatch):
    def broken(image, image_format="PNG", quality=-1):
        raise MemoryError("simulated")

    monkeypatch.setattr(frames, "encode_image", broken)
    encoder = frames.FrameEncoder()
    results = []
    encoder.encoded.connect(results.append)
    try:
        encoder.submit(solid(64, 128, "red"), keyframe=True, deltas=[])
        wait_for(qapp, lambda: results)
    finally:
        encoder.close()
    assert results[0].failed and not encoder.busy
