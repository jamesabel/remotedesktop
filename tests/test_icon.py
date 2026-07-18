import pytest

from remotedesktop.icon import app_icon


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
