import socket

import remotedesktop
from remotedesktop import db
from remotedesktop.client import ClientWindow, DiscoveryPanel
from remotedesktop.config import PairedClients
from remotedesktop.discovery import ServerInfo, discover_servers
from remotedesktop.server import ServerWindow
from remotedesktop.viewer import ViewerWidget

from test_discovery import LOOPBACK, free_udp_port


def test_version() -> None:
    assert remotedesktop.__version__


def test_client_window_hosts_viewer(qapp) -> None:
    window = ClientWindow()
    assert isinstance(window.viewer, ViewerWidget)
    assert isinstance(window.discovery_panel, DiscoveryPanel)


def test_discovery_panel_lists_servers(qapp) -> None:
    panel = DiscoveryPanel()
    server = ServerInfo(name="testbox", host="192.168.1.7", port=12345)
    panel._show_results([server])
    assert panel.server_list.count() == 1
    assert "testbox" in panel.server_list.item(0).text()


def test_server_window_is_discoverable(qapp, credentials, tmp_path) -> None:
    port = free_udp_port()
    window = ServerWindow(
        discovery_port=port,
        connect_port=0,
        paired=PairedClients(db.connect(tmp_path / "server.db")),
        credentials=credentials,
    )
    try:
        servers = discover_servers(
            timeout=2.0, discovery_port=port, broadcast_hosts=(LOOPBACK,)
        )
        assert [s.name for s in servers] == [socket.gethostname()]
        assert servers[0].port == window.share_server.port
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
