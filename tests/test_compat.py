from remotedesktop import __version__
from remotedesktop.compat import major_of, mismatch_warning


def test_major_of_parses_semver():
    assert major_of("0.18.0") == 0
    assert major_of("1.2.3") == 1
    assert major_of("10.0.0-rc.1") == 10
    assert major_of("") is None
    assert major_of("banana") is None
    assert major_of("1.2") is None  # not full semver


def test_same_major_or_unknown_produces_no_warning():
    assert mismatch_warning("1.2.3", "1.9.0", "server") is None
    assert mismatch_warning("0.18.0", "0.1.0", "client") is None
    assert mismatch_warning("1.0.0", "", "server") is None  # pre-0.19 peer
    assert mismatch_warning("", "1.0.0", "server") is None
    assert mismatch_warning(__version__, __version__, "server") is None


def test_major_mismatch_produces_a_strong_warning():
    warning = mismatch_warning("1.2.3", "2.0.0", "server")
    assert warning is not None
    assert "WARNING" in warning
    assert "1.2.3" in warning and "2.0.0" in warning
    assert "server" in warning
    assert "not guaranteed" in warning
    assert "still connect" in warning  # the user may attempt anyway
