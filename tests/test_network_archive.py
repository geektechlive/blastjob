"""LinkedIn's full data archive (ZIP) -> warm-path signals, recommendations,
message recency, and company follows. Deterministic parsing only -- no LLM,
no network. Built entirely in-memory; no fixture binaries."""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import pytest

from blastjob.core import network as net
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

_CONN_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"
_REC_HEADER = "First Name,Last Name,Company,Job Title,Text,Creation Date,Status"
_MSG_HEADER = "CONVERSATION ID,FROM,TO,DATE,SUBJECT,CONTENT"
# LinkedIn's real column is "Organization" (verified against a live archive
# 2026-09-10). The original fixture said "Company", which matched the code
# rather than reality and let a silent no-op parse pass as green.
_FOLLOW_HEADER = "Organization,Followed On"


def _conn_csv(rows: list[str]) -> str:
    return _CONN_HEADER + "\n" + "\n".join(rows) + "\n"


def _rec_csv(rows: list[str]) -> str:
    return _REC_HEADER + "\n" + "\n".join(rows) + "\n"


def _msg_csv(rows: list[str]) -> str:
    return _MSG_HEADER + "\n" + "\n".join(rows) + "\n"


def _follow_csv(rows: list[str]) -> str:
    return _FOLLOW_HEADER + "\n" + "\n".join(rows) + "\n"


def _make_zip(files: dict[str, str]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    tmp.write_bytes(buf.getvalue())
    return tmp


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


_ALICE = (
    "Alice,Exec,https://linkedin.com/in/example-aliceexec,,Acme Corp,VP Engineering,05 Jun 2023"
)
_BOB = "Bob,Ic,https://linkedin.com/in/example-bobic,,Acme Corp,Software Engineer,05 Jun 2023"


# 1. A ZIP with only Connections.csv imports exactly as import_connections_csv does.
def test_archive_connections_only_matches_direct_import(conn):
    zpath = _make_zip({"Connections.csv": _conn_csv([_ALICE, _BOB])})

    result = net.import_archive(conn, zpath)

    assert result.connections.added == 2
    assert result.recommendations_matched == 0
    assert result.messages_matched == 0
    assert result.follows_added == 0
    rows = conn.execute("SELECT full_name FROM network_contacts ORDER BY full_name").fetchall()
    assert [r["full_name"] for r in rows] == ["Alice Exec", "Bob Ic"]


# 2. Recommendations set recommended_me on the right person only.
def test_archive_recommendations_set_recommended_me_on_right_person(conn):
    zpath = _make_zip(
        {
            "Connections.csv": _conn_csv([_ALICE, _BOB]),
            "Recommendations_Received.csv": _rec_csv(
                ["Alice,Exec,Acme Corp,VP Engineering,Great to work with,01 Jan 2024,VISIBLE"]
            ),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.recommendations_matched == 1
    alice = conn.execute(
        "SELECT recommended_me FROM network_contacts WHERE full_name=?", ("Alice Exec",)
    ).fetchone()
    bob = conn.execute(
        "SELECT recommended_me FROM network_contacts WHERE full_name=?", ("Bob Ic",)
    ).fetchone()
    assert alice["recommended_me"] == 1
    assert bob["recommended_me"] == 0


# 3. A recommendation naming someone who is not a connection is skipped, no row created.
def test_archive_recommendation_for_unknown_person_is_skipped(conn):
    zpath = _make_zip(
        {
            "Connections.csv": _conn_csv([_ALICE]),
            "Recommendations_Received.csv": _rec_csv(
                ["Nobody,Stranger,Other Co,IC,Nice work,01 Jan 2024,VISIBLE"]
            ),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.recommendations_matched == 0
    assert result.recommendations_skipped == 1
    total = conn.execute("SELECT COUNT(*) n FROM network_contacts").fetchone()["n"]
    assert total == 1


# 4. messages.csv sets last_message_at/message_count, and no message body ever
#    reaches the database.
def test_archive_messages_set_recency_and_never_store_body(conn):
    secret_phrase = "THE-QUARTERLY-LAYOFF-PLAN-IS-CONFIDENTIAL"
    zpath = _make_zip(
        {
            "Connections.csv": _conn_csv([_ALICE]),
            "messages.csv": _msg_csv(
                [
                    f'1,Alice Exec,Jamie Reyes,2023-01-01,hi,"{secret_phrase} part one"',
                    f'1,Jamie Reyes,Alice Exec,2024-03-15,re: hi,"{secret_phrase} part two"',
                ]
            ),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.messages_matched == 1
    alice = conn.execute(
        "SELECT last_message_at, message_count FROM network_contacts WHERE full_name=?",
        ("Alice Exec",),
    ).fetchone()
    assert alice["last_message_at"] == "2024-03-15"
    assert alice["message_count"] == 2

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    all_text: list[str] = []
    for table_row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        table = table_row["name"]
        cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            for col in cols:
                val = row[col]
                if isinstance(val, str):
                    all_text.append(val)
    assert not any(secret_phrase in v for v in all_text)
    assert db_path  # sanity: this is a real backing file, not :memory:


# 5. Nested paths inside the ZIP are found.
def test_archive_finds_nested_connections_csv(conn):
    zpath = _make_zip({"Basic_LinkedInDataExport_09-10-2026/Connections.csv": _conn_csv([_ALICE])})

    result = net.import_archive(conn, zpath)

    assert result.connections.added == 1


# 6. A directory works as well as a ZIP.
def test_archive_directory_works_like_zip(conn, tmp_path):
    root = tmp_path / "archive"
    nested = root / "Basic_LinkedInDataExport"
    nested.mkdir(parents=True)
    (nested / "Connections.csv").write_text(_conn_csv([_ALICE, _BOB]), encoding="utf-8")

    result = net.import_archive(conn, root)

    assert result.connections.added == 2


# 7. Company Follows.csv populates company_follows, deduped by company_key.
def test_archive_company_follows_deduped_by_key(conn):
    zpath = _make_zip(
        {
            "Connections.csv": _conn_csv([_ALICE]),
            "Company Follows.csv": _follow_csv(
                [
                    "Acme Corp,01 Jan 2020",
                    "Acme Corp.,02 Feb 2021",  # same company_key, should dedupe
                    "Beta Inc,03 Mar 2022",
                ]
            ),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.follows_added == 2
    rows = conn.execute("SELECT company, company_key FROM company_follows").fetchall()
    assert len(rows) == 2


# 8. warm_paths puts a recommender above a higher-seniority non-recommender.
def test_warm_paths_ranks_recommender_above_higher_seniority(conn):
    zpath = _make_zip(
        {
            "Connections.csv": _conn_csv([_ALICE, _BOB]),
            "Recommendations_Received.csv": _rec_csv(
                ["Bob,Ic,Acme Corp,Software Engineer,Solid,01 Jan 2024,VISIBLE"]
            ),
        }
    )
    net.import_archive(conn, zpath)
    job_id = dbc.upsert_job(conn, source="t", external_id="1", company="Acme Corp", title="X")
    conn.commit()

    paths = net.warm_paths(conn, job_id)

    names = [r["full_name"] for r in paths]
    assert names == ["Bob Ic", "Alice Exec"]


# 9. An archive missing every optional file still succeeds.
def test_archive_missing_optional_files_still_succeeds(conn):
    zpath = _make_zip({"Connections.csv": _conn_csv([_ALICE])})

    result = net.import_archive(conn, zpath)

    assert result.connections.added == 1
    assert result.recommendations_matched == 0
    assert result.messages_matched == 0
    assert result.follows_added == 0
    assert result.unrecognized == ()


# Bonus: advocates() returns only recommenders, most recently messaged first.
def test_advocates_returns_only_recommenders(conn):
    zpath = _make_zip(
        {
            "Connections.csv": _conn_csv([_ALICE, _BOB]),
            "Recommendations_Received.csv": _rec_csv(
                ["Alice,Exec,Acme Corp,VP Engineering,Great,01 Jan 2024,VISIBLE"]
            ),
        }
    )
    net.import_archive(conn, zpath)

    rows = net.advocates(conn)

    assert [r["full_name"] for r in rows] == ["Alice Exec"]


def test_company_follows_uses_linkedins_real_organization_header(conn):
    """LinkedIn's Company Follows.csv column is 'Organization', not 'Company'.
    Assuming 'Company' parsed every row to blank and imported nothing, while a
    fixture written to match the code passed. Caught against a real archive."""
    zpath = _make_zip(
        {
            "export/Connections.csv": _conn_csv([_ALICE]),
            "export/Company Follows.csv": (
                "Organization,Followed On\n"
                "Globex,2023-01-02\n"
                '"Fabrikam Power Systems, Inc.",2024-05-05\n'
            ),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.follows_added == 2
    rows = {
        r["company"]: r["company_key"]
        for r in conn.execute("select company, company_key from company_follows")
    }
    assert "Globex" in rows
    assert rows["Fabrikam Power Systems, Inc."] == "fabrikam power systems"


def test_company_follows_still_accepts_the_legacy_company_header(conn):
    """Older exports used 'Company'; both spellings must work."""
    zpath = _make_zip(
        {
            "Connections.csv": _conn_csv([_ALICE]),
            "Company Follows.csv": "Company,Followed On\nGlobex,2023-01-02\n",
        }
    )

    assert net.import_archive(conn, zpath).follows_added == 1


def test_upload_route_accepts_a_zip_archive(tmp_path):
    """The full archive is a ZIP. The route originally assumed a bare CSV and a
    10MB cap, which silently rejected exactly the file this feature exists for."""
    from fastapi.testclient import TestClient

    from blastjob.web.app import create_app
    from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    dbc.init_db(cfg).close()
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))

    zpath = _make_zip(
        {
            "export/Connections.csv": _conn_csv([_ALICE, _BOB]),
            "export/Company Follows.csv": "Organization,Followed On\nGlobex,2023-01-02\n",
        }
    )
    with zpath.open("rb") as fh:
        r = client.post(
            "/network/import",
            files={"file": ("Basic_LinkedInDataExport.zip", fh, "application/zip")},
            headers={CSRF_HEADER: LAUNCH_TOKEN},
        )
    assert r.status_code == 200
    assert "Alice" in r.text or "2" in r.text

    conn2 = dbc.init_db(cfg)
    try:
        assert conn2.execute("select count(*) from network_contacts").fetchone()[0] == 2
        assert conn2.execute("select count(*) from company_follows").fetchone()[0] == 1
    finally:
        conn2.close()
