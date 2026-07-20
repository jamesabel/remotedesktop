import sys

import pytest

from remotedesktop.session_lock import is_session_locked


@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop API")
def test_is_session_locked_reports_a_bool():
    # The answer depends on the session running the tests (an interactive
    # desktop says False; a service-like session may say True) — only the
    # contract is asserted, not the state.
    assert is_session_locked() in (True, False)


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows returns None")
def test_is_session_locked_is_none_off_windows():
    assert is_session_locked() is None
