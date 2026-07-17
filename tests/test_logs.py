import logging

from remotedesktop import logs


def test_init_logging_writes_formatted_records(tmp_path):
    path = logs.init_logging("client", directory=tmp_path)
    root = logging.getLogger("remotedesktop")
    try:
        assert path == tmp_path / "client.log"
        logging.getLogger("remotedesktop.sharing").debug("hello from the test")
        for handler in root.handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
        assert "hello from the test" in text
        assert "remotedesktop.sharing" in text
        assert "DEBUG" in text
    finally:
        # Detach the file handler so other tests (and later init calls)
        # don't keep writing into this tmp_path file.
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
