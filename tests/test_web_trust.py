"""Trust boundaries on what the cockpit renders as fact.

Two holes this file pins down:

1. `research.parse_profile` allowlisted only `sources` against the URLs actually
   fetched. `it_leadership` carries a `url` too and is typed `list[dict]`, so a
   model reading attacker-influenceable pages through r.jina.ai could emit a
   fabricated person with a fabricated link and `company.html` rendered both as
   fact -- on the page the owner uses to plan outreach. `web/people.py` already
   drops contacts whose URL was never fetched; this is the same defense.

2. Jinja escapes quotes but does not sanitize URL schemes, so a
   `javascript:` URL reached `href=` on a page whose `<body>` carries the CSRF
   token. One click ran in the cockpit origin with a valid token.
"""

from __future__ import annotations

import json

from blastjob.web import research
from blastjob.web.urls import safe_external_url

FETCHED = "https://acme.com/about"


def _model_json(**over) -> str:
    d = {
        "company": "Wrong Name",
        "legal_name": "Acme Inc",
        "website": "https://acme.com",
        "hq": "Fairhaven, OH",
        "founded": 1933,
        "industry": "Manufacturing",
        "ownership": "private",
        "size_band": "1,001-5,000",
        "employees": 1072,
        "revenue_usd": 478000000,
        "revenue_growth_pct": 23.0,
        "total_assets_usd": None,
        "net_income_usd": None,
        "customers": None,
        "locations": 36,
        "fiscal_period": "FY2025",
        "ceo": "Jane Doe",
        "ceo_since": "2025-10",
        "it_leadership": [],
        "tech_stack": [{"area": "ERP", "vendor": "SAP", "source": FETCHED}],
        "strategy": "Grow.",
        "recent_events": [{"date": "2026-03", "event": "Bought X", "source": "PRN"}],
        "culture_rating": 3.4,
        "culture_notes": "ok",
        "risks": "r",
        "questions": "q",
        "summary": "s",
        "sources": [{"label": "About", "url": FETCHED}],
        "extra": {},
    }
    d.update(over)
    return json.dumps(d)


# ----- 1. the URL allowlist covers every list that renders a link -------------


def test_fabricated_leader_whose_url_was_never_fetched_is_dropped():
    real = {"name": "K", "title": "Chief Officer", "role": "executive", "url": FETCHED}
    fake = {
        "name": "Invented Person",
        "title": "Chief Officer",
        "role": "executive",
        "url": "https://nope",
    }
    p = research.parse_profile(_model_json(it_leadership=[real, fake, "junk"]), "Acme", {FETCHED})
    assert p is not None
    assert p.it_leadership == [real]


def test_leader_with_no_url_at_all_is_dropped():
    """No source URL means nothing verifiable behind the name -- same rule
    `parse_contacts` applies, and the owner writes to these people."""
    p = research.parse_profile(
        _model_json(
            it_leadership=[{"name": "Ghost", "title": "Chief Officer", "role": "executive"}]
        ),
        "Acme",
        {FETCHED},
    )
    assert p is not None and p.it_leadership == []


def test_sources_allowlist_still_holds():
    p = research.parse_profile(
        _model_json(sources=[{"label": "About", "url": FETCHED}, {"label": "made up", "url": "x"}]),
        "Acme",
        {FETCHED},
    )
    assert p is not None and [s["url"] for s in p.sources] == [FETCHED]


# ----- 2. no javascript: URL survives to an href ------------------------------


def test_javascript_website_is_blanked():
    p = research.parse_profile(_model_json(website="javascript:alert(1)"), "Acme", {FETCHED})
    assert p is not None and p.website == ""


def test_javascript_leadership_url_is_dropped():
    p = research.parse_profile(
        _model_json(
            it_leadership=[
                {
                    "name": "X",
                    "title": "Chief Officer",
                    "role": "executive",
                    "url": "javascript:alert(1)",
                }
            ]
        ),
        "Acme",
        {FETCHED, "javascript:alert(1)"},  # even if it somehow reached the allowlist
    )
    assert p is not None and p.it_leadership == []


def test_good_website_survives():
    p = research.parse_profile(_model_json(website=" https://acme.com "), "Acme", {FETCHED})
    assert p is not None and p.website == "https://acme.com"


# ----- safe_external_url ------------------------------------------------------


def test_safe_external_url_allows_http_and_https():
    assert safe_external_url("https://acme.com/x?y=1#z") == "https://acme.com/x?y=1#z"
    assert safe_external_url("http://10.0.0.1:8765/") == "http://10.0.0.1:8765/"
    assert safe_external_url("  https://acme.com  ") == "https://acme.com"
    assert safe_external_url("HTTPS://ACME.COM") == "HTTPS://ACME.COM"


def test_safe_external_url_rejects_everything_else():
    for bad in (
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "  javascript:alert(1)",
        "\x01javascript:alert(1)",  # browsers skip leading control chars
        "java\tscript:alert(1)",  # ...and ignore TAB/LF/CR inside the scheme
        "java\nscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "//evil.example/x",  # protocol-relative: not ours to emit
        "/jobs/1",
        "",
        "   ",
        None,
        12,
        {"url": "https://acme.com"},
    ):
        assert safe_external_url(bad) == "", bad


# ---------------------------------------------------------------------------
# End-to-end: a poisoned company profile must not produce a clickable
# javascript: link. base.html carries the CSRF token on <body>, and the cockpit
# has no authentication (and may be bound to the LAN), so one click on
# such a link would be account takeover rather than a nuisance.
# ---------------------------------------------------------------------------


def test_no_javascript_url_survives_onto_the_company_page(tmp_path):
    import tempfile

    from fastapi.testclient import TestClient

    from blastjob.core import company as co
    from blastjob.db import client as dbc
    from blastjob.models.config import BlastJobConfig
    from blastjob.web.app import create_app

    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)

    profile = co.CompanyProfile(company="Evil Corp")
    profile.website = "javascript:alert(1)"
    # A tab inside the scheme: browsers skip it and still execute.
    profile.it_leadership = [
        {"name": "Fabricated Person", "title": "Chief Officer", "url": "java\tscript:steal()"}
    ]
    profile.sources = [{"url": "javascript:x()", "label": "src", "accessed": ""}]
    co.upsert_profile(conn, profile)
    conn.close()

    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    html = client.get(f"/companies/{co.company_key('Evil Corp')}").text

    normalized = html.replace("&#9;", "\t").replace("&#x9;", "\t")
    assert "javascript:" not in normalized
    assert "java\tscript:" not in normalized
    # The name still renders as text -- we strip the link, not the information.
    assert "Fabricated Person" in html
