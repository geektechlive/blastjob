"""A dream category's geo exception, weekly section, badge and aggregator handling (#91)."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from blastjob import config as cfg_mod
from blastjob import daily, daily_dream
from blastjob import daily_digest as dd
from blastjob.core import category_store as cs
from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline as mp
from blastjob.matching.scorer import ScoringResult
from blastjob.models.config import BlastJobConfig
from blastjob.web import apply_link, service
from blastjob.web import funnel as fn
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile

JD = "CIO. Lead a team of 20+, own enterprise technology strategy."
WEDNESDAY = dt.date(2026, 10, 28)
THRESHOLD = 78


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    apply_region(c)
    apply_profile(c)
    c.discovery.title_keep_terms = ["director", "head of", "vp", "cio"]
    c.discovery.title_drop_terms = ["engineer"]
    c.discovery.search_firm_weekday = WEDNESDAY.weekday()
    c.discovery.alignment_threshold = THRESHOLD
    c.paths.data_dir = str(tmp_path)
    cfg_mod.data_dir(c).mkdir(parents=True, exist_ok=True)
    (cfg_mod.data_dir(c) / "work_history.md").write_text("Senior exec, 25y.", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, ext="1", source="adzuna", **kw) -> int:
    fields = dict(
        source=source,
        external_id=ext,
        title="CIO",
        company="Acme",
        raw_jd=JD,
        url=f"https://example.com/{ext}",
        geo_city="Phoenix",
        geo_region="AZ",
        geo_location_raw="Phoenix, AZ",
    )
    fields.update(kw)
    return dbc.upsert_job(conn, **fields)


def _category(conn, label="Example robotics firms", dream=True) -> int:
    return cs.add_category(conn, label=label, terms="robotics startup", is_dream=dream)


def _hit(conn, jid: int, cid: int, tv: int = 0) -> None:
    cs.attach_hit(conn, jid, cid, tv)
    conn.commit()


def _row(conn, jid: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def _fake_score(alignment: int = 90):
    async def score(cfg_, wh, candidates, ct, **kw):
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": alignment,
                    "rationale": "stub",
                    "seniority_note": "",
                    "comp_min": 0,
                    "comp_max": 0,
                }
                for c in candidates
            }
        )

    return score


async def _match(cfg, conn, monkeypatch, **kw):
    monkeypatch.setattr(mp, "score_candidates", _fake_score())
    return await mp.run_matching(cfg, conn, CostTracker(), **kw)


# ----------------------------------------------------------------------------- matching


async def test_a_far_job_from_a_dream_category_is_kept_flagged_and_surfaced(cfg, conn, monkeypatch):
    jid = _job(conn)
    _hit(conn, jid, _category(conn))
    res = await _match(cfg, conn, monkeypatch)
    row = _row(conn, jid)
    assert res.geo_rejected == 0
    assert (row["dream_far"], row["review_state"]) == (1, "surfaced")
    assert row["geo_status"] == "verified"  # the stored verdict stays truthful: far


async def test_the_same_job_from_a_non_dream_category_is_rejected_on_location(
    cfg, conn, monkeypatch
):
    jid = _job(conn)
    _hit(conn, jid, _category(conn, dream=False))
    res = await _match(cfg, conn, monkeypatch)
    row = _row(conn, jid)
    assert res.geo_rejected == 1
    assert (row["dream_far"], row["review_state"]) == (0, "new")


async def test_a_far_job_with_no_category_hit_is_rejected_as_before(cfg, conn, monkeypatch):
    jid = _job(conn)
    _category(conn)
    res = await _match(cfg, conn, monkeypatch)
    assert res.geo_rejected == 1 and _row(conn, jid)["dream_far"] == 0


async def test_a_dream_category_job_with_an_unknown_place_follows_todays_rules(
    cfg, conn, monkeypatch
):
    jid = _job(conn, geo_city="", geo_region="", geo_location_raw="")
    _hit(conn, jid, _category(conn))
    res = await _match(cfg, conn, monkeypatch)
    row = _row(conn, jid)
    assert res.geo_unknown == 1 and res.geo_rejected == 0
    assert row["dream_far"] == 0
    assert row["review_state"] == "queued"  # review, not a dream hit
    assert not daily_dream.pending_dream_jobs(conn, THRESHOLD, cfg.discovery)


@pytest.mark.parametrize("change", ["disable", "delete", "terms", "undream"])
async def test_a_category_that_no_longer_counts_stops_the_exception(cfg, conn, monkeypatch, change):
    jid = _job(conn)
    cid = _category(conn)
    _hit(conn, jid, cid)
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    assert _row(conn, jid)["dream_far"] == 1
    if change == "disable":
        cs.set_enabled(conn, cid, 0, False)
    elif change == "delete":
        cs.delete_category(conn, cid, 0)
    elif change == "terms":
        cs.update_category(conn, cid, 0, label="Renamed", terms="other", is_dream=True)
    else:
        cs.update_category(conn, cid, 0, label="Renamed", terms="robotics startup", is_dream=False)
    res = await _match(cfg, conn, monkeypatch, only_unscored=True)
    row = _row(conn, jid)
    assert res.geo_rejected == 1
    assert (row["dream_far"], row["review_state"]) == (0, "new")
    assert not daily_dream.pending_dream_jobs(conn, THRESHOLD, cfg.discovery)
    assert _badges(conn, cfg, jid) == []


async def test_a_label_edit_keeps_the_exception(cfg, conn, monkeypatch):
    jid = _job(conn)
    cid = _category(conn)
    _hit(conn, jid, cid)
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    cs.update_category(conn, cid, 0, label="Renamed", terms="robotics startup", is_dream=True)
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    assert _row(conn, jid)["dream_far"] == 1


async def test_the_funnel_agrees_with_matching(cfg, conn, monkeypatch):
    jid = _job(conn, "1")
    other = _job(conn, "2")
    _hit(conn, jid, _category(conn))
    await _match(cfg, conn, monkeypatch)
    funnel = fn.build_funnel(conn, cfg)
    staged = {e["id"]: s for s, ex in funnel.examples.items() for e in ex}
    assert staged[jid] == "dream_far" and staged[other] == "geo_rejected"


# ----------------------------------------------------------------------------- digest


def _surface(conn, jid: int, score: int = 90) -> None:
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=?, dream_far=1 WHERE id=?",
        (score, jid),
    )
    conn.commit()


def test_a_dream_category_job_is_in_the_weekly_section_and_not_the_daily_list(cfg, conn):
    jid = _job(conn)
    _hit(conn, jid, _category(conn))
    _surface(conn, jid)
    assert [r["id"] for r in daily_dream.weekly_jobs(cfg, conn, WEDNESDAY)] == [jid]
    assert daily_dream.weekly_jobs(cfg, conn, WEDNESDAY + dt.timedelta(days=1)) == []
    assert jid not in [r["id"] for r in daily.pending_jobs(conn)]


def test_a_dream_far_flag_without_a_current_hit_is_not_announced(cfg, conn):
    jid = _job(conn)
    _surface(conn, jid)
    assert daily_dream.weekly_jobs(cfg, conn, WEDNESDAY) == []


def test_the_dream_block_names_the_location_and_the_category_label(cfg, conn):
    jid = _job(conn)
    _hit(conn, jid, _category(conn, label="Example robotics firms"))
    _surface(conn, jid)
    rows = daily_dream.weekly_jobs(cfg, conn, WEDNESDAY)
    text = "\n".join(dd.format_dream_block(rows, "http://127.0.0.1:8765", {}))
    assert "Phoenix, AZ" in text
    assert "Dream: Example robotics firms" in text


def test_the_label_is_defanged_in_the_digest(cfg, conn):
    jid = _job(conn)
    _hit(conn, jid, _category(conn, label="@everyone <https://evil.example>"))
    _surface(conn, jid)
    rows = daily_dream.weekly_jobs(cfg, conn, WEDNESDAY)
    text = "\n".join(dd.format_dream_block(rows, "http://127.0.0.1:8765", {}))
    assert "@everyone" not in text and "<https://evil.example>" not in text


# ----------------------------------------------------------------------------- badges


def _badges(conn, cfg, jid: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchall()
    return service.annotate_network(conn, rows, cfg.candidate)[0]["category_badges"]


def test_a_dream_hit_shows_a_dream_badge_with_the_label(cfg, conn):
    jid = _job(conn)
    conn.execute("UPDATE jobs SET geo_status='verified' WHERE id=?", (jid,))
    _hit(conn, jid, _category(conn, label="Example robotics firms"))
    assert _badges(conn, cfg, jid) == [{"text": "Dream: Example robotics firms", "dream": True}]


def test_a_non_dream_hit_shows_a_category_badge(cfg, conn):
    jid = _job(conn)
    conn.execute("UPDATE jobs SET geo_status='verified' WHERE id=?", (jid,))
    _hit(conn, jid, _category(conn, label="Example robotics firms", dream=False))
    assert _badges(conn, cfg, jid) == [{"text": "Category: Example robotics firms", "dream": False}]


def test_a_dream_hit_on_an_unknown_place_is_never_shown_as_a_dream_hit(cfg, conn):
    jid = _job(conn)
    conn.execute("UPDATE jobs SET geo_status='unknown' WHERE id=?", (jid,))
    _hit(conn, jid, _category(conn, label="Example robotics firms"))
    assert _badges(conn, cfg, jid) == [{"text": "Category: Example robotics firms", "dream": False}]


def test_a_job_with_no_hit_has_no_category_badge(cfg, conn):
    jid = _job(conn)
    assert _badges(conn, cfg, jid) == []


# ----------------------------------------------------------------------------- sources


def test_category_web_rows_are_not_an_aggregator_snippet(conn):
    from blastjob.core import employer_jd as ejd

    assert "category-web" not in ejd.AGGREGATOR_SOURCES
    assert "adzuna" in ejd.AGGREGATOR_SOURCES  # category Adzuna rows are plain adzuna


def test_category_web_rows_are_not_authoritative_closure_evidence():
    from blastjob.core import closure, closure_links

    assert closure_links.is_authoritative("category-web") is False
    stale = {"source": "category-web", "source_state": "stale"}
    assert closure._fallback_verdict(stale) == ""
    assert closure._fallback_verdict({"source": "adzuna", "source_state": "stale"}) == (
        "likely_closed"
    )


def test_a_category_web_row_offers_its_own_page_as_the_apply_link(conn):
    jid = _job(conn, source="category-web", url="https://boards.greenhouse.io/x/jobs/1")
    link = apply_link.best_apply_link(
        [conn.execute(f"SELECT {apply_link.APPLY_COLS} FROM jobs WHERE id=?", (jid,)).fetchone()]
    )
    assert link is not None
    assert link.url == "https://boards.greenhouse.io/x/jobs/1"
    assert link.label == "web search result"


def test_an_unsafe_category_web_url_gives_no_link(conn):
    jid = _job(conn, source="category-web", url="javascript:alert(1)")
    link = apply_link.best_apply_link(
        [conn.execute(f"SELECT {apply_link.APPLY_COLS} FROM jobs WHERE id=?", (jid,)).fetchone()]
    )
    assert link is None
