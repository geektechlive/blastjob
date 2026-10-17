"""W2: the badge links to the posting the text came from, not the current employer link."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import employer_jd as web_ejd
from blastjob.web.app import create_app

FULL_JD = "Director of Technology. " + "Own MSP governance, data strategy and AI. " * 40
LABEL = "full description from the employer's posting"
MANUAL = "https://manual.example/later"
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


def _job(conn, *, source="adzuna", external_id="a1", url="https://www.adzuna.com/d/1", **cols):
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, url, raw_jd, review_state, "
        "first_seen, last_seen) VALUES (?, ?, 'Acme', 'Director of Technology', ?, ?, 'new', "
        "'2026-10-01', '2026-10-01')",
        (source, external_id, url, FULL_JD),
    )
    jid = int(cur.lastrowid or 0)
    for col, val in cols.items():
        conn.execute(f"UPDATE jobs SET {col}=? WHERE id=?", (val, jid))
    conn.commit()
    return jid


def _badge(client, jid) -> str:
    html = client.get(f"/jobs/{jid}").text
    start = html.index('<span class="jd-note jd-employer">')
    return html[start : html.index("</span>", start)]


def test_links_to_the_held_source_row_url_not_a_later_manual_link(client, conn):
    _job(
        conn,
        source="greenhouse:acme",
        external_id="42",
        url="https://boards.greenhouse.io/acme/jobs/42",
    )
    jid = _job(
        conn,
        jd_source="employer:greenhouse:acme",
        jd_source_ref="42",
        jd_fetched_at=FETCHED,
        employer_link_source="manual",
        employer_link_url=MANUAL,
    )
    badge = _badge(client, jid)
    assert 'href="https://boards.greenhouse.io/acme/jobs/42"' in badge
    assert MANUAL not in badge


def test_page_source_links_to_the_canonical_page_url(client, conn):
    jid = _job(
        conn,
        jd_source="employer:page:careers.acme.example",
        jd_source_ref="https://careers.acme.example/jobs/42",
        jd_fetched_at=FETCHED,
        employer_link_source="manual",
        employer_link_url=MANUAL,
    )
    badge = _badge(client, jid)
    assert 'href="https://careers.acme.example/jobs/42"' in badge
    assert MANUAL not in badge


def test_falls_back_to_the_employer_link_when_its_source_matches(client, conn):
    jid = _job(
        conn,
        jd_source="employer:greenhouse:acme",
        jd_source_ref="42",
        jd_fetched_at=FETCHED,
        employer_link_source="greenhouse:acme",
        employer_link_url="https://boards.greenhouse.io/acme/jobs/42",
    )
    assert 'href="https://boards.greenhouse.io/acme/jobs/42"' in _badge(client, jid)


def test_no_link_when_a_later_manual_link_is_the_only_one(client, conn):
    jid = _job(
        conn,
        jd_source="employer:greenhouse:acme",
        jd_source_ref="42",
        jd_fetched_at=FETCHED,
        employer_link_source="manual",
        employer_link_url=MANUAL,
    )
    badge = _badge(client, jid)
    assert "<a " not in badge
    assert LABEL in badge


def test_no_link_for_a_free_copy_with_no_link_at_all(client, conn):
    jid = _job(
        conn,
        jd_source="employer:greenhouse:acme",
        jd_source_ref="gone",
        jd_fetched_at=FETCHED,
    )
    badge = _badge(client, jid)
    assert "<a " not in badge
    assert "Greenhouse" in badge


def test_unsafe_source_url_is_not_linked(client, conn):
    _job(conn, source="greenhouse:acme", external_id="42", url="javascript:alert(1)")
    jid = _job(
        conn,
        jd_source="employer:greenhouse:acme",
        jd_source_ref="42",
        jd_fetched_at=FETCHED,
    )
    assert "<a " not in _badge(client, jid)


def test_provenance_url_helper_unit():
    row = {
        "jd_source": "employer:page:h.example",
        "jd_source_ref": "https://h.example/j/1",
        "jd_link_url": "",
        "employer_link_source": "",
        "employer_link_url": "",
    }
    assert web_ejd.jd_provenance_url(row) == "https://h.example/j/1"
    assert web_ejd.jd_provenance_url({**row, "jd_source": ""}) == ""
