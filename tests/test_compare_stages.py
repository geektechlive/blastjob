"""Compare builds its rows from the stage lists (#51): same cluster rule, real stage names."""

from __future__ import annotations

import re
import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import compare
from blastjob.web.app import create_app

_HREF = re.compile(r'href="/jobs/(\d+)"')
_seq = iter(range(1, 1_000_000))


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, **cols) -> int:
    n = next(_seq)
    jid = dbc.upsert_job(
        conn,
        source=cols.pop("source", f"s{n}"),
        external_id=f"e{n}",
        company=cols.pop("company", f"Acme{n}"),
        title=cols.pop("title", f"VP Role {n}"),
    )
    if cols:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _cluster(conn) -> int:
    cur = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES (?, datetime('now'))",
        (f"fp{next(_seq)}",),
    )
    conn.commit()
    return int(cur.lastrowid)


def _ids(rows: list[dict]) -> list[int]:
    return [r["id"] for r in rows]


def test_surfaced_job_with_a_kept_sibling_is_hidden_on_compare(conn):
    cid = _cluster(conn)
    surfaced = _job(conn, review_state="surfaced", alignment_score=90, cluster_id=cid)
    kept = _job(conn, review_state="kept", alignment_score=80, cluster_id=cid)

    assert surfaced not in _ids(compare.compare_rows(conn, state="surfaced"))
    assert _ids(compare.compare_rows(conn, state="shortlist")) == [kept]


def test_surfaced_job_with_an_archived_sibling_is_hidden_on_compare(conn):
    cid = _cluster(conn)
    surfaced = _job(conn, review_state="surfaced", alignment_score=90, cluster_id=cid)
    _job(conn, review_state="archived", cluster_id=cid)

    assert surfaced not in _ids(compare.compare_rows(conn, state="surfaced"))


def test_kept_job_whose_posting_went_stale_is_still_compared(conn):
    stale = _job(conn, review_state="kept", source_state="stale", alignment_score=85)

    assert _ids(compare.compare_rows(conn, state="shortlist")) == [stale]


def test_each_stage_name_selects_only_that_stage(conn):
    shortlist = _job(conn, review_state="kept", alignment_score=70)
    applied = _job(conn, review_state="kept", application_state="applied", alignment_score=71)
    closed = _job(conn, review_state="kept", application_state="rejected", alignment_score=72)

    assert _ids(compare.compare_rows(conn, state="shortlist")) == [shortlist]
    assert _ids(compare.compare_rows(conn, state="applied")) == [applied]
    assert _ids(compare.compare_rows(conn, state="closed")) == [closed]


def test_rows_stay_ranked_by_alignment_and_filter_by_source(conn):
    low = _job(conn, review_state="surfaced", alignment_score=60, source="alpha")
    high = _job(conn, review_state="surfaced", alignment_score=95, source="beta")

    assert _ids(compare.compare_rows(conn)) == [high, low]
    assert _ids(compare.compare_rows(conn, source="alpha")) == [low]


def test_page_offers_the_real_stage_names_and_maps_old_kept_to_shortlist(cfg, conn):
    kept = _job(conn, review_state="kept", alignment_score=80)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))

    html = client.get("/compare?state=kept").text

    assert [int(i) for i in _HREF.findall(html)] == [kept]
    for stage in ("surfaced", "queued", "shortlist", "applied", "closed"):
        assert f'<option value="{stage}"' in html
    assert '<option value="kept"' not in html
    assert '<option value="shortlist" selected' in html
