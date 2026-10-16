"""Issue #5: a tailored packet carries a one-tap link to the apply page."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import apply_file


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, *, url: str, source: str = "workday:acme", title: str = "VP, IT & Ops") -> int:
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, url, first_seen, last_seen) "
        "VALUES (?, 'r1', 'Acme <Group>', ?, ?, '2026-10-01', '2026-10-01')",
        (source, title, url),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _packet(tmp_path: Path) -> tuple[Path, list[Path]]:
    out = tmp_path / "acme-2026-10-16"
    out.mkdir()
    meta = out / "vp-it-ops.metadata.json"
    meta.write_text("{}", encoding="utf-8")
    return out, [meta]


def test_writes_a_role_sidecar_with_the_best_apply_link(conn, tmp_path):
    jid = _job(conn, url="https://acme.wd1.myworkdayjobs.com/Careers/job/X/VP_R1")
    out, files = _packet(tmp_path)
    path = apply_file.write_apply_file(conn, jid, files)
    assert path == out / "vp-it-ops.apply_link.html"
    html = path.read_text(encoding="utf-8")
    assert 'href="https://acme.wd1.myworkdayjobs.com/Careers/job/X/VP_R1"' in html
    assert "employer site" in html
    assert "Acme &lt;Group&gt;" in html and "VP, IT &amp; Ops" in html  # escaped


def test_no_safe_link_means_no_file(conn, tmp_path):
    jid = _job(conn, url="javascript:alert(1)", source="manual")
    _out, files = _packet(tmp_path)
    assert apply_file.write_apply_file(conn, jid, files) is None
    assert not list((tmp_path / "acme-2026-10-16").glob("*.apply_link.html"))


def test_a_quarantined_metadata_file_never_hosts_the_page(conn, tmp_path):
    jid = _job(conn, url="https://acme.wd1.myworkdayjobs.com/Careers/job/X/VP_R1")
    out, files = _packet(tmp_path)
    q = out / "quarantine"
    q.mkdir()
    held = q / "vp-it-ops.metadata.json"
    held.write_text("{}", encoding="utf-8")
    path = apply_file.write_apply_file(conn, jid, [held, *files])
    assert path == out / "vp-it-ops.apply_link.html"


def test_no_metadata_sidecar_means_no_file(conn, tmp_path):
    jid = _job(conn, url="https://acme.wd1.myworkdayjobs.com/Careers/job/X/VP_R1")
    assert apply_file.write_apply_file(conn, jid, []) is None
