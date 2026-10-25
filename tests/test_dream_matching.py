"""The dream-list geo exception in matching, the shared geo-reject rule and the funnel (#90)."""

from __future__ import annotations

import sqlite3

import pytest

from blastjob import config as cfg_mod
from blastjob.core import target_store as ts
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline as mp
from blastjob.matching.geo import is_dream_bypass, is_geo_reject
from blastjob.matching.scorer import ScoringResult
from blastjob.models.config import BlastJobConfig, TargetBoard
from blastjob.models.job import GeoStatus
from blastjob.web import funnel as fn
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile

JD = "VP of Technology. Lead a team of 20+, own enterprise technology strategy."

FAR = (GeoStatus.VERIFIED, None, False, False)  # an affirmative other place, on-site
TOO_FAR = (GeoStatus.APPROXIMATE, 200.0, False, False)
UNKNOWN = (GeoStatus.UNKNOWN, None, True, False)
REMOTE = (GeoStatus.VERIFIED, None, True, False)
NEAR = (GeoStatus.APPROXIMATE, 3.0, True, False)
CONDITIONAL = (GeoStatus.APPROXIMATE, 45.0, True, True)


# ----------------------------------------------------------------------------- predicate


@pytest.mark.parametrize("verdict", [FAR, TOO_FAR])
def test_a_far_verdict_is_a_reject_unless_the_company_is_a_dream(verdict):
    assert is_geo_reject(verdict, dream=False) is True
    assert is_geo_reject(verdict, dream=True) is False
    assert is_dream_bypass(verdict, dream=True) is True
    assert is_dream_bypass(verdict, dream=False) is False


@pytest.mark.parametrize("verdict", [UNKNOWN, REMOTE, NEAR, CONDITIONAL])
def test_a_passing_or_unknown_verdict_is_never_a_reject_or_a_bypass(verdict):
    for dream in (True, False):
        assert is_geo_reject(verdict, dream=dream) is False
        assert is_dream_bypass(verdict, dream=dream) is False


# ----------------------------------------------------------------------------- matching


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    apply_region(c)
    apply_profile(c)
    c.discovery.title_keep_terms = ["director", "head of", "vp"]
    c.discovery.title_drop_terms = ["engineer"]
    c.paths.data_dir = str(tmp_path)
    c.discovery.target_boards = [
        TargetBoard(
            company="Northwind Traders",
            ats="greenhouse",
            token="northwind",
            identity_basis="owner_confirmed",
        )
    ]
    cfg_mod.data_dir(c).mkdir(parents=True, exist_ok=True)
    (cfg_mod.data_dir(c) / "work_history.md").write_text("Senior exec, 25y.", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _dream(conn, name: str = "Northwind Traders", tier: str = "dream") -> None:
    ts.add_targets(conn, [ts.TargetInput(name=name)], tier)


def _job(conn, ext: str, company: str = "Northwind Traders", **kw) -> int:
    fields = dict(
        source="greenhouse:northwind",
        external_id=ext,
        title="VP of Technology",
        company=company,
        raw_jd=JD,
        geo_city="Phoenix",
        geo_region="AZ",
        geo_location_raw="Phoenix, AZ",
    )
    fields.update(kw)
    return dbc.upsert_job(conn, **fields)


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


def _row(conn, jid: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


async def _match(cfg, conn, monkeypatch, **kw):
    monkeypatch.setattr(mp, "score_candidates", _fake_score())
    return await mp.run_matching(cfg, conn, CostTracker(), **kw)


async def test_a_far_role_from_a_dream_company_is_kept_flagged_and_surfaced(cfg, conn, monkeypatch):
    _dream(conn)
    jid = _job(conn, "1")
    res = await _match(cfg, conn, monkeypatch)
    row = _row(conn, jid)
    assert res.geo_rejected == 0
    assert row["dream_far"] == 1
    assert row["review_state"] == "surfaced"
    # The stored geo verdict stays truthful: it is far.
    assert row["geo_status"] == "verified"
    assert row["geo_distance_mi"] is None


async def test_the_same_far_role_from_a_company_that_is_not_a_dream_is_rejected(
    cfg, conn, monkeypatch
):
    _dream(conn, "Contoso")
    jid = _job(conn, "1", company="Fabrikam")
    res = await _match(cfg, conn, monkeypatch)
    row = _row(conn, jid)
    assert res.geo_rejected == 1
    assert row["dream_far"] == 0
    assert row["review_state"] == "new"
    assert row["alignment_score"] is None


@pytest.mark.parametrize("source", ["adzuna", "mailalert", "apple", "searchfirm:acme"])
async def test_a_far_row_from_any_other_source_gets_no_exception(cfg, conn, monkeypatch, source):
    """Only a configured, identity-proven board row is exempt; a name alone is not enough."""
    _dream(conn)
    jid = _job(conn, "1", source=source)
    res = await _match(cfg, conn, monkeypatch)
    row = _row(conn, jid)
    assert res.geo_rejected == 1
    assert row["dream_far"] == 0 and row["review_state"] == "new"


async def test_a_board_row_without_an_identity_basis_gets_no_exception(cfg, conn, monkeypatch):
    cfg.discovery.target_boards[0].identity_basis = ""
    _dream(conn)
    jid = _job(conn, "1")
    res = await _match(cfg, conn, monkeypatch)
    assert res.geo_rejected == 1
    assert _row(conn, jid)["dream_far"] == 0


async def test_a_board_for_another_company_gives_this_company_no_exception(cfg, conn, monkeypatch):
    _dream(conn)
    _dream(conn, "Contoso")
    jid = _job(conn, "1", company="Contoso")  # source is Northwind's board
    res = await _match(cfg, conn, monkeypatch)
    assert res.geo_rejected == 1
    assert _row(conn, jid)["dream_far"] == 0


async def test_a_core_target_gets_no_exception(cfg, conn, monkeypatch):
    _dream(conn, tier="core")
    jid = _job(conn, "1")
    res = await _match(cfg, conn, monkeypatch)
    assert res.geo_rejected == 1
    assert _row(conn, jid)["dream_far"] == 0


async def test_a_dream_role_with_an_unknown_location_is_unchanged(cfg, conn, monkeypatch):
    _dream(conn)
    jid = _job(conn, "1", geo_city="", geo_region="", geo_location_raw="Corporate HQ")
    res = await _match(cfg, conn, monkeypatch)
    row = _row(conn, jid)
    assert res.geo_unknown == 1
    assert row["dream_far"] == 0
    assert row["review_state"] == "queued"


async def test_posted_comp_under_the_floor_still_rejects_a_dream_far_role(cfg, conn, monkeypatch):
    cfg.discovery.comp_floor_usd = 200000
    _dream(conn)
    jid = _job(conn, "1", comp_provenance="posted", comp_max=90000)
    res = await _match(cfg, conn, monkeypatch)
    row = _row(conn, jid)
    assert res.scored == 1  # the role reached the scorer; the comp gate then turned it away
    assert row["review_state"] == "new"


async def test_the_title_prefilter_still_applies_to_a_dream_far_role(cfg, conn, monkeypatch):
    _dream(conn)
    jid = _job(conn, "1", title="Software Engineer", raw_jd="Build backend services.")
    res = await _match(cfg, conn, monkeypatch)
    assert res.prefiltered_out == 1
    assert _row(conn, jid)["review_state"] == "new"


async def test_leaving_the_dream_tier_rejects_far_roles_again_on_the_next_daily_pass(
    cfg, conn, monkeypatch
):
    _dream(conn)
    jid = _job(conn, "1")
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    assert _row(conn, jid)["review_state"] == "surfaced"
    assert ts.set_tier(conn, company_key("Northwind Traders"), "core")
    # The daily run only scores unscored rows, but a flagged row is revisited.
    res = await _match(cfg, conn, monkeypatch, only_unscored=True)
    row = _row(conn, jid)
    assert res.geo_rejected == 1
    assert row["dream_far"] == 0
    assert row["review_state"] == "new"


async def test_a_role_that_becomes_remote_loses_the_flag(cfg, conn, monkeypatch):
    _dream(conn)
    jid = _job(conn, "1")
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    assert _row(conn, jid)["dream_far"] == 1
    conn.execute("UPDATE jobs SET geo_remote=1 WHERE id=?", (jid,))
    conn.commit()
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    row = _row(conn, jid)
    assert row["dream_far"] == 0
    assert row["review_state"] == "surfaced"


async def test_a_role_that_becomes_local_loses_the_flag(cfg, conn, monkeypatch):
    _dream(conn)
    jid = _job(conn, "1")
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    conn.execute(
        "UPDATE jobs SET geo_city='Fairhaven', geo_region='OH', "
        "geo_location_raw='Fairhaven, OH' WHERE id=?",
        (jid,),
    )
    conn.commit()
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    assert _row(conn, jid)["dream_far"] == 0


async def test_a_radius_change_moves_the_flag(cfg, conn, monkeypatch):
    _dream(conn)
    jid = _job(conn, "1", geo_city="Rivermont", geo_region="OH", geo_location_raw="Rivermont, OH")
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    assert _row(conn, jid)["dream_far"] == 1  # about 69 miles: past the extended radius
    cfg.discovery.extended_radius_miles = 100.0
    await _match(cfg, conn, monkeypatch, only_unscored=True)
    assert _row(conn, jid)["dream_far"] == 0


async def test_a_decided_job_is_never_touched(cfg, conn, monkeypatch):
    _dream(conn)
    jid = _job(conn, "1")
    conn.execute("UPDATE jobs SET review_state='archived' WHERE id=?", (jid,))
    conn.commit()
    await _match(cfg, conn, monkeypatch)
    assert _row(conn, jid)["dream_far"] == 0


# ----------------------------------------------------------------------------- funnel


def _stage(conn, cfg, jid: int) -> str:
    funnel = fn.build_funnel(conn, cfg)
    for stage, examples in funnel.examples.items():
        if any(e["id"] == jid for e in examples):
            return stage
    raise AssertionError("job not staged")


async def test_the_funnel_agrees_with_matching_about_a_dream_far_role(cfg, conn, monkeypatch):
    _dream(conn)
    jid = _job(conn, "1")
    other = _job(conn, "2", company="Fabrikam")
    await _match(cfg, conn, monkeypatch)
    assert _stage(conn, cfg, jid) == "dream_far"
    assert _stage(conn, cfg, other) == "geo_rejected"
    named = _job(conn, "3", source="adzuna")
    assert _stage(conn, cfg, named) == "geo_rejected"
    assert ("dream_far", "Dream, on-site far") in fn.STAGES
