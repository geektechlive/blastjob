"""Persistent error log at ~/.blastjob/blastjob.log"""

import logging
import sys
import traceback
from pathlib import Path


def _log_path() -> Path:
    # Use the same directory as the data store
    log_dir = Path.home() / ".blastjob"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "blastjob.log"


def setup() -> None:
    log_file = _log_path()
    logging.basicConfig(
        filename=str(log_file),
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
