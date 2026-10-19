"""Tests for service.py: honest header counts, empty-list explanations, fit/comp
sort, the kept funnel order, unkeep/unarchive CAS transitions, and back_href.

Uses a raw sqlite3 connection (via dbc.init_db against a temp data dir) rather
than the FastAPI TestClient — these are service-layer unit tests, not route
tests (tests/test_web.py owns route-level coverage).
"""

from __future__ import annotations

import tempfile

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import service, stage_lists, stage_moves


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _new_cluster(conn, fingerprint: str) -> int:
    cur = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES (?, datetime('now'))",
        (fingerprint,),
    )
    return int(cur.lastrowid)


# ----- Task 1: counts must equal what the page renders -----------------------


def test_counts_queued_excludes_cluster_decided_elsewhere(conn):
    """The exact live shape hit in production: a queued job shares a cluster with an
    archived job. /queue renders 0 rows for it; counts() must say 0, not 1."""
    cid = _new_cluster(conn, "northwind|director it|")
    archived = dbc.upsert_job(conn, source="a", external_id="c1", company="Co", title="Dir Ops")
    queued = dbc.upsert_job(conn, source="b", external_id="c2", company="Co", title="Dir Ops")
    conn.execute(
        "UPDATE jobs SET review_state='archived', archive_reason='company', cluster_id=? "
        "WHERE id=?",
        (cid, archived),
    )
    conn.execute(
        "UPDATE jobs SET review_state='queued', alignment_score=70, cluster_id=? WHERE id=?",
        (cid, queued),
    )
    conn.commit()

    assert len(stage_lists.queued_jobs(conn)) == 0
    assert stage_lists.counts(conn)["queued"] == 0


def test_counts_surfaced_matches_rendered_list_length(conn):
    """A surfaced job whose cluster has a kept sibling belongs on /shortlist, not
    /. counts()['surfaced'] must match len(surfaced_jobs()), not a raw
    GROUP BY count."""
    cid = _new_cluster(conn, "decided|vp eng|")
    kept = dbc.upsert_job(conn, source="a", external_id="d1", company="Decided", title="VP Eng")
    surf = dbc.upsert_job(conn, source="b", external_id="d2", company="Decided", title="VP Eng")
    other_surf = dbc.upsert_job(conn, source="c", external_id="d3", company="X", title="Y")
    conn.execute(
        "UPDATE jobs SET review_state='kept', alignment_score=70, cluster_id=? WHERE id=?",
        (cid, kept),
    )
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=88, cluster_id=? WHERE id=?",
        (cid, surf),
    )
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=90 WHERE id=?",
        (other_surf,),
    )
    conn.commit()

    rendered = stage_lists.surfaced_jobs(conn)
    assert len(rendered) == 1
    assert stage_lists.counts(conn)["surfaced"] == 1


def test_counts_kept_includes_stale_source_job(conn):
    """The Shortlist has no source_state filter; a kept job whose posting went
    stale still counts."""
    jid = dbc.upsert_job(conn, source="adzuna", external_id="k1", company="Co", title="VP Ops")
    conn.execute("UPDATE jobs SET review_state='kept', source_state='stale' WHERE id=?", (jid,))
    conn.commit()

    rendered = stage_lists.shortlist_jobs(conn)
    assert len(rendered) == 1
    assert stage_lists.counts(conn)["shortlist"] == 1


def test_counts_total_is_raw_active_count(conn):
    """/all shows every active row uncollapsed, so total stays a raw count."""
    for i in range(3):
        dbc.upsert_job(conn, source="s", external_id=str(i), company="Co", title="T")
    conn.commit()
    assert stage_lists.counts(conn)["total"] == 3


# ----- Task 2: empty_reason explains a zeroed-out list ------------------------


def test_empty_reason_queued_explains_decided_clusters(conn):
    cid = _new_cluster(conn, "northwind|director it|")
    archived = dbc.upsert_job(conn, source="a", external_id="e1", company="Co", title="Dir Ops")
    queued = dbc.upsert_job(conn, source="b", external_id="e2", company="Co", title="Dir Ops")
    conn.execute(
        "UPDATE jobs SET review_state='archived', cluster_id=? WHERE id=?", (cid, archived)
    )
    conn.execute(
        "UPDATE jobs SET review_state='queued', alignment_score=70, cluster_id=? WHERE id=?",
        (cid, queued),
    )
    conn.commit()

    reason = stage_lists.empty_reason(conn, "queued")
    assert reason != ""
    assert "1" in reason and "queued" in reason


def test_empty_reason_blank_when_list_renders_rows(conn):
    jid = dbc.upsert_job(conn, source="a", external_id="f1", company="Co", title="T")
    conn.execute("UPDATE jobs SET review_state='queued', alignment_score=70 WHERE id=?", (jid,))
    conn.commit()
    assert stage_lists.empty_reason(conn, "queued") == ""


def test_empty_reason_blank_when_genuinely_empty(conn):
    assert stage_lists.empty_reason(conn, "queued") == ""
    assert stage_lists.empty_reason(conn, "surfaced") == ""
    assert stage_lists.empty_reason(conn, "shortlist") == ""


def test_empty_reason_unknown_view_returns_blank(conn):
    assert stage_lists.empty_reason(conn, "not-a-real-view") == ""


# ----- Task 3: fit/comp sort toggle -------------------------------------------


def test_surfaced_jobs_defaults_to_fit_sort(conn):
    lo = dbc.upsert_job(conn, source="a", external_id="s1", company="Lo", title="T")
    hi = dbc.upsert_job(conn, source="b", external_id="s2", company="Hi", title="T")
    # Lower fit but posted (higher) comp; higher fit but no comp.
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=60, "
        "comp_provenance='posted', comp_total_est=300000 WHERE id=?",
        (lo,),
    )
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=95 WHERE id=?",
        (hi,),
    )
    conn.commit()

    fit_order = [r["id"] for r in stage_lists.surfaced_jobs(conn, sort="fit")]
    assert fit_order[0] == hi

    comp_order = [r["id"] for r in stage_lists.surfaced_jobs(conn, sort="comp")]
    assert comp_order[0] == lo

    # Default (no sort arg) must be fit, preserving today's default swap.
    assert [r["id"] for r in stage_lists.surfaced_jobs(conn)] == fit_order


def test_all_matches_sort_toggle(conn):
    lo = dbc.upsert_job(conn, source="a", external_id="m1", company="Lo", title="T")
    hi = dbc.upsert_job(conn, source="b", external_id="m2", company="Hi", title="T")
    conn.execute(
        "UPDATE jobs SET alignment_score=70, comp_provenance='posted', "
        "comp_total_est=300000 WHERE id=?",
        (lo,),
    )
    conn.execute("UPDATE jobs SET alignment_score=95 WHERE id=?", (hi,))
    conn.commit()

    assert [r["id"] for r in stage_lists.matches(conn, floor=65, sort="fit")][0] == hi
    assert [r["id"] for r in stage_lists.matches(conn, floor=65, sort="comp")][0] == lo


# ----- Task 4: kept funnel order ----------------------------------------------


def test_kept_jobs_orders_by_fit_not_by_tailoring_stage(conn):
    """Rules (2026-09-13): every list is best fit first. Grouping by stage sank
    a freshly tailored 93 below every untailored 70."""
    tailored_hi = dbc.upsert_job(conn, source="a", external_id="n1", company="A", title="T")
    none_lo = dbc.upsert_job(conn, source="b", external_id="n2", company="B", title="T")
    unscored = dbc.upsert_job(conn, source="c", external_id="n3", company="C", title="T")
    conn.execute(
        "UPDATE jobs SET review_state='kept', build_state='tailored', alignment_score=93 "
        "WHERE id=?",
        (tailored_hi,),
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', build_state='none', alignment_score=70 WHERE id=?",
        (none_lo,),
    )
    conn.execute("UPDATE jobs SET review_state='kept', build_state='none' WHERE id=?", (unscored,))
    conn.commit()

    order = [r["id"] for r in stage_lists.shortlist_jobs(conn)]
    assert order == [tailored_hi, none_lo, unscored]


def test_all_active_orders_by_fit_before_disposition(conn):
    """The All page grouped by disposition, so a 91 kept job sat below a 79
    surfaced one. Scored jobs now run best fit first; unscored ones follow."""
    kept_hi = dbc.upsert_job(conn, source="a", external_id="d1", company="A", title="VP Ops")
    surfaced_lo = dbc.upsert_job(conn, source="b", external_id="d2", company="B", title="VP Ops")
    unscored = dbc.upsert_job(conn, source="c", external_id="d3", company="C", title="VP Ops")
    conn.execute("UPDATE jobs SET review_state='kept', alignment_score=91 WHERE id=?", (kept_hi,))
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=79 WHERE id=?", (surfaced_lo,)
    )
    conn.commit()

    order = [j["id"] for j in service.all_active(conn, BlastJobConfig())]
    assert order.index(kept_hi) < order.index(surfaced_lo) < order.index(unscored)


def test_all_active_filtered_disposition_uses_scope_classifier(conn):
    """Perf pass regression: all_active() now fetches raw_jd in a second,
    targeted pass (only for unscored rows) instead of for every active row.
    The scope-classified "filtered" disposition must still work."""
    jid = dbc.upsert_job(conn, source="a", external_id="fl1", company="Co", title="Engineer")
    conn.execute(
        "UPDATE jobs SET geo_remote=1, raw_jd='Write code and ship features.' WHERE id=?",
        (jid,),
    )
    conn.commit()
    cfg = BlastJobConfig()
    cfg.discovery.title_drop_terms = ["engineer"]
    out = {j["id"]: j for j in service.all_active(conn, cfg)}
    assert out[jid]["disposition"] == "filtered"
    assert "not a leadership role" in out[jid]["reason"]


def test_all_active_scored_rows_never_need_raw_jd(conn):
    """A scored row's disposition ("low fit") never touches raw_jd -- garbage
    or missing JD text must not affect it."""
    jid = dbc.upsert_job(conn, source="a", external_id="fl2", company="Co", title="VP Ops")
    conn.execute(
        "UPDATE jobs SET alignment_score=40, raw_jd='' WHERE id=?",
        (jid,),
    )
    conn.commit()
    out = {j["id"]: j for j in service.all_active(conn, BlastJobConfig())}
    assert out[jid]["disposition"] == "low fit"


def test_kept_jobs_tie_breaks_equal_fit_by_funnel_stage(conn):
    """String-sorting build_state yields failed, none, tailored, tailoring.
    Among equal scores the funnel is none -> tailoring -> tailored."""
    none_j = dbc.upsert_job(conn, source="a", external_id="t1", company="A", title="T")
    tailoring_j = dbc.upsert_job(conn, source="b", external_id="t2", company="B", title="T")
    tailored_j = dbc.upsert_job(conn, source="c", external_id="t3", company="C", title="T")
    for jid, state in ((tailored_j, "tailored"), (none_j, "none"), (tailoring_j, "tailoring")):
        conn.execute(
            "UPDATE jobs SET review_state='kept', build_state=?, alignment_score=80 WHERE id=?",
            (state, jid),
        )
    conn.commit()

    order = [r["id"] for r in stage_lists.shortlist_jobs(conn)]
    assert order == [none_j, tailoring_j, tailored_j]


def test_kept_jobs_orders_by_fit_within_stage(conn):
    lo = dbc.upsert_job(conn, source="a", external_id="o1", company="A", title="T")
    hi = dbc.upsert_job(conn, source="b", external_id="o2", company="B", title="T")
    conn.execute(
        "UPDATE jobs SET review_state='kept', build_state='none', alignment_score=50 WHERE id=?",
        (lo,),
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', build_state='none', alignment_score=95 WHERE id=?",
        (hi,),
    )
    conn.commit()
    assert [r["id"] for r in stage_lists.shortlist_jobs(conn)] == [hi, lo]


def test_pipeline_board_orders_lanes_by_fit(conn):
    lo = dbc.upsert_job(conn, source="a", external_id="p1", company="A", title="T")
    hi = dbc.upsert_job(conn, source="b", external_id="p2", company="B", title="T")
    conn.execute(
        "UPDATE jobs SET review_state='kept', build_state='none', alignment_score=40 WHERE id=?",
        (lo,),
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', build_state='none', alignment_score=99 WHERE id=?",
        (hi,),
    )
    conn.commit()
    board = stage_lists.pipeline_board(conn)
    assert [r["id"] for r in board["none"]] == [hi, lo]


# ----- Task 5: unkeep (now Remove, from the Shortlist only) ---------------------


def test_unkeep_job_returns_false_when_not_kept(conn):
    jid = dbc.upsert_job(conn, source="a", external_id="u1", company="A", title="T")
    conn.execute("UPDATE jobs SET review_state='surfaced' WHERE id=?", (jid,))
    conn.commit()
    assert stage_moves.unkeep(conn, jid, 78) is False


def test_unkeep_job_preserves_application_fields(conn):
    """Do NOT destructively blank application progress -- it's recoverable
    automatically if the job is kept again, because nothing touched it."""
    jid = dbc.upsert_job(conn, source="a", external_id="u3", company="A", title="T")
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='drafted', alignment_score=90, "
        "app_notes='great call', next_action_due='2026-02-01' WHERE id=?",
        (jid,),
    )
    conn.commit()
    assert stage_moves.unkeep(conn, jid, 78) is True
    row = conn.execute(
        "SELECT review_state, application_state, app_notes, next_action_due FROM jobs WHERE id=?",
        (jid,),
    ).fetchone()
    assert row["review_state"] == "surfaced"
    assert row["application_state"] == "drafted"
    assert row["app_notes"] == "great call"
    assert row["next_action_due"] == "2026-02-01"


def test_unkeep_job_missing_id_returns_false(conn):
    assert stage_moves.unkeep(conn, 999999, 78) is False


# ----- Task 6: unarchive ------------------------------------------------------


def test_unarchive_job_returns_false_when_not_archived(conn):
    jid = dbc.upsert_job(conn, source="a", external_id="v1", company="A", title="T")
    conn.execute("UPDATE jobs SET review_state='surfaced' WHERE id=?", (jid,))
    conn.commit()
    assert service.unarchive_job(conn, jid) is False


def test_unarchive_job_transitions_and_clears_reason(conn):
    jid = dbc.upsert_job(conn, source="a", external_id="v2", company="A", title="T")
    conn.execute(
        "UPDATE jobs SET review_state='archived', archive_reason='comp', "
        "archive_note='too low' WHERE id=?",
        (jid,),
    )
    conn.commit()
    assert service.unarchive_job(conn, jid) is True
    row = conn.execute(
        "SELECT review_state, archive_reason, archive_note FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    assert row["review_state"] == "surfaced"
    assert row["archive_reason"] == ""
    assert row["archive_note"] == ""


# ----- Task 7: back_href ------------------------------------------------------


def test_back_href_known_views():
    assert service.back_href("surfaced") == "/"
    assert service.back_href("queued") == "/queue"
    assert service.back_href("matches") == "/matches"
    assert service.back_href("shortlist") == "/shortlist"
    assert service.back_href("applied") == "/applied"
    assert service.back_href("closed") == "/closed"
    # Links saved before the stages existed still land somewhere real.
    assert service.back_href("kept") == "/shortlist"


def test_back_href_unknown_view_falls_back_to_surfaced():
    assert service.back_href("") == "/"
    assert service.back_href("bogus") == "/"


# ----- default path unaffected: guards the collapse_clusters signature -------


def test_collapse_clusters_default_signature_still_works(conn):
    """service.collapse_clusters(rows) with no kwargs must behave exactly as
    before -- this guards the shared collapse helper other workers depend on."""
    rows = conn.execute(f"SELECT {service._COLS} FROM jobs").fetchall()
    assert service.collapse_clusters(rows) == []


# ----- /pipeline computed the same stage lists twice -------------------------


def test_counts_from_shared_lists_match_counts_computed_independently(conn):
    """`/pipeline` called `pipeline_board` (which builds shortlist/applied/closed)
    and then `counts` (which rebuilds all five), so eight stage-list computations
    happened where five would do.

    Sharing them has to preserve the invariant docs/how-it-works/web.md states outright --
    header counts ARE the page lengths -- so this asserts the shared path gives
    exactly the independent numbers, not merely plausible ones.
    """
    from blastjob.web import stage_lists as sl

    for i, (review, app_state, score) in enumerate(
        [
            ("surfaced", "", 90),
            ("queued", "", 74),
            ("kept", "", 88),
            ("kept", "applied", 85),
            ("kept", "rejected", 70),
        ]
    ):
        jid = dbc.upsert_job(
            conn, source=f"s{i}", external_id=f"x{i}", company=f"Co{i}", title="VP"
        )
        conn.execute(
            "UPDATE jobs SET review_state=?, application_state=?, alignment_score=?, "
            "applied_at=CASE WHEN ?<>'' THEN '2026-09-01' ELSE '' END WHERE id=?",
            (review, app_state, score, app_state, jid),
        )
    conn.commit()

    independent = sl.counts(conn)
    lists = sl.stage_list_cache(conn)
    shared = sl.counts(conn, lists=lists)
    board = sl.pipeline_board(conn, lists=lists)

    assert shared == independent
    for stage in ("shortlist", "applied", "closed"):
        assert len(lists[stage]) == independent[stage]
    boarded = {j["id"] for lane in board.values() for j in lane}
    expected = {j["id"] for stage in ("shortlist", "applied", "closed") for j in lists[stage]}
    assert boarded == expected
