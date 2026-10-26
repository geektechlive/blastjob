"""Regression tests for five verified-against-live-data defects:

1. `\\bvp\\b` cannot match inside SVP/EVP/AVP -- senior execs filed as IC.
2. `\\bowner\\b` makes every "Product Owner" (an Agile IC role) an executive.
3. A name collision must credit NOBODY, never everybody -- inventing a vouch
   or a message count for the wrong person is worse than missing one.
4. `company_key` misses the legal-name / brand-name split (The Globex Company
   vs Globex, legal-suffix "Company" descriptors, apostrophes, and the
   `\\bco\\b` suffix guard mangling "CO-OP").
5. `negotiated_floor` only matched the literal token "pushback" -- "they
   pushed back" or a mixed note both slipped through as an accepted floor.

Deterministic parsing/classification only -- no LLM, no network.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import pytest

from blastjob.core import company as co
from blastjob.core import network as net
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

_CONN_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"
_REC_HEADER = "First Name,Last Name,Company,Job Title,Text,Creation Date,Status"


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _make_zip(files: dict[str, str]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    tmp.write_bytes(buf.getvalue())
    return tmp


# --------------------------------------------------------------------------
# Defect 1 -- SVP/EVP/AVP must classify as exec, not ic.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("position", ["SVP, Finance", "EVP Operations", "AVP - Sales"])
def test_classify_seniority_recognizes_vp_variants(position):
    assert net.classify_seniority(position) == "exec", position


def test_classify_seniority_unrelated_title_still_falls_back_to_ic():
    assert net.classify_seniority("Principal Consultant") == "ic"


# --------------------------------------------------------------------------
# Defect 2 -- "Product Owner" (Agile IC role) must not classify as exec.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "position", ["Product Owner", "Senior Product Owner", "Technical Product Owner"]
)
def test_classify_seniority_product_owner_is_not_exec(position):
    assert net.classify_seniority(position) != "exec", position


def test_classify_seniority_genuine_owner_titles_are_still_exec():
    assert net.classify_seniority("Owner") == "exec"
    assert net.classify_seniority("Founder & Owner") == "exec"


# --------------------------------------------------------------------------
# Defect 3 -- ambiguous names credit nobody.
# --------------------------------------------------------------------------


def test_ambiguous_recommender_name_credits_nobody(conn):
    """Two distinct connections happen to share a full name (distinct profile
    URLs keep them as separate rows). A recommendation naming that ambiguous
    name must credit NEITHER of them -- crediting both would fabricate a
    vouch for whichever one didn't actually write it."""
    rows = [
        "John,Smith,https://linkedin.com/in/example-johnsmith1,"
        ",Acme Corp,VP Engineering,05 Jun 2023",
        "John,Smith,https://linkedin.com/in/example-johnsmith2,,Beta Inc,Director,06 Jun 2023",
    ]
    zpath = _make_zip(
        {
            "Connections.csv": _CONN_HEADER + "\n" + "\n".join(rows) + "\n",
            "Recommendations_Received.csv": (
                _REC_HEADER + "\nJohn,Smith,Acme Corp,VP Engineering,Great,01 Jan 2024,VISIBLE\n"
            ),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.recommendations_matched == 0
    assert result.recommendations_ambiguous == 1
    recommended = conn.execute(
        "SELECT COUNT(*) n FROM network_contacts WHERE recommended_me=1"
    ).fetchone()["n"]
    assert recommended == 0


def test_ambiguous_message_sender_touches_nobody(conn):
    """Same ambiguity, via messages.csv -- neither same-named contact should
    have last_message_at/message_count set from a message that can't be
    attributed to either of them specifically."""
    rows = [
        "John,Smith,https://linkedin.com/in/example-johnsmith1,"
        ",Acme Corp,VP Engineering,05 Jun 2023",
        "John,Smith,https://linkedin.com/in/example-johnsmith2,,Beta Inc,Director,06 Jun 2023",
    ]
    zpath = _make_zip(
        {
            "Connections.csv": _CONN_HEADER + "\n" + "\n".join(rows) + "\n",
            "messages.csv": (
                "CONVERSATION ID,FROM,TO,DATE,SUBJECT,CONTENT\n"
                '1,John Smith,Jamie Reyes,2024-03-15,hi,"hello"\n'
            ),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.messages_matched == 0
    assert result.messages_ambiguous == 1
    touched = conn.execute(
        "SELECT COUNT(*) n FROM network_contacts WHERE message_count > 0"
    ).fetchone()["n"]
    assert touched == 0


def test_unambiguous_recommendation_still_credits_the_right_person(conn):
    """Control: a unique name is unaffected by the ambiguity fix."""
    rows = [
        "Alice,Exec,https://linkedin.com/in/example-aliceexec,,Acme Corp,VP Engineering,05 Jun 2023"
    ]
    zpath = _make_zip(
        {
            "Connections.csv": _CONN_HEADER + "\n" + "\n".join(rows) + "\n",
            "Recommendations_Received.csv": (
                _REC_HEADER + "\nAlice,Exec,Acme Corp,VP Engineering,Great,01 Jan 2024,VISIBLE\n"
            ),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.recommendations_matched == 1
    assert result.recommendations_ambiguous == 0


# --------------------------------------------------------------------------
# Defect 4 -- company_key legal-name / brand-name split.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "legal_name,brand_name",
    [
        ("The Globex Company", "Globex"),
        ("Vance's", "Vances"),
    ],
)
def test_company_key_matches_legal_and_brand_name(legal_name, brand_name):
    """Generic normalization alone (no alias table entry) must line these up:
    a leading "The" / trailing "Company" descriptor, and an apostrophe."""
    assert co.company_key(legal_name) == co.company_key(brand_name)


def test_company_key_alias_table_resolves_pairs_a_generic_regex_cannot(monkeypatch):
    """Some legal-name / brand-name pairs can't be split by any generic
    suffix regex (e.g. "International plc" vs the bare brand) -- that's what
    `_COMPANY_ALIASES` is for. The default table is empty; this proves the
    lookup mechanism itself with a fictional pair."""
    co.set_company_aliases({"Fabrikam International plc": "Fabrikam"})
    assert co.company_key("Fabrikam International plc") == co.company_key("Fabrikam")


def test_company_key_co_op_is_not_mangled_by_the_co_suffix_guard():
    """The old \\bco\\b suffix strip matched the "co" in "CO-OP" (a hyphen
    counts as a word boundary), silently eating it down to 'op widget
    services'. The 'co' token must survive when it's part of 'co-op'."""
    key = co.company_key("CO-OP Widget Services")
    assert key != "op widget services"
    assert key.split()[0] == "co"


@pytest.mark.parametrize(
    "name",
    [
        "Tailspin Marketing Group - Global Division",
        "Tailspin Marketing Group – Midwest Region",
        "Tailspin Marketing Group | Retail Subsidiary",
    ],
)
def test_company_key_strips_a_trailing_division_segment(name):
    """The Tailspin posting carried its division as a dash suffix, so its key never
    met the employer's own postings."""
    assert co.company_key(name) == "tailspin marketing group"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Foo - Bar", "foo bar"),
        ("Tailspin Marketing Group", "tailspin marketing group"),
        ("Acme Holdings", "acme holdings"),
        ("Acme Brands - Retail", "acme brands retail"),
        ("Division Street Partners", "division street partners"),
    ],
)
def test_company_key_division_strip_stays_narrow(name, expected):
    """Only a delimited trailing segment naming a division/region/subsidiary
    goes; Group/Holdings/Brands and undelimited words stay."""
    assert co.company_key(name) == expected


def test_rekey_companies_moves_exclusions_and_job_comp_signals_too(conn, cfg):
    """An exclusion left on the old key would silently lapse (matching reads
    exclusions by key), and comp signals are read by key as well."""
    name = "Tailspin Marketing Group - Global Division"
    old = "tailspin marketing group global division"
    conn.execute(
        "INSERT INTO company_exclusions (company_key, company, reason, created_at) "
        "VALUES (?,?,?,?)",
        (old, name, "", "x"),
    )
    jid = dbc.upsert_job(conn, source="adzuna", external_id="w1", company=name, title="CIO")
    conn.execute(
        "INSERT INTO comp_signals (job_id, company_key, kind, observed_at, created_at) "
        "VALUES (?,?,?,?,?)",
        (jid, old, "asked_minimum", "2026-10-01", "x"),
    )
    conn.execute(
        "INSERT INTO comp_signals (job_id, company_key, kind, observed_at, created_at) "
        "VALUES (NULL,'acme','current_salary','2026-10-01','x')"
    )
    conn.commit()

    result = co.rekey_companies(conn, cfg)

    assert result["company_exclusions"] == co.RekeyResult(updated=1, conflicts=0)
    assert result["comp_signals"] == co.RekeyResult(updated=1, conflicts=0)
    keys = {r[0] for r in conn.execute("SELECT company_key FROM company_exclusions")}
    assert keys == {"tailspin marketing group"}
    signals = {r[0] for r in conn.execute("SELECT company_key FROM comp_signals")}
    assert signals == {"tailspin marketing group", "acme"}  # a job-less signal is left alone


def test_rekey_companies_reports_an_exclusion_collision(conn, cfg):
    for key, company in (
        ("tailspin marketing group global division", "Tailspin Marketing Group - Global Division"),
        ("tailspin marketing group", "Tailspin Marketing Group"),
    ):
        conn.execute(
            "INSERT INTO company_exclusions (company_key, company, reason, created_at) "
            "VALUES (?,?,?,?)",
            (key, company, "", "x"),
        )
    conn.commit()

    result = co.rekey_companies(conn, cfg)

    assert result["company_exclusions"] == co.RekeyResult(updated=0, conflicts=1)
    assert conn.execute("SELECT COUNT(*) FROM company_exclusions").fetchone()[0] == 2


def test_rekey_companies_moves_a_division_key_and_reports_the_conflict(conn, cfg):
    conn.execute(
        "INSERT INTO company_follows (company, company_key, followed_at) VALUES (?,?,?)",
        (
            "Tailspin Marketing Group - Global Division",
            "tailspin marketing group global division",
            "x",
        ),
    )
    co.upsert_profile(conn, co.CompanyProfile(company="Tailspin Marketing Group - Global Division"))
    conn.execute(
        "UPDATE company_profiles SET company_key='tailspin marketing group global division'"
    )
    co.upsert_profile(conn, co.CompanyProfile(company="Tailspin Marketing Group"))
    conn.commit()

    result = co.rekey_companies(conn, cfg)

    assert result["company_follows"] == co.RekeyResult(updated=1, conflicts=0)
    follow = conn.execute("SELECT company_key FROM company_follows").fetchone()
    assert follow["company_key"] == "tailspin marketing group"
    # The plain-name profile already holds the key: a conflict, both rows kept.
    assert result["company_profiles"] == co.RekeyResult(updated=0, conflicts=1)
    keys = {r["company_key"] for r in conn.execute("SELECT company_key FROM company_profiles")}
    assert keys == {"tailspin marketing group", "tailspin marketing group global division"}


def test_rekey_companies_updates_a_row_stored_under_the_old_narrower_key(conn, cfg):
    """Simulates data imported before this fix: the row's stored company_key
    reflects the OLD (narrower) normalization even though `company_key()` now
    computes something else for the same display name -- here because an
    alias table entry resolves "Fabrikam International plc" to "fabrikam"."""
    cfg.discovery.company_aliases = {"Fabrikam International plc": "Fabrikam"}
    co.upsert_profile(conn, co.CompanyProfile(company="Fabrikam International plc"))
    conn.execute(
        "UPDATE company_profiles SET company_key='fabrikam international plc' "
        "WHERE company='Fabrikam International plc'"
    )
    conn.commit()

    result = co.rekey_companies(conn, cfg)

    assert result["company_profiles"].updated == 1
    assert result["company_profiles"].conflicts == 0
    row = conn.execute(
        "SELECT company_key FROM company_profiles WHERE company='Fabrikam International plc'"
    ).fetchone()
    assert row["company_key"] == "fabrikam"


def test_rekey_companies_reports_collisions_without_raising_or_losing_rows(conn, cfg):
    """company_follows.company_key is UNIQUE -- widening the normalization can
    legitimately collapse two previously-distinct keys onto one (a stray
    "Vance's" row alongside a "Vances" row). The pass must report that as a
    conflict, not raise IntegrityError mid-pass and not delete either row."""
    conn.execute(
        "INSERT INTO company_follows (company, company_key, followed_at) VALUES (?,?,?)",
        ("Vance's", "vance s", "2020-01-01"),
    )
    conn.execute(
        "INSERT INTO company_follows (company, company_key, followed_at) VALUES (?,?,?)",
        ("Vances", "vances", "2020-01-02"),
    )
    conn.commit()

    result = co.rekey_companies(conn, cfg)

    assert result["company_follows"].conflicts == 1
    rows = conn.execute("SELECT company FROM company_follows").fetchall()
    assert {r["company"] for r in rows} == {"Vance's", "Vances"}


def test_company_key_alias_matches_the_alumni_employer_list(monkeypatch):
    """An alumni roster stores the legal entity from the work history
    ('Fabrikam International plc'), but a job posting reads the brand
    ('Fabrikam') -- these must resolve to the same company_key or the
    alumni signal silently never fires for that former employer."""
    from blastjob.models.config import CandidateConfig

    co.set_company_aliases({"Fabrikam International plc": "Fabrikam"})
    candidate = CandidateConfig(former_employers=["Fabrikam International plc"])
    assert net.is_alumni_company("Fabrikam", candidate) is True


# --------------------------------------------------------------------------
# Defect 5 -- negotiated_floor must not treat a rejected ask as accepted.
# --------------------------------------------------------------------------


def test_negotiated_floor_none_when_employer_pushed_back_in_other_words(conn):
    jid = dbc.upsert_job(conn, source="t", external_id="1", company="Acme", title="Generic VP Role")
    co.add_comp_signal(conn, company="Acme", job_id=jid, kind="asked_minimum", low_usd=260_000)
    co.add_comp_signal(
        conn, company="Acme", job_id=jid, kind="employer_response", note="they pushed back hard"
    )

    assert co.negotiated_floor(conn, jid) is None


def test_negotiated_floor_none_for_a_mixed_note(conn):
    jid = dbc.upsert_job(conn, source="t", external_id="1", company="Acme", title="Generic VP Role")
    co.add_comp_signal(conn, company="Acme", job_id=jid, kind="asked_minimum", low_usd=260_000)
    co.add_comp_signal(
        conn,
        company="Acme",
        job_id=jid,
        kind="employer_response",
        note="no pushback at 240, pushback at 260",
    )

    assert co.negotiated_floor(conn, jid) is None


def test_negotiated_floor_still_accepts_an_explicit_no_pushback(conn):
    jid = dbc.upsert_job(conn, source="t", external_id="1", company="Acme", title="Generic VP Role")
    co.add_comp_signal(conn, company="Acme", job_id=jid, kind="asked_minimum", low_usd=200_000)
    co.add_comp_signal(
        conn, company="Acme", job_id=jid, kind="employer_response", note="No pushback"
    )

    assert co.negotiated_floor(conn, jid) == 200_000
