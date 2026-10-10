"""Where each job stands, at a glance, and a filter for it.

With 10+ roles in play the user could not tell from a list which ones were already
tailored or applied to. Each row now carries a status chip, and every list page
filters by status without changing its best-fit-first order.
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import status_filter as sf
from blastjob.web.app import create_app


def _job(**kw) -> dict:
    base = {
        "review_state": "kept",
        "build_state": "none",
        "application_state": "",
        "posting_check": "",
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    ("job", "key", "label"),
    [
        (_job(), "not_tailored", "not tailored"),
        (_job(build_state="tailoring"), "not_tailored", "tailoring"),
        (_job(build_state="failed"), "not_tailored", "tailor failed"),
        (_job(build_state="tailored"), "tailored", "tailored"),
        (_job(build_state="tailored", application_state="applied"), "applied", "applied"),
        (_job(application_state="screen"), "applied", "screen"),
        (
            _job(build_state="tailored", application_state="posting_closed"),
            "closed",
            "posting closed",
        ),
        (_job(application_state="rejected"), "closed", "rejected"),
        (_job(build_state="tailored", posting_check="closed"), "closed", "posting closed"),
    ],
)
def test_work_status(job, key, label):
    assert sf.work_status(job) == (key, label)


def test_a_screen_whose_posting_closed_still_reads_as_in_process():
    """A posting can close because they are interviewing the candidate."""
    assert sf.work_status(_job(application_state="screen", posting_check="closed"))[0] == "applied"


def test_filter_keeps_order_and_counts_every_status():
    jobs = [
        {"id": 1, **_job(build_state="tailored")},
        {"id": 2, **_job()},
        {"id": 3, **_job(build_state="tailored")},
        {"id": 4, **_job(application_state="applied")},
    ]
    shown, counts = sf.filter_jobs(jobs, "tailored")
    assert [j["id"] for j in shown] == [1, 3]
    assert counts == {"all": 4, "not_tailored": 1, "tailored": 2, "applied": 1, "closed": 0}


def test_unknown_filter_shows_everything():
    jobs = [{"id": 1, **_job()}, {"id": 2, **_job(build_state="tailored")}]
    shown, _ = sf.filter_jobs(jobs, "bogus")
    assert [j["id"] for j in shown] == [1, 2]


@pytest.fixture
def client_cfg():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    rows = [
        ("k1", "Acme", "VP Tailored", "tailored", "", 93),
        ("k2", "Beta", "VP Untailored", "none", "", 80),
        ("k3", "Gamma", "VP Applied", "tailored", "applied", 70),
    ]
    for ext, company, title, build, app_state, score in rows:
        jid = dbc.upsert_job(conn, source="s", external_id=ext, company=company, title=title)
        conn.execute(
            "UPDATE jobs SET review_state='kept', build_state=?, application_state=?, "
            "alignment_score=? WHERE id=?",
            (build, app_state, score, jid),
        )
    conn.commit()
    conn.close()
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None)), cfg


def test_stage_pages_show_a_status_chip_on_every_row(client_cfg):
    client, _ = client_cfg
    shortlist = client.get("/shortlist").text
    assert 'class="badge work work-tailored"' in shortlist
    assert 'class="badge work work-not_tailored"' in shortlist
    assert "VP Applied" not in shortlist
    assert 'class="badge work work-applied"' in client.get("/applied").text


def test_shortlist_filters_by_status_and_shows_counts(client_cfg):
    client, _ = client_cfg
    page = client.get("/shortlist?status=tailored").text
    assert "VP Tailored" in page
    assert "VP Untailored" not in page
    assert "VP Applied" not in page
    assert 'href="?status=not_tailored"' in page
    assert "Not tailored <b>1</b>" in page


def test_old_kept_links_keep_their_filter(client_cfg):
    client, _ = client_cfg
    r = client.get("/kept?status=tailored", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/shortlist?status=tailored"


def test_undecided_pages_have_no_status_chips(client_cfg):
    """Nothing on Surfaced, Queue or Matches is tailored or applied yet."""
    client, cfg = client_cfg
    conn = dbc.init_db(cfg)
    for n, state in enumerate(("surfaced", "surfaced", "queued", "queued")):
        jid = dbc.upsert_job(conn, source="u", external_id=f"u{n}", company=f"U{n}", title="VP U")
        conn.execute("UPDATE jobs SET review_state=?, alignment_score=80 WHERE id=?", (state, jid))
    conn.commit()
    conn.close()
    for path in ("/", "/queue", "/matches?sort=comp"):
        page = client.get(path).text
        assert page.count('id="job-') >= 2, path  # rows render, so the guard is exercised
        assert 'class="status-filters"' not in page, path


def test_all_page_filters_and_chips_too(client_cfg):
    client, _ = client_cfg
    page = client.get("/all?status=applied").text
    assert "VP Applied" in page
    assert "VP Tailored" not in page
    assert 'class="badge work work-applied"' in page
    assert 'aria-current="true">Applied <b>1</b>' in page  # the filter chip, not the nav tab


def test_filtered_list_stays_best_fit_first(client_cfg):
    client, _ = client_cfg
    page = client.get("/shortlist?status=all").text
    assert page.index("VP Tailored") < page.index("VP Untailored")
