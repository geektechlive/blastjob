"""The agentic enrichment pass: privacy, resumability, and never guessing.

No live LLM calls anywhere in here.
"""

from __future__ import annotations

import sqlite3
import tempfile

import pytest

from blastjob.core import network_enrich as ne
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def conn() -> sqlite3.Connection:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    c.executemany(
        "INSERT INTO network_contacts (full_name, url, company, company_key, position) "
        "VALUES (?,?,?,?,?)",
        [
            ("Jane A", "https://l/1", "Acme", "acme", "SVP, Finance"),
            ("John B", "https://l/2", "Acme", "acme", "SVP, Finance"),
            ("Kim C", "https://l/3", "Beta", "beta", "Product Owner"),
        ],
    )
    c.commit()
    return c


# --- privacy: the model must never be sent a person ------------------------


def test_prompt_carries_titles_and_companies_but_no_names(conn):
    """1,364 contacts collapse to ~1,100 distinct title/company pairs, and this
    is what makes the pass cheap AND removes the disclosure question."""
    pairs = ne.pending_pairs(conn)
    _system, user = ne.build_messages(pairs)
    blob = " ".join(b["text"] for b in user)
    assert "SVP, Finance" in blob
    assert "Acme" in blob
    for name in ("Jane A", "John B", "Kim C", "https://l/1"):
        assert name not in blob


def test_distinct_pairs_collapse_duplicate_holders(conn):
    """Two people with the identical title/company are one classification."""
    pairs = ne.pending_pairs(conn)
    assert pairs.count(("SVP, Finance", "Acme")) == 1
    assert len(pairs) == 2


def test_batch_is_not_inside_a_cached_block(conn):
    """docs/how-it-works/llm.md: never interpolate per-call data into a cached block."""
    system, user = ne.build_messages(ne.pending_pairs(conn))
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in b for b in user)


# --- resumability ----------------------------------------------------------


def test_enriched_pairs_are_not_resent(conn):
    batch = ne.pending_pairs(conn)
    ne.apply_verdicts(
        conn,
        batch,
        [ne.TitleVerdict(n=1, tier="evp_svp", function="technology")],
        now="2026-09-12T12:00:00",
    )
    remaining = ne.pending_pairs(conn)
    assert ("SVP, Finance", "Acme") not in remaining
    assert ("Product Owner", "Beta") in remaining


def test_a_verdict_updates_every_contact_holding_that_pair(conn):
    batch = [("SVP, Finance", "Acme")]
    n = ne.apply_verdicts(conn, batch, [ne.TitleVerdict(n=1, tier="evp_svp")])
    assert n == 2  # both Jane and John


# --- never guess, never write junk ----------------------------------------


def test_unknown_values_fall_back_rather_than_being_stored(conn):
    v = ne.TitleVerdict(
        n=1, tier="supreme-overlord", function="astrology", recruiter="telepathy"
    ).normalized()
    assert (v.tier, v.function, v.recruiter) == ("ic", "other", "none")


def test_malformed_elements_are_skipped_not_written():
    raw = '[{"n":1,"tier":"vp"}, "not-an-object", {"no_n_field":true}]'
    verdicts = ne._parse(raw)
    assert [v.n for v in verdicts] == [1]


def test_unparseable_response_yields_nothing():
    assert ne._parse("the model apologised instead of answering") == []


def test_out_of_range_item_numbers_are_ignored(conn):
    """A verdict pointing past the batch must not write onto the wrong title."""
    batch = [("SVP, Finance", "Acme")]
    assert ne.apply_verdicts(conn, batch, [ne.TitleVerdict(n=99, tier="vp")]) == 0


def test_canonical_company_is_stored_but_not_used_as_a_join_key(conn):
    """It is a suggestion. company_key stays untouched, so a wrong canonical
    name can never manufacture a warm introduction that does not exist."""
    batch = [("Product Owner", "Beta")]
    ne.apply_verdicts(
        conn, batch, [ne.TitleVerdict(n=1, canonical_company="Totally Different Corp")]
    )
    row = conn.execute(
        "SELECT company_key, enrich_canonical_company FROM network_contacts "
        "WHERE position='Product Owner'"
    ).fetchone()
    assert row["company_key"] == "beta"
    assert row["enrich_canonical_company"] == "Totally Different Corp"
