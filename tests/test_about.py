from PySide6.QtWidgets import QLabel

from remotedesktop import __version__
from remotedesktop.about import AboutDialog, AboutPage


def page_text(page) -> str:
    return "\n".join(label.text() for label in page.findChildren(QLabel))


def test_about_page_shows_pypi_metadata(qapp):
    page = AboutPage()
    text = page_text(page)
    assert f"Remote Desktop {__version__}" in text
    assert "MIT" in text
    assert "James Abel" in text
    assert "3.14" in text
    assert "https://github.com/jamesabel/remotedesktop" in text
    assert "https://pypi.org/project/remotedesktop/" in text
    assert "screen, keyboard, mouse, and clipboard" in text


def test_help_menu_opens_the_about_dialog(qapp, tmp_path):
    from test_main_window import make_window

    window = make_window(tmp_path)
    try:
        # About is a Help-menu dialog, not a tab.
        tabs = window.centralWidget()
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        assert "About" not in labels
        assert not window._about_dialog.isVisible()
        window.about_action.trigger()
        assert window._about_dialog.isVisible()
        assert isinstance(window._about_dialog, AboutDialog)
        assert f"Remote Desktop {__version__}" in page_text(window._about_dialog)
        window._about_dialog.close()
        # Reuses the one instance: triggering again just re-shows it.
        window.about_action.trigger()
        assert window._about_dialog.isVisible()
    finally:
        window.close()
