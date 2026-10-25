"""How a dream company's board is read: title keep-terms as queries and a cap per run (#90)."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from blastjob.core import target_store as ts
from blastjob.db import client as dbc
from blastjob.discovery import dream_boards
from blastjob.discovery.base import RawPosting
from blastjob.discovery.dayforce import DayforceAdapter
from blastjob.discovery.pipeline import build_adapters
from blastjob.discovery.workday import WorkdayAdapter
from blastjob.models.config import BlastJobConfig, TargetBoard

KEEP = ["director", "vp", "head of", "chief"]
DROP = ["engineer", "analyst"]


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    c.discovery.title_keep_terms = list(KEEP)
    c.discovery.title_drop_terms = list(DROP)
    c.discovery.adzuna_queries = ["aggregator query only"]
    c.discovery.enable_aggregators = False
    c.discovery.target_boards = [
        TargetBoard(company="Northwind Traders", ats="workday", token="nw/wd5/Careers"),
        TargetBoard(company="Contoso", ats="workday", token="contoso/wd1/External"),
        TargetBoard(company="Northwind Traders", ats="greenhouse", token="northwind"),
    ]
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    ts.add_targets(c, [ts.TargetInput(name="Northwind Traders")], "dream")
    yield c
    c.close()


def test_a_dream_workday_board_is_queried_by_title_keep_terms_with_a_page_cap(cfg, conn):
    adapters = {a.source: a for a in build_adapters(cfg, conn)}
    dream = adapters["workday:nw"]
    other = adapters["workday:contoso"]
    assert isinstance(dream, WorkdayAdapter)
    assert dream.queries == KEEP
    assert dream.max_pages == dream_boards.DREAM_MAX_PAGES
    assert other.queries == ["aggregator query only"]  # an ordinary board is unchanged


def test_a_dream_full_board_is_wrapped_in_the_cap(cfg, conn):
    adapters = {a.source: a for a in build_adapters(cfg, conn)}
    wrapped = adapters["greenhouse:northwind"]
    assert isinstance(wrapped, dream_boards.CappedBoardAdapter)
    assert wrapped.snapshot is True
    assert wrapped.stale_after_days > 0  # a capped read cannot prove absence


def test_no_dream_set_without_a_connection(cfg):
    adapters = {a.source: a for a in build_adapters(cfg, None)}
    assert not isinstance(adapters["greenhouse:northwind"], dream_boards.CappedBoardAdapter)


class _Fake:
    source = "greenhouse:northwind"

    def __init__(self, titles: list[str]):
        self.titles = titles

    async def fetch(self) -> list[RawPosting]:
        return [
            RawPosting(source=self.source, external_id=str(i), company="Northwind", title=t)
            for i, t in enumerate(self.titles)
        ]


async def test_a_large_full_board_keeps_the_leadership_roles_and_is_bounded():
    titles = [f"Software Engineer {i}" for i in range(900)] + [
        "VP of Technology",
        "Director of Infrastructure",
        "Chief Information Officer",
    ]
    capped = dream_boards.CappedBoardAdapter(_Fake(titles), KEEP, DROP, max_postings=50)
    got = await capped.fetch()
    assert [p.title for p in got] == [
        "VP of Technology",
        "Director of Infrastructure",
        "Chief Information Officer",
    ]


async def test_the_cap_limits_how_many_postings_a_run_takes():
    titles = [f"Director of Thing {i}" for i in range(40)]
    capped = dream_boards.CappedBoardAdapter(_Fake(titles), KEEP, DROP, max_postings=10)
    assert len(await capped.fetch()) == 10


async def test_a_large_workday_board_makes_a_bounded_number_of_calls(cfg, conn):
    """900 postings on the board; the dream read takes few listing calls and no more detail
    fetches than the posting cap, and still keeps the leadership roles."""
    calls = {"list": 0, "detail": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls["list"] += 1
            body = json.loads(request.content)
            offset = int(body["offset"])
            leaders = body["searchText"] in {"director", "vp"} and offset == 0
            items = [
                {
                    "title": f"Software Engineer {offset + i}",
                    "externalPath": f"/job/X/eng-{body['searchText']}-{offset + i}",
                    "locationsText": "Phoenix, AZ",
                    "bulletFields": [f"R{body['searchText']}{offset + i}"],
                }
                for i in range(20)
            ]
            if leaders:
                items[0] = {
                    "title": f"Director of {body['searchText'].upper()} Platforms",
                    "externalPath": f"/job/X/lead-{body['searchText']}",
                    "locationsText": "Phoenix, AZ",
                    "bulletFields": [f"L{body['searchText']}"],
                }
            return httpx.Response(200, json={"total": 900, "jobPostings": items})
        calls["detail"] += 1
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "title": "Director",
                    "jobDescription": "<p>Lead.</p>",
                    "location": "Phoenix, AZ",
                    "jobReqId": "x",
                    "country": {"descriptor": "United States of America"},
                }
            },
        )

    adapters = {a.source: a for a in build_adapters(cfg, conn)}
    adapter = adapters["workday:nw"]
    assert isinstance(adapter, WorkdayAdapter)
    adapter._transport = httpx.MockTransport(handler)
    postings = await adapter.fetch()
    # Only the two leadership roles were wanted; the 900 engineers never cost a detail call.
    assert len(postings) == 2
    assert calls["detail"] == 2
    assert calls["list"] <= len(KEEP) * dream_boards.DREAM_MAX_PAGES
    assert calls["detail"] <= dream_boards.DREAM_MAX_POSTINGS


def test_a_dream_dayforce_board_gets_the_page_cap(cfg, conn):
    cfg.discovery.target_boards = [
        TargetBoard(company="Northwind Traders", ats="dayforce", token="nwcu/CANDIDATEPORTAL")
    ]
    (adapter,) = build_adapters(cfg, conn)
    assert isinstance(adapter, DayforceAdapter)
    assert adapter.max_pages == dream_boards.DREAM_MAX_PAGES
