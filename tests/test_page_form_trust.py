"""Issue #23: a careers page bound to the employer only through an off-domain
`<form action>` ATS host gets LIMITED trust.

Full trust: the page host IS the ATS tenant host (acme1.applytojob.com), or a
domain the user listed for that employer in `discovery.employer_domains`.
Limited (anything else): the stored and shown link is the ATS tenant root, never
the intermediary page; a 404/410 from the page proves nothing; its text is never
adopted as the employer description. Invented company and hosts throughout.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import json
import tempfile
from datetime import datetime as dt
from pathlib import Path

import pytest

import tests.geo_fixture as geo_fixture
from blastjob.core import closure, employer_domains
from blastjob.core import closure_links as cl
from blastjob.core import page_fetch as pf
from blastjob.core import page_links as pl
from blastjob.core.closure_links import Link, LinkDeps
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig, DiscoveryConfig
from blastjob.web import settings_form as sf
from blastjob.web.apply_link import best_apply_link

COMPANY, TITLE, REGION = "Acme Widgets", "VP of Technology", "OH"
TENANT_HOST = "acmewidgets1.applytojob.com"
TENANT_ROOT = f"https://{TENANT_HOST}/"
OFF_HOST = "careers.example-staffing.example"
OFF_URL = f"https://{OFF_HOST}/jobs/vp-technology"
LISTED_HOST = "careers.acmewidgets.example"
LISTED_URL = f"https://{LISTED_HOST}/jobs/vp-technology"
ATS_URL = f"https://{TENANT_HOST}/apply/AbC123/VP-of-Technology"
TODAY = datetime.date(2026, 10, 18)
TEXT = "VP of Technology. Lead the IT organization. " * 20


def _html(url: str, *, form: str = f"https://{TENANT_HOST}/apply/AbC123") -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": TITLE,
        "url": url,
        "validThrough": "2099-12-31",
        "description": TEXT,
        "hiringOrganization": {"@type": "Organization", "name": "Acme Widgets Inc"},
        "jobLocation": {"@type": "Place", "address": {"addressRegion": "OH"}},
    }
    action = f'<form action="{form}"></form>' if form else ""
    script = f'<script type="application/ld+json">{json.dumps(posting)}</script>'
    return f"<html><head>{script}</head><body>{action}</body></html>"


def _fetch(url: str, status: int = 200, form: str | None = None):
    calls: list[str] = []

    async def fetch(u: str):
        calls.append(u)
        html = _html(url) if form is None else _html(url, form=form)
        return pf.Page(status=status, url=url, html=html, cross_host=False)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _search(*urls: str):
    async def search(_q: str, _key: str) -> list[str]:
        return list(urls)

    return search


def _list_domain() -> None:
    employer_domains.set_employer_domains({COMPANY: ["acmewidgets.example"]})


async def _find(url: str) -> Link | None:
    return await pl.find_page_link(
        COMPANY, TITLE, REGION, "k", search=_search(url), fetch=_fetch(url), today=TODAY
    )


# --- linking ---------------------------------------------------------------------------


async def test_off_domain_form_binding_stores_the_tenant_root_not_the_page():
    link = await _find(OFF_URL)

    assert link == Link(f"page:{OFF_HOST}", OFF_URL, TENANT_ROOT)


async def test_listed_employer_domain_keeps_the_page_url():
    _list_domain()

    link = await _find(LISTED_URL)

    assert link == Link(f"page:{LISTED_HOST}", LISTED_URL, LISTED_URL)


async def test_ats_hosted_page_is_unchanged():
    link = await pl.find_page_link(
        COMPANY,
        TITLE,
        REGION,
        "k",
        search=_search(ATS_URL),
        fetch=_fetch(ATS_URL, form=""),
        today=TODAY,
    )

    assert link == Link(f"page:{TENANT_HOST}", ATS_URL, ATS_URL)


# --- closure evidence ----------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 404, 410])
async def test_off_domain_page_is_no_evidence_either_way(status):
    old_row = Link(f"page:{OFF_HOST}", OFF_URL, OFF_URL)  # tenant root not yet known
    fetch = _fetch(OFF_URL, status=status)

    assert await pl.check_page(old_row, COMPANY, TITLE, REGION, fetch=fetch, today=TODAY) is None
    assert fetch.calls == [OFF_URL]  # the page itself is read, never the tenant root


@pytest.mark.parametrize("status", [200, 410])
async def test_off_domain_page_with_a_known_root_is_not_even_fetched(status):
    link = Link(f"page:{OFF_HOST}", OFF_URL, TENANT_ROOT)
    fetch = _fetch(OFF_URL, status=status)

    assert await pl.check_page(link, COMPANY, TITLE, REGION, fetch=fetch, today=TODAY) is None
    assert fetch.calls == []


async def test_probe_page_returns_the_tenant_root_of_an_old_row():
    old_row = Link(f"page:{OFF_HOST}", OFF_URL, OFF_URL)

    got = await pl.probe_page(old_row, COMPANY, TITLE, REGION, fetch=_fetch(OFF_URL), today=TODAY)

    assert got == cl.PageCheck(None, TENANT_ROOT)


async def test_interleaved_probes_each_get_their_own_root():
    other_url = "https://jobs.other-staffing.example/vp-technology"
    other_root = "https://acmewidgets2.applytojob.com/"
    forms = {OFF_URL: f"{TENANT_ROOT}apply/1", other_url: f"{other_root}apply/2"}

    async def fetch(u: str):
        await asyncio.sleep(0)  # let the other probe run in between
        return pf.Page(status=200, url=u, html=_html(u, form=forms[u]), cross_host=False)

    first, second = await asyncio.gather(
        *(
            pl.probe_page(Link(f"page:{pf.host_of(u)}", u, u), COMPANY, TITLE, REGION, fetch=fetch)
            for u in (OFF_URL, other_url)
        )
    )

    assert (first.tenant_root, second.tenant_root) == (TENANT_ROOT, other_root)
    # No mutable module-level state carries a root between probes.
    mutable = (dict, list, set)
    assert not [n for n, v in vars(pl).items() if isinstance(v, mutable) and n[:2] != "__"]


async def test_listed_domain_page_gone_is_closed():
    _list_domain()
    link = Link(f"page:{LISTED_HOST}", LISTED_URL, LISTED_URL)
    fetch = _fetch(LISTED_URL, status=410)

    assert await pl.check_page(link, COMPANY, TITLE, REGION, fetch=fetch, today=TODAY) is False


async def test_ats_hosted_page_gone_is_still_closed():
    link = Link(f"page:{TENANT_HOST}", ATS_URL, ATS_URL)
    fetch = _fetch(ATS_URL, status=404, form="")

    assert await pl.check_page(link, COMPANY, TITLE, REGION, fetch=fetch, today=TODAY) is False


# --- employer description ------------------------------------------------------------------


@pytest.mark.parametrize("url", [OFF_URL, TENANT_ROOT])
async def test_off_domain_page_never_supplies_the_description(url):
    link = Link(f"page:{OFF_HOST}", OFF_URL, url)  # an old row (page url) or a new one
    fetch = _fetch(OFF_URL)

    got = await pl.page_description(link, COMPANY, TITLE, REGION, fetch=fetch, today=TODAY)

    assert got == (None, "company_mismatch")
    assert fetch.calls == []  # refused before any request


async def test_listed_domain_page_supplies_the_description():
    _list_domain()
    link = Link(f"page:{LISTED_HOST}", LISTED_URL, LISTED_URL)

    got = await pl.page_description(
        link, COMPANY, TITLE, REGION, fetch=_fetch(LISTED_URL), today=TODAY
    )

    assert got == (TEXT, "ok")


async def test_verified_description_refuses_an_off_domain_page_without_spending():
    calls: list[Link] = []

    async def page_description(link, company, title, region):
        calls.append(link)
        return TEXT, "ok"

    deps = LinkDeps(page_description=page_description)
    link = Link(f"page:{OFF_HOST}", OFF_URL, OFF_URL)

    got = await cl.verified_description(link, COMPANY, TITLE, REGION, deps)

    assert got == (None, "company_mismatch")
    assert calls == []


# --- apply link ----------------------------------------------------------------------------


def _member(link_url: str, host: str, page_url: str) -> dict:
    return {
        "id": 1,
        "source": "adzuna",
        "company": COMPANY,
        "url": "https://www.adzuna.com/details/1",
        "employer_link_source": f"page:{host}",
        "employer_link_external_id": page_url,
        "employer_link_url": link_url,
        "source_state": "active",
        "cluster_id": None,
    }


def test_apply_link_is_the_tenant_root_for_an_off_domain_page():
    link = best_apply_link([_member(TENANT_ROOT, OFF_HOST, OFF_URL)])

    assert link is not None and link.url == TENANT_ROOT


def test_an_old_off_domain_row_never_shows_the_page_as_the_apply_link():
    link = best_apply_link([_member(OFF_URL, OFF_HOST, OFF_URL)])

    assert link is not None and link.url == "https://www.adzuna.com/details/1"


def test_listed_domain_shows_the_page_even_on_a_tenant_root_row():
    _list_domain()

    link = best_apply_link([_member(TENANT_ROOT, LISTED_HOST, LISTED_URL)])

    assert link is not None and link.url == LISTED_URL


def test_employer_href_filter_follows_the_same_rule():
    assert pl.employer_href(_member(OFF_URL, OFF_HOST, OFF_URL)) == ""
    assert pl.employer_href(_member(TENANT_ROOT, OFF_HOST, OFF_URL)) == TENANT_ROOT
    assert pl.employer_href({"employer_link_source": "manual", "employer_link_url": "x"}) == "x"


# --- the closure pass over a stored row ----------------------------------------------------


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.discovery.closure_auto = True
    geo_fixture.apply_region(c)
    (Path(c.paths.data_dir) / "work_history.md").write_text("exec", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg):
    conn = dbc.init_db(cfg)
    yield conn
    conn.close()


def _old_page_row(conn) -> int:
    """A kept, unapplied job linked before #23: url == the off-domain page."""
    jid = dbc.upsert_job(conn, source="linkedin", external_id="li-1", company=COMPANY, title=TITLE)
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='', geo_region=?, "
        "employer_link_source=?, employer_link_external_id=?, employer_link_url=?, "
        "posting_check='verified_open' WHERE id=?",
        (REGION, f"page:{OFF_HOST}", OFF_URL, OFF_URL, jid),
    )
    conn.commit()
    return jid


def _deps(*, status: int, find: Link | None = None, searches: list | None = None) -> LinkDeps:
    async def find_page(company, title, region, key):
        if searches is not None:
            searches.append(company)
        return find

    async def none(*_a):
        return None

    return LinkDeps(
        jina_api_key="k",
        probe_company=lambda c: [],
        probe_workday=lambda c, k: [],
        probe_dayforce=lambda c, k: [],
        fetch_board=none,
        check_link=none,
        find_page=find_page,
        probe_page=functools.partial(pl.probe_page, fetch=_fetch(OFF_URL, status=status)),
    )


def _get(conn, jid: int):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


NOW = dt(2026, 10, 18, 8)


@pytest.mark.parametrize("status", [200, 410])
async def test_an_off_domain_page_neither_closes_nor_verifies_a_kept_job(cfg, conn, status):
    jid = _old_page_row(conn)

    await closure.run_closure(cfg, conn, deps=_deps(status=status), now=NOW)

    row = _get(conn, jid)
    assert row["application_state"] == ""
    assert row["posting_check"] == "unverifiable"  # the old "verified open" badge goes too
    assert row["employer_link_external_id"] == OFF_URL  # the row is kept, not dropped


async def test_a_fake_false_from_an_off_domain_page_closes_nothing(cfg, conn):
    jid = _old_page_row(conn)

    async def says_gone(*_a):
        return False

    deps = _deps(status=200)
    deps = LinkDeps(**{**deps.__dict__, "check_page": says_gone, "probe_page": None})
    await closure.run_closure(cfg, conn, deps=deps, now=NOW)

    row = _get(conn, jid)
    assert row["application_state"] == ""
    assert row["posting_check"] == "unverifiable"


async def test_an_old_row_learns_its_tenant_root_from_one_probe_without_a_search(cfg, conn):
    jid = _old_page_row(conn)
    searches: list[str] = []

    await closure.run_closure(cfg, conn, deps=_deps(status=200, searches=searches), now=NOW)

    row = _get(conn, jid)
    assert row["employer_link_url"] == TENANT_ROOT
    assert row["employer_link_external_id"] == OFF_URL
    assert searches == []


async def test_a_newly_found_off_domain_page_is_linked_but_not_verified_open(cfg, conn):
    jid = dbc.upsert_job(conn, source="linkedin", external_id="li-2", company=COMPANY, title=TITLE)
    conn.execute("UPDATE jobs SET review_state='kept', geo_region=? WHERE id=?", (REGION, jid))
    conn.commit()
    found = Link(f"page:{OFF_HOST}", OFF_URL, TENANT_ROOT)

    await closure.run_closure(cfg, conn, deps=_deps(status=200, find=found), now=NOW)

    row = _get(conn, jid)
    assert row["employer_link_url"] == TENANT_ROOT
    assert row["posting_check"] == "unverifiable"


async def test_listing_the_domain_restores_the_page_url_and_closure(cfg, conn):
    jid = _old_page_row(conn)
    conn.execute("UPDATE jobs SET employer_link_url=? WHERE id=?", (TENANT_ROOT, jid))
    conn.commit()
    employer_domains.set_employer_domains({COMPANY: ["example-staffing.example"]})

    await closure.run_closure(cfg, conn, deps=_deps(status=410), now=NOW)

    row = _get(conn, jid)
    assert row["employer_link_url"] == OFF_URL
    assert row["application_state"] == "posting_closed"


# --- the setting ---------------------------------------------------------------------------


def test_employer_domains_are_normalized_bare_hostnames():
    d = DiscoveryConfig(employer_domains={COMPANY: ["AcmeWidgets.Example", "jobs.acme.example"]})

    assert d.employer_domains == {COMPANY: ["acmewidgets.example", "jobs.acme.example"]}


@pytest.mark.parametrize(
    "bad", ["https://acme.example", "acme.example/jobs", "acme.example:443", "localhost", ""]
)
def test_employer_domains_reject_anything_but_a_hostname(bad):
    with pytest.raises(ValueError):
        DiscoveryConfig(employer_domains={COMPANY: [bad]})


def test_settings_form_parses_employer_domains():
    text = f"{COMPANY}, acmewidgets.example, jobs.acme.example\n\nOther Co, other.example"

    got = sf.parse_employer_domains(text)

    assert got == {
        COMPANY: ["acmewidgets.example", "jobs.acme.example"],
        "Other Co": ["other.example"],
    }
    assert sf.employer_domains_text(got).splitlines()[0] == (
        f"{COMPANY}, acmewidgets.example, jobs.acme.example"
    )


def test_settings_form_rejects_a_url():
    with pytest.raises(sf.FormError):
        sf.parse_employer_domains(f"{COMPANY}, https://acmewidgets.example/careers")


def test_a_subdomain_matches_but_a_suffix_lookalike_does_not():
    _list_domain()

    assert employer_domains.listed("careers.acmewidgets.example", COMPANY)
    assert employer_domains.listed("acmewidgets.example", COMPANY)
    assert not employer_domains.listed("evilacmewidgets.example", COMPANY)
    assert not employer_domains.listed("careers.acmewidgets.example", "Other Co")


def _detail(cfg, jid: int) -> str:
    from fastapi.testclient import TestClient

    from blastjob.web.app import create_app

    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))
    return client.get(f"/jobs/{jid}").text


def test_detail_page_never_links_or_names_an_old_off_domain_page(cfg, conn):
    jid = _old_page_row(conn)

    page = _detail(cfg, jid)

    assert OFF_HOST not in page
    assert f"checked on {pl.NEUTRAL_PAGE_LABEL}" in page


def test_detail_page_names_the_tenant_host_once_known(cfg, conn):
    jid = _old_page_row(conn)
    conn.execute("UPDATE jobs SET employer_link_url=? WHERE id=?", (TENANT_ROOT, jid))
    conn.commit()

    page = _detail(cfg, jid)

    assert OFF_HOST not in page
    assert f"checked on {TENANT_HOST}" in page


def test_same_role_block_never_names_the_off_domain_host(cfg, conn):
    from blastjob.web import same_role

    jid = _old_page_row(conn)
    job = _get(conn, jid)

    ctx = same_role.detail_context(conn, job, 30.0, cfg.discovery)

    assert ctx["employer_link_auto"] == pl.NEUTRAL_PAGE_LABEL


def test_employer_label_keeps_full_trust_and_board_sources():
    _list_domain()
    listed = _member(LISTED_URL, LISTED_HOST, LISTED_URL)

    assert pl.employer_label(listed) == f"page:{LISTED_HOST}"
    assert pl.employer_label({"employer_link_source": "greenhouse:acme"}) == "greenhouse:acme"


@pytest.mark.parametrize(
    "host",
    [
        "www.indeed.com",
        "linkedin.com",
        "acme.applytojob.com",
        "applytojob.com",
        "boards.greenhouse.io",
        "acme.github.io",
        "sites.google.com",
        "acme.wixsite.com",
        "acme.netlify.app",
        "acme.vercel.app",
    ],
)
def test_employer_domains_refuse_boards_ats_and_shared_hosts(host):
    with pytest.raises(ValueError, match="cannot be listed"):
        DiscoveryConfig(employer_domains={COMPANY: [host]})


def test_a_company_name_with_a_comma_is_refused():
    # The /settings textarea is comma-separated: such a name could never be saved back.
    with pytest.raises(ValueError):
        DiscoveryConfig(employer_domains={"Acme, Inc.": ["acmewidgets.example"]})


async def test_a_budgeted_page_probe_is_metered_like_a_page_fetch():
    async def probe(*_a):
        return cl.PageCheck(None, TENANT_ROOT)

    budget = cl.RequestBudget(1000)
    deps = cl.budgeted(LinkDeps(probe_page=probe), budget)

    assert deps.probe_page is not None
    assert await deps.probe_page(Link("page:x", "u", "u"), COMPANY, TITLE, REGION) == (
        cl.PageCheck(None, TENANT_ROOT)
    )
    assert budget.used == cl.UNITS_PAGE_FETCH
