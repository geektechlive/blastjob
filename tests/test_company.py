"""Company profiles + comp signals: key normalization, upsert/partial update,
JSON round-trip, signal history and the negotiated-floor read."""

from __future__ import annotations

import tempfile

import pytest

from blastjob.core import company as co
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def test_company_key_normalizes_case_punctuation_and_suffixes():
    assert co.company_key("NORTHWIND CREDIT UNION") == "northwind credit union"
    assert co.company_key("Northwind Credit Union, Inc.") == "northwind credit union"
    assert co.company_key("Acme Corp") == co.company_key("ACME")


def test_upsert_then_get_roundtrips_json_columns(conn):
    p = co.CompanyProfile(
        company="Northwind Credit Union",
        total_assets_usd=7_726_422_516,
        employees=1072,
        it_leadership=[
            {"name": "Sam Lee", "title": "Chief Officer", "role": "executive", "url": "u"}
        ],
        sources=[{"label": "NCUA", "url": "https://x", "accessed": "2026-09-05"}],
        extra={"net_worth_ratio_pct": 10.59},
    )
    pid = co.upsert_profile(conn, p)
    got = co.get_profile(conn, "NORTHWIND CREDIT UNION")
    assert got["id"] == pid and got["employees"] == 1072
    assert got["it_leadership"][0]["name"] == "Sam Lee"
    assert got["extra"]["net_worth_ratio_pct"] == 10.59
    # second upsert refreshes in place, no duplicate row
    p2 = p.model_copy(update={"employees": 1100})
    assert co.upsert_profile(conn, p2) == pid
    assert co.get_profile(conn, "Northwind Credit Union")["employees"] == 1100
    assert len(co.list_profiles(conn)) == 1


def test_unknown_company_has_no_profile(conn):
    co.upsert_profile(conn, co.CompanyProfile(company="Acme"))
    assert co.get_profile(conn, "Nobody") is None


def test_comp_signals_history_and_negotiated_floor(conn):
    jid = dbc.upsert_job(conn, source="t", external_id="1", company="Acme", title="VP Ops")
    co.add_comp_signal(
        conn,
        company="Acme",
        job_id=jid,
        kind="aggregator_estimate",
        low_usd=95_000,
        high_usd=147_000,
        source="Glassdoor",
        observed_at="2026-09-05",
    )
    assert co.negotiated_floor(conn, jid) is None
    co.add_comp_signal(
        conn,
        company="Acme",
        job_id=jid,
        kind="asked_minimum",
        low_usd=200_000,
        source="phone screen",
        observed_at="2026-09-04",
    )
    co.add_comp_signal(
        conn,
        company="Acme",
        job_id=jid,
        kind="employer_response",
        note="No pushback",
        observed_at="2026-09-04",
    )
    assert co.negotiated_floor(conn, jid) == 200_000
    hist = co.comp_signals(conn, job_id=jid)
    assert [h["kind"] for h in hist] == [
        "asked_minimum",
        "employer_response",
        "aggregator_estimate",
    ]
    assert len(co.comp_signals(conn, company="ACME")) == 3
    with pytest.raises(ValueError):
        co.add_comp_signal(conn, company="Acme", kind="vibes")


def test_negotiated_floor_respects_pushback(conn):
    jid = dbc.upsert_job(conn, source="t", external_id="1", company="Acme", title="VP Ops")
    co.add_comp_signal(conn, company="Acme", job_id=jid, kind="asked_minimum", low_usd=220_000)
    co.add_comp_signal(
        conn,
        company="Acme",
        job_id=jid,
        kind="employer_response",
        note="Pushback: band tops out at 190",
    )
    assert co.negotiated_floor(conn, jid) is None


def test_current_salary_is_a_valid_comp_kind():
    """The candidate's own comp is the baseline every role is judged against --
    'is this a raise?' -- so it needs a first-class kind rather than being
    squeezed into market_band, which means something else."""
    from blastjob.core.company import COMP_KINDS

    assert "current_salary" in COMP_KINDS


def test_prior_salary_is_a_valid_comp_kind():
    """A current salary can understate the candidate if a prior role, before a
    layoff, paid more in total comp. Prior comp is the better read of market
    value in that case."""
    from blastjob.core.company import COMP_KINDS

    assert "prior_salary" in COMP_KINDS
