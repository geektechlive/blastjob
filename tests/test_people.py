"""People search + addressed outreach: query shape, extraction validation
(URL must come from the results), persistence, routes, and the addressed
draft — search and LLM are stubbed."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.models.contact import Contact, ContactRole
from blastjob.web import build as wbuild
from blastjob.web import people
from blastjob.web.app import create_app
from blastjob.web.runtime import Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

RESULTS = [
    {
        "title": "Jane Doe - Chief Operations Officer - Acme Health | LinkedIn",
        "url": "https://www.linkedin.com/in/example-janedoe",
        "description": "Chief Operations Officer at Acme Health. Springfield.",
    },
    {
        "title": "Sam Lee - Senior Technical Recruiter - Acme Health | LinkedIn",
        "url": "https://www.linkedin.com/in/example-samlee",
        "description": "Talent acquisition, operations leadership hiring.",
    },
]

MODEL_OUTPUT = """Here you go:
```json
[
 {"name": "Jane Doe", "title": "Chief Operations Officer", "role": "hiring_manager",
  "url": "https://www.linkedin.com/in/example-janedoe", "why": "Likely the role's boss."},
 {"name": "Sam Lee", "title": "Senior Technical Recruiter", "role": "recruiter",
  "url": "https://www.linkedin.com/in/example-samlee", "why": "Owns tech exec hiring."},
 {"name": "Made Up", "title": "COO", "role": "hiring_manager",
  "url": "https://www.linkedin.com/in/example-madeup", "why": "invented"},
 {"name": "X", "role": "peer", "url": "https://www.linkedin.com/in/example-janedoe"}
]
```"""


def test_search_queries_target_leadership_and_recruiters():
    qs = people.search_queries("Acme Health")
    assert len(qs) == 3
    assert all('"Acme Health"' in q for q in qs)
    assert "site:linkedin.com/in" in qs[0] and "recruiter" in qs[1]


def test_parse_contacts_keeps_only_people_from_results():
    got = people.parse_contacts(MODEL_OUTPUT, {r["url"] for r in RESULTS})
    assert [c.name for c in got] == ["Jane Doe", "Sam Lee"]  # invented + dup dropped
    assert got[0].role == ContactRole.HIRING_MANAGER
    assert people.parse_contacts("no json here", {"x"}) == []


def test_results_block_fences_untrusted_results():
    block = people.results_block("Acme", "VP Ops", RESULTS)
    assert block.startswith("Company: Acme")
    assert "<RESULTS>" in block and "</RESULTS>" in block
    assert "[2] Sam Lee" in block


@pytest.fixture
def kept():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    Path(cfg.paths.data_dir).mkdir(parents=True, exist_ok=True)
    (Path(cfg.paths.data_dir) / "work_history.md").write_text(
        "Senior operations exec.", encoding="utf-8"
    )
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn, source="t", external_id="1", company="Acme", title="VP Ops", raw_jd="Lead Ops."
    )
    conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    return cfg, jid


def test_add_contacts_is_idempotent_and_ordered(kept):
    cfg, jid = kept
    conn = dbc.init_db(cfg)
    cs = [
        Contact(name="Sam Lee", role="recruiter", url="https://l/in/samlee"),
        Contact(name="Jane Doe", role="hiring_manager", url="https://l/in/janedoe"),
    ]
    assert people.add_contacts(conn, jid, cs) == 2
    assert people.add_contacts(conn, jid, cs) == 0
    rows = people.contacts_for_job(conn, jid)
    assert [r["name"] for r in rows] == ["Jane Doe", "Sam Lee"]  # hiring manager first
    assert people.mark_contacted(conn, rows[0]["id"])
    assert people.contact(conn, rows[0]["id"])["contacted_at"]
    conn.close()


def _client(cfg):
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def _tok():
    return {CSRF_HEADER: LAUNCH_TOKEN}


def test_find_people_route_stores_and_renders(kept, monkeypatch):
    cfg, jid = kept

    async def fake_find(cfg_, company, title):
        assert company == "Acme"
        return [
            Contact(
                name="Jane Doe",
                title="COO",
                role="hiring_manager",
                url="https://www.linkedin.com/in/example-janedoe",
                why="Likely boss.",
            )
        ]

    monkeypatch.setattr(people, "find_people", fake_find)
    client = _client(cfg)
    r = client.post(f"/jobs/{jid}/people", headers=_tok())
    assert r.status_code == 200
    assert "Jane Doe" in r.text and "hiring manager" in r.text and "Draft note to Jane" in r.text
    # detail page includes the contacts block for kept jobs
    assert "Jane Doe" in client.get(f"/jobs/{jid}").text


def test_find_people_route_reports_missing_key(kept, monkeypatch):
    cfg, jid = kept

    async def no_key(cfg_, company, title):
        raise people.PeopleSearchUnavailable("JINA_API_KEY is not set")

    monkeypatch.setattr(people, "find_people", no_key)
    r = _client(cfg).post(f"/jobs/{jid}/people", headers=_tok())
    assert r.status_code == 200 and "JINA_API_KEY is not set" in r.text


def test_mark_contacted_route_toggles(kept):
    cfg, jid = kept
    conn = dbc.init_db(cfg)
    people.add_contacts(conn, jid, [Contact(name="Jane Doe", url="https://l/in/janedoe")])
    cid = people.contacts_for_job(conn, jid)[0]["id"]
    conn.close()
    client = _client(cfg)
    r = client.post(f"/contacts/{cid}/contacted", headers=_tok())
    assert "contacted" in r.text and "Undo" in r.text
    r = client.post(f"/contacts/{cid}/contacted", data={"undo": "1"}, headers=_tok())
    assert "Mark contacted" in r.text


@pytest.mark.asyncio
async def test_outreach_addressed_to_stored_contact(kept, monkeypatch):
    cfg, jid = kept
    conn = dbc.init_db(cfg)
    people.add_contacts(
        conn,
        jid,
        [
            Contact(
                name="Jane Doe",
                title="COO",
                role="hiring_manager",
                url="https://l/in/janedoe",
                why="Likely boss.",
            )
        ],
    )
    cid = people.contacts_for_job(conn, jid)[0]["id"]
    conn.close()
    captured: dict = {}

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def text_stream(self):
            async def gen():
                yield "Hi Jane, "
                yield "quick note."

            return gen()

    async def fake_make_stream(cfg_, system, messages, max_tokens=0):
        captured["user"] = messages[0]["content"]
        return FakeStream()

    monkeypatch.setattr(wbuild, "make_stream", fake_make_stream)
    run = Run(id="o1", name="outreach")
    result = await wbuild.make_outreach_work(jid, load_config=lambda: cfg, contact_id=cid)(run)
    assert result["email"] == "Hi Jane, quick note."
    blob = "".join(b.get("text", "") for b in captured["user"])
    assert "Recipient (verified by the candidate)" in blob and "Jane Doe" in blob
    assert any("addressed to Jane Doe" in e.data for e in run.events if e.kind == "log")


@pytest.mark.asyncio
async def test_outreach_rejects_contact_from_another_job(kept):
    cfg, jid = kept
    conn = dbc.init_db(cfg)
    other = dbc.upsert_job(conn, source="t", external_id="2", company="Beta", title="COO")
    people.add_contacts(conn, other, [Contact(name="Someone Else", url="https://l/in/x")])
    cid = people.contacts_for_job(conn, other)[0]["id"]
    conn.close()
    run = Run(id="o2", name="outreach")
    assert (
        await wbuild.make_outreach_work(jid, load_config=lambda: cfg, contact_id=cid)(run) is None
    )
    assert any(e.kind == "error" for e in run.events)


# --- Jina zero-hit searches are not failures ---------------------------------


def _resp(status: int, payload=None, *, text: str = ""):
    import httpx

    req = httpx.Request("GET", "https://s.jina.ai/?q=x")
    if payload is not None:
        return httpx.Response(status, json=payload, request=req)
    return httpx.Response(status, text=text, request=req)


def test_is_no_results_detects_jina_zero_hit_422():
    from blastjob.web.people import _is_no_results

    # Verbatim shape of the live 422 (2026-09-07, site:example-search-firm.com).
    body = {
        "data": None,
        "code": 422,
        "name": "AssertionFailureError",
        "status": 42206,
        "message": 'No search results available for query site:example-search-firm.com ("chief '
        'operations officer" OR "operations director") (Ohio OR remote)',
    }
    assert _is_no_results(_resp(422, body)) is True


def test_is_no_results_false_for_other_422_and_non_json():
    from blastjob.web.people import _is_no_results

    assert _is_no_results(_resp(422, {"message": "query too long"})) is False
    assert _is_no_results(_resp(422, text="<html>gateway</html>")) is False


def test_jina_search_returns_empty_on_zero_hit_422(monkeypatch):
    """A narrow site: query that matches nothing must not raise -- otherwise the
    search-firm adapter logs a WARNING and reports a per-source error every run."""
    import httpx

    from blastjob.web import people

    body = {"data": None, "code": 422, "message": "No search results available for query x"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _resp(422, body)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert asyncio.run(people.jina_search("site:nowhere.example x", "k")) == []


def test_jina_search_still_raises_on_real_errors(monkeypatch):
    import httpx

    from blastjob.web import people

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _resp(500, text="boom")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(people.jina_search("q", "k"))
