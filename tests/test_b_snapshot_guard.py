"""B6: a successful-but-short snapshot must not stale-mark a whole board.

For an authoritative source stale means closed, so an empty or truncated 200
(a maintenance page, a broken filter, a half-served listing) used to retire every
posting on the board. The guard skips stale-marking for the source and says so.
"""

from __future__ import annotations

import tempfile

import httpx
import pytest

from blastjob.db import client as dbc
from blastjob.discovery import dayforce
from blastjob.discovery.base import RawPosting
from blastjob.discovery.pipeline import run_discovery
from blastjob.models.config import BlastJobConfig

SRC = "greenhouse:acme"


class _Board:
    source = SRC
    snapshot = True

    def __init__(self, ids, stale_after_days=0):
        self._ids = list(ids)
        self.stale_after_days = stale_after_days

    async def fetch(self):
        return [
            RawPosting(source=SRC, external_id=i, company="Acme", title="VP IT") for i in self._ids
        ]


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


async def _seed(conn, n):
    await run_discovery(BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(n)])])


def _active(conn):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE source_state='active'").fetchone()[0]


@pytest.mark.asyncio
async def test_empty_snapshot_with_many_active_rows_marks_nothing(conn):
    await _seed(conn, 5)
    res = await run_discovery(BlastJobConfig(), conn, [_Board([])])
    assert _active(conn) == 5
    assert res.stale_marked == 0
    assert SRC in res.notes and "stale" in res.notes[SRC].lower()


@pytest.mark.asyncio
async def test_empty_snapshot_with_few_active_rows_still_marks(conn):
    await _seed(conn, 2)  # a 2-posting board legitimately empties
    res = await run_discovery(BlastJobConfig(), conn, [_Board([])])
    assert _active(conn) == 0 and res.stale_marked == 2 and SRC not in res.notes


@pytest.mark.asyncio
async def test_full_snapshot_losing_more_than_half_of_a_big_board_marks_nothing(conn):
    await _seed(conn, 12)
    res = await run_discovery(BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(5)])])
    assert _active(conn) == 12 and res.stale_marked == 0 and SRC in res.notes


@pytest.mark.asyncio
async def test_normal_churn_on_a_big_board_still_marks(conn):
    await _seed(conn, 12)
    res = await run_discovery(BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(9)])])
    assert res.stale_marked == 3 and _active(conn) == 9 and SRC not in res.notes


@pytest.mark.asyncio
async def test_half_rule_does_not_apply_to_windowed_sources(conn):
    """A windowed source (Workday/Adzuna) sees a slice each run by design."""
    await run_discovery(
        BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(12)], stale_after_days=14)]
    )
    conn.execute("UPDATE jobs SET last_seen='2000-01-01T00:00:00'")
    conn.commit()
    res = await run_discovery(
        BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(3)], stale_after_days=14)]
    )
    assert res.stale_marked == 9 and SRC not in res.notes


@pytest.mark.asyncio
async def test_empty_windowed_snapshot_marks_nothing(conn):
    await run_discovery(
        BlastJobConfig(), conn, [_Board([f"j{i}" for i in range(4)], stale_after_days=14)]
    )
    conn.execute("UPDATE jobs SET last_seen='2000-01-01T00:00:00'")
    conn.commit()
    res = await run_discovery(BlastJobConfig(), conn, [_Board([], stale_after_days=14)])
    assert res.stale_marked == 0 and SRC in res.notes


# ---- Dayforce raises when pagination is short of maxCount --------------------


def _dayforce_transport(pages: dict[int, list[dict]], max_count: int):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/csrf"):
            return httpx.Response(200, json={"csrfToken": "t"})
        import json as _json

        offset = _json.loads(req.content).get("paginationStart", 0)
        return httpx.Response(
            200, json={"maxCount": max_count, "jobPostings": pages.get(offset, [])}
        )

    return httpx.MockTransport(handler)


def _item(i: int) -> dict:
    return {"jobPostingId": i, "jobReqId": i, "jobTitle": f"VP {i}", "jobDescription": "d"}


@pytest.mark.asyncio
async def test_dayforce_short_of_max_count_raises():
    pages = {0: [_item(1), _item(2)]}  # server claims 30 but pages run dry at 2
    ad = dayforce.DayforceAdapter(
        "Acme", "acme/BOARD", transport=_dayforce_transport(pages, 30), max_retries=1
    )
    with pytest.raises(ValueError, match="short"):
        await ad.fetch()


@pytest.mark.asyncio
async def test_dayforce_complete_listing_does_not_raise():
    pages = {0: [_item(1), _item(2)]}
    ad = dayforce.DayforceAdapter(
        "Acme", "acme/BOARD", transport=_dayforce_transport(pages, 2), max_retries=1
    )
    assert len(await ad.fetch()) == 2
