"""Round 2 D5/D6/D8: per-posting savepoints in alert messages, Dayforce page cap,
and the small-board half guard."""

from __future__ import annotations

import json
import tempfile

import httpx
import pytest

from blastjob.db import client as dbc
from blastjob.discovery import dayforce, pipeline
from blastjob.discovery.base import RawPosting
from blastjob.discovery.mailalerts import LedgerEntry
from blastjob.discovery.pipeline import run_discovery
from blastjob.models.config import BlastJobConfig

SRC = "greenhouse:acme"


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


# --- D5: one always-failing posting must not sink its whole message ---------------


class _MailLike:
    source = "mail-alerts"
    snapshot = False

    def __init__(self, ids: list[str]):
        self._postings = [
            RawPosting(source="linkedin", external_id=i, company="Acme", title="VP IT") for i in ids
        ]
        entry = LedgerEntry("<m1>", "linkedin", "1", "ok", len(ids))
        self.batches = [(entry, self._postings)]

    async def fetch(self):
        return self._postings


def _break_posting(monkeypatch, bad: set[str]):
    real = dbc.upsert_job

    def flaky(conn, **kw):
        if kw["external_id"] in bad:
            raise ValueError("boom")
        return real(conn, **kw)

    monkeypatch.setattr(dbc, "upsert_job", flaky)


def _ledger(conn):
    return conn.execute("SELECT status FROM mail_ledger WHERE message_id='<m1>'").fetchone()[0]


def _stored(conn):
    return sorted(r[0] for r in conn.execute("SELECT external_id FROM jobs"))


async def test_a_failing_posting_saves_the_rest_and_marks_the_message_ok(conn, monkeypatch):
    _break_posting(monkeypatch, {"bad"})
    res = await run_discovery(BlastJobConfig(), conn, [_MailLike(["a", "bad", "b"])])
    assert _stored(conn) == ["a", "b"]
    assert _ledger(conn) == "ok"
    assert "mail-alerts" in res.errors and "bad" in res.errors["mail-alerts"]
    assert res.inserted_or_updated == 2


async def test_a_message_where_nothing_saves_is_recorded_as_error(conn, monkeypatch):
    _break_posting(monkeypatch, {"x", "y"})
    await run_discovery(BlastJobConfig(), conn, [_MailLike(["x", "y"])])
    assert _stored(conn) == [] and _ledger(conn) == "error"


# --- D6: dayforce page cap -------------------------------------------------------


def _item(i: int) -> dict:
    return {"jobPostingId": i, "jobReqId": i, "jobTitle": f"VP {i}", "jobDescription": "d"}


def _full_pages(max_count: int):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/csrf"):
            return httpx.Response(200, json={"csrfToken": "t"})
        start = json.loads(req.content).get("paginationStart", 0)
        ids = range(start + 1, start + dayforce.PAGE_SIZE + 1)
        return httpx.Response(
            200, json={"maxCount": max_count, "jobPostings": [_item(i) for i in ids]}
        )

    return httpx.MockTransport(handler)


async def test_page_cap_returns_the_items_and_flags_the_listing_partial():
    ad = dayforce.DayforceAdapter("Acme", "acme/BOARD", max_pages=3, transport=_full_pages(10_000))
    out = await ad.fetch()
    assert len(out) == 3 * dayforce.PAGE_SIZE
    assert "page cap" in ad.partial_listing_note


async def test_a_complete_listing_is_not_flagged_partial():
    ad = dayforce.DayforceAdapter("Acme", "acme/BOARD", transport=_full_pages(25))
    await ad.fetch()
    assert ad.partial_listing_note == ""


class _Board:
    source = SRC
    snapshot = True
    stale_after_days = 0

    def __init__(self, ids, note=""):
        self._ids = list(ids)
        self.partial_listing_note = note

    async def fetch(self):
        return [
            RawPosting(source=SRC, external_id=i, company="Acme", title="VP IT") for i in self._ids
        ]


def _active(conn):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE source_state='active'").fetchone()[0]


async def test_a_partial_listing_never_stale_marks_the_source(conn):
    await run_discovery(BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(10)])])
    res = await run_discovery(
        BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(9)], note="page cap hit")]
    )
    assert res.stale_marked == 0 and _active(conn) == 10
    assert "page cap hit" in res.notes[SRC]


# --- D8: the half guard covers small boards too -----------------------------------


async def test_a_small_full_snapshot_losing_more_than_half_marks_nothing(conn):
    await run_discovery(BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(6)])])
    res = await run_discovery(BlastJobConfig(), conn, [_Board(["j0", "j1"])])
    assert res.stale_marked == 0 and _active(conn) == 6 and SRC in res.notes


async def test_a_small_board_losing_one_of_three_still_marks(conn):
    await run_discovery(BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(3)])])
    res = await run_discovery(BlastJobConfig(), conn, [_Board(["j0", "j1"])])
    assert res.stale_marked == 1 and _active(conn) == 2


def test_pipeline_exposes_the_guard_constants():
    assert pipeline.MIN_ACTIVE_HALF_GUARD >= pipeline.MIN_ACTIVE_EMPTY_GUARD
