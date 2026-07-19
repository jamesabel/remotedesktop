from pathlib import Path

import pytest
from PIL import Image

from remotedesktop.icon import _SIZES, app_icon


def test_icon_renders_at_all_sizes(qapp):
    icon = app_icon("app")
    assert not icon.isNull()
    for size in (16, 32, 256):
        pixmap = icon.pixmap(size)
        assert not pixmap.isNull()


def test_default_role_is_app(qapp):
    assert app_icon().pixmap(64).toImage() == app_icon("app").pixmap(64).toImage()


def test_unknown_role_is_rejected(qapp):
    with pytest.raises(KeyError):
        app_icon("toaster")


def test_checked_in_ico_has_all_sizes():
    # icon/remotedesktop.ico is build input for the pyship installer/launcher;
    # regenerate with tools/make_icon.py after changing the drawn icon.
    ico_path = Path(__file__).parent.parent / "icon" / "remotedesktop.ico"
    with Image.open(ico_path) as ico:
        assert {(s, s) for s in _SIZES} <= set(ico.info["sizes"])
