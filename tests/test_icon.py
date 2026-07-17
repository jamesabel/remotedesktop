import pytest

from remotedesktop.icon import app_icon


def test_icons_render_for_both_roles(qapp):
    client, server = app_icon("client"), app_icon("server")
    for icon in (client, server):
        assert not icon.isNull()
        for size in (16, 32, 256):
            pixmap = icon.pixmap(size)
            assert not pixmap.isNull()
    # The roles are distinguishable (different accent colors).
    assert client.pixmap(64).toImage() != server.pixmap(64).toImage()


def test_unknown_role_is_rejected(qapp):
    with pytest.raises(KeyError):
        app_icon("toaster")
