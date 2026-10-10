"""LinkedIn connections export -> warm-path signals. Deterministic parsing and
classification only -- no LLM, no network."""

from __future__ import annotations

import tempfile

import pytest

from blastjob.core import network as net
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"

_PREAMBLE = (
    "Notes:\n"
    '"When exporting your connection data, you may notice fewer connections than expected."\n'
    "\n"
)


def _csv(rows: list[str], *, with_preamble: bool = True) -> str:
    body = _HEADER + "\n" + "\n".join(rows) + "\n"
    return (_PREAMBLE + body) if with_preamble else body


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _write(tmp_path, text: str):
    p = tmp_path / "Connections.csv"
    p.write_text(text, encoding="utf-8")
    return p


# 1. The "Notes:" preamble is skipped and the real header found.
def test_import_skips_notes_preamble(conn, tmp_path):
    rows = [
        "Jane,Doe,https://linkedin.com/in/example-janedoe,,Acme Corp,VP Engineering,05 Jun 2023"
    ]
    path = _write(tmp_path, _csv(rows, with_preamble=True))

    result = net.import_connections_csv(conn, path)

    assert result.added == 1
    row = conn.execute("SELECT * FROM network_contacts").fetchone()
    assert row["full_name"] == "Jane Doe"
    assert row["company"] == "Acme Corp"


# 2. A row with a blank Email Address still imports.
def test_import_blank_email_still_imports(conn, tmp_path):
    rows = [
        "John,Smith,https://linkedin.com/in/example-johnsmith,"
        ",Beta Inc,Director of Operations,01 Jan 2022"
    ]
    path = _write(tmp_path, _csv(rows))

    result = net.import_connections_csv(conn, path)

    assert result.added == 1
    row = conn.execute("SELECT * FROM network_contacts").fetchone()
    assert row["email"] == ""
    assert row["full_name"] == "John Smith"


# 3. Re-importing the same export updates company/position and does not duplicate.
def test_reimport_updates_without_duplicating(conn, tmp_path):
    url = "https://linkedin.com/in/example-janedoe"
    path1 = _write(tmp_path, _csv([f"Jane,Doe,{url},,Acme Corp,Manager,05 Jun 2023"]))
    net.import_connections_csv(conn, path1)

    path2 = _write(tmp_path, _csv([f"Jane,Doe,{url},,Acme Corp,VP Engineering,05 Jun 2023"]))
    result = net.import_connections_csv(conn, path2)

    assert result.added == 0
    assert result.updated == 1
    rows = conn.execute("SELECT * FROM network_contacts WHERE url=?", (url,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["position"] == "VP Engineering"
    assert rows[0]["seniority"] == "exec"


# 4. Re-import preserves a manually written `notes` value.
def test_reimport_preserves_manual_notes(conn, tmp_path):
    url = "https://linkedin.com/in/example-janedoe"
    path1 = _write(tmp_path, _csv([f"Jane,Doe,{url},,Acme Corp,Manager,05 Jun 2023"]))
    net.import_connections_csv(conn, path1)
    conn.execute("UPDATE network_contacts SET notes=? WHERE url=?", ("met at conference", url))
    conn.commit()

    path2 = _write(tmp_path, _csv([f"Jane,Doe,{url},,Acme Corp,VP Engineering,05 Jun 2023"]))
    net.import_connections_csv(conn, path2)

    row = conn.execute("SELECT * FROM network_contacts WHERE url=?", (url,)).fetchone()
    assert row["notes"] == "met at conference"


# 5. classify_seniority across chief/VP/director/head of/manager/engineer.
@pytest.mark.parametrize(
    "position,expected",
    [
        ("Chief Operations Officer", "exec"),
        ("VP of Engineering", "exec"),
        ("Director of Engineering", "director"),
        ("Head of Platform", "director"),
        ("Engineering Manager", "manager"),
        ("Software Engineer", "ic"),
    ],
)
def test_classify_seniority(position, expected):
    assert net.classify_seniority(position) == expected


# 6. is_recruiter true for "Talent Acquisition Partner", false for "Software Engineer".
def test_is_recruiter():
    assert net.is_recruiter("Talent Acquisition Partner") is True
    assert net.is_recruiter("Software Engineer") is False


# 7. warm_paths returns only same-company connections, exec first.
def test_warm_paths_same_company_exec_first(conn, tmp_path):
    rows = [
        "Alice,Exec,https://linkedin.com/in/example-aliceexec,"
        ",Acme Corp,VP Engineering,05 Jun 2023",
        "Bob,Ic,https://linkedin.com/in/example-bobic,,Acme Corp,Software Engineer,05 Jun 2023",
        "Carl,Other,https://linkedin.com/in/example-carlother,,Beta Inc,Director,05 Jun 2023",
    ]
    path = _write(tmp_path, _csv(rows))
    net.import_connections_csv(conn, path)

    job_id = dbc.upsert_job(conn, source="t", external_id="1", company="Acme Corp", title="X")
    conn.commit()

    paths = net.warm_paths(conn, job_id)

    names = [r["full_name"] for r in paths]
    assert names == ["Alice Exec", "Bob Ic"]
    assert paths[0]["seniority"] == "exec"


# 8. is_alumni_company true for a former employer, false otherwise -- driven by
# [candidate] former_employers (config), not a hard-coded constant.
def test_is_alumni_company():
    from blastjob.models.config import CandidateConfig

    candidate = CandidateConfig(former_employers=["Northwind Logistics", "Contoso Health"])

    assert net.is_alumni_company("Northwind Logistics", candidate) is True
    assert net.is_alumni_company("Contoso Health", candidate) is True
    assert net.is_alumni_company("Some Random Company", candidate) is False


# 9. Connected On parses "05 Jun 2023" to ISO, and an unparseable value yields ''.
def test_connected_on_parsing(conn, tmp_path):
    rows = [
        "Jane,Doe,https://linkedin.com/in/example-janedoe,,Acme Corp,VP Engineering,05 Jun 2023",
        "Nan,Date,https://linkedin.com/in/example-nandate,,Acme Corp,IC,not-a-date",
    ]
    path = _write(tmp_path, _csv(rows))
    net.import_connections_csv(conn, path)

    good = conn.execute(
        "SELECT connected_on FROM network_contacts WHERE url=?",
        ("https://linkedin.com/in/example-janedoe",),
    ).fetchone()
    bad = conn.execute(
        "SELECT connected_on FROM network_contacts WHERE url=?",
        ("https://linkedin.com/in/example-nandate",),
    ).fetchone()
    assert good["connected_on"] == "2023-06-05"
    assert bad["connected_on"] == ""


def test_recruiter_titles_are_not_ranked_as_executives():
    """ "Partner" means exec at a law or consulting firm, not in "Talent
    Acquisition Partner". Ranking a recruiter top of the intro list defeats the
    whole point of the ranking. Caught by a realistic-CSV smoke test, not by the
    original fixtures."""
    from blastjob.core.network import classify_seniority, is_recruiter

    for title in ("Talent Acquisition Partner", "HR Business Partner", "People Partner"):
        assert classify_seniority(title) != "exec", title
    assert is_recruiter("Talent Acquisition Partner") is True
    # A genuine firm partner is still an exec.
    assert classify_seniority("Partner") == "exec"
    assert classify_seniority("Managing Partner") == "exec"
    # And recruiters keep any real seniority they carry.
    assert classify_seniority("Director of Talent Acquisition") == "director"
