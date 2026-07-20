import sys

import pytest

from remotedesktop.cursor_shape import SHAPE_NAMES, current_cursor_shape
from remotedesktop.viewer import _CURSOR_SHAPES


def test_every_wire_name_has_a_client_side_cursor():
    # The server's vocabulary and the viewer's map must stay in sync, or a
    # shape would silently degrade to the arrow.
    assert SHAPE_NAMES <= set(_CURSOR_SHAPES)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows cursor API")
def test_current_cursor_shape_reports_a_known_name():
    shape = current_cursor_shape()
    # None only when GetCursorInfo fails (e.g. no interactive desktop).
    if shape is not None:
        assert shape in SHAPE_NAMES
