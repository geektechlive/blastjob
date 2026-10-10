"""Unit tests for logging.py.

The redaction tests exist because of a measured defect: on 2026-09-16 the
Adzuna `app_key` was found in plaintext on 284 lines of a 48 MB
~/.blastjob/blastjob.log. Root was at DEBUG, httpx logs every request URL at
INFO, and the Adzuna adapter passes credentials as query parameters. These
tests pin all three halves of the fix.
"""

import logging
import logging.handlers

import pytest

from blastjob import logging as applog


def test_log_path_returns_string():
    path = applog.log_path()
    assert isinstance(path, str)
    assert "blastjob.log" in path


def test_log_exception_does_not_raise(caplog):
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError("test error")
        except ValueError as e:
            applog.log_exception("test", e)
    assert "test: test error" in caplog.text


# ----- redaction ------------------------------------------------------------


def test_redact_strips_a_query_parameter_credential():
    url = "https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=abc123&app_key=s3cr3tvalue&what=vp"
    out = applog.redact(url)
    assert "s3cr3tvalue" not in out
    assert "abc123" not in out
    assert "app_key=[REDACTED]" in out
    # Everything that is not a credential survives, or the log stops being useful.
    assert "api.adzuna.com" in out
    assert "what=vp" in out


def test_redact_strips_a_bearer_token():
    out = applog.redact("Authorization: Bearer jina_abcdefghijklmnop")
    assert "jina_abcdefghijklmnop" not in out
    assert "Bearer [REDACTED]" in out


def test_redact_leaves_ordinary_text_alone():
    text = "fetched 731 postings from workday:northwind in 4.2s"
    assert applog.redact(text) == text


def test_the_filter_redacts_the_message_template(caplog):
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: GET https://api.adzuna.com/search?app_key=leakedkey "200 OK"',
        args=(),
        exc_info=None,
    )
    assert applog._RedactingFilter().filter(record) is True
    assert "leakedkey" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_the_filter_redacts_lazy_args_not_just_the_template():
    """A secret usually arrives as an ARG, not in the template: httpx logs
    "HTTP Request: %s %s". Redacting the template alone would leave it in
    `record.args` for the handler to format back in."""
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: %s %s",
        args=("GET", "https://api.adzuna.com/search?app_key=leakedkey"),
        exc_info=None,
    )
    applog._RedactingFilter().filter(record)
    assert "leakedkey" not in record.getMessage()


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Point `setup()` at a temp file and restore root's handlers afterwards.

    Learned the hard way: an earlier version of these tests called `setup()`
    directly, which opened the real ~/.blastjob/blastjob.log and — because the
    new handler rotates above 10 MB — immediately rolled a 48 MB production log
    over to blastjob.log.1 mid-test-run. A unit test must never touch the
    owner's data directory.
    """
    monkeypatch.setattr(applog, "_log_path", lambda: tmp_path / "blastjob.log")
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    try:
        yield tmp_path / "blastjob.log"
    finally:
        for h in list(root.handlers):
            h.close()
        root.handlers = saved


def test_setup_pins_the_http_loggers_above_info(isolated_log):
    """httpx at INFO is what wrote the key to disk 284 times."""
    applog.setup()
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level >= logging.WARNING, name


def test_setup_installs_exactly_one_rotating_handler_when_called_twice(isolated_log):
    """`setup()` runs from both the web app and the daily job; a second handler
    would double every line and a plain FileHandler would grow unbounded."""
    applog.setup()
    applog.setup()
    handlers = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == applog.MAX_BYTES
    assert handlers[0].backupCount == applog.BACKUP_COUNT


def test_a_leaked_url_is_redacted_on_its_way_to_disk(isolated_log):
    """End to end: the record goes through the real handler and filter, and the
    file on disk must not contain the secret."""
    applog.setup()
    logging.getLogger("httpx").warning(
        "HTTP Request: GET https://api.adzuna.com/search?app_key=ondiskleak"
    )
    for h in logging.getLogger().handlers:
        h.flush()
    written = isolated_log.read_text(encoding="utf-8")
    assert "ondiskleak" not in written
    assert "[REDACTED]" in written
