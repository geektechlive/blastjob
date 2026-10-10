"""Retry policy for an alert email that parses to 0 postings.

Defect this guards: a message that parses to 0 postings used to be stamped
ledger status "ok" -- the same status a normally-parsed message gets -- so
`discovery.pipeline.processed_message_ids` (status IN ('ok','ignored')) treated
it as done forever. A LinkedIn/Indeed template restyle then permanently
blinds the adapter to every alert in that window, with no recovery path short
of hand-deleting the `mail_ledger` row. Since the alert-email feed is this
app's best source (see module docstring in `mailalerts.py`), losing it
silently is losing the channel that works.

Fix: a zero-posting parse of a recognized alert now records status "empty"
instead of "ok". "empty" is not in `processed_message_ids`'s skip set, so the
pipeline retries the message on every subsequent run -- bounded not by a
retry counter but by `fetch_alert_messages`'s existing `since` window: once
the message ages past `mail_lookback_days` it stops being returned by IMAP
SEARCH SINCE at all, so it cannot be retried forever.

No live IMAP or HTTP here -- `fetch_messages` and `search`/`reader` callables
are always injected fakes.
"""

from __future__ import annotations

import datetime as dt
import logging
import tempfile

import pytest

from blastjob.db import client as dbc
from blastjob.discovery import mailalerts as ma
from blastjob.discovery.pipeline import processed_message_ids, run_discovery
from blastjob.models.config import BlastJobConfig

# A recognized (indeed.com) source whose body carries no job link at all --
# e.g. a "your alert is now active" confirmation, or a template restyle that
# broke every anchor pattern the parser knows.
_ZERO_POSTING_BODY = "Your job alert is now active. You'll get an email when new jobs match."


def _adapter(messages, skip=(), debug_dir=None):
    def fake_fetch(**kw):
        assert kw["skip_message_ids"] == set(skip)
        return [m for m in messages if m.message_id not in kw["skip_message_ids"]]

    return ma.MailAlertAdapter(
        host="h",
        user="u",
        password="p",
        skip_message_ids=set(skip),
        fetch_messages=fake_fetch,
        enrich=False,
        debug_dir=debug_dir,
    )


# ---------------------------------------------------------------------------
# A zero-posting parse is retryable, not "ok"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_posting_parse_records_empty_not_ok(caplog):
    msgs = [
        ma.MailMessage("<empty@in>", "20", "alert@indeed.com", "Alert active", _ZERO_POSTING_BODY)
    ]
    a = _adapter(msgs)
    with caplog.at_level(logging.WARNING):
        out = await a.fetch()
    assert out == []
    (entry,) = a.ledger
    assert entry.status == "empty"
    assert entry.status != "ok"
    assert entry.postings == 0
    assert entry.source == "indeed"
    assert "alert parsed to 0 postings" in caplog.text


@pytest.mark.asyncio
async def test_normal_parse_still_records_ok_exactly_once():
    """A message that DOES yield postings is unaffected by the fix."""
    html = (
        '<a href="https://www.linkedin.com/comm/jobs/view/4123456789/">'
        "Chief Operations Officer</a><p>Acme Health · Fairhaven, OH</p>"
    )
    msgs = [ma.MailMessage("<ok@li>", "1", "jobs-noreply@linkedin.com", "alert", html)]
    a = _adapter(msgs)
    out = await a.fetch()
    assert len(out) == 1
    (entry,) = a.ledger
    assert entry.status == "ok"
    assert entry.postings == 1


@pytest.mark.asyncio
async def test_ignored_source_still_records_ignored_not_empty():
    """An unrecognized sender/body stays 'ignored' -- distinct from a
    recognized alert that parsed to zero."""
    msgs = [ma.MailMessage("<x@x>", "1", "news@example.com", "spam", "<p>hi</p>")]
    a = _adapter(msgs)
    await a.fetch()
    (entry,) = a.ledger
    assert entry.status == "ignored"


@pytest.mark.asyncio
async def test_parse_exception_still_records_error_not_empty():
    """A genuine parse exception is a distinct status from a clean zero-result
    parse -- both are retryable, but only 'error' carries the exception text."""
    msgs = [ma.MailMessage("<bad@li>", "1", "jobs-noreply@linkedin.com", "alert", "<html>")]
    a = _adapter(msgs)

    def boom(sender, html):
        raise ValueError("boom")

    import blastjob.discovery.mailalerts as mod

    orig = mod.parse_alert
    mod.parse_alert = boom
    try:
        await a.fetch()
    finally:
        mod.parse_alert = orig
    (entry,) = a.ledger
    assert entry.status == "error"
    assert entry.error


# ---------------------------------------------------------------------------
# "empty" is retried by the pipeline; "ok" and "ignored" are not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_retries_empty_status_next_run():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    msgs = [
        ma.MailMessage("<empty@in>", "20", "alert@indeed.com", "Alert active", _ZERO_POSTING_BODY)
    ]

    res1 = await run_discovery(cfg, conn, adapters=[_adapter(msgs)])
    assert res1.inserted_or_updated == 0
    # not marked processed -- the pipeline will hand it back next run
    assert processed_message_ids(conn) == set()

    # second run: skip_message_ids is still empty, so the fake adapter
    # re-returns the same message -- proving it was NOT treated as done.
    res2 = await run_discovery(cfg, conn, adapters=[_adapter(msgs, skip=[])])
    assert res2.fetched == 0  # still zero postings found
    assert processed_message_ids(conn) == set()
    conn.close()


@pytest.mark.asyncio
async def test_pipeline_does_not_retry_ok_status():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    html = (
        '<a href="https://www.linkedin.com/comm/jobs/view/4123456789/">'
        "Chief Operations Officer</a><p>Acme Health · Fairhaven, OH</p>"
    )
    msgs = [ma.MailMessage("<ok@li>", "1", "jobs-noreply@linkedin.com", "alert", html)]
    await run_discovery(cfg, conn, adapters=[_adapter(msgs)])
    assert processed_message_ids(conn) == {"<ok@li>"}
    conn.close()


# ---------------------------------------------------------------------------
# The bound: an "empty" message is retried only within the IMAP lookback
# window -- once it ages past `since`, SEARCH SINCE stops returning it.
# ---------------------------------------------------------------------------


def test_empty_message_ages_out_of_imap_lookback_window():
    """fetch_alert_messages never re-fetches a message once its date falls
    outside `since` -- the mechanism that bounds "empty" retries without a
    separate attempt counter. skip_message_ids is empty (as it always is for
    an "empty"-status message), yet a `since` past the message's own send
    date means the fake IMAP's SEARCH never surfaces its UID at all."""

    class FakeIMAP:
        def __init__(self, host):
            pass

        def login(self, u, p):
            return "OK", []

        def select(self, mailbox, readonly=False):
            return "OK", []

        def logout(self):
            return "BYE", []

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                # A real IMAP server only returns UIDs whose INTERNALDATE is
                # on/after `since` -- simulate that this message (uid 1) is
                # now older than any `since` the caller passes.
                return "OK", [b""]
            raise AssertionError("no FETCH expected once SEARCH returns nothing")

    out = ma.fetch_alert_messages(
        host="h",
        user="u",
        password="p",
        since=dt.date.today(),
        skip_message_ids=set(),
        connect=FakeIMAP,
    )
    assert out == []
