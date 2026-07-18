"""The "About" tab, shared by the client and server windows.

Shows the same metadata the PyPI page shows — summary, author, license,
Python requirement, links — read from the installed package's metadata via
importlib.metadata, with static fallbacks so the tab still renders in odd
environments (the version always comes from `remotedesktop.__version__`,
the single source hatchling packages)."""

from importlib import metadata

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from remotedesktop import __version__, icon

_HOMEPAGE = "https://github.com/jamesabel/remotedesktop"
_PYPI = "https://pypi.org/project/remotedesktop/"
_FALLBACKS = {
    "Summary": (
        "Remote desktop client/server for Windows computers on the same LAN, "
        "with autodiscovery. Provides screen, keyboard, mouse, and clipboard "
        "sharing without RDP or Microsoft authentication."
    ),
    "Author-email": "James Abel <j@abel.co>",
    "License-Expression": "MIT",
    "Requires-Python": ">=3.14",
}


def _field(name: str) -> str:
    try:
        value = metadata.metadata("remotedesktop").get(name)
    except metadata.PackageNotFoundError:
        value = None
    return str(value) if value else _FALLBACKS.get(name, "")


class AboutTab(QWidget):
    """Package metadata as a rich-text page with clickable links."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        author = _field("Author-email").replace("<", "&lt;").replace(">", "&gt;")
        body = QLabel(
            f"""
            <h2 style="margin-bottom:2px">Remote Desktop {__version__}</h2>
            <p>{_field("Summary")}</p>
            <table cellspacing="4">
              <tr><td><b>Author</b></td><td>{author}</td></tr>
              <tr><td><b>License</b></td><td>{_field("License-Expression")}</td></tr>
              <tr><td><b>Python</b></td><td>{_field("Requires-Python")}</td></tr>
              <tr><td><b>Package</b></td><td>remotedesktop</td></tr>
            </table>
            <p>
              <a href="{_HOMEPAGE}">GitHub</a> &nbsp;·&nbsp;
              <a href="{_PYPI}">PyPI</a>
            </p>
            """
        )
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setOpenExternalLinks(True)
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        logo = QLabel()
        logo.setPixmap(icon.app_icon("client").pixmap(48, 48))
        layout = QVBoxLayout(self)
        layout.addWidget(logo)
        layout.addWidget(body)
        layout.addStretch(1)
