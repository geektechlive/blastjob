"""Integration test for run_matching orchestration.

The LLM scorer is stubbed so this is deterministic and offline — it verifies the
wiring (geo skip, prefilter skip, comp gate, borderline band, surface) end to end,
which the live runs confirmed against the real CLI scorer separately.
"""

from __future__ import annotations

import tempfile

import pytest

from blastjob import config as cfg_mod
from blastjob.db import client as dbc
from blastjob.matching import pipeline as mp
from blastjob.matching.scorer import ScoringResult
from blastjob.models.config import BlastJobConfig
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile

VP = "VP of Ops. Lead a team of 20+, own enterprise ops strategy, report to CEO."
ENG = "Software Engineer. Build backend services and ship features."
SALES = "Head of Enterprise Sales. Manage quota-carrying reps."


@pytest.fixture
def cfg_conn(tmp_path):
    cfg = BlastJobConfig()
    apply_region(cfg)
    apply_profile(cfg)
    # Exercise the configured scope prefilter (title_keep_terms/title_drop_terms
    # are empty by default and keep everything -- see matching/prefilter.py).
    cfg.discovery.title_keep_terms = ["director", "head of", "vp"]
    cfg.discovery.title_drop_terms = ["engineer"]
    cfg.paths.data_dir = tempfile.mkdtemp()
    # give the matcher a work history file
    (cfg_mod.data_dir(cfg)).mkdir(parents=True, exist_ok=True)
    (cfg_mod.data_dir(cfg) / "work_history.md").write_text(
        "Senior operations exec, 25y.", encoding="utf-8"
    )
    conn = dbc.init_db(cfg)
    return cfg, conn


async def test_run_matching_dispositions(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    jobs = [
        dict(
            source="t",
            external_id="1",
            title="VP, Operations",
            company="Acme",
            raw_jd=VP,
            geo_remote=1,
            comp_provenance="posted",
            comp_max=250000,
        ),
        dict(
            source="t",
            external_id="2",
            title="VP, Operations",
            company="Beta",
            raw_jd=VP,
            geo_remote=1,
        ),  # comp unknown
        dict(
            source="t",
            external_id="3",
            title="VP, Operations",
            company="Gamma",
            raw_jd=VP,
            geo_city="New York",
            geo_region="NY",
            geo_location_raw="New York, NY",
        ),
        dict(
            source="t",
            external_id="4",
            title="Director of Ops",
            company="Delta",
            raw_jd=VP,
            geo_city="Fairhaven",
            geo_region="OH",
            geo_location_raw="Fairhaven, OH",
        ),  # comp unknown
        dict(
            source="t",
            external_id="5",
            title="Software Engineer",
            company="Eps",
            raw_jd=ENG,
            geo_remote=1,
        ),  # prefiltered
        dict(
            source="t",
            external_id="6",
            title="Head of Enterprise Sales",
            company="Zeta",
            raw_jd=SALES,
            geo_remote=1,
            comp_provenance="posted",
            comp_max=260000,
        ),
    ]
    ids = {}
    for j in jobs:
        ids[j["external_id"]] = dbc.upsert_job(conn, **j)

    # Stub the scorer: high align for the IT roles, low for sales. Keyed by job_id.
    align_by_extid = {"1": 90, "2": 90, "4": 75, "6": 12}

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        out = {}
        extid_of = {v: k for k, v in ids.items()}
        for c in candidates:
            ext = extid_of[c["job_id"]]
            out[c["job_id"]] = {
                "alignment": align_by_extid.get(ext, 50),
                "rationale": "stub",
                "seniority_note": "",
                "comp_min": 0,
                "comp_max": 0,
            }
        return ScoringResult(scores=out)

    monkeypatch.setattr(mp, "score_candidates", fake_score)

    from blastjob.llm.cost import CostTracker

    res = await mp.run_matching(cfg, conn, CostTracker())

    assert res.geo_rejected == 1  # job 3 (NY on-site)
    assert res.prefiltered_out == 1  # job 5 (engineer)
    assert res.scored == 4  # jobs 1,2,4,6

    def state(ext):
        return conn.execute("SELECT review_state FROM jobs WHERE id=?", (ids[ext],)).fetchone()[
            "review_state"
        ]

    assert state("1") == "surfaced"  # high align + verified comp + remote
    assert state("2") == "surfaced"  # high align; unknown comp now surfaces (badged)
    assert state("3") == "new"  # geo far, unscored
    assert state("4") == "queued"  # borderline 84 -> queue
    assert state("5") == "new"  # prefiltered, unscored
    assert state("6") == "new"  # sales, align below band


async def test_user_keep_during_scoring_is_not_overwritten(cfg_conn, monkeypatch):
    """CAS: a Keep clicked in the cockpit while the LLM batch is in flight must
    survive the score landing afterwards (daily job + open cockpit)."""
    cfg, conn = cfg_conn
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id="1",
        title="VP, Operations",
        company="Acme",
        raw_jd=VP,
        geo_remote=1,
        comp_provenance="posted",
        comp_max=250000,
    )

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        # user acts mid-run
        conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
        conn.commit()
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": 20,
                    "rationale": "stub",
                    "seniority_note": "",
                    "comp_min": 0,
                    "comp_max": 0,
                }
                for c in candidates
            }
        )

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    from blastjob.llm.cost import CostTracker

    await mp.run_matching(cfg, conn, CostTracker())
    row = conn.execute(
        "SELECT review_state, alignment_score FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    assert row["review_state"] == "kept"


def test_rejection_block_in_prompt():
    from blastjob.matching.prompts import build_user_content, rejection_block

    assert rejection_block([]) == ""
    rej = [{"title": "Security Manager", "company": "X", "reason": "level", "note": "IC"}]
    text = build_user_content(
        "wh", [{"job_id": 1, "title": "T", "company": "C", "jd": "d"}], 100, rej
    )
    assert "Calibration: roles the candidate rejected" in text
    assert "Security Manager @ X: level — IC" in text
    assert text.index("Calibration") < text.index("## Jobs to score")


async def test_company_rejection_auto_archives_and_level_feeds_scorer(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    old = dbc.upsert_job(
        conn, source="t", external_id="0", title="COO", company="NopeCo", raw_jd=VP
    )
    conn.execute(
        "UPDATE jobs SET review_state='archived', archive_reason='company' WHERE id=?", (old,)
    )
    # "Never this company" is now an explicit policy row rather than something
    # inferred from any archived job carrying the reason — the inference could
    # not be reversed, because unarchiving one row left every sibling still
    # satisfying it. service.archive_job writes this for real; the test seeds it
    # directly because it drives the pipeline, not the route.
    from blastjob.web.service import exclude_company

    exclude_company(conn, "NopeCo")
    lvl = dbc.upsert_job(
        conn, source="t", external_id="9", title="Ops Manager", company="Q", raw_jd=VP
    )
    conn.execute(
        "UPDATE jobs SET review_state='archived', archive_reason='level', archive_note='IC' "
        "WHERE id=?",
        (lvl,),
    )
    conn.commit()
    again = dbc.upsert_job(
        conn, source="t", external_id="1", title="VP Ops", company="nopeco", raw_jd=VP, geo_remote=1
    )
    fresh = dbc.upsert_job(
        conn, source="t", external_id="2", title="VP Ops", company="Acme", raw_jd=VP, geo_remote=1
    )
    seen: dict = {}

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        seen["ids"] = [c["job_id"] for c in candidates]
        seen["rejections"] = kw.get("rejections")
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": 90,
                    "rationale": "stub",
                    "seniority_note": "",
                    "comp_min": 0,
                    "comp_max": 0,
                }
                for c in candidates
            }
        )

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    from blastjob.llm.cost import CostTracker

    res = await mp.run_matching(cfg, conn, CostTracker())
    assert res.company_skipped == 1
    assert seen["ids"] == [fresh]
    assert seen["rejections"] == [
        {"title": "Ops Manager", "company": "Q", "reason": "level", "note": "IC"}
    ]
    row = conn.execute(
        "SELECT review_state, archive_reason FROM jobs WHERE id=?", (again,)
    ).fetchone()
    assert (row["review_state"], row["archive_reason"]) == ("archived", "company")


async def test_only_unscored_skips_already_scored_jobs(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    done = dbc.upsert_job(
        conn, source="t", external_id="1", title="VP Ops", company="A", raw_jd=VP, geo_remote=1
    )
    conn.execute("UPDATE jobs SET alignment_score=70, review_state='new' WHERE id=?", (done,))
    fresh = dbc.upsert_job(
        conn, source="t", external_id="2", title="VP Ops", company="B", raw_jd=VP, geo_remote=1
    )
    conn.commit()
    seen: list[int] = []

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        seen.extend(c["job_id"] for c in candidates)
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": 90,
                    "rationale": "stub",
                    "seniority_note": "",
                    "comp_min": 0,
                    "comp_max": 0,
                }
                for c in candidates
            }
        )

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    from blastjob.llm.cost import CostTracker

    await mp.run_matching(cfg, conn, CostTracker(), only_unscored=True)
    assert seen == [fresh]
    await mp.run_matching(cfg, conn, CostTracker())  # default: re-score everything
    assert sorted(seen) == sorted([fresh, done, fresh])


# ---------------------------------------------------------------------------
# These two drive run_matching END TO END. The gate and scorer unit tests both
# passed while the pipeline never produced an INFERRED row and never supplied
# prior_scores -- the integration patches were written and never applied, so the
# behaviour was unreachable in production. Assert the STORED state.
# ---------------------------------------------------------------------------


async def test_llm_read_comp_is_stored_as_inferred_not_posted(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    cfg.discovery.comp_floor_usd = 200_000  # LLM-read 190K sits below it
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id="c1",
        title="VP Ops",
        company="Acme",
        raw_jd=VP,
        geo_remote=1,
    )
    conn.commit()

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": 90,
                    "rationale": "stub",
                    "strengths": [],
                    "gaps": [],
                    "comp_min": 150_000,
                    "comp_max": 190_000,
                }
                for c in candidates
            }
        )

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    from blastjob.llm.cost import CostTracker

    await mp.run_matching(cfg, conn, CostTracker())
    row = conn.execute(
        "SELECT comp_provenance, review_state FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    assert row["comp_provenance"] == "inferred"
    # Sub-floor, but OUR reading rather than the employer's number, so it goes to
    # the queue for a human look instead of being binned.
    assert row["review_state"] == "queued"


async def test_an_unchanged_posting_is_not_rescored(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    # Long enough to be content-identifiable: a short JD deliberately has no
    # reuse fingerprint, because two unrelated roles can share three lines of
    # boilerplate.
    long_jd = VP + " " + ("Own the roadmap, budget and vendor relationships. " * 12)
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id="r1",
        title="VP Ops",
        company="Acme",
        raw_jd=long_jd,
        geo_remote=1,
    )
    conn.execute(
        "UPDATE jobs SET alignment_score=84, rationale='scored yesterday' WHERE id=?", (jid,)
    )
    conn.commit()

    # Let the REAL score_candidates run and fail the test if it reaches the model.
    # Reuse happens inside it, so faking it would prove nothing.
    calls: list[object] = []

    async def _must_not_be_called(*a, **kw):
        calls.append(a)
        raise AssertionError("an unchanged posting was sent to the model")

    monkeypatch.setattr("blastjob.matching.scorer.make_stream", _must_not_be_called)
    from blastjob.llm.cost import CostTracker

    res = await mp.run_matching(cfg, conn, CostTracker(), only_unscored=False)
    # One live repost scored 90/89/88/84 on four mornings and crossed the
    # threshold in both directions on nothing but scorer noise.
    assert calls == []
    assert res.scoring_failures == 0
    row = conn.execute("SELECT alignment_score FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["alignment_score"] == 84


async def test_company_exclusion_matches_on_the_key_not_the_raw_string(cfg_conn):
    """`Fabrikam Corp.` and `Fabrikam Corp` are the same employer and the same
    company_key, but a lowercased string compare let the second slip past an
    exclusion recorded for the first."""
    cfg, conn = cfg_conn
    from blastjob.web.service import exclude_company

    exclude_company(conn, "Fabrikam Corp")
    jid = dbc.upsert_job(
        conn, source="t", external_id="k1", title="VP Ops", company="Fabrikam Corp.", raw_jd=VP
    )
    conn.commit()
    assert mp.auto_archive_rejected_companies(conn) == 1
    row = conn.execute("SELECT review_state FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["review_state"] == "archived"


async def test_job_ids_scopes_matching_to_the_given_job(cfg_conn, monkeypatch):
    """A manually-added job must be scoreable on its own, without re-considering
    every other unscored job in the backlog."""
    cfg, conn = cfg_conn
    scoped = dbc.upsert_job(
        conn,
        source="t",
        external_id="scoped",
        title="VP, Operations",
        company="Acme",
        raw_jd=VP,
        geo_remote=1,
    )
    other = dbc.upsert_job(
        conn,
        source="t",
        external_id="other",
        title="VP, Operations",
        company="Beta",
        raw_jd=VP,
        geo_remote=1,
    )
    conn.commit()

    seen_job_ids: list[int] = []

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        seen_job_ids.extend(c["job_id"] for c in candidates)
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": 90,
                    "rationale": "stub",
                    "seniority_note": "",
                    "comp_min": 0,
                    "comp_max": 0,
                }
                for c in candidates
            }
        )

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    from blastjob.llm.cost import CostTracker

    res = await mp.run_matching(cfg, conn, CostTracker(), job_ids=[scoped])

    assert seen_job_ids == [scoped]
    assert res.considered == 1
    assert res.scored == 1
    other_row = conn.execute("SELECT alignment_score FROM jobs WHERE id=?", (other,)).fetchone()
    assert other_row["alignment_score"] is None


async def test_job_ids_empty_collection_scores_nothing(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    dbc.upsert_job(
        conn,
        source="t",
        external_id="untouched",
        title="VP, Operations",
        company="Acme",
        raw_jd=VP,
        geo_remote=1,
    )
    conn.commit()

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        assert candidates == []
        return ScoringResult(scores={})

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    from blastjob.llm.cost import CostTracker

    res = await mp.run_matching(cfg, conn, CostTracker(), job_ids=[])
    assert res.considered == 0
    assert res.scored == 0


# ----- Empty-install guard: no candidate profile -> no LLM call --------------


async def test_unconfigured_candidate_profile_skips_scoring_entirely(monkeypatch):
    """`[candidate] target_titles` unset (the shipped default) must never reach
    the scorer -- geo + the scope prefilter still run, but nothing is scored."""
    cfg = BlastJobConfig()
    apply_region(cfg)
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg_mod.data_dir(cfg).mkdir(parents=True, exist_ok=True)
    (cfg_mod.data_dir(cfg) / "work_history.md").write_text("25y, senior ops.", encoding="utf-8")
    conn = dbc.init_db(cfg)
    dbc.upsert_job(
        conn,
        source="t",
        external_id="1",
        title="VP, Operations",
        company="Acme",
        raw_jd=VP,
        geo_remote=1,
    )
    conn.commit()

    calls: list[object] = []

    async def _must_not_be_called(*a, **kw):
        calls.append(a)
        raise AssertionError("scorer called with no candidate profile configured")

    monkeypatch.setattr(mp, "score_candidates", _must_not_be_called)
    from blastjob.llm.cost import CostTracker

    res = await mp.run_matching(cfg, conn, CostTracker())
    assert calls == []
    assert res.scored == 0
    assert mp.NO_CANDIDATE_PROFILE_NOTE in res.notes
    row = conn.execute("SELECT alignment_score, review_state FROM jobs").fetchone()
    assert row["alignment_score"] is None
    assert row["review_state"] in ("new", "surfaced", "queued")


@pytest.mark.parametrize("placeholder", ["", "<role title>"])
async def test_placeholder_target_titles_count_as_unconfigured(monkeypatch, placeholder):
    """A `<placeholder>`-style value in target_titles must not count as real."""
    cfg = BlastJobConfig()
    apply_region(cfg)
    cfg.candidate.target_titles = [placeholder] if placeholder else []
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg_mod.data_dir(cfg).mkdir(parents=True, exist_ok=True)
    (cfg_mod.data_dir(cfg) / "work_history.md").write_text("25y, senior ops.", encoding="utf-8")
    conn = dbc.init_db(cfg)
    dbc.upsert_job(
        conn,
        source="t",
        external_id="1",
        title="VP, Operations",
        company="Acme",
        raw_jd=VP,
        geo_remote=1,
    )
    conn.commit()

    async def _must_not_be_called(*a, **kw):
        raise AssertionError("scorer called with only placeholder target_titles")

    monkeypatch.setattr(mp, "score_candidates", _must_not_be_called)
    from blastjob.llm.cost import CostTracker

    res = await mp.run_matching(cfg, conn, CostTracker())
    assert res.scored == 0
    assert mp.NO_CANDIDATE_PROFILE_NOTE in res.notes


async def test_configured_candidate_profile_still_scores(cfg_conn, monkeypatch):
    """The counterpart to the guard test: a configured profile scores normally."""
    cfg, conn = cfg_conn
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id="1",
        title="VP, Operations",
        company="Acme",
        raw_jd=VP,
        geo_remote=1,
    )
    conn.commit()

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": 90,
                    "rationale": "stub",
                    "seniority_note": "",
                    "comp_min": 0,
                    "comp_max": 0,
                }
                for c in candidates
            }
        )

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    from blastjob.llm.cost import CostTracker

    res = await mp.run_matching(cfg, conn, CostTracker())
    assert res.scored == 1
    assert res.notes == []
    row = conn.execute("SELECT alignment_score FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["alignment_score"] == 90
