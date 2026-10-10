"""Persistent error log at <data dir>/blastjob.log (default ~/.blastjob).

Two things here are defences, not conveniences, and both were written after the
same live defect: on 2026-09-16 this file's `basicConfig(level=DEBUG)` on the
ROOT logger, plus httpx's habit of logging every request URL at INFO, plus
`discovery/adzuna.py` passing credentials as query parameters, had written the
Adzuna `app_key` into this log in plaintext 284 times across a 48 MB file.

So: third-party HTTP loggers are pinned above INFO, and every record is passed
through a redaction filter on its way to disk. The filter is the belt-and-braces
half -- the next adapter that puts a credential in a URL must not be able to
reopen this -- and the file is size-capped so it cannot grow unbounded again.
"""

import logging
import logging.handlers
import re
import sys
import traceback
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 3

# Query-parameter credentials (Adzuna's app_id/app_key today) and bearer
# tokens. Matched on the wire form, since that is how they leak: inside a URL
# that some library decided to log.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(app_key|app_id|api_key|apikey|key|token|password|secret)=[^&\s\"']+", re.I),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._\-+/=]{8,}", re.I), r"\1 [REDACTED]"),
)


def redact(text: str) -> str:
    """Strip credential-shaped substrings. Public so callers that build an
    error string for the DB or the browser can use the same rule -- a source
    error recorded into `daily_runs.errors` renders on the funnel page."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class _RedactingFilter(logging.Filter):
    """Redact the formatted message and every string argument.

    Rewrites `record.msg`/`record.args` rather than the formatted output,
    because a handler formats after filtering -- redacting the template alone
    would leave a secret sitting in `args`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def _log_path() -> Path:
    # Same directory as the data store (BLASTJOB_DATA_DIR, else ~/.blastjob).
    from blastjob.config import ensure_private_dir, resolve_data_dir

    log_dir = ensure_private_dir(resolve_data_dir())
    return log_dir / "blastjob.log"


def setup() -> None:
    log_file = _log_path()
    handler = logging.handlers.RotatingFileHandler(
        str(log_file), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(_RedactingFilter())

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Idempotent: `setup()` runs from both the web app and the daily job, and a
    # second handler would double every line.
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)

    # httpx logs the full request URL at INFO, query string included. blastjob's
    # own loggers stay at DEBUG; these do not need to be.
    for noisy in ("httpx", "httpcore", "hpack", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Also capture unhandled exceptions
    def _excepthook(exc_type, exc_value, exc_tb):
        logging.critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook
    logging.info("blastjob started — log: %s", log_file)


def log_exception(label: str, exc: BaseException) -> None:
    logging.error("%s: %s\n%s", label, exc, traceback.format_exc())


def log_path() -> str:
    return str(_log_path())
