"""Generate icon/remotedesktop.ico from the code-drawn application icon.

The app draws its icon at runtime (icon.app_icon), but the pyship freezer
needs a real .ico file for the launcher exe and the installer (pyship's
get_icon searches icon/<name>.ico). The .ico is build input only — it is
not packaged into the wheel. Re-run this script after changing icon.py and
commit the result; tests/test_icon.py checks the file stays multi-size.

Usage:  uv run python tools/make_icon.py [output_path]
        (default output_path: icon/remotedesktop.ico)
"""

import io
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer
from PySide6.QtGui import QGuiApplication

from remotedesktop.icon import _ACCENTS, _SIZES, _pixmap


def main() -> None:
    _app = QGuiApplication([])  # QPixmap rendering needs a GUI application
    default = Path(__file__).parent.parent / "icon" / "remotedesktop.ico"
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default

    images = []
    for size in _SIZES:
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.ReadWrite)
        _pixmap(size, _ACCENTS["app"]).save(buffer, "PNG")
        images.append(Image.open(io.BytesIO(buffer.data().data())))

    # Largest as the base frame, the rest appended, so every size is a real
    # render (Pillow only rescales the base for sizes with no matching frame).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    images[-1].save(
        out_path,
        format="ICO",
        append_images=images[:-1],
        sizes=[(s, s) for s in _SIZES],
    )
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, sizes {list(_SIZES)})")


if __name__ == "__main__":
    main()
