from PySide6.QtWidgets import QLabel

from remotedesktop import __version__
from remotedesktop.about import AboutTab


def tab_text(tab: AboutTab) -> str:
    return "\n".join(label.text() for label in tab.findChildren(QLabel))


def test_about_tab_shows_pypi_metadata(qapp):
    tab = AboutTab()
    text = tab_text(tab)
    assert f"Remote Desktop {__version__}" in text
    assert "MIT" in text
    assert "James Abel" in text
    assert "3.14" in text
    assert "https://github.com/jamesabel/remotedesktop" in text
    assert "https://pypi.org/project/remotedesktop/" in text
    assert "screen, keyboard, mouse, and clipboard" in text


def test_both_windows_have_an_about_tab(qapp, credentials, tmp_path):
    from test_client_window import make_window as make_client_window
    from test_server_window import make_window as make_server_window

    server_window = make_server_window(credentials, tmp_path)
    client_window = make_client_window(tmp_path)
    try:
        for window in (server_window, client_window):
            tabs = window.centralWidget()
            labels = [tabs.tabText(i) for i in range(tabs.count())]
            assert labels[-1] == "About"
    finally:
        client_window.close()
        server_window.close()
