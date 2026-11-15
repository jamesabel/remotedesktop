"""The TLS backend is pinned to schannel, whatever OpenSSL DLLs are on PATH.

Every test and helper in this file is AI-generated (Claude Code).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtNetwork import QSslSocket

from remotedesktop import tls

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="schannel is Windows-only")

_OPENSSL_DLL = "libssl-3-x64.dll"
_GIT_OPENSSL_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "mingw64" / "bin"

# Run in a fresh process: the backend is process-global and this one has already chosen.
_REPORT_BACKENDS = """
from PySide6.QtNetwork import QSslSocket
from remotedesktop import tls
print(QSslSocket.activeBackend())
print(tls.select_backend())
print(QSslSocket.activeBackend())
"""


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_select_backend_activates_schannel():
    assert tls.select_backend() is True
    assert QSslSocket.activeBackend() == tls.BACKEND == "schannel"


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_select_backend_wins_over_openssl_on_path():
    """A fresh process with Git's OpenSSL DLLs on PATH still ends up on schannel."""
    if not (_GIT_OPENSSL_DIR / _OPENSSL_DLL).exists():
        pytest.skip(f"no {_OPENSSL_DLL} in {_GIT_OPENSSL_DIR}")
    env = dict(os.environ)
    env["PATH"] = f"{_GIT_OPENSSL_DIR}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        [sys.executable, "-c", _REPORT_BACKENDS], env=env, capture_output=True, text=True, timeout=60, check=True
    )
    default_backend, selected, active_backend = result.stdout.split()
    # The premise: with those DLLs findable, Qt would have picked openssl.
    assert default_backend == "openssl"
    assert selected == "True"
    assert active_backend == "schannel"
