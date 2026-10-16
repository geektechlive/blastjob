"""E7: redaction happens in the Formatter, so it sees what actually reaches disk
(non-string args, exception messages, tracebacks), not just str args."""

from __future__ import annotations

import logging
import logging.handlers

import pytest

from blastjob import logging as applog

SECRET = "sup3rs3cretvalue"


class _UrlLike:
    """An httpx.URL-style object: not a str, but it formats to one."""

    def __str__(self) -> str:
        return f"https://api.example.org/search?app_key={SECRET}&what=vp"


@pytest.fixture
def disk_log(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "_log_path", lambda: tmp_path / "blastjob.log")
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    applog.setup()
    try:
        yield tmp_path / "blastjob.log"
    finally:
        for h in list(root.handlers):
            h.close()
        root.handlers = saved


def _written(path) -> str:
    for h in logging.getLogger().handlers:
        h.flush()
    return path.read_text(encoding="utf-8")


def test_non_string_args_are_redacted_on_disk(disk_log):
    logging.getLogger("blastjob.test").warning("fetching %s", _UrlLike())
    out = _written(disk_log)
    assert SECRET not in out
    assert "app_key=[REDACTED]" in out
    assert "what=vp" in out


def test_exception_tracebacks_are_redacted_on_disk(disk_log):
    log = logging.getLogger("blastjob.test")
    try:
        raise RuntimeError(f"GET https://api.example.org/x?api_key={SECRET} failed")
    except RuntimeError:
        log.exception("boom")
    out = _written(disk_log)
    assert SECRET not in out
    assert "api_key=[REDACTED]" in out
    assert "Traceback" in out  # the traceback itself is still there


def test_the_installed_handler_uses_the_redacting_formatter(disk_log):
    handlers = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert isinstance(handlers[0].formatter, applog._RedactingFormatter)


def test_a_record_shared_with_other_handlers_is_not_mutated_by_the_formatter():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "u=%s", (_UrlLike(),), None)
    out = applog._RedactingFormatter("%(message)s").format(record)
    assert SECRET not in out
    assert SECRET in record.getMessage()  # other handlers still see the original


def test_unhandled_exception_hook_redacts_stderr(disk_log, capsys):
    import sys

    try:
        raise RuntimeError(f"GET https://api.example.org/x?token={SECRET}")
    except RuntimeError as e:
        sys.excepthook(type(e), e, e.__traceback__)
    err = capsys.readouterr().err
    assert SECRET not in err
    assert "RuntimeError" in err
