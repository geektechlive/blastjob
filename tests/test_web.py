"""Web route tests via FastAPI TestClient.

Config is injected to point at a temp data dir, so tests never touch ~/.blastjob
or the real config file. CSRF, keep/archive CAS, and rendering are exercised.
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import service, stage_lists
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN


@pytest.fixture
def client_cfg():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg.discovery.title_drop_terms = ["engineer"]
    saved: dict = {}

    def load():
        return cfg

    def save(c):
        saved["cfg"] = c

    conn = dbc.init_db(cfg)
    # seed one surfaced + one queued job
    s = dbc.upsert_job(conn, source="t", external_id="1", company="Acme", title="VP Ops")
    q = dbc.upsert_job(conn, source="t", external_id="2", company="Beta", title="Director Ops")
    conn.execute("UPDATE jobs SET review_state='surfaced', alignment_score=90 WHERE id=?", (s,))
    conn.execute("UPDATE jobs SET review_state='queued', alignment_score=82 WHERE id=?", (q,))
    conn.commit()
    conn.close()

    app = create_app(load_config=load, save_config=save)
    return TestClient(app), cfg, saved, {"surfaced": s, "queued": q}


def _tok():
    return {CSRF_HEADER: LAUNCH_TOKEN}


def test_dashboard_renders_surfaced(client_cfg):
    client, cfg, _, ids = client_cfg
    r = client.get("/")
    assert r.status_code == 200
    assert "VP Ops" in r.text
    assert "90% fit" in r.text


def test_queue_renders_queued(client_cfg):
    client, *_ = client_cfg
    r = client.get("/queue")
    assert r.status_code == 200 and "Director Ops" in r.text


def test_detail_and_404(client_cfg):
    client, cfg, _, ids = client_cfg
    assert client.get(f"/jobs/{ids['surfaced']}").status_code == 200
    assert client.get("/jobs/99999").status_code == 404


def test_keep_requires_csrf(client_cfg):
    client, cfg, _, ids = client_cfg
    # no token -> 403
    assert client.post(f"/jobs/{ids['surfaced']}/keep").status_code == 403


def test_keep_with_token_transitions(client_cfg):
    client, cfg, _, ids = client_cfg
    r = client.post(f"/jobs/{ids['surfaced']}/keep", headers=_tok())
    assert r.status_code == 200
    assert "Mark applied" in r.text
    conn = dbc.init_db(cfg)
    state = conn.execute("SELECT review_state FROM jobs WHERE id=?", (ids["surfaced"],)).fetchone()[
        "review_state"
    ]
    conn.close()
    assert state == "kept"


def test_archive_with_token(client_cfg):
    client, cfg, _, ids = client_cfg
    r = client.post(f"/jobs/{ids['queued']}/archive", headers=_tok())
    assert r.status_code == 200 and "archived" in r.text.lower()


def test_bad_origin_rejected(client_cfg):
    client, cfg, _, ids = client_cfg
    r = client.post(
        f"/jobs/{ids['surfaced']}/keep",
        headers={**_tok(), "Origin": "http://evil.example.com"},
    )
    assert r.status_code == 403


def test_settings_shows_which_llm_provider_is_active(client_cfg):
    """Which model writes the resumes is not visible anywhere else, and the
    answer changes with the environment (API key present vs. the claude CLI).
    `llm.providers.active_model` existed and was tested but had no route --
    "web is the only surface"."""
    client, _cfg, _saved, _ = client_cfg
    body = client.get("/settings").text
    assert "Active model" in body
    # one of the three shapes active_model() can return
    assert any(s in body for s in ("anthropic /", "openai /", "claude-cli", "no provider"))


def test_settings_get_and_save(client_cfg):
    client, cfg, saved, _ = client_cfg
    assert client.get("/settings").status_code == 200
    r = client.post(
        "/settings",
        data={
            "_token": LAUNCH_TOKEN,
            "comp_floor_usd": 225000,
            "alignment_threshold": 88,
            "commute_radius_miles": 40,
            "remote_ok": "true",
            "exclude_companies": "Acme Staffing, Globex Corp",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert saved["cfg"].discovery.comp_floor_usd == 225000
    assert saved["cfg"].discovery.alignment_threshold == 88
    assert "Globex Corp" in saved["cfg"].discovery.exclude_companies


def test_matches_shows_below_bar_scored_jobs(client_cfg):
    client, cfg, _, ids = client_cfg
    # add a below-bar (new) scored job — invisible on Surfaced, visible on Matches
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn, source="t", external_id="9", company="Delta", title="Operations Director"
    )
    conn.execute("UPDATE jobs SET review_state='new', alignment_score=74 WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    r = client.get("/matches")
    assert r.status_code == 200
    assert "Operations Director" in r.text and "74% fit" in r.text
    # and it's keepable straight from the matches list
    kr = client.post(f"/jobs/{jid}/keep", headers=_tok())
    assert kr.status_code == 200 and "Mark applied" in kr.text


def test_all_view_explains_every_active_job(client_cfg):
    client, cfg, _, ids = client_cfg
    # add a filtered (IC-family, unscored) job — must still appear on /all with a reason
    conn = dbc.init_db(cfg)
    dbc.upsert_job(
        conn,
        source="t",
        external_id="9",
        company="Eps",
        title="Software Engineer",
        raw_jd="Write code.",
        geo_remote=1,
    )
    conn.close()
    r = client.get("/all")
    assert r.status_code == 200
    assert "Software Engineer" in r.text
    assert "different role type" in r.text  # the prefilter reason is shown
    assert "VP Ops" in r.text  # the surfaced one is here too


def test_health(client_cfg):
    client, *_ = client_cfg
    assert client.get("/health").json() == {"ok": True}


# ----- application status tracking -----


def _kept_job(client, cfg, ids):
    """Keep the surfaced job and return its id (application tracking only applies
    to kept jobs)."""
    jid = ids["surfaced"]
    client.post(f"/jobs/{jid}/keep", headers=_tok())
    return jid


def test_set_status_requires_csrf(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    assert client.post(f"/jobs/{jid}/status", data={"status": "applied"}).status_code == 403


def test_set_status_rejects_invalid(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    r = client.post(f"/jobs/{jid}/status", headers=_tok(), data={"status": "bogus"})
    assert r.status_code == 400


def test_set_status_transitions_and_stamps_applied_at(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    r = client.post(f"/jobs/{jid}/status", headers=_tok(), data={"status": "applied"})
    assert r.status_code == 200 and "applied" in r.text
    conn = dbc.init_db(cfg)
    row = conn.execute(
        "SELECT application_state, applied_at FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    conn.close()
    assert row["application_state"] == "applied"
    assert row["applied_at"]  # stamped, non-empty


def test_set_status_only_applies_to_kept(client_cfg):
    client, cfg, _, ids = client_cfg
    # queued (not kept) job — should not accept an application status
    r = client.post(f"/jobs/{ids['queued']}/status", headers=_tok(), data={"status": "applied"})
    conn = dbc.init_db(cfg)
    state = conn.execute(
        "SELECT application_state FROM jobs WHERE id=?", (ids["queued"],)
    ).fetchone()["application_state"]
    conn.close()
    assert r.status_code == 200  # route renders, but nothing was written
    assert state == ""


def _stored_notes(cfg, jid):
    conn = dbc.init_db(cfg)
    try:
        return conn.execute("SELECT app_notes FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    finally:
        conn.close()


def _post_notes(client, jid, notes, loaded):
    return client.post(
        f"/jobs/{jid}/notes",
        headers=_tok(),
        data={"notes": notes, "notes_version": service.notes_version(loaded)},
    )


def test_set_notes(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    r = _post_notes(client, jid, "referral via Jane", loaded="")  # empty initial save
    assert r.status_code == 200 and "referral via Jane" in r.text
    assert _stored_notes(cfg, jid) == "referral via Jane"
    assert service.notes_version("referral via Jane") in r.text  # next save is fresh


def test_set_notes_fresh_second_save_works(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    _post_notes(client, jid, "one", loaded="")
    r = _post_notes(client, jid, "two", loaded="one")
    assert r.status_code == 200 and _stored_notes(cfg, jid) == "two"


def test_set_notes_stale_save_is_refused_and_shows_both_texts(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    # The page loaded with empty notes, then a merge appended a record.
    conn = dbc.init_db(cfg)
    conn.execute("UPDATE jobs SET app_notes='Merged #9 as a duplicate' WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    r = _post_notes(client, jid, "my unsaved thoughts", loaded="")
    assert r.status_code == 200
    assert _stored_notes(cfg, jid) == "Merged #9 as a duplicate"  # untouched
    assert "Merged #9 as a duplicate" in r.text  # current stored text
    assert "my unsaved thoughts" in r.text  # the user's text is kept
    assert "changed since" in r.text  # plain error
    # Re-saving from the conflict form (now based on the current text) works.
    r2 = _post_notes(client, jid, "my unsaved thoughts", loaded="Merged #9 as a duplicate")
    assert r2.status_code == 200 and _stored_notes(cfg, jid) == "my unsaved thoughts"


def test_set_notes_without_version_is_refused(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    r = client.post(f"/jobs/{jid}/notes", headers=_tok(), data={"notes": "blind"})
    assert r.status_code == 200 and _stored_notes(cfg, jid) in ("", None)


def test_pipeline_board_places_applied_job(client_cfg):
    from blastjob.web import stage_lists

    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    client.post(f"/jobs/{jid}/status", headers=_tok(), data={"status": "interview"})
    conn = dbc.init_db(cfg)
    board = stage_lists.pipeline_board(conn)
    conn.close()
    assert any(r["id"] == jid for r in board["interview"])
    assert all(r["id"] != jid for r in board["none"])


# ----- follow-up reminders -----


def _apply_days_ago(cfg, jid, days):
    import datetime

    when = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = dbc.init_db(cfg)
    conn.execute(
        "UPDATE jobs SET application_state='applied', applied_at=? WHERE id=?", (when, jid)
    )
    conn.commit()
    conn.close()


def test_stale_application_appears_in_followups(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    _apply_days_ago(cfg, jid, 10)  # applied 10 days ago, no update -> nudge
    r = client.get("/followups")
    assert r.status_code == 200
    assert "VP Ops" in r.text
    assert "10 days ago" in r.text


def test_fresh_application_not_flagged_for_followup(client_cfg):
    """Follow-ups moved onto the Applied page: a fresh application is listed
    there (it is in a hiring process) but carries no follow-up flag."""
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    _apply_days_ago(cfg, jid, 2)  # applied recently -> not yet due
    r = client.get("/followups")
    assert r.status_code == 200 and "VP Ops" in r.text
    assert "followup-flag" not in r.text and "follow-up due" not in r.text


def test_explicit_reminder_due_today_nudges(client_cfg):
    import datetime

    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    _apply_days_ago(cfg, jid, 1)  # fresh, but an explicit reminder is set
    today = datetime.date.today().isoformat()
    client.post(f"/jobs/{jid}/followup", headers=_tok(), data={"next_action_due": today})
    r = client.get("/followups")
    assert r.status_code == 200 and "VP Ops" in r.text
    assert f"follow-up due {today}" in r.text


def test_followup_rejects_bad_date(client_cfg):
    from blastjob.web import service

    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    conn = dbc.init_db(cfg)
    assert service.set_next_action_due(conn, jid, "not-a-date") is False
    conn.close()


def test_archive_records_reason_and_note(client_cfg):
    client, cfg, _, ids = client_cfg
    r = client.post(
        f"/jobs/{ids['queued']}/archive",
        data={"reason": "level", "note": "IC role dressed as director"},
        headers=_tok(),
    )
    assert r.status_code == 200 and "wrong level" in r.text
    conn = dbc.init_db(cfg)
    row = conn.execute(
        "SELECT review_state, archive_reason, archive_note FROM jobs WHERE id=?", (ids["queued"],)
    ).fetchone()
    conn.close()
    assert (row["review_state"], row["archive_reason"]) == ("archived", "level")
    assert row["archive_note"] == "IC role dressed as director"


def test_archive_rejects_unknown_reason(client_cfg):
    client, _, _, ids = client_cfg
    r = client.post(f"/jobs/{ids['queued']}/archive", data={"reason": "vibes"}, headers=_tok())
    assert r.status_code == 400


def test_job_row_offers_reason_select(client_cfg):
    client, _, _, _ = client_cfg
    r = client.get("/")
    assert 'name="reason"' in r.text and "never this company" in r.text


# ----- interview prep -----


def test_prep_requires_csrf(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    assert client.post(f"/jobs/{jid}/prep").status_code == 403


def test_prep_starts_run_and_returns_panel(client_cfg, monkeypatch):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)

    def fake_make_prep_work(job_id, load_config):
        async def work(run):
            run.emit("log", "stub")
            return {"path": "x"}

        return work

    monkeypatch.setattr("blastjob.web.prep.make_prep_work", fake_make_prep_work)
    r = client.post(f"/jobs/{jid}/prep", headers=_tok())
    assert r.status_code == 200
    assert "Interview prep" in r.text


def test_detail_shows_prep_button_only_when_kept(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    r = client.get(f"/jobs/{jid}")
    assert r.status_code == 200
    assert f'hx-post="/jobs/{jid}/prep"' in r.text

    queued_id = ids["queued"]
    r2 = client.get(f"/jobs/{queued_id}")
    assert r2.status_code == 200
    assert f'hx-post="/jobs/{queued_id}/prep"' not in r2.text


# ----- compare view -----


def test_compare_lists_surfaced_and_filters_state(client_cfg):
    client, *_ = client_cfg
    r = client.get("/compare")
    assert r.status_code == 200
    assert "VP Ops" in r.text

    r2 = client.get("/compare?state=queued")
    assert r2.status_code == 200
    assert "Director Ops" in r2.text and "VP Ops" not in r2.text

    r3 = client.get("/compare?state=bogus")
    assert r3.status_code == 200


def test_nav_has_compare_link(client_cfg):
    client, *_ = client_cfg
    r = client.get("/")
    assert 'href="/compare"' in r.text


# ----- cross-source duplicate collapse + agency flag -----


def test_dashboard_collapses_clustered_duplicate(client_cfg):
    client, cfg, _, ids = client_cfg
    conn = dbc.init_db(cfg)
    cur = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) "
        "VALUES ('acme|vp ops|', datetime('now'))"
    )
    cluster_id = cur.lastrowid
    other = dbc.upsert_job(conn, source="t2", external_id="1", company="Acme", title="VP Ops")
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=70, cluster_id=? WHERE id=?",
        (cluster_id, other),
    )
    conn.execute("UPDATE jobs SET cluster_id=? WHERE id=?", (cluster_id, ids["surfaced"]))
    conn.commit()
    conn.close()
    r = client.get("/")
    assert r.status_code == 200
    assert r.text.count("VP Ops") == 1
    assert "+1 sources" in r.text


def test_dashboard_flags_staffing_agency(client_cfg):
    client, cfg, _, ids = client_cfg
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id="agency-1",
        company="Crestview Staffing",
        title="Operations Manager",
    )
    conn.execute("UPDATE jobs SET review_state='surfaced', alignment_score=80 WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    r = client.get("/")
    assert r.status_code == 200
    assert "staffing agency" in r.text


def test_kept_still_renders_after_collapse_change(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    r = client.get("/shortlist")
    assert r.status_code == 200
    assert "VP Ops" in r.text
    assert client.get(f"/jobs/{jid}").status_code == 200


# --- run-status banner follows the user across pages -------------------------


def test_runs_active_empty_when_nothing_running(client_cfg):
    client, *_ = client_cfg
    r = client.get("/runs/active")
    assert r.status_code == 200
    assert "run-status" not in r.text


def test_runs_active_shows_running_discovery(client_cfg, monkeypatch):
    from blastjob.web import runtime as rt

    client, *_ = client_cfg
    monkeypatch.setattr(rt, "RUNTIME", rt.Runtime())
    monkeypatch.setattr("blastjob.web.app.RUNTIME", rt.RUNTIME)
    run = rt.Run(id="discovery-9-abc", name="discovery")
    run.emit("log", "Discovering jobs...")
    rt.RUNTIME._runs[run.id] = run

    r = client.get("/runs/active")
    assert r.status_code == 200
    assert "run-running" in r.text and "discovery" in r.text
    assert 'hx-get="/runs/discovery-9-abc"' in r.text


def test_runs_active_shows_recently_finished_then_hides_old(client_cfg, monkeypatch):
    from blastjob.web import runtime as rt

    client, *_ = client_cfg
    monkeypatch.setattr(rt, "RUNTIME", rt.Runtime())
    monkeypatch.setattr("blastjob.web.app.RUNTIME", rt.RUNTIME)
    run = rt.Run(id="matching-3-def", name="matching", status="done")
    run.finished_at = rt.time.monotonic() - 10
    rt.RUNTIME._runs[run.id] = run
    assert "run-done" in client.get("/runs/active").text

    run.finished_at = rt.time.monotonic() - 10_000
    assert "run-status" not in client.get("/runs/active").text


def test_runs_active_ignores_per_job_runs(client_cfg, monkeypatch):
    """Tailor/prep/research runs stream on their own job page; the global
    banner is only for discovery + matching."""
    from blastjob.web import runtime as rt

    client, *_ = client_cfg
    monkeypatch.setattr(rt, "RUNTIME", rt.Runtime())
    monkeypatch.setattr("blastjob.web.app.RUNTIME", rt.RUNTIME)
    rt.RUNTIME._runs["tailor-1-x"] = rt.Run(id="tailor-1-x", name="tailor")
    assert "run-status" not in client.get("/runs/active").text


def test_every_page_asks_for_active_run(client_cfg):
    client, *_ = client_cfg
    for path in ("/", "/queue", "/shortlist", "/applied", "/closed", "/pipeline"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'hx-get="/runs/active"' in r.text, path


def test_runtime_active_prefers_running_over_finished():
    from blastjob.web import runtime as rt

    reg = rt.Runtime()
    done = rt.Run(id="discovery-1-a", name="discovery", status="done")
    done.finished_at = rt.time.monotonic()
    running = rt.Run(id="matching-2-b", name="matching")
    reg._runs[done.id] = done
    reg._runs[running.id] = running
    assert reg.active(("discovery", "matching")) is running


def test_run_driver_stamps_finished_at():
    import asyncio

    from blastjob.web import runtime as rt

    async def go():
        reg = rt.Runtime()

        async def work(run):
            return 1

        run = reg.start("discovery", work)
        assert run.finished_at is None
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return run

    run = asyncio.run(go())
    assert run.status == "done" and run.finished_at is not None


# ----- a cluster has ONE review state: the most user-decided one --------------
#
# Regression (2026-09-07): a kept Adzuna posting went stale while the same role
# stayed live on the company's Workday board as a separate row in the same
# cluster. The list queries filter source_state='active' BEFORE collapsing, so
# the kept member never reached the collapser and the twin rendered with Keep /
# Archive buttons -- a role the user had already kept looked unkept.


def _new_cluster(conn, fingerprint: str) -> int:
    cur = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES (?, datetime('now'))",
        (fingerprint,),
    )
    return int(cur.lastrowid)


def test_kept_cluster_member_wins_over_active_sibling(client_cfg):
    """The exact live shape: kept + stale (adzuna) clustered with new + active
    (workday). The cluster is decided: it is on the Shortlist (via the kept
    row) and not on /matches, which is undecided roles only."""
    client, cfg, _, _ = client_cfg
    conn = dbc.init_db(cfg)
    cid = _new_cluster(conn, "oakmont|director ops|")
    title = "Director Ops Manufacturing"
    kept = dbc.upsert_job(conn, source="adzuna", external_id="g1", company="Oakmont", title=title)
    twin = dbc.upsert_job(
        conn, source="workday:oakmont", external_id="g2", company="Oakmont", title=title
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', source_state='stale', alignment_score=78, "
        "cluster_id=? WHERE id=?",
        (cid, kept),
    )
    conn.execute(
        "UPDATE jobs SET review_state='new', alignment_score=66, cluster_id=? WHERE id=?",
        (cid, twin),
    )
    conn.commit()
    conn.close()

    r = client.get("/matches")
    assert r.status_code == 200
    assert title not in r.text
    assert f"/jobs/{twin}/keep" not in r.text
    shortlist = client.get("/shortlist").text
    assert shortlist.count(title) == 1
    assert f'id="job-{kept}"' in shortlist
    assert f'id="job-{twin}"' not in shortlist


def test_archived_cluster_member_hides_cluster_from_matches(client_cfg):
    client, cfg, _, _ = client_cfg
    conn = dbc.init_db(cfg)
    cid = _new_cluster(conn, "nope|vp ops|")
    gone = dbc.upsert_job(conn, source="a", external_id="n1", company="Nope", title="VP Infra")
    live = dbc.upsert_job(conn, source="b", external_id="n2", company="Nope", title="VP Infra")
    conn.execute(
        "UPDATE jobs SET review_state='archived', archive_reason='company', alignment_score=88, "
        "cluster_id=? WHERE id=?",
        (cid, gone),
    )
    conn.execute(
        "UPDATE jobs SET review_state='new', alignment_score=90, cluster_id=? WHERE id=?",
        (cid, live),
    )
    conn.commit()
    conn.close()

    r = client.get("/matches")
    assert r.status_code == 200
    assert "VP Infra" not in r.text


def test_kept_cluster_row_survives_null_score(client_cfg):
    """The Northwind shape: the kept record is a manual row with no alignment
    score. The cluster is decided, so it is not on /matches; on Applied it is
    represented by the row carrying the progress, whatever the scores."""
    client, cfg, _, _ = client_cfg
    conn = dbc.init_db(cfg)
    cid = _new_cluster(conn, "northwind|generic vp role|")
    manual = dbc.upsert_job(
        conn,
        source="manual",
        external_id="l1",
        company="Northwind CU",
        title="Generic VP Role",
    )
    ats = dbc.upsert_job(
        conn,
        source="dayforce:x",
        external_id="l2",
        company="Northwind CU",
        title="Generic VP Role",
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='screen', alignment_score=NULL, "
        "cluster_id=? WHERE id=?",
        (cid, manual),
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', alignment_score=90, cluster_id=? WHERE id=?",
        (cid, ats),
    )
    conn.commit()
    conn.close()

    r = client.get("/matches")
    assert r.status_code == 200
    assert "Generic VP Role" not in r.text
    applied = client.get("/applied").text
    assert applied.count("Generic VP Role") == 1
    assert f'id="job-{manual}"' in applied
    assert f'id="job-{ats}"' not in applied
    assert "Generic VP Role" not in client.get("/shortlist").text


def test_dupes_badge_counts_members_excluded_by_query(client_cfg):
    """ "+N sources" is about the real world, not about what the query selected."""
    client, cfg, _, _ = client_cfg
    conn = dbc.init_db(cfg)
    cid = _new_cluster(conn, "trio|chief officer|")
    ids_ = [
        dbc.upsert_job(
            conn, source=f"s{i}", external_id=f"t{i}", company="Trio", title="Chief Officer"
        )
        for i in range(3)
    ]
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=80, cluster_id=? WHERE id=?",
        (cid, ids_[0]),
    )
    # Excluded by the query: one stale, one unscored.
    conn.execute(
        "UPDATE jobs SET review_state='new', source_state='stale', cluster_id=? WHERE id=?",
        (cid, ids_[1]),
    )
    conn.execute(
        "UPDATE jobs SET review_state='new', alignment_score=NULL, cluster_id=? WHERE id=?",
        (cid, ids_[2]),
    )
    conn.commit()
    conn.close()

    r = client.get("/matches")
    assert r.status_code == 200
    assert "+2 sources" in r.text


def test_collapse_clusters_unchanged_without_overrides(client_cfg):
    """The default path must behave exactly as before (guards every caller
    that has not opted in)."""
    from blastjob.web import service

    client, cfg, _, _ = client_cfg
    conn = dbc.init_db(cfg)
    cid = _new_cluster(conn, "plain|dir|")
    a = dbc.upsert_job(conn, source="a", external_id="p1", company="Plain", title="Dir Ops")
    b = dbc.upsert_job(conn, source="b", external_id="p2", company="Plain", title="Dir Ops")
    conn.execute("UPDATE jobs SET alignment_score=70, cluster_id=? WHERE id=?", (cid, a))
    conn.execute("UPDATE jobs SET alignment_score=90, cluster_id=? WHERE id=?", (cid, b))
    conn.commit()
    rows = conn.execute(
        f"SELECT {service._COLS} FROM jobs WHERE cluster_id=? ORDER BY alignment_score DESC",
        (cid,),
    ).fetchall()
    out = service.collapse_clusters(rows)
    conn.close()

    assert [r["id"] for r in out] == [b]
    assert out[0]["dupes"] == 1


def test_surfaced_and_queued_respect_cluster_kept_state(client_cfg):
    """A cluster the user already kept is decided; it belongs on /shortlist, not in
    the review lists."""
    client, cfg, _, _ = client_cfg
    conn = dbc.init_db(cfg)
    cid = _new_cluster(conn, "decided|vp eng|")
    kept = dbc.upsert_job(conn, source="a", external_id="d1", company="Decided", title="VP Eng")
    surf = dbc.upsert_job(conn, source="b", external_id="d2", company="Decided", title="VP Eng")
    quo = dbc.upsert_job(conn, source="c", external_id="d3", company="Decided", title="VP Eng")
    conn.execute(
        "UPDATE jobs SET review_state='kept', alignment_score=70, cluster_id=? WHERE id=?",
        (cid, kept),
    )
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=88, cluster_id=? WHERE id=?",
        (cid, surf),
    )
    conn.execute(
        "UPDATE jobs SET review_state='queued', alignment_score=72, cluster_id=? WHERE id=?",
        (cid, quo),
    )
    conn.commit()
    conn.close()

    assert "VP Eng" not in client.get("/").text
    assert "VP Eng" not in client.get("/queue").text
    assert "VP Eng" in client.get("/shortlist").text


# ----- decided (kept) lists collapse a cluster too ---------------------------
#
# Regression (2026-09-07, from a real screenshot): jobs 667 and 691 are BOTH
# kept Northwind "Generic VP Role" rows in one cluster, differing only in
# application_state ('screen' vs empty). pipeline_board buckets by stage, so one
# real job drew two cards in two different columns. /kept, /followups and
# /compare shared the same gap: they never collapsed at all.


def _kept_cluster_pair(cfg):
    """The live shape: two kept members of one cluster, one carrying the
    application progress."""
    conn = dbc.init_db(cfg)
    cid = _new_cluster(conn, "northwind|generic vp role|")
    title = "Generic VP Role"
    tracked = dbc.upsert_job(
        conn, source="manual", external_id="k1", company="Northwind CU", title=title
    )
    bare = dbc.upsert_job(
        conn, source="dayforce:x", external_id="k2", company="Northwind CU", title=title
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='screen', "
        "next_action_due='2020-01-01', comp_total_est=250000, cluster_id=? WHERE id=?",
        (cid, tracked),
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', alignment_score=90, comp_total_est=260000, "
        "cluster_id=? WHERE id=?",
        (cid, bare),
    )
    conn.commit()
    conn.close()
    return title, tracked, bare


def test_pipeline_board_shows_one_card_per_cluster(client_cfg):
    client, cfg, _, _ = client_cfg
    title, tracked, bare = _kept_cluster_pair(cfg)

    r = client.get("/pipeline")
    assert r.status_code == 200
    assert r.text.count(title) == 1

    conn = dbc.init_db(cfg)
    board = stage_lists.pipeline_board(conn)
    conn.close()
    lanes = {lane: [row["id"] for row in rows] for lane, rows in board.items() if rows}
    # One card, and it is the row carrying the application progress.
    assert lanes == {"screen": [tracked]}, lanes
    assert bare not in [i for ids in lanes.values() for i in ids]


def test_kept_list_collapses_duplicate_kept_cluster(client_cfg):
    """The pair shows once, on Applied (the member at 'screen'), and its bare
    twin does not also sit on the Shortlist."""
    client, cfg, _, _ = client_cfg
    title, tracked, _ = _kept_cluster_pair(cfg)

    r = client.get("/applied")
    assert r.status_code == 200
    assert r.text.count(title) == 1
    assert f'id="job-{tracked}"' in r.text
    assert title not in client.get("/shortlist").text


def test_followups_collapse_duplicate_kept_cluster(client_cfg):
    client, cfg, _, _ = client_cfg
    title, tracked, _ = _kept_cluster_pair(cfg)

    conn = dbc.init_db(cfg)
    # Both members due, so a naive query would list the role twice.
    conn.execute(
        "UPDATE jobs SET application_state='applied', applied_at='2020-01-01', "
        "next_action_due='2020-01-01' WHERE review_state='kept' AND application_state=''"
    )
    conn.commit()
    due = stage_lists.followups_due(conn)
    conn.close()
    assert [d["id"] for d in due] == [tracked]

    r = client.get("/followups")
    assert r.status_code == 200
    assert r.text.count(title) == 1


def test_compare_collapses_duplicate_kept_cluster(client_cfg):
    from blastjob.web import compare

    client, cfg, _, _ = client_cfg
    title, tracked, _ = _kept_cluster_pair(cfg)

    conn = dbc.init_db(cfg)
    rows = compare.compare_rows(conn, state="applied")
    conn.close()
    assert [r["id"] for r in rows] == [tracked]

    r = client.get("/compare?state=applied")
    assert r.status_code == 200
    assert r.text.count(title) == 1


def test_collapse_kept_prefers_progress_and_keeps_caller_order(client_cfg):
    """The winner is the member with the most application progress, wherever it
    sits in the caller's ordering; unclustered rows pass through untouched."""
    client, cfg, _, _ = client_cfg
    conn = dbc.init_db(cfg)
    cid = _new_cluster(conn, "prog|vp|")
    bare = dbc.upsert_job(conn, source="a", external_id="q1", company="Prog", title="VP")
    offer = dbc.upsert_job(conn, source="b", external_id="q2", company="Prog", title="VP")
    solo = dbc.upsert_job(conn, source="c", external_id="q3", company="Solo", title="Dir")
    conn.execute("UPDATE jobs SET review_state='kept', cluster_id=? WHERE id=?", (cid, bare))
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='offer', cluster_id=? WHERE id=?",
        (cid, offer),
    )
    conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (solo,))
    conn.commit()
    # bare comes FIRST in this ordering, but offer carries the progress.
    rows = conn.execute(f"SELECT {service._COLS} FROM jobs WHERE review_state='kept' ORDER BY id")
    out = stage_lists.collapse_kept(rows.fetchall())
    conn.close()
    assert [r["id"] for r in out] == [offer, solo]


# ----- job-board resellers are not employers ---------------------------------


def test_is_agency_flags_job_board_resellers():
    """Job-board resellers repost other companies' openings under their own
    name on LinkedIn. On 2026-09-10 they took 5 of the 6 surfaced slots,
    outranking real employers on score, and were not being flagged."""
    from blastjob.web.compare import is_agency

    for name in ("Lensa", "Riverside Staffing", "Keystone Recruiting", "Crestview Personnel"):
        assert is_agency(name) is True, name
    for name in ("Fabrikam Equipment", "Fabrikam Manufacturing", "Northwind Mutual", "Oakmont"):
        assert is_agency(name) is False, name


# ----- network: LinkedIn connections import ----------------------------------


def test_network_page_renders(client_cfg):
    client, *_ = client_cfg
    r = client.get("/network")
    assert r.status_code == 200
    assert "Network" in r.text


def test_network_import_requires_csrf(client_cfg):
    client, *_ = client_cfg
    csv_bytes = (
        b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        b"Jane,Doe,https://linkedin.com/in/example-janedoe,,Acme Corp,VP Engineering,05 Jun 2023\n"
    )
    r = client.post(
        "/network/import",
        files={"file": ("Connections.csv", csv_bytes, "text/csv")},
    )
    assert r.status_code == 403


def test_network_import_with_form_token_succeeds(client_cfg):
    client, *_ = client_cfg
    csv_bytes = (
        b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        b"Jane,Doe,https://linkedin.com/in/example-janedoe,,Acme Corp,VP Engineering,05 Jun 2023\n"
    )
    r = client.post(
        "/network/import",
        files={"file": ("Connections.csv", csv_bytes, "text/csv")},
        data={"_token": LAUNCH_TOKEN},
    )
    assert r.status_code == 200
    assert "1 added" in r.text


def test_keep_preserves_network_badge(client_cfg):
    client, cfg, _, ids = client_cfg
    conn = dbc.init_db(cfg)
    conn.execute(
        "UPDATE jobs SET company='Acme' WHERE id=?",
        (ids["surfaced"],),
    )
    conn.execute(
        "INSERT INTO network_contacts (full_name, url, company, company_key, position, "
        "seniority, is_recruiter, notes, imported_at) VALUES "
        "('Alice Exec', 'https://linkedin.com/in/example-aliceexec', 'Acme', 'acme', "
        "'VP Engineering', 'exec', 0, '', '2026-09-10')"
    )
    conn.commit()
    conn.close()

    r = client.post(f"/jobs/{ids['surfaced']}/keep", headers=_tok())

    assert r.status_code == 200
    assert "1 in your network" in r.text


# ----- LAN access (the owner's iPad, 2026-09-12) -----------------------------


def test_origin_check_allows_private_addresses_and_blocks_public():
    """Reaching the cockpit from another device on the LAN means the browser
    sends that device's view of the server as Origin. With a loopback-only
    allowlist every button 403s and the page is read-only. Public origins stay
    rejected, which is what stops a malicious page driving this by DNS
    rebinding: its Origin is its own domain, never a private address."""
    from blastjob.web.security import _host_ok

    for ok in (
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://192.168.20.179:8765",
        "http://10.0.0.5:8765",
        "http://172.16.0.9:8765",
    ):
        assert _host_ok(ok) is True, ok

    for bad in ("https://evil.example.com", "http://8.8.8.8", "http://203.0.113.7:8765", ""):
        assert _host_ok(bad) is False, bad


def test_keep_works_from_a_lan_origin(client_cfg):
    """The whole point: a click from the iPad must actually change state."""
    client, cfg, _, ids = client_cfg
    r = client.post(
        f"/jobs/{ids['surfaced']}/keep",
        headers={**_tok(), "Origin": "http://192.168.20.179:8765"},
    )
    assert r.status_code == 200
    conn = dbc.init_db(cfg)
    try:
        state = conn.execute(
            "select review_state from jobs where id=?", (ids["surfaced"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert state == "kept"


def test_keep_still_blocked_from_a_public_origin(client_cfg):
    client, cfg, _, ids = client_cfg
    r = client.post(
        f"/jobs/{ids['surfaced']}/keep",
        headers={**_tok(), "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_launch_host_flag_defaults_to_loopback_and_can_be_overridden():
    """Binding off loopback must be opt-in per launch, never the default."""
    from unittest.mock import patch

    from blastjob.web import launch

    def run_with(argv):
        with (
            patch("blastjob.web.launch.uvicorn.run") as served,
            patch.object(launch, "_recover_on_start"),
            patch.object(launch, "_start_browser_thread"),
            patch("blastjob.web.launch.create_app"),
        ):
            launch.main(argv)
        return served.call_args.kwargs["host"]

    assert run_with(["--no-browser"]) == "127.0.0.1"
    assert run_with(["--no-browser", "--host", "0.0.0.0"]) == "0.0.0.0"


def test_browser_never_opened_against_a_bind_address():
    """0.0.0.0 is a bind address, not somewhere a browser can go."""
    from unittest.mock import patch

    from blastjob.web import launch

    with (
        patch("blastjob.web.launch.webbrowser.open") as opened,
        patch("blastjob.web.launch.time.sleep"),
    ):
        launch._open_browser(8765, "0.0.0.0")
    opened.assert_called_once_with("http://127.0.0.1:8765/")


def test_keep_and_archive_404_on_a_missing_job(client_cfg):
    """They returned 500 — the CAS matched no row, job_detail returned None, and
    _job_row.html then dereferenced it. Found by an end-to-end LAN probe that
    used a nonexistent id deliberately, so it would not mutate real data."""
    client, *_ = client_cfg

    for path in ("/jobs/99999999/keep", "/jobs/99999999/archive"):
        r = client.post(path, headers=_tok())
        assert r.status_code == 404, f"{path} returned {r.status_code}"


def test_keep_on_a_missing_job_is_rejected_before_the_origin_check_matters(client_cfg):
    """A bad origin must still 403 rather than 404, so the guard cannot become a
    way to probe which job ids exist from an untrusted page."""
    client, *_ = client_cfg

    r = client.post(
        "/jobs/99999999/keep",
        headers={**_tok(), "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# UI wiring for the 2026-09-12 pass. Each new route gets its 403-without-token
# twin, following this file's existing convention.
# ---------------------------------------------------------------------------


def test_packet_route_renders_for_a_job_with_no_packet(client_cfg):
    client, _cfg, _saved, ids = client_cfg
    r = client.get(f"/jobs/{ids['surfaced']}/packet")
    assert r.status_code == 200
    assert "No packet yet" in r.text


def test_unkeep_returns_the_row_and_needs_a_token(client_cfg):
    client, _cfg, _saved, ids = client_cfg
    jid = ids["surfaced"]
    assert client.post(f"/jobs/{jid}/keep", headers=_tok()).status_code == 200
    assert client.post(f"/jobs/{jid}/unkeep").status_code == 403
    r = client.post(f"/jobs/{jid}/unkeep", headers=_tok())
    assert r.status_code == 200
    assert "btn-keep" in r.text  # back to a keepable row


def test_unarchive_clears_the_reason_and_needs_a_token(client_cfg):
    client, _cfg, _saved, ids = client_cfg
    jid = ids["queued"]
    client.post(f"/jobs/{jid}/archive", data={"reason": "level"}, headers=_tok())
    assert client.post(f"/jobs/{jid}/unarchive").status_code == 403
    r = client.post(f"/jobs/{jid}/unarchive", headers=_tok())
    assert r.status_code == 200
    assert "btn-keep" in r.text


def test_reveal_is_refused_for_a_non_local_peer(client_cfg):
    """The cockpit may be bound to the LAN and the Origin check accepts RFC1918, so only the
    peer address can tell this computer from another device. Finder must never open here
    for a remote viewer."""
    client, _cfg, _saved, ids = client_cfg
    r = client.post(
        f"/jobs/{ids['surfaced']}/reveal",
        headers={**_tok(), "Origin": "http://192.168.20.5:8765"},
    )
    assert r.status_code in (403, 404)


def test_network_desks_render(client_cfg):
    client, _cfg, _saved, _ids = client_cfg
    assert "Recruiter desk" in client.get("/network/recruiters").text
    assert "Dormant senior ties" in client.get("/network/dormant").text


def test_sort_toggle_is_accepted_on_surfaced_and_matches(client_cfg):
    client, _cfg, _saved, _ids = client_cfg
    for path in ("/?sort=comp", "/?sort=fit", "/matches?sort=comp"):
        assert client.get(path).status_code == 200


def test_fit_badge_follows_the_configured_threshold(client_cfg):
    """The badge used to hardcode 85 while the gate surfaced at the configurable
    threshold (78 by default), so every 78-84 role looked borderline.

    The score has to sit BETWEEN the two values or the test passes under either,
    which is how the first version of this test proved nothing.
    """
    client, cfg, _saved, ids = client_cfg
    from blastjob.db import client as dbc

    conn = dbc.init_db(cfg)
    conn.execute("UPDATE jobs SET alignment_score=80 WHERE id=?", (ids["surfaced"],))
    conn.commit()
    conn.close()

    cfg.discovery.alignment_threshold = 78
    assert "fit-hi" in client.get("/").text  # 80 >= 78: cleared the bar
    cfg.discovery.alignment_threshold = 85
    assert "fit-mid" in client.get("/").text  # 80 < 85: genuinely borderline


def test_funnel_page_accounts_for_every_job(client_cfg):
    """The counts must reconcile: a job sits in exactly one stage, so the stages
    other than `discovered` sum to the total. That invariant is the whole point
    of the page -- a funnel you cannot add up cannot tell you where the leak is."""
    client, cfg, _saved, _ids = client_cfg
    from blastjob.db import client as dbc
    from blastjob.web.funnel import STAGES, build_funnel

    conn = dbc.init_db(cfg)
    f = build_funnel(conn, cfg)
    placed = sum(f.counts.get(k, 0) for k, _ in STAGES if k != "discovered")
    assert placed == f.total
    conn.close()
    assert client.get("/funnel").status_code == 200


def test_dead_boards_flags_a_source_that_never_produces(client_cfg):
    """The check that would have caught the wrong-company board on day one."""
    client, cfg, _saved, _ids = client_cfg
    from blastjob.db import client as dbc
    from blastjob.web.funnel import dead_boards

    conn = dbc.init_db(cfg)
    for i in range(12):
        dbc.upsert_job(
            conn,
            source="greenhouse:wrongco",
            external_id=f"w{i}",
            company="Wrong Co",
            title="Boiler Technician II",
            raw_jd="hvac",
        )
        conn.execute(
            "UPDATE jobs SET alignment_score=4 WHERE source='greenhouse:wrongco' AND external_id=?",
            (f"w{i}",),
        )
    conn.commit()
    flagged = {b["source"] for b in dead_boards(conn, cfg)}
    assert "greenhouse:wrongco" in flagged
    conn.close()


def test_follow_seeds_excludes_configured_boards_and_seen_companies(client_cfg):
    """A followed company is only a candidate if it has produced no job AND is
    not already configured -- otherwise the list just restates what the user
    already has."""
    client, cfg, _saved, _ids = client_cfg
    from blastjob.db import client as dbc
    from blastjob.discovery.follow_seeds import unresolved_follows

    conn = dbc.init_db(cfg)
    conn.executemany(
        "INSERT INTO company_follows (company, company_key, followed_at) VALUES (?,?,?)",
        [
            ("Acme", "acme", "Tue Feb 14"),
            ("Fabrikam Magazine", "fabrikam magazine", "Tue Feb 14"),
            ("Brand New Co", "brand new co", "Wed Mar 19"),
        ],
    )
    conn.commit()
    names = {s.company: s for s in unresolved_follows(conn, cfg)}
    # Acme already has a job in the fixture, so it is not a candidate.
    assert "Acme" not in names
    assert names["Brand New Co"].likely_employer is True
    assert names["Fabrikam Magazine"].likely_employer is False
    assert "magazine" in names["Fabrikam Magazine"].note
    conn.close()
    assert client.get("/network/follows").status_code == 200


def test_enrich_route_starts_a_run_and_needs_a_token(client_cfg):
    client, _cfg, _saved, _ids = client_cfg
    assert client.post("/network/enrich").status_code == 403
    r = client.post("/network/enrich", headers=_tok())
    assert r.status_code == 200
    assert "Reading titles" in r.text


def test_history_page_renders(client_cfg):
    client, _cfg, _saved, _ids = client_cfg
    assert client.get("/history").status_code == 200


def test_refine_needs_feedback_and_a_token(client_cfg):
    client, _cfg, _saved, ids = client_cfg
    jid = ids["surfaced"]
    assert client.post(f"/jobs/{jid}/refine", data={"feedback": "x"}).status_code == 403
    # empty feedback is refused rather than starting a pointless run
    assert (
        client.post(f"/jobs/{jid}/refine", data={"feedback": "   "}, headers=_tok()).status_code
        == 400
    )
    r = client.post(f"/jobs/{jid}/refine", data={"feedback": "drop the NIST claim"}, headers=_tok())
    assert r.status_code == 200


def test_company_exclusion_is_explicit_and_reversible(client_cfg):
    """Archiving with reason 'company' used to be a one-way door: the rule was
    inferred from any archived row carrying it, and nothing anywhere ever cleared
    archive_reason, so unarchiving one row left every sibling still blocking."""
    client, cfg, _saved, ids = client_cfg
    from blastjob.db import client as dbc
    from blastjob.web import service

    jid = ids["surfaced"]
    r = client.post(
        f"/jobs/{jid}/archive", data={"reason": "company", "note": "never"}, headers=_tok()
    )
    assert r.status_code == 200

    conn = dbc.init_db(cfg)
    assert [e["company"] for e in service.excluded_companies(conn)] == ["Acme"]

    # A new posting from that company is archived on arrival, with no LLM call.
    from blastjob.matching.pipeline import auto_archive_rejected_companies

    nid = dbc.upsert_job(
        conn, source="t", external_id="later", company="Acme", title="VP Ops", raw_jd="x"
    )
    assert auto_archive_rejected_companies(conn) == 1
    state = conn.execute("SELECT review_state FROM jobs WHERE id=?", (nid,)).fetchone()
    assert state["review_state"] == "archived"
    conn.close()

    # Lifting it is one action, and it actually stops the blocking.
    assert client.post("/companies/unexclude", data={"company": "Acme"}).status_code == 403
    r = client.post("/companies/unexclude", data={"company": "Acme"}, headers=_tok())
    assert r.status_code == 200

    conn = dbc.init_db(cfg)
    assert service.excluded_companies(conn) == []
    nid2 = dbc.upsert_job(
        conn, source="t", external_id="later2", company="Acme", title="VP Ops", raw_jd="x"
    )
    assert auto_archive_rejected_companies(conn) == 0
    state = conn.execute("SELECT review_state FROM jobs WHERE id=?", (nid2,)).fetchone()
    assert state["review_state"] != "archived"
    conn.close()


def test_silent_search_firms_are_reported(client_cfg):
    """Fabrikam Search failed with an identical 422 on every weekly run since it was
    added and contributed nothing. Nobody saw it, because a channel that fails
    quietly looks the same as one with no matches."""
    client, cfg, _saved, _ids = client_cfg
    from blastjob.db import client as dbc
    from blastjob.models.config import SearchFirm
    from blastjob.web.funnel import silent_channels

    cfg.discovery.search_firms = [
        SearchFirm(name="Fabrikam Search", slug="fabrikamsearch", domain="h.com")
    ]
    conn = dbc.init_db(cfg)
    reported = silent_channels(conn, cfg)
    assert [r["firm"] for r in reported] == ["Fabrikam Search"]
    assert "never produced" in reported[0]["state"]
    conn.close()
    assert client.get("/funnel").status_code == 200


def test_job_page_notes_form_carries_the_loaded_version(client_cfg):
    client, cfg, _, ids = client_cfg
    jid = _kept_job(client, cfg, ids)
    _post_notes(client, jid, "call Tuesday", loaded="")
    r = client.get(f"/jobs/{jid}")
    assert r.status_code == 200
    assert f'name="notes_version" value="{service.notes_version("call Tuesday")}"' in r.text


# ----- Alumni badge (#63) ------------------------------------------------------

ALUMNI_BADGE = "you worked here"


def test_alumni_badge_shows_on_list_and_job_page_for_former_employer(client_cfg):
    client, cfg, _, ids = client_cfg
    # Different legal suffix than the job's "Acme": company_key must reconcile them.
    cfg.candidate.former_employers = ["Acme Corporation, Inc."]

    assert ALUMNI_BADGE in client.get("/").text
    assert ALUMNI_BADGE in client.get(f"/jobs/{ids['surfaced']}").text


def test_alumni_badge_absent_for_other_employers(client_cfg):
    client, cfg, _, ids = client_cfg
    cfg.candidate.former_employers = ["Acme Corporation, Inc."]

    assert ALUMNI_BADGE not in client.get("/queue").text
    assert ALUMNI_BADGE not in client.get(f"/jobs/{ids['queued']}").text


def test_alumni_badge_absent_when_no_former_employers(client_cfg):
    client, _, _, ids = client_cfg

    assert ALUMNI_BADGE not in client.get("/").text
    assert ALUMNI_BADGE not in client.get(f"/jobs/{ids['surfaced']}").text


def test_keep_row_response_keeps_alumni_badge(client_cfg):
    client, cfg, _, ids = client_cfg
    cfg.candidate.former_employers = ["Acme"]

    r = client.post(f"/jobs/{ids['surfaced']}/keep", headers=_tok())

    assert r.status_code == 200
    assert ALUMNI_BADGE in r.text


def test_alumni_badge_shows_on_all_page_for_former_employer(client_cfg):
    client, cfg, _, ids = client_cfg
    cfg.candidate.former_employers = ["Acme Corporation, Inc."]

    assert ALUMNI_BADGE in client.get("/all").text


def test_alumni_badge_absent_on_all_page_without_former_employers(client_cfg):
    client, _, _, _ = client_cfg

    assert ALUMNI_BADGE not in client.get("/all").text


def test_funnel_page_shows_channel_counts_without_percent(client_cfg):
    """#81: the channel block is counts only. The funnel table above it has a
    Share column, so only the block's own table is checked for a percent sign."""
    client, cfg, _saved, _ids = client_cfg
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(conn, source="t", external_id="ch1", company="Acme", title="VP IT")
    conn.execute(
        "UPDATE jobs SET app_channel='referral', applied_at='2026-01-05', "
        "application_state='screen' WHERE id=?",
        (jid,),
    )
    conn.commit()
    conn.close()
    html = client.get("/funnel").text
    start = html.index("Applications by channel")
    block = html[start : html.index("</table>", start)]
    assert "Referral" in block and "Unknown" in block
    assert "%" not in block
