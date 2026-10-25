"""#89: untrusted text stays out of |safe, and upsert_job never builds SQL from data keys."""

from __future__ import annotations

import tempfile

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import app as web_app


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def test_funnel_silent_table_escapes_last_seen_and_keeps_dash():
    rows = [
        {"firm": "Acme Search", "postings": 1, "last": "<script>alert(1)</script>", "state": "x"},
        {"firm": "Globex Search", "postings": 0, "last": None, "state": "never produced"},
    ]
    template = web_app.templates.env.get_template("funnel.html")
    # Render only the silent-channels block: the rest of the page needs a full context.
    source = template.environment.loader.get_source(template.environment, "funnel.html")[0]
    start = source.index('<table class="funnel-table">\n  <tr><th>Firm')
    block = source[start : source.index("</table>", start) + len("</table>")]
    html = web_app.templates.env.from_string(block).render(silent=rows)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert '<td class="num">&mdash;</td>' in html


def test_upsert_job_insert_drops_unknown_columns(conn, caplog):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    hostile = "title) VALUES (1); DROP TABLE jobs; --"
    with caplog.at_level("WARNING"):
        job_id = dbc.upsert_job(
            conn, source="s", external_id="e1", title="Role", **{hostile: "x", "bogus": 1}
        )
    conn.set_trace_callback(None)
    inserts = [s for s in statements if s.startswith("INSERT INTO jobs")]
    assert inserts
    assert all("DROP TABLE" not in s and "bogus" not in s for s in inserts)
    assert conn.execute("SELECT title FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "Role"
    assert "bogus" in caplog.text
