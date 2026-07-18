"""DXGI desktop-duplication capture and its grabWindow fallback in ShareServer.

The real-hardware test skips wherever duplication is unavailable (headless
CI runners, RDP sessions); the ShareServer wiring is tested with fakes so
no real duplication is ever created there.
"""

import pytest
from PySide6.QtGui import QGuiApplication, QImage

from remotedesktop import dxgi

from test_sharing import make_server


def test_real_desktop_duplication_matches_screen_size(qapp):
    duplication = dxgi.DesktopDuplication.create()
    if duplication is None:
        pytest.skip("DXGI desktop duplication unavailable in this session")
    try:
        image = duplication.grab()
        if image is None:
            pytest.skip("duplication lost before the first frame")
        screen = QGuiApplication.primaryScreen()
        expected = screen.size() * screen.devicePixelRatio()
        assert (image.width(), image.height()) == (
            round(expected.width()),
            round(expected.height()),
        )
        # A second grab either reports "unchanged" (same object), delivers a
        # new frame, or the duplication was lost — never anything else.
        again = duplication.grab()
        assert again is None or isinstance(again, QImage)
    finally:
        duplication.close()


class FakeDuplication:
    def __init__(self, images):
        self.images = list(images)
        self.closed = False

    def grab(self):
        return self.images.pop(0) if self.images else None

    def close(self):
        self.closed = True


def test_capture_uses_dxgi_and_repeats_unchanged_frames(qapp, credentials, tmp_path, monkeypatch):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    frame = QImage(4, 4, QImage.Format.Format_RGB32)
    fake = FakeDuplication([frame, frame])
    monkeypatch.setattr(dxgi.DesktopDuplication, "create", staticmethod(lambda: fake))
    try:
        assert server._capture() is frame
        assert server._capture() is frame  # unchanged screen: same object again
    finally:
        server.close()
    assert fake.closed  # server.close() releases the duplication


def test_capture_dxgi_backs_off_after_loss(qapp, credentials, tmp_path, monkeypatch):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    fake = FakeDuplication([])  # first grab returns None: lost immediately
    created = []
    monkeypatch.setattr(
        dxgi.DesktopDuplication,
        "create",
        staticmethod(lambda: created.append(True) or fake),
    )
    try:
        assert server._capture_dxgi() is None  # created, then lost -> closed
        assert fake.closed
        assert server._capture_dxgi() is None  # within the retry cooldown...
        assert len(created) == 1  # ...so no re-create attempt yet
    finally:
        server.close()
