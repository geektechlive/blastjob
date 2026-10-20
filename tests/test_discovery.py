"""Discovery adapter + pipeline tests.

Adapters are tested as pure parsers against fixtures shaped like the real feeds
(Greenhouse + Ashby fixtures mirror live responses verified during Phase 1). The
pipeline is tested with injected fake adapters — no network, deterministic.
"""

from __future__ import annotations

import tempfile

import pytest

from blastjob.comp import parse_salary_range
from blastjob.db import client as dbc
from blastjob.discovery import adzuna, ashby, greenhouse, lever
from blastjob.discovery.base import RawPosting
from blastjob.discovery.normalize import cluster_fingerprint, to_job_columns
from blastjob.discovery.pipeline import run_discovery
from blastjob.discovery.textutil import html_to_text, split_location
from blastjob.models.config import BlastJobConfig

# ---------- text + comp helpers ----------


def test_html_to_text_unescapes_and_strips():
    raw = (
        "&lt;p&gt;Lead &lt;strong&gt;Ops&lt;/strong&gt;&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;AWS&lt;/li&gt;&lt;/ul&gt;"
    )
    out = html_to_text(raw)
    assert "Lead Ops" in out
    assert "AWS" in out
    assert "<" not in out and "&lt;" not in out


@pytest.mark.parametrize(
    "text,lo,hi",
    [
        ("$211.4K – $290.6K • Offers Equity", 211400, 290600),
        ("$180,000 to $220,000", 180000, 220000),
        ("Up to $1.2M", 1200000, 1200000),
        ("competitive", 0, 0),
        ("call ext 250 for details", 0, 0),  # small number ignored
    ],
)
def test_parse_salary_range(text, lo, hi):
    a, b, cur = parse_salary_range(text)
    assert (a, b) == (lo, hi)
    assert cur == "USD"


def test_split_location():
    assert split_location("Springfield, OH") == ("Springfield", "OH")
    assert split_location("Remote - US") == ("US", "")


def test_split_location_takes_first_semicolon_segment():
    assert split_location("Columbus, OH; Austin, TX") == ("Columbus", "OH")
    assert split_location("Columbus, OH ;") == ("Columbus", "OH")
    assert split_location("; Austin, TX") == ("Austin", "TX")


# ---------- adapter parsers ----------


def test_greenhouse_parse():
    payload = {
        "jobs": [
            {
                "id": 8130725,
                "title": "VP, Business Operations",
                "location": {"name": "Springfield, OH"},
                "absolute_url": "https://x.com/jobs/8130725",
                "content": "&lt;p&gt;Own enterprise ops.&lt;/p&gt;",
                "updated_at": "2026-08-19T14:02:07-04:00",
            }
        ]
    }
    posts = greenhouse.parse_jobs("Acme", payload)
    assert len(posts) == 1
    p = posts[0]
    assert p.source == "greenhouse" and p.external_id == "8130725"
    assert p.company == "Acme" and p.title == "VP, Business Operations"
    assert p.city == "Springfield" and p.region == "OH"
    assert "Own enterprise ops." in p.description


def test_ashby_parse_extracts_comp_and_remote():
    payload = {
        "jobs": [
            {
                "id": "abc-123",
                "title": "Chief Operations Officer",
                "location": "New York, NY (HQ)",
                "address": {
                    "postalAddress": {
                        "addressRegion": "NY",
                        "addressLocality": "New York",
                        "addressCountry": "USA",
                    }
                },
                "isRemote": True,
                "workplaceType": "Hybrid",
                "applyUrl": "https://jobs.ashbyhq.com/acme/abc-123/application",
                "descriptionPlain": "Lead all technology.",
                "compensation": {"compensationTierSummary": "$211.4K – $290.6K • Offers Equity"},
                "isListed": True,
            }
        ]
    }
    posts = ashby.parse_jobs("Acme", payload)
    p = posts[0]
    assert p.external_id == "abc-123" and p.region == "NY"
    assert p.remote is True and p.comp_min == 211400 and p.comp_max == 290600
    assert p.comp_is_estimated is False


def test_ashby_skips_unlisted():
    payload = {"jobs": [{"id": "x", "title": "t", "isListed": False}]}
    assert ashby.parse_jobs("Acme", payload) == []


def test_lever_parse():
    payload = [
        {
            "id": "lev-1",
            "text": "Head of Operations",
            "categories": {"location": "Fairview, OH"},
            "hostedUrl": "https://jobs.lever.co/acme/lev-1",
            "descriptionPlain": "Run tech.",
            "workplaceType": "remote",
            "createdAt": 1700000000000,
        }
    ]
    posts = lever.parse_jobs("Acme", payload)
    p = posts[0]
    assert p.external_id == "lev-1" and p.title == "Head of Operations"
    assert p.remote is True and p.city == "Fairview" and p.region == "OH"


def test_adzuna_parse_marks_predicted_as_estimated():
    payload = {
        "results": [
            {
                "id": "az-1",
                "title": "Operations Director",
                "company": {"display_name": "Beta LLC"},
                "location": {
                    "display_name": "Oakbrook, OH",
                    "area": ["US", "Ohio", "Oakbrook"],
                },
                "redirect_url": "https://adzuna/az-1",
                "description": "Lead Ops.",
                "salary_min": 190000,
                "salary_max": 210000,
                "salary_is_predicted": "1",
                "created": "2026-08-01T00:00:00Z",
            }
        ]
    }
    posts = adzuna.parse_jobs(payload)
    p = posts[0]
    assert p.comp_is_estimated is True and p.comp_max == 210000
    assert p.region == "Ohio" and p.city == "Oakbrook"


# ---------- normalize ----------


def test_normalize_omits_comp_when_absent():
    p = RawPosting(source="s", external_id="1", company="Acme", title="VP Ops")
    cols = to_job_columns(p)
    assert "comp_min" not in cols and "comp_max" not in cols  # so refresh won't zero inference
    assert cols["company"] == "Acme"


def test_normalize_posted_comp_provenance():
    p = RawPosting(source="s", external_id="1", comp_min=200000, comp_max=250000)
    cols = to_job_columns(p)
    assert cols["comp_provenance"] == "posted" and cols["comp_total_est"] == 250000


def test_normalize_downgrades_an_intermediarys_range_to_estimated():
    """Acme Staffing's "This range is provided by Acme Staffing" is a reseller's
    number, not the employer's published pay — POSTED comp is the only kind
    allowed to hard-reject a role, so an intermediary's figure must never carry
    it."""
    p = RawPosting(
        source="linkedin",
        external_id="1",
        company="Acme Staffing",
        comp_min=282000,
        comp_max=322000,
    )
    cols = to_job_columns(p)
    assert cols["comp_provenance"] == "estimated"
    assert cols["comp_confidence"] == 0.5
    assert cols["comp_max"] == 322000  # the figure is kept, only its trust level drops


def test_normalize_keeps_posted_when_the_lister_is_the_employer():
    """Fabrikam Health's own LinkedIn posting also reads "provided by Fabrikam
    Health" — that IS the employer publishing pay, so it stays POSTED."""
    p = RawPosting(
        source="linkedin",
        external_id="1",
        company="Fabrikam Health",
        comp_min=160000,
        comp_max=240000,
    )
    assert to_job_columns(p)["comp_provenance"] == "posted"


def test_cluster_fingerprint_stable_across_sources():
    a = RawPosting(
        source="greenhouse",
        external_id="1",
        company="Acme Inc.",
        title="VP Ops",
        city="Springfield",
    )
    b = RawPosting(
        source="adzuna", external_id="9", company="acme inc", title="vp ops", city="Springfield"
    )
    assert cluster_fingerprint(a) == cluster_fingerprint(b)


# ---------- pipeline ----------


class _FakeAdapter:
    def __init__(self, source, posts):
        self.source = source
        self._posts = posts

    async def fetch(self):
        return self._posts


class _BoomAdapter:
    source = "boom"

    async def fetch(self):
        raise RuntimeError("source down")


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c, cfg
    c.close()


async def test_pipeline_upserts_excludes_and_clusters(conn):
    c, cfg = conn
    cfg.discovery.exclude_companies = ["Woodhaven Analytics"]
    posts = [
        RawPosting(
            source="greenhouse", external_id="1", company="Acme", title="VP Ops", city="Springfield"
        ),
        RawPosting(
            source="adzuna", external_id="9", company="Acme", title="VP Ops", city="Springfield"
        ),
        RawPosting(
            source="greenhouse", external_id="2", company="Woodhaven Analytics", title="Dir Ops"
        ),
    ]
    adapters = [
        _FakeAdapter("greenhouse", [posts[0], posts[2]]),
        _FakeAdapter("adzuna", [posts[1]]),
    ]
    res = await run_discovery(cfg, c, adapters=adapters)
    assert res.skipped_excluded == 1  # Woodhaven Analytics dropped
    assert res.inserted_or_updated == 2
    # the two Acme VP Ops postings across sources share one cluster
    clusters = {r["cluster_id"] for r in c.execute("SELECT cluster_id FROM jobs")}
    assert len(clusters) == 1
    assert c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 2


async def test_pipeline_tolerates_source_failure(conn):
    c, cfg = conn
    adapters = [
        _FakeAdapter(
            "greenhouse",
            [RawPosting(source="greenhouse", external_id="1", company="Acme", title="t")],
        ),
        _BoomAdapter(),
    ]
    res = await run_discovery(cfg, c, adapters=adapters)
    assert "boom" in res.errors and "source down" in res.errors["boom"]
    assert res.inserted_or_updated == 1  # good source still landed


async def test_a_recorded_source_error_never_carries_a_credential(conn):
    """This string is persisted into `daily_runs.errors`, rendered on the funnel
    page to any LAN browser, pushed to Discord and streamed over SSE. httpx puts
    the full request URL in its exception message and Adzuna's credentials travel
    as query parameters -- that combination put the app_key in the log 284 times
    before 2026-09-16."""
    c, cfg = conn

    class _LeakyAdapter:
        source = "adzuna"

        async def fetch(self):
            raise RuntimeError(
                "Client error '401 Unauthorized' for url "
                "'https://api.adzuna.com/v1/api/jobs/us/search/1"
                "?app_id=publicid&app_key=supersecretkey&what=vp'"
            )

    res = await run_discovery(cfg, c, adapters=[_LeakyAdapter()])
    recorded = res.errors["adzuna"]
    assert "supersecretkey" not in recorded
    assert "publicid" not in recorded
    assert "[REDACTED]" in recorded
    # Still diagnosable: the reader must be able to tell what broke.
    assert "401" in recorded and "api.adzuna.com" in recorded


async def test_pipeline_marks_stale_only_for_successful_sources(conn):
    c, cfg = conn
    a1 = [RawPosting(source="greenhouse", external_id="1", company="Acme", title="t")]
    await run_discovery(cfg, c, adapters=[_FakeAdapter("greenhouse", a1)])
    # next run: greenhouse no longer returns req 1 -> it should go stale
    res = await run_discovery(cfg, c, adapters=[_FakeAdapter("greenhouse", [])])
    assert res.stale_marked == 1
    assert (
        c.execute("SELECT source_state FROM jobs WHERE external_id='1'").fetchone()["source_state"]
        == "stale"
    )


@pytest.mark.asyncio
async def test_incremental_source_never_stale_marks():
    """An alert-email style source returns only what is new; its silence about
    older postings must not mark them stale."""
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)

    class Feed:
        source = "mail"
        snapshot = False

        def __init__(self, ids):
            self.ids = ids

        async def fetch(self):
            return [
                RawPosting(source="mail", external_id=i, company="Acme", title=f"VP {i}")
                for i in self.ids
            ]

    await run_discovery(cfg, conn, adapters=[Feed(["1", "2"])])
    res = await run_discovery(cfg, conn, adapters=[Feed(["3"])])
    assert res.stale_marked == 0
    states = [
        r["source_state"]
        for r in conn.execute("SELECT source_state FROM jobs ORDER BY external_id").fetchall()
    ]
    assert states == ["active", "active", "active"]
    conn.close()


# ---------- adapter wiring (build_adapters) ----------


def _bare_cfg() -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg.discovery.mail_alerts_enabled = False
    cfg.discovery.enable_aggregators = False
    return cfg


def test_search_firm_adapter_built_only_on_its_weekday():
    """The weekly firm sweep is gated by the calendar, not by the adapter: on
    any other day the pipeline simply never constructs it."""
    from datetime import date

    from blastjob.discovery.pipeline import build_adapters
    from blastjob.discovery.searchfirms import SearchFirmAdapter
    from blastjob.models.config import SearchFirm

    cfg = _bare_cfg()
    cfg.discovery.search_firms = [
        SearchFirm(name="Acme Executive Search", slug="acmesearch", domain="acmesearch.example")
    ]
    cfg.discovery.search_firm_queries = ["chief technology officer"]
    monday = build_adapters(cfg, today=date(2026, 9, 7))
    tuesday = build_adapters(cfg, today=date(2026, 9, 8))
    assert any(isinstance(a, SearchFirmAdapter) for a in monday)
    assert not any(isinstance(a, SearchFirmAdapter) for a in tuesday)


def test_build_search_firm_adapter_loads_known_ids_from_db():
    """`build_search_firm_adapter` is the on-demand constructor used both by the
    weekday gate and by `daily.sweep_search_firms`; it must populate known_ids
    from existing searchfirm rows that already carry a JD, same as the
    weekday-gated path in `build_adapters` used to do inline."""
    from blastjob.discovery.pipeline import build_search_firm_adapter

    cfg = _bare_cfg()
    conn = dbc.init_db(cfg)
    dbc.upsert_job(
        conn,
        source="searchfirm:acmesearch",
        external_id="acmesearch.example/opportunities/1",
        company="Acme Executive Search (retained search)",
        title="Chief Operations Officer",
        url="https://acmesearch.example/opportunities/1",
        raw_jd="Full description text",
    )
    dbc.upsert_job(
        conn,
        source="searchfirm:acmesearch",
        external_id="acmesearch.example/opportunities/2",
        company="Acme Executive Search (retained search)",
        title="VP Ops",
        url="https://acmesearch.example/opportunities/2",
        raw_jd="",  # no JD yet -> must NOT be treated as known
    )

    sf = build_search_firm_adapter(cfg, conn)
    assert sf.known_ids == {"acmesearch.example/opportunities/1"}
    conn.close()


def test_dayforce_board_builds_a_per_board_adapter():
    from blastjob.discovery.dayforce import DayforceAdapter
    from blastjob.discovery.pipeline import build_adapters
    from blastjob.models.config import TargetBoard

    cfg = _bare_cfg()
    cfg.discovery.search_firms_enabled = False
    cfg.discovery.target_boards = [
        TargetBoard(
            company="Lakeshore Credit Union",
            ats="dayforce",
            token="lakeshorecu/CANDIDATEPORTAL",
        )
    ]
    adapters = build_adapters(cfg)
    dayforce = [a for a in adapters if isinstance(a, DayforceAdapter)]
    assert len(dayforce) == 1
    assert dayforce[0].source == "dayforce:lakeshorecu"


def test_cluster_fingerprint_ignores_corporate_suffixes():
    a = RawPosting(
        source="adzuna",
        external_id="1",
        company="Fabrikam Services",
        title="VP of Operations",
        city="Springfield",
    )
    b = RawPosting(
        source="linkedin",
        external_id="2",
        company="Fabrikam Services Inc",
        title="VP of Operations",
        city="Springfield",
    )
    assert cluster_fingerprint(a) == cluster_fingerprint(b)


def test_is_excluded_matches_whole_words_only():
    from blastjob.discovery.pipeline import _is_excluded

    assert _is_excluded("Meta Platforms", ["Meta"])
    assert _is_excluded("META", ["meta"])
    assert not _is_excluded("Metavante", ["Meta"])
    assert not _is_excluded("Intelerad", ["Intel"])
    # multi-word terms match as a phrase on word boundaries
    assert _is_excluded("The Woodhaven Analytics Group", ["Woodhaven Analytics"])
    assert not _is_excluded("Woodhaven Analyticsco", ["Woodhaven Analytics"])
    # punctuation in a term is literal and still bounded by non-word characters
    assert _is_excluded("AT&T Services", ["AT&T"])
    assert _is_excluded("Acme, Inc.", ["Acme, Inc."])
    assert not _is_excluded("Acme", ["", "  "])
