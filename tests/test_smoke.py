import socket

import remotedesktop
from remotedesktop.client import DiscoveryPanel
from remotedesktop.discovery import ServerInfo, discover_servers
from remotedesktop.viewer import ViewerWidget

from test_discovery import LOOPBACK, free_udp_port
from test_main_window import make_window


def test_version() -> None:
    assert remotedesktop.__version__


def test_main_window_hosts_discovery_panel(qapp, tmp_path) -> None:
    window = make_window(tmp_path)
    try:
        assert isinstance(window.discovery_panel, DiscoveryPanel)
        assert window._sessions == []  # a viewer tab appears per server connection
        assert not window.sharing_tab.serving  # sharing is opt-in
    finally:
        window.close()


def test_discovery_panel_lists_servers(qapp) -> None:
    panel = DiscoveryPanel()
    server = ServerInfo(name="testbox", host="192.168.1.7", port=12345)
    panel._show_results([server])
    assert panel.server_list.count() == 1
    assert "testbox" in panel.server_list.item(0).text()


def test_sharing_window_is_discoverable(qapp, credentials, tmp_path) -> None:
    port = free_udp_port()
    window = make_window(tmp_path, credentials, serving=True, discovery_port=port)
    try:
        servers = discover_servers(
            timeout=2.0, discovery_port=port, broadcast_hosts=(LOOPBACK,)
        )
        assert [s.name for s in servers] == [socket.gethostname()]
        assert servers[0].port == window.sharing_tab.share_server.port
    finally:
        window.close()


def test_viewer_widget_shows_and_clears_frames(qapp) -> None:
    from PySide6.QtGui import QImage

    viewer = ViewerWidget()
    assert not viewer.has_frame
    viewer.show_frame(QImage(8, 8, QImage.Format.Format_RGB32))
    assert viewer.has_frame
    viewer.clear("gone")
    assert not viewer.has_frame


def test_viewer_scales_frames_in_device_pixels(qapp) -> None:
    from PySide6.QtGui import QImage

    viewer = ViewerWidget()
    viewer.resize(320, 240)
    viewer.show_frame(QImage(640, 480, QImage.Format.Format_RGB32))  # same 4:3 aspect
    viewer.grab()  # forces a paint pass without showing a window
    assert viewer._scaled is not None
    dpr = viewer.devicePixelRatioF()
    # Scaled to physical pixels and stamped with the ratio, so painting it
    # never resamples a second time.
    assert viewer._scaled.devicePixelRatio() == dpr
    assert viewer._scaled.size().width() == round(320 * dpr)
    assert viewer._scaled.size().height() == round(240 * dpr)
