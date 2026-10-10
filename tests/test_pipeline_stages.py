"""The pipeline as exclusive stages: Surfaced, Queue, Shortlist, Applied, Closed.

Audited on live data (2026-09-13): 21 of 26 kept jobs showed on Matches AND
Kept AND a Pipeline lane; Kept mixed roles being weighed with live applications;
Unkeep dumped any job back into Surfaced; acted-on rows stayed on the page; and
one sort served every stage. These tests pin the replacement: every job is on
at most one stage page, each stage has its own order, moves are CAS on stored
state, and a row acted on from a list leaves it.
"""

from __future__ import annotations

import datetime
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.core import closure
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import service, stage_lists, stage_moves, stages
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

TOKEN = {CSRF_HEADER: LAUNCH_TOKEN}
REVIEW_STATES = ("new", "surfaced", "queued", "kept", "archived")
APP_STATES = ("", *stages.SHORTLIST_STATES[1:], *stages.IN_PROCESS_STATES, *stages.CLOSED_STATES)
STAGE_PAGES = {
    "surfaced": "/",
    "queued": "/queue",
    "shortlist": "/shortlist",
    "applied": "/applied",
    "closed": "/closed",
}
_ROW_ID = re.compile(r'id="job-(\d+)"')


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


def _client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))


_seq = iter(range(1, 1_000_000))


def _job(conn, **cols) -> int:
    n = next(_seq)
    jid = dbc.upsert_job(
        conn,
        source=cols.pop("source", f"s{n}"),
        external_id=f"e{n}",
        company=cols.pop("company", f"Co{n}"),
        title=cols.pop("title", f"VP Role {n}"),
    )
    if cols:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _cluster(conn, key: str) -> int:
    cur = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES (?, datetime('now'))", (key,)
    )
    conn.commit()
    return int(cur.lastrowid)


def _page_ids(client: TestClient, path: str) -> list[int]:
    return [int(i) for i in _ROW_ID.findall(client.get(path).text)]


def _stored(conn, jid: int) -> dict:
    return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


# ----- exclusivity ----------------------------------------------------------------


def _seed_every_combination(conn) -> dict[int, list[int]]:
    """Every review_state x application_state x posting_check, plus clusters
    whose members sit in different stages. Returns cluster id -> member ids."""
    score = 60
    for review in REVIEW_STATES:
        for app_state in APP_STATES:
            for check in ("", "likely_closed"):
                score = 60 + (score - 59) % 40
                _job(
                    conn,
                    review_state=review,
                    application_state=app_state,
                    posting_check=check,
                    alignment_score=score,
                    applied_at="2026-09-01" if app_state in stages.IN_PROCESS_STATES else "",
                )
    _job(conn, review_state="surfaced", alignment_score=95, source_state="stale")
    shapes = {
        "surfaced+screen+shortlist": [
            ("surfaced", "", 90),
            ("kept", "screen", None),
            ("kept", "", 88),
        ],
        "surfaced+queued": [("surfaced", "", 80), ("queued", "", 85)],
        "shortlist+rejected": [("kept", "", 91), ("kept", "rejected", 70)],
        "queued+archived": [("queued", "", 75), ("archived", "", 99)],
        "closed+applied": [("kept", "posting_closed", 90), ("kept", "applied", 60)],
    }
    clusters: dict[int, list[int]] = {}
    for key, members in shapes.items():
        cid = _cluster(conn, key)
        clusters[cid] = [
            _job(
                conn,
                review_state=r,
                application_state=a,
                alignment_score=s,
                cluster_id=cid,
                company=key,
                title="Same Role",
            )
            for r, a, s in members
        ]
    return clusters


def test_every_job_is_on_at_most_one_stage_page(cfg, conn):
    clusters = _seed_every_combination(conn)
    client = _client(cfg)

    on_page = {stage: _page_ids(client, path) for stage, path in STAGE_PAGES.items()}
    seen: dict[int, str] = {}
    for stage, ids in on_page.items():
        assert len(ids) == len(set(ids)), f"{stage} lists a job twice"
        for jid in ids:
            assert jid not in seen, f"job {jid} on {seen.get(jid)} and {stage}"
            seen[jid] = stage

    # Unclustered jobs land exactly where the one stage rule says.
    for row in conn.execute("SELECT * FROM jobs WHERE cluster_id IS NULL").fetchall():
        assert seen.get(row["id"]) == stages.stage_of(row), dict(row)

    # A cluster shows once, in the stage its furthest member reached.
    expected = {
        "surfaced+screen+shortlist": "applied",
        "surfaced+queued": "queued",
        "shortlist+rejected": "closed",
        "queued+archived": None,
        "closed+applied": "applied",
    }
    for cid, members in clusters.items():
        key = _stored(conn, members[0])["company"]
        hits = [seen[m] for m in members if m in seen]
        assert hits == ([expected[key]] if expected[key] else []), key

    # Kept jobs are never on the undecided pages, Matches included.
    kept = {r["id"] for r in conn.execute("SELECT id FROM jobs WHERE review_state='kept'")}
    undecided = on_page["surfaced"] + on_page["queued"] + _page_ids(client, "/matches")
    assert not kept & set(undecided)
    archived = {r["id"] for r in conn.execute("SELECT id FROM jobs WHERE review_state='archived'")}
    assert not archived & set(seen)


def test_header_counts_equal_page_lengths(cfg, conn):
    _seed_every_combination(conn)
    client = _client(cfg)
    html = client.get("/pipeline").text
    counts = stage_lists.counts(conn)
    for stage, path in STAGE_PAGES.items():
        label = stages.LABELS[stage]
        m = re.search(rf'href="{re.escape(path)}"[^>]*>{label} <b>(\d+)</b>', html)
        assert m, stage
        assert int(m.group(1)) == len(_page_ids(client, path)) == counts[stage], stage


def test_pipeline_board_puts_each_kept_job_in_one_lane(conn):
    _seed_every_combination(conn)
    board = stage_lists.pipeline_board(conn)
    ids = [j["id"] for lane in board.values() for j in lane]
    assert len(ids) == len(set(ids))
    expected = {
        j["id"] for stage in stages.KEPT_STAGES for j in stage_lists.STAGE_LISTS[stage](conn)
    }
    assert set(ids) == expected
    assert list(board) == [
        "none", "tailoring", "tailored", "failed",
        "applied", "screen", "interview", "offer", "closed",
    ]  # fmt: skip


# ----- sorts ------------------------------------------------------------------------


def test_surfaced_sorts_by_fit_then_newest(conn):
    old = _job(conn, review_state="surfaced", alignment_score=80, first_seen="2026-09-01")
    new = _job(conn, review_state="surfaced", alignment_score=80, first_seen="2026-09-10")
    best = _job(conn, review_state="surfaced", alignment_score=92, first_seen="2026-08-01")
    assert [j["id"] for j in stage_lists.surfaced_jobs(conn)] == [best, new, old]


def test_shortlist_is_fit_first_with_likely_closed_sunk(conn):
    hi_closed = _job(conn, review_state="kept", alignment_score=88, posting_check="likely_closed")
    lo_closed = _job(conn, review_state="kept", alignment_score=60, posting_check="likely_closed")
    mid = _job(conn, review_state="kept", alignment_score=75, application_state="drafted")
    unscored = _job(conn, review_state="kept")
    top = _job(conn, review_state="kept", alignment_score=93, build_state="tailored")
    order = [j["id"] for j in stage_lists.shortlist_jobs(conn)]
    assert order == [top, mid, unscored, hi_closed, lo_closed]


def test_shortlist_page_sinks_closed_and_likely_closed_postings(cfg, conn):
    """closure_auto is off, so a proven-closed posting stays kept; it must not
    outrank open ones. Open postings by fit, then closed/likely-closed by fit."""
    closed_top = _job(conn, review_state="kept", alignment_score=95, posting_check="closed")
    likely = _job(conn, review_state="kept", alignment_score=90, posting_check="likely_closed")
    closed_low = _job(conn, review_state="kept", alignment_score=70, posting_check="closed")
    open_hi = _job(conn, review_state="kept", alignment_score=85, posting_check="verified_open")
    open_lo = _job(conn, review_state="kept", alignment_score=60)
    rendered = _page_ids(_client(cfg), "/shortlist")
    assert rendered == [open_hi, open_lo, closed_top, likely, closed_low]


def test_applied_is_furthest_stage_first_then_most_overdue(conn):
    today = datetime.date.today()

    def ago(days: int) -> str:
        return (today - datetime.timedelta(days=days)).isoformat()

    fresh = _job(conn, review_state="kept", application_state="applied", applied_at=ago(1))
    stale = _job(conn, review_state="kept", application_state="applied", applied_at=ago(20))
    staler = _job(conn, review_state="kept", application_state="applied", applied_at=ago(30))
    screen = _job(
        conn, review_state="kept", application_state="screen", next_action_due=ago(2),
        alignment_score=50,
    )  # fmt: skip
    offer = _job(conn, review_state="kept", application_state="offer", alignment_score=10)
    interview = _job(conn, review_state="kept", application_state="interview")
    jobs = stage_lists.applied_jobs(conn)
    assert [j["id"] for j in jobs] == [offer, interview, screen, staler, stale, fresh]
    flagged = {j["id"] for j in jobs if j["followup_reason"]}
    assert flagged == {screen, staler, stale}


def test_applied_rows_carry_their_latest_event_and_render_it(conn, cfg):
    from blastjob.core import events

    jid = _job(conn, review_state="kept", application_state="interview", company="Wild")
    quiet = _job(conn, review_state="kept", application_state="applied")
    events.add_event(conn, jid, "interview", "2026-10-09", note="onsite")
    events.add_event(conn, jid, "thank_you", "2026-10-09")

    rows = {j["id"]: j for j in stage_lists.applied_jobs(conn)}

    assert rows[jid]["latest_event"] == {"label": "Thank-you sent", "date": "Oct 9"}
    assert rows[quiet]["latest_event"] is None
    assert "Thank-you sent · Oct 9" in _client(cfg).get("/applied").text


def test_closed_is_most_recently_closed_first(conn):
    by_stamp = _job(
        conn, review_state="kept", application_state="rejected",
        stage_changed_at="2026-09-12T10:00:00", posting_closed_at="2026-01-01T00:00:00",
    )  # fmt: skip
    by_closure = _job(
        conn, review_state="kept", application_state="posting_closed",
        posting_closed_at="2026-09-10T08:00:00",
    )  # fmt: skip
    by_applied = _job(
        conn, review_state="kept", application_state="ghosted", applied_at="2026-08-01"
    )
    newest = _job(
        conn, review_state="kept", application_state="withdrawn",
        stage_changed_at="2026-09-13T09:00:00",
    )  # fmt: skip
    order = [j["id"] for j in stage_lists.closed_jobs(conn)]
    assert order == [newest, by_stamp, by_closure, by_applied]


# ----- transitions (stored state) -----------------------------------------------------


def test_mark_applied_moves_shortlist_to_applied(cfg, conn):
    jid = _job(conn, review_state="kept", alignment_score=90)
    r = _client(cfg).post(f"/jobs/{jid}/stage/apply", headers=TOKEN)
    assert r.status_code == 200 and r.text == ""
    row = _stored(conn, jid)
    assert row["application_state"] == "applied"
    assert row["applied_at"] == datetime.date.today().isoformat()
    assert row["stage_changed_at"].startswith(datetime.date.today().isoformat())
    client = _client(cfg)
    assert jid in _page_ids(client, "/applied")
    assert jid not in _page_ids(client, "/shortlist")


def test_mark_applied_without_a_packet_still_moves_the_job(cfg, conn):
    """The send snapshot is bookkeeping. A job with no tailored packet on disk
    records nothing and still moves — a missing document must never cost the
    user the ability to say they applied."""
    jid = _job(conn, review_state="kept", alignment_score=90)
    assert stage_moves.mark_applied(conn, jid) is True
    assert _stored(conn, jid)["application_state"] == "applied"
    assert (
        conn.execute("SELECT COUNT(*) c FROM packet_sends WHERE job_id=?", (jid,)).fetchone()["c"]
        == 0
    )


def test_advance_walks_the_hiring_stages_and_stops_at_offer(cfg, conn):
    jid = _job(conn, review_state="kept", application_state="applied", applied_at="2026-09-01")
    client = _client(cfg)
    for expected in ("screen", "interview", "offer"):
        assert client.post(f"/jobs/{jid}/stage/advance", headers=TOKEN).status_code == 200
        assert _stored(conn, jid)["application_state"] == expected
    r = client.post(f"/jobs/{jid}/stage/advance", headers=TOKEN)
    assert r.headers.get("HX-Refresh") == "true"
    assert _stored(conn, jid)["application_state"] == "offer"


def test_withdraw_moves_to_closed(cfg, conn):
    jid = _job(conn, review_state="kept", application_state="interview")
    _client(cfg).post(f"/jobs/{jid}/stage/withdraw", headers=TOKEN)
    assert _stored(conn, jid)["application_state"] == "withdrawn"
    assert jid in _page_ids(_client(cfg), "/closed")


def test_withdraw_is_refused_from_the_shortlist(conn):
    jid = _job(conn, review_state="kept")
    assert stage_moves.withdraw(conn, jid) is False
    assert _stored(conn, jid)["application_state"] == ""


@pytest.mark.parametrize(("applied_at", "lands"), [("2026-09-01", "applied"), ("", "")])
def test_reopen_returns_to_applied_or_shortlist(cfg, conn, applied_at, lands):
    jid = _job(conn, review_state="kept", application_state="rejected", applied_at=applied_at)
    _client(cfg).post(f"/jobs/{jid}/stage/reopen", headers=TOKEN)
    assert _stored(conn, jid)["application_state"] == lands


def test_reopen_out_of_posting_closed_pins_the_verdict(conn):
    jid = _job(
        conn, review_state="kept", application_state="posting_closed", posting_check="closed",
        posting_closed_at="2026-09-10T08:00:00",
    )  # fmt: skip
    assert stage_moves.reopen(conn, jid) is True
    row = _stored(conn, jid)
    assert row["application_state"] == ""
    assert row["posting_check_manual"] == 1
    assert row["posting_closed_at"] == ""
    # Reopening says the posting is live: no longer badged closed or sunk.
    assert row["posting_check"] == "verified_open"
    # The next closure pass respects the pin and does not close it again.
    now = datetime.datetime.now()
    assert closure.close_transition(conn, jid, now=now, auto=True) is False
    assert _stored(conn, jid)["application_state"] == ""


@pytest.mark.parametrize(
    ("cols", "lands"),
    [
        ({"alignment_score": 78}, "surfaced"),
        ({"alignment_score": 90}, "surfaced"),
        ({"alignment_score": 73}, "queued"),
        ({"alignment_score": 77}, "queued"),
        ({"alignment_score": 72}, "new"),
        ({}, "queued"),
        ({"alignment_score": 95, "source_state": "stale"}, "new"),
    ],
)
def test_remove_from_shortlist_lands_where_the_gate_would(cfg, conn, cols, lands):
    jid = _job(conn, review_state="kept", build_state="tailored", **cols)
    r = _client(cfg).post(f"/jobs/{jid}/unkeep", data={"from_list": "1"}, headers=TOKEN)
    assert r.status_code == 200 and r.text == ""
    row = _stored(conn, jid)
    assert row["review_state"] == lands
    assert row["build_state"] == "none"


@pytest.mark.parametrize("app_state", ["applied", "screen", "offer", "rejected", "filled"])
def test_remove_is_refused_once_applied_or_closed(cfg, conn, app_state):
    jid = _job(
        conn, review_state="kept", application_state=app_state, alignment_score=90,
        build_state="tailored",
    )  # fmt: skip
    r = _client(cfg).post(f"/jobs/{jid}/unkeep", data={"from_list": "1"}, headers=TOKEN)
    assert r.headers.get("HX-Refresh") == "true"
    row = _stored(conn, jid)
    assert (row["review_state"], row["application_state"], row["build_state"]) == (
        "kept",
        app_state,
        "tailored",
    )


def test_threshold_band_comes_from_the_gate():
    from blastjob.matching.gate import BAND

    at = 80
    assert stages.unkeep_target(source_state="active", alignment_score=at - BAND, threshold=at) == (
        "queued"
    )
    assert (
        stages.unkeep_target(source_state="active", alignment_score=at - BAND - 1, threshold=at)
        == "new"
    )


def test_stage_changed_at_stamped_only_on_a_real_change(conn):
    jid = _job(conn, review_state="kept", application_state="screen", stage_changed_at="old")
    service.set_application_state(conn, jid, "screen", ["screen"])
    assert _stored(conn, jid)["stage_changed_at"] == "old"
    service.set_application_state(conn, jid, "interview", ["screen"])
    assert _stored(conn, jid)["stage_changed_at"] != "old"


def test_closure_lifecycle_moves_stamp_stage_changed_at(conn):
    now = datetime.datetime(2026, 9, 13, 7, 30, 0)
    closing = _job(conn, review_state="kept", posting_check="closed")
    assert closure.close_transition(conn, closing, now=now, auto=True) is True
    assert _stored(conn, closing)["stage_changed_at"] == "2026-09-13T07:30:00"

    conn.execute("UPDATE jobs SET stage_changed_at='' WHERE id=?", (closing,))
    assert closure.reopen_transition(conn, closing, auto=True) is True
    assert _stored(conn, closing)["application_state"] == ""
    assert _stored(conn, closing)["stage_changed_at"] != ""

    filled = _job(
        conn, review_state="kept", application_state="applied", posting_check="closed",
        posting_closed_at="2026-08-01T00:00:00",
    )  # fmt: skip
    assert closure.sweep_filled(conn, now=now, auto=True) == 1
    row = _stored(conn, filled)
    assert (row["application_state"], row["stage_changed_at"]) == ("filled", "2026-09-13T07:30:00")


# ----- HTMX + pages ---------------------------------------------------------------------


def test_list_row_actions_remove_the_row(cfg, conn):
    surfaced = _job(conn, review_state="surfaced", alignment_score=90)
    queued = _job(conn, review_state="queued", alignment_score=75)
    client = _client(cfg)
    page = client.get("/").text
    assert (
        f'hx-post="/jobs/{surfaced}/keep" hx-target="#job-{surfaced}" hx-swap="outerHTML"' in page
    )
    assert '"from_list": "1"' in page

    keep = client.post(f"/jobs/{surfaced}/keep", data={"from_list": "1"}, headers=TOKEN)
    archive = client.post(f"/jobs/{queued}/archive", data={"from_list": "1"}, headers=TOKEN)
    assert (keep.status_code, keep.text, archive.status_code, archive.text) == (200, "", 200, "")
    assert surfaced not in _page_ids(client, "/")
    assert surfaced in _page_ids(client, "/shortlist")
    assert queued not in _page_ids(client, "/queue")


def test_shortlist_row_offers_its_stage_actions(cfg, conn):
    jid = _job(conn, review_state="kept", alignment_score=90)
    applied = _job(conn, review_state="kept", application_state="screen")
    closed_ = _job(conn, review_state="kept", application_state="rejected")
    client = _client(cfg)
    shortlist = client.get("/shortlist").text
    assert f"/jobs/{jid}/stage/apply" in shortlist and f"/jobs/{jid}/unkeep" in shortlist
    applied_page = client.get("/applied").text
    assert f"/jobs/{applied}/stage/advance" in applied_page and "To interview" in applied_page
    assert f"/jobs/{applied}/stage/withdraw" in applied_page
    assert f"/jobs/{applied}/unkeep" not in applied_page
    assert f"/jobs/{closed_}/stage/reopen" in client.get("/closed").text


def test_job_detail_flows_still_render(cfg, conn):
    jid = _job(conn, review_state="kept", application_state="applied", alignment_score=90)
    client = _client(cfg)
    page = client.get(f"/jobs/{jid}?source_view=applied").text
    assert 'id="app-status"' in page
    assert f"/jobs/{jid}/unkeep" not in page  # Remove is a Shortlist action only
    r = client.post(f"/jobs/{jid}/status", data={"status": "screen"}, headers=TOKEN)
    assert r.status_code == 200 and 'id="app-status"' in r.text
    shortlisted = _job(conn, review_state="kept", alignment_score=90)
    assert f"/jobs/{shortlisted}/unkeep" in client.get(f"/jobs/{shortlisted}").text
    # Without the list flag the row action still answers with the row.
    r = client.post(f"/jobs/{shortlisted}/unkeep", headers=TOKEN)
    assert r.status_code == 200 and f'id="job-{shortlisted}"' in r.text


def test_unknown_stage_action_is_rejected(cfg, conn):
    jid = _job(conn, review_state="kept")
    assert _client(cfg).post(f"/jobs/{jid}/stage/delete", headers=TOKEN).status_code == 400


@pytest.mark.parametrize(("old", "new"), [("/kept", "/shortlist"), ("/followups", "/applied")])
def test_old_routes_redirect(cfg, old, new):
    r = _client(cfg).get(old, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == new


def test_no_template_links_to_removed_routes():
    templates = Path(stages.__file__).parent / "templates"
    for path in templates.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert 'href="/kept' not in text and 'href="/followups' not in text, path.name


def test_nav_has_five_stage_tabs_with_counts_and_a_more_menu(cfg, conn):
    _job(conn, review_state="kept", application_state="offer")
    html = _client(cfg).get("/settings").text
    tabs = re.findall(r'class="stage-tab[^"]*"[^>]*>(\w+) <b>(\d+)</b>', html)
    assert tabs == [
        ("Surfaced", "0"),
        ("Queue", "0"),
        ("Shortlist", "0"),
        ("Applied", "1"),
        ("Closed", "0"),
    ]
    more = html[html.index('<details class="more-menu">') :]
    for label in ("Browse", "Track", "Research", "Setup", "Matches", "Packets", "Settings"):
        assert label in more
    assert "Find + Score" in html and "Re-score" in html


def test_matches_is_undecided_only(cfg, conn):
    new = _job(conn, alignment_score=70)
    kept = _job(conn, review_state="kept", alignment_score=95)
    archived = _job(conn, review_state="archived", alignment_score=95)
    ids = _page_ids(_client(cfg), "/matches")
    assert new in ids and kept not in ids and archived not in ids


def test_all_page_names_the_stage(cfg, conn):
    _job(conn, review_state="kept", application_state="screen", alignment_score=90)
    _job(conn, review_state="kept", alignment_score=80)
    html = _client(cfg).get("/all").text
    assert "in a hiring process (screen)" in html
    assert "on your shortlist" in html
