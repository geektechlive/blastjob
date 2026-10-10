"""Regression coverage for the perf pass on `web/funnel.py`: the two-pass fetch
that only reads `raw_jd` for rows that actually reach the scope classifier must
produce identical stage assignments to the single-query version it replaced.

The broader funnel behavior (counts add up, examples, trend runs) is covered
in tests/test_web.py; this file is scoped to the split between
`_pre_scope_stage` and `_post_scope_stage`.
"""

from __future__ import annotations

import tempfile

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.funnel import build_funnel


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def test_kept_row_stages_correctly_with_no_raw_jd(conn):
    """A kept job short-circuits before the scope classifier, so it must never
    need raw_jd -- even an empty one stages correctly."""
    jid = dbc.upsert_job(conn, source="a", external_id="k1", company="Co", title="VP Ops")
    conn.execute("UPDATE jobs SET review_state='kept', raw_jd='' WHERE id=?", (jid,))
    conn.commit()
    f = build_funnel(conn, BlastJobConfig())
    assert f.counts.get("kept") == 1


def test_archived_row_stages_correctly_with_no_raw_jd(conn):
    jid = dbc.upsert_job(conn, source="a", external_id="a1", company="Co", title="VP Ops")
    conn.execute("UPDATE jobs SET review_state='archived', raw_jd='' WHERE id=?", (jid,))
    conn.commit()
    f = build_funnel(conn, BlastJobConfig())
    assert f.counts.get("archived") == 1


def test_stale_row_stages_correctly_with_no_raw_jd(conn):
    jid = dbc.upsert_job(conn, source="a", external_id="s1", company="Co", title="VP Ops")
    conn.execute(
        "UPDATE jobs SET review_state='new', source_state='stale', raw_jd='' WHERE id=?", (jid,)
    )
    conn.commit()
    f = build_funnel(conn, BlastJobConfig())
    assert f.counts.get("stale") == 1


def test_active_ic_role_with_no_management_signal_is_prefiltered(conn):
    """This is the one path that DOES need raw_jd -- the scope classifier reads
    it. An IC title with no management-scope language in the JD is dropped."""
    jid = dbc.upsert_job(
        conn,
        source="a",
        external_id="p1",
        company="Co",
        title="Software Engineer",
    )
    conn.execute(
        "UPDATE jobs SET review_state='new', source_state='active', "
        "geo_remote=1, raw_jd='Write code and ship features.' WHERE id=?",
        (jid,),
    )
    conn.commit()
    cfg = BlastJobConfig()
    cfg.discovery.title_drop_terms = ["engineer"]
    f = build_funnel(conn, cfg)
    assert f.counts.get("prefiltered") == 1


def test_active_ic_role_with_management_signal_passes_scope(conn):
    jid = dbc.upsert_job(
        conn,
        source="a",
        external_id="p2",
        company="Co",
        title="Solutions Architect",
    )
    conn.execute(
        "UPDATE jobs SET review_state='new', source_state='active', geo_remote=1, "
        "raw_jd='Owns budget and manages a team of engineers and direct reports.' "
        "WHERE id=?",
        (jid,),
    )
    conn.commit()
    cfg = BlastJobConfig()
    cfg.discovery.title_drop_terms = ["architect"]
    f = build_funnel(conn, cfg)
    # Dropped-family title, but the JD shows the target scope -> rescued,
    # passed geo + scope, unscored (no alignment_score yet).
    assert f.counts.get("unscored") == 1


def test_counts_still_sum_to_total_after_two_pass_split(conn):
    for i in range(5):
        jid = dbc.upsert_job(conn, source="a", external_id=f"t{i}", company="Co", title="VP Ops")
        conn.execute(
            "UPDATE jobs SET geo_remote=1, raw_jd='leads a team and owns the budget' WHERE id=?",
            (jid,),
        )
    conn.commit()
    f = build_funnel(conn, BlastJobConfig())
    assert sum(v for k, v in f.counts.items() if k != "discovered") == f.total
