import remotedesktop
from remotedesktop.client import ClientWindow
from remotedesktop.server import ServerWindow
from remotedesktop.viewer import ViewerWidget


def test_version() -> None:
    assert remotedesktop.__version__


def test_client_window_hosts_viewer(qapp) -> None:
    window = ClientWindow()
    assert isinstance(window.centralWidget(), ViewerWidget)


def test_server_window(qapp) -> None:
    window = ServerWindow()
    assert window.windowTitle() == "Remote Desktop Server"
