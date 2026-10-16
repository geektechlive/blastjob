"""Round 2, R8: redaction is the Formatter's job alone. A filter that rewrote
`record.msg` broke `%s` templates it matched and mutated records shared with other
handlers."""

from __future__ import annotations

import logging

from tests.test_e_logging import SECRET, _written, disk_log  # noqa: F401


def test_a_template_that_looks_like_a_credential_still_formats_and_is_redacted(disk_log):  # noqa: F811
    logging.getLogger("blastjob.test").warning("GET /x?token=%s done", SECRET)

    out = _written(disk_log)

    assert SECRET not in out
    assert "token=[REDACTED]" in out
    assert "Logging error" not in out


def test_other_handlers_get_the_record_unmodified(disk_log):  # noqa: F811
    seen: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record)

    capture = Capture()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        logging.getLogger("blastjob.test").warning("GET /x?token=%s done", SECRET)
    finally:
        root.removeHandler(capture)

    assert seen[0].msg == "GET /x?token=%s done"
    assert seen[0].args == (SECRET,)


def test_no_redacting_filter_remains():
    from blastjob import logging as applog

    assert not hasattr(applog, "_RedactingFilter")
