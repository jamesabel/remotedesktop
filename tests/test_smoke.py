import socket

import remotedesktop
from remotedesktop.client import ClientWindow, DiscoveryPanel
from remotedesktop.discovery import ServerInfo, discover_servers
from remotedesktop.server import ServerWindow
from remotedesktop.viewer import ViewerWidget

from test_discovery import LOOPBACK, free_udp_port


def test_version() -> None:
    assert remotedesktop.__version__


def test_client_window_hosts_viewer(qapp) -> None:
    window = ClientWindow()
    assert isinstance(window.centralWidget(), ViewerWidget)
    assert isinstance(window.discovery_panel, DiscoveryPanel)


def test_discovery_panel_lists_servers(qapp) -> None:
    panel = DiscoveryPanel()
    server = ServerInfo(name="testbox", host="192.168.1.7", port=12345)
    panel._show_results([server])
    assert panel.server_list.count() == 1
    assert "testbox" in panel.server_list.item(0).text()


def test_server_window_is_discoverable(qapp) -> None:
    port = free_udp_port()
    window = ServerWindow(discovery_port=port)
    try:
        servers = discover_servers(
            timeout=2.0, discovery_port=port, broadcast_hosts=(LOOPBACK,)
        )
        assert [s.name for s in servers] == [socket.gethostname()]
    finally:
        window.close()
