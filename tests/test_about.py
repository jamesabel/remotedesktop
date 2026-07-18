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


def test_window_has_an_about_tab(qapp, tmp_path):
    from test_main_window import make_window

    window = make_window(tmp_path)
    try:
        tabs = window.centralWidget()
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        assert labels[-1] == "About"
    finally:
        window.close()
