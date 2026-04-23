"""Unit tests for logging.py."""

from blastjob import logging as applog


def test_log_path_returns_string():
    path = applog.log_path()
    assert isinstance(path, str)
    assert "blastjob.log" in path


def test_log_exception_does_not_raise():
    try:
        raise ValueError("test error")
    except ValueError as e:
        applog.log_exception("test", e)
