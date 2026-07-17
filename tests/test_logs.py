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


def test_read_log_tail_returns_recent_content(tmp_path):
    (tmp_path / "server.log").write_text("early line\nrecent line\n", encoding="utf-8")
    text = logs.read_log_tail("server", directory=tmp_path)
    assert "early line" in text and "recent line" in text


def test_read_log_tail_keeps_only_the_end_of_a_big_log(tmp_path):
    (tmp_path / "client.log").write_bytes(b"OLD" * 1000 + b"NEWEST")
    text = logs.read_log_tail("client", directory=tmp_path, max_bytes=100)
    assert len(text) == 100
    assert text.endswith("NEWEST")


def test_read_log_tail_without_a_log_file_reports_instead_of_failing(tmp_path):
    text = logs.read_log_tail("server", directory=tmp_path)
    assert "no log available" in text
