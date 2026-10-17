"""U8: the JD block badge/button, jd_staleness, and the split re-score."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import employer_jd as web_ejd
from blastjob.web import packets, service
from blastjob.web import same_role as web_same_role
from blastjob.web.app import create_app
from blastjob.web.runtime import Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

SNIPPET = "Director of Technology. Lead strategy for an employee-owned holding company…"
FULL_JD = "Director of Technology. " + "Own MSP governance, data strategy and AI. " * 40
HEADERS = {CSRF_HEADER: LAUNCH_TOKEN}
FETCHED = "2026-10-15T09:30:00"


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


@pytest.fixture
def client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def _job(conn, *, source="adzuna", company="Acme", raw_jd=SNIPPET, **cols) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, url, raw_jd, review_state, "
        "first_seen, last_seen) VALUES (?, ?, ?, 'Director of Technology', "
        "'https://www.adzuna.com/details/1', ?, 'new', '2026-10-01', '2026-10-01')",
        (source, f"x-{source}-{company}-{len(raw_jd)}", company, raw_jd),
    )
    jid = int(cur.lastrowid or 0)
    for col, val in cols.items():
        conn.execute(f"UPDATE jobs SET {col}=? WHERE id=?", (val, jid))
    conn.commit()
    return jid


def _page(client, jid) -> str:
    resp = client.get(f"/jobs/{jid}")
    assert resp.status_code == 200
    return resp.text


def _adopted(conn, **cols) -> int:
    return _job(
        conn,
        raw_jd=FULL_JD,
        jd_source="employer:greenhouse:acme",
        jd_fetched_at=FETCHED,
        employer_link_source="greenhouse:acme",
        employer_link_url="https://boards.greenhouse.io/acme/jobs/42",
        **cols,
    )


# --- JD block ---------------------------------------------------------------------------


def test_badge_shows_board_date_and_link(client, conn):
    jid = _adopted(conn)
    html = _page(client, jid)
    assert "full description from the employer's posting (Greenhouse, fetched 2026-10-15)" in (html)
    assert 'href="https://boards.greenhouse.io/acme/jobs/42"' in html


def test_badge_names_the_page_host_for_a_careers_page(client, conn):
    jid = _job(
        conn,
        raw_jd=FULL_JD,
        jd_source="employer:page:careers.acme.example",
        jd_fetched_at=FETCHED,
        employer_link_source="page:careers.acme.example",
        employer_link_url="https://careers.acme.example/jobs/42",
    )
    assert "careers.acme.example, fetched 2026-10-15" in _page(client, jid)


def test_badge_absent_without_provenance(client, conn):
    jid = _job(conn, raw_jd=FULL_JD)
    assert "full description from the employer" not in _page(client, jid)


def test_button_shown_for_an_eligible_snippet_row(client, conn):
    jid = _job(conn)
    html = _page(client, jid)
    assert "Find the employer's posting" in html
    assert f'hx-post="/jobs/{jid}/employer-jd"' in html


@pytest.mark.parametrize(
    "cols",
    [
        {"raw_jd": FULL_JD},
        {"jd_pasted_at": "2026-10-02T00:00:00"},
        {"source": "greenhouse:acme"},
        {"company": "Acme Staffing"},
        {"company": ""},
    ],
)
def test_button_hidden_when_ineligible(client, conn, cols):
    jid = _job(conn, **cols)
    assert "Find the employer's posting" not in _page(client, jid)


def _open_href(html: str) -> str:
    m = re.search(r'<a class="ext" href="([^"]*)"[^>]*>open the full posting', html)
    assert m, "open the full posting link missing"
    return m.group(1)


def test_open_full_posting_prefers_the_employer_link(client, conn):
    jid = _job(
        conn,
        employer_link_source="manual",
        employer_link_url="https://careers.acme.example/jobs/42",
    )
    assert _open_href(_page(client, jid)) == "https://careers.acme.example/jobs/42"


def test_open_full_posting_falls_back_to_the_aggregator_url(client, conn):
    jid = _job(conn)
    assert _open_href(_page(client, jid)) == "https://www.adzuna.com/details/1"


def test_job_detail_selects_provenance_columns(conn):
    jid = _adopted(conn)
    row = service.job_detail(conn, jid)
    assert row["jd_source"] == "employer:greenhouse:acme"
    assert row["jd_fetched_at"] == FETCHED
    assert row["employer_link_url"].startswith("https://boards.greenhouse.io")


def test_provenance_label_mapping():
    row = {"jd_source": "employer:workday:acme", "employer_link_url": ""}
    assert web_ejd.jd_provenance(row) == "Workday"
    row = {"jd_source": "employer:page:careers.acme.example", "employer_link_url": ""}
    assert web_ejd.jd_provenance(row) == "careers.acme.example"
    assert web_ejd.jd_provenance({"jd_source": "", "employer_link_url": ""}) == ""


# --- jd_staleness ------------------------------------------------------------------------


def test_packet_built_before_the_employer_text_is_stale(conn):
    jid = _adopted(conn)
    stale, why = packets.jd_staleness(conn, jid, "2026-10-14T00:00:00")
    assert stale is True
    assert "employer's full description arrived after this packet was built" in why


def test_packet_built_after_the_employer_text_is_fresh(conn):
    jid = _adopted(conn)
    assert packets.jd_staleness(conn, jid, "2026-10-16T00:00:00") == (False, "")


# --- split re-score ----------------------------------------------------------------------


@pytest.fixture
def started(monkeypatch) -> list[str]:
    calls: list[str] = []

    def fake_start(name, work):
        calls.append(name)
        return Run(id=f"{name}-{len(calls)}", name=name)

    monkeypatch.setattr(web_ejd.RUNTIME, "start", fake_start)
    return calls


def test_decide_starts_a_rescore_for_ids_the_split_reverted(conn, cfg, monkeypatch):
    a, b = _job(conn, company="Acme"), _job(conn, company="Globex")
    monkeypatch.setattr(web_same_role.rc, "split_pair", lambda *a_, **k: [a])
    seen: list[list[int]] = []
    web_same_role.decide(
        conn, a, b, "different", 50.0, cfg.discovery, rescore=lambda ids: seen.append(list(ids))
    )
    assert seen == [[a]]


def test_decide_starts_no_rescore_when_nothing_reverted(conn, cfg, monkeypatch):
    a, b = _job(conn, company="Acme"), _job(conn, company="Globex")
    monkeypatch.setattr(web_same_role.rc, "split_pair", lambda *a_, **k: [])
    seen: list[list[int]] = []
    web_same_role.decide(
        conn, a, b, "different", 50.0, cfg.discovery, rescore=lambda ids: seen.append(list(ids))
    )
    assert seen == []


def test_different_route_runs_a_score_job_for_reverted_rows(client, conn, started, monkeypatch):
    a, b = _job(conn, company="Acme"), _job(conn, company="Globex")
    monkeypatch.setattr(web_same_role.rc, "split_pair", lambda *a_, **k: [a])
    resp = client.post(f"/jobs/{a}/same-role/{b}/different", headers=HEADERS)
    assert resp.status_code == 200
    assert started == ["score-job"]
    assert resp.headers.get("HX-Trigger") == "refresh-run-status"
