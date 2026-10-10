"""Company pages, comp-signal form, job-detail snapshot, and the research
parser (LLM output -> validated CompanyProfile with URL-allowlisted sources)."""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.core import company as co
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import research
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN


@pytest.fixture
def env():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn,
        source="manual",
        external_id="1",
        company="Northwind Credit Union",
        title="Generic VP Role",
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='screen' WHERE id=?", (jid,)
    )
    conn.commit()
    co.upsert_profile(
        conn,
        co.CompanyProfile(
            company="Northwind Credit Union",
            hq="Fairhaven, OH",
            industry="Credit union",
            employees=1072,
            total_assets_usd=7_726_422_516,
            culture_rating=3.2,
            ceo="Jordan Hale",
            summary="Ohio's #2 credit union.",
            it_leadership=[
                {
                    "name": "Sam Lee",
                    "title": "Chief Officer",
                    "role": "executive",
                    "url": "https://l/in/k",
                }
            ],
            sources=[{"label": "NCUA", "url": "https://ncua.example", "accessed": "2026-09-05"}],
        ),
    )
    co.add_comp_signal(
        conn,
        company="Northwind Credit Union",
        job_id=jid,
        kind="asked_minimum",
        low_usd=200_000,
        source="phone screen",
        observed_at="2026-09-04",
    )
    # A floor is only "negotiated" once an employer has actually responded
    # without pushing back. An ask with no response is just a number the
    # candidate typed into a form, and reporting that as a floor would put a
    # fabricated agreement into the interview prep pack.
    co.add_comp_signal(
        conn,
        company="Northwind Credit Union",
        job_id=jid,
        kind="employer_response",
        note="No pushback on $200K",
        source="recruiter",
        observed_at="2026-09-05",
    )
    conn.close()
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    return client, cfg, jid


def _tok():
    return {CSRF_HEADER: LAUNCH_TOKEN}


def test_companies_list_and_detail(env):
    client, _, jid = env
    r = client.get("/companies")
    assert r.status_code == 200 and "Northwind Credit Union" in r.text and "$7.7B" in r.text
    r = client.get("/companies/northwind%20credit%20union")
    assert r.status_code == 200
    assert "Jordan Hale" in r.text and "Sam Lee" in r.text and "Generic VP Role" in r.text
    assert "asked minimum" in r.text and "$200K" in r.text
    assert client.get("/companies/nobody").status_code == 404


def test_job_detail_shows_snapshot_and_floor(env):
    client, _, jid = env
    r = client.get(f"/jobs/{jid}")
    assert "workup" in r.text and "negotiated floor $200K" in r.text
    assert f'hx-post="/jobs/{jid}/comp-signal"' in r.text


def test_add_comp_signal_form(env):
    client, cfg, jid = env
    r = client.post(
        f"/jobs/{jid}/comp-signal",
        data={
            "kind": "employer_response",
            "note": "No pushback",
            "source": "recruiter",
            "observed_at": "2026-09-04",
        },
        headers=_tok(),
    )
    assert r.status_code == 200 and "employer response" in r.text and "No pushback" in r.text
    assert (
        client.post(f"/jobs/{jid}/comp-signal", data={"kind": "vibes"}, headers=_tok()).status_code
        == 400
    )
    conn = dbc.init_db(cfg)
    # 3, not 2: the fixture now seeds an asked_minimum AND the employer_response
    # that makes it a genuine negotiated floor, and this test adds a third.
    assert len(co.comp_signals(conn, job_id=jid)) == 3
    conn.close()


MODEL_JSON = """```json
{"company": "Wrong Name", "legal_name": "Acme Inc", "website": "https://acme.com",
 "hq": "Fairhaven, OH", "founded": "1933", "industry": "Manufacturing", "ownership": "private",
 "size_band": "1,001-5,000", "employees": "1,072", "revenue_usd": 478000000,
 "revenue_growth_pct": "23", "total_assets_usd": null, "net_income_usd": null,
 "customers": null, "locations": 36, "fiscal_period": "FY2025",
 "ceo": "Jane Doe", "ceo_since": "2025-10",
 "it_leadership": [{"name": "K", "title": "Chief Officer", "role": "executive", "url": "https://l/in/k"},
                   "junk"],
 "tech_stack": [{"area": "ERP", "vendor": "SAP", "source": "https://acme.com/about"}],
 "strategy": "Grow.",
 "recent_events": [{"date": "2026-03", "event": "Bought X", "source": "PRN"}],
 "culture_rating": "3.4", "culture_notes": "ok", "risks": "r", "questions": "q1\\nq2",
 "summary": "s",
 "sources": [{"label": "About", "url": "https://acme.com/about"},
             {"label": "made up", "url": "https://nope"}],
 "extra": {"net_worth_pct": 10.5}}
```"""


def test_parse_profile_coerces_and_allowlists_sources():
    p = research.parse_profile(MODEL_JSON, "Acme", {"https://acme.com/about"})
    assert p is not None
    assert p.company == "Acme"  # model may not rename the row
    assert p.founded == 1933 and p.employees == 1072 and p.revenue_growth_pct == 23.0
    assert p.culture_rating == 3.4 and p.total_assets_usd is None
    # "https://l/in/k" was never among the fetched pages, so the leader is dropped.
    # The old assertion encoded the vulnerability: it_leadership renders as an
    # href on the company page, and only `sources` was allowlisted (W16).
    assert p.it_leadership == []
    assert [s["url"] for s in p.sources] == ["https://acme.com/about"]
    assert p.extra == {"net_worth_pct": 10.5}
    assert research.parse_profile("no json", "Acme", set()) is None


def test_pick_pages_prefers_primary_sources():
    results = [
        {"title": "x", "url": "https://random.blog/post", "description": ""},
        {"title": "y", "url": "https://acme.com/about-us/", "description": ""},
        {"title": "z", "url": "https://projects.propublica.org/nonprofits/1", "description": ""},
        {
            "title": "z2",
            "url": "https://projects.propublica.org/nonprofits/1?x=1",
            "description": "",
        },
        {"title": "pdf", "url": "https://acme.com/annual.pdf", "description": ""},
    ]
    picked = [p["url"] for p in research.pick_pages(results, limit=3)]
    assert picked == [
        "https://acme.com/about-us/",
        "https://projects.propublica.org/nonprofits/1",
        "https://random.blog/post",
    ]


@pytest.mark.asyncio
async def test_research_work_upserts_profile(env, monkeypatch):
    client, cfg, jid = env

    async def fake_research(cfg_, company, *, emit):
        emit("log", "stub")
        return co.CompanyProfile(company=company, employees=5, summary="fresh")

    monkeypatch.setattr(research, "research_company", fake_research)
    from blastjob.web.runtime import Run

    run = Run(id="r1", name="research")
    out = await research.make_research_work("Northwind Credit Union", load_config=lambda: cfg)(run)
    assert out["profile_id"]
    conn = dbc.init_db(cfg)
    assert co.get_profile(conn, "Northwind Credit Union")["employees"] == 5
    conn.close()
    r = client.post(f"/jobs/{jid}/research", headers=_tok())
    assert r.status_code == 200 and "Researching Northwind Credit Union" in r.text


def test_an_unanswered_ask_is_not_a_negotiated_floor(env):
    """Silence is not agreement. A desired-salary box filled in on an
    application form used to surface as "negotiated floor $235K" -- a shape
    seen in a real application -- and that figure reaches the interview prep
    pack as the number to anchor on."""
    from blastjob.core import company as co
    from blastjob.db import client as dbc

    _client, cfg, jid = env
    conn = dbc.init_db(cfg)
    conn.execute("DELETE FROM comp_signals WHERE kind='employer_response' AND job_id=?", (jid,))
    conn.commit()
    assert co.negotiated_floor(conn, jid) is None
    conn.close()
