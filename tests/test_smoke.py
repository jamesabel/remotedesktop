import remotedesktop
from remotedesktop import client, server


def test_version() -> None:
    assert remotedesktop.__version__


def test_entry_points_exist() -> None:
    assert callable(client.main)
    assert callable(server.main)
