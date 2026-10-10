"""Stale-marking must match what a source actually promises.

Three shapes, not two:

- FULL snapshot (Greenhouse/Lever/Ashby list the whole board): absence proves
  the posting is gone -> stale immediately.
- WINDOWED snapshot (Workday caps at 100 per relevance-ranked searchText;
  Adzuna fetches page 1 only): absence from one run's window proves nothing,
  but prolonged absence still means gone -> age out on a per-source TTL, and
  confirm the closure against the detail endpoint where the source has one.
- INCREMENTAL (alert emails, search firms): absence means nothing, ever.

Also covers per-board `source` namespacing: one failing Greenhouse board must
not stale-mark a sibling board's postings.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

import httpx

from blastjob.db import client as dbc
from blastjob.discovery import workday
from blastjob.discovery.adzuna import AdzunaAdapter
from blastjob.discovery.ashby import AshbyAdapter
from blastjob.discovery.base import RawPosting, stale_after_days
from blastjob.discovery.greenhouse import GreenhouseAdapter
from blastjob.discovery.lever import LeverAdapter
from blastjob.discovery.pipeline import run_discovery
from blastjob.models.config import BlastJobConfig


def _cfg_conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    return cfg, dbc.init_db(cfg)


def _age(conn, external_id: str, days: int) -> None:
    """Backdate last_seen, in the same ISO shape upsert_job writes (the ageing
    comparison is lexicographic over those strings)."""
    when = (datetime.now() - timedelta(days=days)).isoformat()
    conn.execute("UPDATE jobs SET last_seen=? WHERE external_id=?", (when, external_id))
    conn.commit()


def _state(conn, external_id: str) -> str:
    return conn.execute(
        "SELECT source_state FROM jobs WHERE external_id=?", (external_id,)
    ).fetchone()["source_state"]


class _Windowed:
    """A query-limited snapshot source with no detail endpoint (Adzuna's shape)."""

    snapshot = True
    stale_after_days = 14

    def __init__(self, source: str, ids: list[str]):
        self.source = source
        self.ids = ids

    async def fetch(self) -> list[RawPosting]:
        return [
            RawPosting(source=self.source, external_id=i, company="Acme", title=f"VP {i}")
            for i in self.ids
        ]


class _Full:
    def __init__(self, source: str, ids: list[str]):
        self.source = source
        self.ids = ids

    async def fetch(self) -> list[RawPosting]:
        return [
            RawPosting(source=self.source, external_id=i, company="Acme", title=f"VP {i}")
            for i in self.ids
        ]


class _Boom:
    def __init__(self, source: str):
        self.source = source

    async def fetch(self) -> list[RawPosting]:
        raise RuntimeError("board down")


# ---------- windowed sources ----------


async def test_one_missed_window_keeps_the_job_active():
    cfg, conn = _cfg_conn()
    await run_discovery(cfg, conn, adapters=[_Windowed("adzuna", ["1", "2"])])
    res = await run_discovery(cfg, conn, adapters=[_Windowed("adzuna", ["1"])])
    assert res.stale_marked == 0
    assert _state(conn, "2") == "active"
    conn.close()


async def test_prolonged_absence_ages_out():
    cfg, conn = _cfg_conn()
    await run_discovery(cfg, conn, adapters=[_Windowed("adzuna", ["1", "2"])])
    _age(conn, "2", days=30)
    res = await run_discovery(cfg, conn, adapters=[_Windowed("adzuna", ["1"])])
    assert res.stale_marked == 1
    assert _state(conn, "2") == "stale"
    assert _state(conn, "1") == "active"
    conn.close()


async def test_reappearing_job_resets_the_clock():
    cfg, conn = _cfg_conn()
    await run_discovery(cfg, conn, adapters=[_Windowed("adzuna", ["1"])])
    _age(conn, "1", days=13)
    await run_discovery(cfg, conn, adapters=[_Windowed("adzuna", ["1"])])
    res = await run_discovery(cfg, conn, adapters=[_Windowed("adzuna", [])])
    assert res.stale_marked == 0
    assert _state(conn, "1") == "active"
    conn.close()


async def test_adzuna_and_workday_declare_themselves_windowed():
    adzuna = AdzunaAdapter(
        queries=["director"],
        where="Springfield",
        distance_km=50,
        salary_min=0,
        app_id_env="X",
        app_key_env="Y",
    )
    wd = workday.WorkdayAdapter("Acme", "acme/wd1/Careers", queries=["director"])
    assert stale_after_days(adzuna) > 0
    assert stale_after_days(wd) > 0
    # a full-board feed keeps the immediate behaviour
    assert stale_after_days(GreenhouseAdapter("Acme", "acme")) == 0


# ---------- closure confirmation via the detail endpoint ----------

LISTING = {
    "total": 1,
    "jobPostings": [
        {
            "title": "Director, Business Infrastructure",
            "externalPath": "/job/Ohio/Director_R26-1",
            "locationsText": "Springfield, Ohio",
            "postedOn": "Posted Today",
            "bulletFields": ["R26-1"],
        }
    ],
}
DETAIL = {
    "jobPostingInfo": {
        "title": "Director, Business Infrastructure",
        "jobDescription": "<p>Lead infrastructure.</p>",
        "location": "Springfield, Ohio",
        "jobReqId": "R26-1",
    }
}
GONE_URL = "https://acme.wd1.myworkdayjobs.com/Careers/job/Ohio/Director_R26-9"


def _wd_transport(detail_status: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json=LISTING)
        if "R26-9" in str(request.url):
            return httpx.Response(detail_status, json={} if detail_status == 200 else None)
        return httpx.Response(200, json=DETAIL)

    return httpx.MockTransport(handler)


async def test_workday_confirm_open_reads_the_detail_endpoint():
    a = workday.WorkdayAdapter(
        "Acme", "acme/wd1/Careers", queries=["director"], transport=_wd_transport(404)
    )
    assert await a.confirm_open([("R26-9", GONE_URL)]) == set()

    b = workday.WorkdayAdapter(
        "Acme", "acme/wd1/Careers", queries=["director"], transport=_wd_transport(200)
    )
    assert await b.confirm_open([("R26-9", GONE_URL)]) == {"R26-9"}


async def test_aged_out_workday_job_is_confirmed_closed_before_stale_marking():
    cfg, conn = _cfg_conn()
    dbc.upsert_job(
        conn,
        source="workday:acme",
        external_id="R26-9",
        company="Acme",
        title="VP Ops",
        url=GONE_URL,
    )
    _age(conn, "R26-9", days=30)

    still_open = workday.WorkdayAdapter(
        "Acme", "acme/wd1/Careers", queries=["director"], transport=_wd_transport(200)
    )
    res = await run_discovery(cfg, conn, adapters=[still_open])
    assert res.stale_marked == 0
    assert _state(conn, "R26-9") == "active"

    closed = workday.WorkdayAdapter(
        "Acme", "acme/wd1/Careers", queries=["director"], transport=_wd_transport(404)
    )
    _age(conn, "R26-9", days=30)
    res = await run_discovery(cfg, conn, adapters=[closed])
    assert res.stale_marked == 1
    assert _state(conn, "R26-9") == "stale"
    conn.close()


# ---------- full snapshots are unchanged ----------


async def test_full_snapshot_still_stales_on_first_absence():
    cfg, conn = _cfg_conn()
    await run_discovery(cfg, conn, adapters=[_Full("greenhouse:acme", ["1"])])
    res = await run_discovery(cfg, conn, adapters=[_Full("greenhouse:acme", [])])
    assert res.stale_marked == 1
    assert _state(conn, "1") == "stale"
    conn.close()


# ---------- per-board namespacing ----------


def test_ats_adapters_namespace_their_source_per_board():
    assert GreenhouseAdapter("Acme", "acme").source == "greenhouse:acme"
    assert LeverAdapter("Globex", "globex").source == "lever:globex"
    assert AshbyAdapter("Acme", "acme").source == "ashby:acme"
    assert GreenhouseAdapter("Acme", "acme").source != GreenhouseAdapter("Z", "zed").source


def test_greenhouse_postings_carry_the_per_board_source():
    from blastjob.discovery.greenhouse import parse_jobs

    payload = {"jobs": [{"id": 7, "title": "VP Ops", "location": {"name": "Springfield, OH"}}]}
    posts = parse_jobs("Acme", payload, source="greenhouse:acme")
    assert posts[0].source == "greenhouse:acme"


async def test_failing_board_never_stale_marks_a_sibling_board():
    cfg, conn = _cfg_conn()
    await run_discovery(
        cfg,
        conn,
        adapters=[_Full("greenhouse:acme", ["1"]), _Full("greenhouse:zed", ["2"])],
    )
    res = await run_discovery(
        cfg,
        conn,
        adapters=[_Full("greenhouse:acme", ["1"]), _Boom("greenhouse:zed")],
    )
    assert res.stale_marked == 0
    assert _state(conn, "2") == "active"
    assert "greenhouse:zed" in res.errors
    conn.close()


async def test_shared_source_name_with_one_failure_is_never_swept():
    """Belt and braces behind the namespacing: even if two adapters DID report
    the same source, a sibling's success is not evidence about the failed
    board's postings, so nothing under that name may be stale-marked."""
    cfg, conn = _cfg_conn()
    await run_discovery(cfg, conn, adapters=[_Full("greenhouse", ["1", "2"])])
    res = await run_discovery(cfg, conn, adapters=[_Full("greenhouse", ["1"]), _Boom("greenhouse")])
    assert res.stale_marked == 0
    assert _state(conn, "2") == "active"
    conn.close()


async def test_per_source_counts_sum_instead_of_overwriting():
    cfg, conn = _cfg_conn()
    res = await run_discovery(
        cfg,
        conn,
        adapters=[_Full("greenhouse", ["1", "2"]), _Full("greenhouse", ["3"])],
    )
    assert res.per_source["greenhouse"] == 3
    conn.close()


# ---------- the silent search-firm channel reports itself ----------


async def test_search_firm_with_no_results_reports_a_note():
    from blastjob.discovery.searchfirms import FirmReport

    class Silent:
        source = "searchfirms"
        snapshot = False

        def __init__(self):
            self.firm_reports = [
                FirmReport(
                    slug="fabrikamsearch",
                    name="Fabrikam Search Partners",
                    source="searchfirm:fabrikamsearch",
                    queries=3,
                    results=0,
                    kept=0,
                    errors=0,
                )
            ]

        async def fetch(self) -> list[RawPosting]:
            return []

    cfg, conn = _cfg_conn()
    res = await run_discovery(cfg, conn, adapters=[Silent()])
    assert not res.errors
    note = res.notes["searchfirm:fabrikamsearch"]
    assert "Fabrikam Search" in note and "never" in note.lower()
    conn.close()


async def test_search_firm_that_has_produced_postings_is_not_reported():
    from blastjob.discovery.searchfirms import FirmReport

    class Quiet:
        source = "searchfirms"
        snapshot = False

        def __init__(self):
            self.firm_reports = [
                FirmReport(
                    slug="acmesearch",
                    name="Acme Executive Search",
                    source="searchfirm:acmesearch",
                    queries=3,
                    results=2,
                    kept=0,
                    errors=0,
                )
            ]

        async def fetch(self) -> list[RawPosting]:
            return []

    cfg, conn = _cfg_conn()
    dbc.upsert_job(
        conn,
        source="searchfirm:acmesearch",
        external_id="x",
        company="Acme Executive Search",
        title="Chief Officer",
    )
    res = await run_discovery(cfg, conn, adapters=[Quiet()])
    assert "searchfirm:acmesearch" not in res.notes
    conn.close()
